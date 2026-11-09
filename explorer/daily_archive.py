import csv
import hashlib
import json
import lzma
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from time import monotonic, sleep, time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import server as panel

ROOT = Path("/archives")
IMPORTS = ROOT / ".imports"
LIVE = "file:/data/flows.sqlite3?mode=ro"
DAY = 86400
CHUNK = 1024 * 1024
FIELDS = "received router direction client bytes packets flow".split()
COLUMNS = ",".join(FIELDS)
MATCH = " AND ".join(f"a.{field}=r.{field}" for field in FIELDS)
APP_ID = 1179408463
UPLOAD = BoundedSemaphore(1)


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack(source, output, remaining, limit=None):
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=128 * CHUNK)
    total = 0
    with output.open("wb") as target:
        while not decoder.eof:
            if decoder.needs_input:
                block = source.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("Arquivo XZ incompleto.")
                remaining -= len(block)
            else:
                block = b""
            data = decoder.decompress(block, max_length=CHUNK)
            total += len(data)
            if limit is not None and total > limit:
                raise ValueError("O arquivo descomprimido excede o espaço disponível.")
            target.write(data)
        if remaining or decoder.unused_data:
            raise ValueError("O ficheiro deve conter um único arquivo XZ.")


def open_archive(path):
    db = panel.connect(path.as_uri() + "?mode=ro&immutable=1")
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    return db


def validate(path):
    with closing(open_archive(path)) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise ValueError("O ficheiro não é um arquivo diário Fluxo.")
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        if tables != {"records", "routers", "archive_info"}:
            raise ValueError("Estrutura do arquivo inválida.")
        if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise ValueError("O SQLite do arquivo falhou a verificação.")
        info = db.execute("SELECT day, rows FROM archive_info").fetchall()
        if len(info) != 1:
            raise ValueError("O arquivo deve representar um dia.")
        day, count = info[0]
        actual, first, last = db.execute(
            "SELECT count(*),min(received),max(received) FROM records"
        ).fetchone()
        if (not count or actual != count or day % DAY
                or not day <= first <= last < day + DAY):
            raise ValueError("Datas ou contagem de fluxos inválidas.")
        return day, count


def merge_day(raw, day):
    with closing(sqlite3.connect(raw, uri=True, timeout=10)) as db:
        db.executescript(f"""
            PRAGMA application_id={APP_ID};
            CREATE TABLE IF NOT EXISTS records (
                source_id INTEGER NOT NULL, received INTEGER NOT NULL,
                router TEXT NOT NULL, direction TEXT NOT NULL, client TEXT NOT NULL,
                bytes INTEGER NOT NULL, packets INTEGER NOT NULL, flow TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS source_row ON records(source_id);
            CREATE INDEX IF NOT EXISTS records_received ON records(received);
            CREATE TABLE IF NOT EXISTS routers(address TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS archive_info(day INTEGER, rows INTEGER);
        """)
        db.execute("ATTACH DATABASE ? AS live", (LIVE,))
        select = ",".join("r." + field for field in FIELDS)
        with db:
            added = db.execute(f"""
                INSERT INTO records (source_id,{COLUMNS})
                SELECT r.rowid,{select} FROM live.records AS r
                WHERE r.received>=? AND r.received<?
                  AND NOT EXISTS (
                      SELECT 1 FROM records AS a
                      WHERE a.source_id=r.rowid AND {MATCH}
                  )
            """, (day, day + DAY)).rowcount
            if added:
                db.execute("INSERT OR IGNORE INTO routers SELECT DISTINCT router FROM records")
                db.execute("DELETE FROM archive_info")
                db.execute("INSERT INTO archive_info SELECT ?,count(*) FROM records", (day,))
        return added


def publish(raw, destination, work):
    candidate = work / "day.xz"
    with raw.open("rb") as source, candidate.open("wb") as target:
        with lzma.LZMAFile(target, "w", preset=9 | lzma.PRESET_EXTREME) as packed:
            shutil.copyfileobj(source, packed, CHUNK)
        target.flush()
        os.fsync(target.fileno())

    with raw.open("rb") as original, lzma.open(candidate, "rb") as restored:
        if digest(original) != digest(restored):
            raise ValueError("A descompressão não reproduziu o SQLite original.")
    with candidate.open("rb") as stream:
        sha = digest(stream)
    checksum = work / "checksum"
    checksum.write_text(f"{sha}  {destination.name}\n")
    os.replace(candidate, destination)
    os.replace(checksum, destination.with_name(destination.name + ".sha256"))
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def expire_verified(raw, day):
    # Delete only exact copies. Late arrivals stay until they are archived.
    with closing(sqlite3.connect("/data/flows.sqlite3", uri=True, timeout=5)) as db:
        db.execute("ATTACH DATABASE ? AS saved", (raw.as_uri() + "?mode=ro&immutable=1",))
        while True:
            with db:
                removed = db.execute(f"""
                    DELETE FROM records WHERE rowid IN (
                        SELECT r.rowid FROM records AS r
                        JOIN saved.records AS a ON a.source_id=r.rowid AND {MATCH}
                        WHERE r.received>=? AND r.received<? LIMIT 2000
                    )
                """, (day, day + DAY)).rowcount
            if not removed:
                break
            sleep(.05)

        with db:
            db.execute("BEGIN IMMEDIATE")
            remaining = db.execute(
                "SELECT 1 FROM records WHERE received>=? AND received<? LIMIT 1",
                (day, day + DAY),
            ).fetchone()
            if remaining is None:
                for table in ("minute_clients", "minute_ports"):
                    db.execute(f"DELETE FROM {table} WHERE minute>=? AND minute<?",
                               (day, day + DAY))
        db.execute("PRAGMA incremental_vacuum(128)")


def archive_pass():
    now = int(time())
    complete = ((now - 900) // DAY) * DAY
    independent = ROOT.stat().st_dev != Path("/data").stat().st_dev
    with closing(sqlite3.connect(LIVE, uri=True, timeout=5)) as source:
        oldest = source.execute("SELECT min(received) FROM records").fetchone()[0]
    if oldest is None:
        return

    for day in range(oldest // DAY * DAY, complete, DAY):
        date = datetime.fromtimestamp(day, UTC).date().isoformat()
        destination = ROOT / f"fluxo-{date}.sqlite3.xz"
        expire = independent and day + DAY <= now - 7 * DAY
        if destination.exists() and not expire:
            continue
        with TemporaryDirectory(prefix=".build-", dir=ROOT) as folder:
            work = Path(folder)
            raw = work / "day.sqlite3"
            if destination.exists():
                with destination.open("rb") as stream:
                    unpack(stream, raw, destination.stat().st_size)
                saved_day, _ = validate(raw)
                if saved_day != day:
                    raise ValueError(f"Data inesperada em {destination.name}")
                checksum = destination.with_name(destination.name + ".sha256")
                if checksum.exists():
                    with destination.open("rb") as stream:
                        if digest(stream) != checksum.read_text().split()[0]:
                            raise ValueError(f"SHA-256 inválido em {destination.name}")

            added = merge_day(raw, day)
            if not added and not destination.exists():
                continue
            _, count = validate(raw)
            if added:
                publish(raw, destination, work)
                print(f"Arquivo verificado: {destination.name} · {count} registos", flush=True)
            if expire:
                expire_verified(raw, day)


def worker():
    os.nice(10)
    if ROOT.stat().st_dev == Path("/data").stat().st_dev:
        print("Arquivo no mesmo sistema de ficheiros. Expiração suspensa.", flush=True)
    while True:
        try:
            archive_pass()
        except Exception as exc:
            print(f"Falha no arquivo diário: {exc}", file=sys.stderr, flush=True)
        sleep(900)


CONTROLS = r"""
<script>
(() => {
  const bar = document.createElement('div');
  bar.className = 'card';
  bar.style.marginBottom = '12px';
  bar.innerHTML = '<strong id="source-mode">Em directo · últimos 7 dias</strong> ' +
    '<button type="button" id="load-day">↑ Importar arquivo .xz</button> ' +
    '<button type="button" id="back-live" hidden>Voltar ao directo</button>' +
    '<input type="file" id="day-file" accept=".xz,application/x-xz" hidden>';
  form.before(bar);
  form.elements.minutes.querySelector('option[value="43200"]')?.remove();
  const original = params;
  let archive = null;
  params = (page, charts) => {
    const p = original(page, charts);
    if (archive) p.set('archive', archive.id);
    return p;
  };
  $('load-day').onclick = () => $('day-file').click();
  $('day-file').onchange = async () => {
    const file = $('day-file').files[0];
    $('day-file').value = '';
    if (!file) return;
    $('load-day').disabled = true;
    status('A importar e verificar o arquivo…');
    try {
      const response = await fetch('/api/import', {
        method: 'POST', headers: {'Content-Type': 'application/x-xz'}, body: file
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error);
      archive = result;
      $('source-mode').textContent = 'Arquivo UTC · ' + result.date;
      $('back-live').hidden = false;
      form.elements.minutes.value = '1440';
      apply();
    } catch (error) {
      status(error.message, true);
    } finally {
      $('load-day').disabled = false;
    }
  };
  $('back-live').onclick = () => {
    archive = null;
    $('source-mode').textContent = 'Em directo · últimos 7 dias';
    $('back-live').hidden = true;
    form.elements.minutes.value = '60';
    apply();
  };
})();
</script>
"""


def handler(base):
    class ArchiveHandler(base):
        def do_POST(self):
            if self.path != "/api/import":
                return super().do_POST()
            if not UPLOAD.acquire(blocking=False):
                return self.send_json(429, {"error": "Já está a decorrer uma importação."})
            try:
                length = int(self.headers.get("Content-Length", 0))
                budget = min(16 * 1024 * CHUNK, shutil.disk_usage(ROOT).free - 2 * 1024 * CHUNK)
                if (self.headers.get_content_type() != "application/x-xz"
                        or not 0 < length <= min(budget, 2 * 1024 * CHUNK)):
                    raise ValueError("Escolhe um arquivo XZ até 2 GiB com espaço suficiente no disco.")
                IMPORTS.mkdir(mode=0o700, exist_ok=True)
                token = uuid4().hex
                with TemporaryDirectory(prefix=".upload-", dir=IMPORTS) as directory:
                    path = Path(directory) / "view.sqlite3"
                    unpack(self.rfile, path, length, budget)
                    day, count = validate(path)
                    target = IMPORTS / f"{token}.sqlite3"
                    os.replace(path, target)
                for old in IMPORTS.glob("*.sqlite3"):
                    if old != target:
                        old.unlink(missing_ok=True)
                date = datetime.fromtimestamp(day, UTC).date().isoformat()
                return self.send_json(200, {"id": token, "date": date, "rows": count})
            except (ValueError, OSError, EOFError, lzma.LZMAError, sqlite3.Error) as exc:
                return self.send_json(400, {"error": f"Importação recusada. {exc}"})
            finally:
                UPLOAD.release()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/":
                html = (panel.ROOT / "index.html").read_text()
                html = html.replace("</html>", CONTROLS + "</html>", 1)
                return self.send(200, "text/html; charset=utf-8", html.encode())
            if url.path not in {"/api/view", "/api/export"}:
                return super().do_GET()

            try:
                args = {key: values[0] for key, values in parse_qs(
                    url.query, max_num_fields=16).items()}
                token = args.pop("archive", "")
                if not token:
                    if int(args.get("minutes", 60)) > 10080:
                        raise ValueError("A consulta em directo abrange até 7 dias.")
                    return super().do_GET()
                if len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token):
                    raise ValueError("Arquivo inválido.")
                path = IMPORTS / f"{token}.sqlite3"
                if not path.is_file():
                    raise ValueError("Importa novamente o arquivo.")
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})

            if not panel.QUERIES.acquire(blocking=False):
                return self.send_json(429, {"error": "O explorador está ocupado."})
            export = url.path == "/api/export"
            try:
                with closing(open_archive(path)) as db:
                    deadline = monotonic() + 15
                    db.set_progress_handler(lambda: monotonic() >= deadline, 10_000)
                    day = db.execute("SELECT day FROM archive_info").fetchone()[0]
                    result = panel.query(db, args, export=export, now=day + DAY)
            except (ValueError, sqlite3.Error) as exc:
                return self.send_json(400, {"error": f"Consulta indisponível. {exc}"})
            finally:
                panel.QUERIES.release()
            if export:
                return self.send(200, "text/csv; charset=utf-8",
                    panel.csv_bytes(result["rows"], args.get("group", "flow")), "fluxo-arquivo.csv")
            return self.send_json(200, result)

    return ArchiveHandler


if __name__ == "__main__":
    def stop(*_):
        raise SystemExit
    signal.signal(signal.SIGTERM, stop)
    worker()
