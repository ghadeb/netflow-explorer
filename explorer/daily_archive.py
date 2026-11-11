import hashlib
import json
import lzma
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from time import monotonic, sleep, time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import server as panel

ROOT = Path("/archives")
IMPORTS = ROOT / ".imports"
LIVE_PATH = Path("/data/flows.sqlite3")
DAY = 86400
CHUNK = 1024 * 1024
IMPORT_RESERVE = 2 * 1024 * CHUNK
SPACE_CHECK = 64 * CHUNK
ARCHIVE_NAME = re.compile(r"fluxo-([0-9]{4}-[0-9]{2}-[0-9]{2})\.sqlite3\.xz")
IMPORT_TOKEN = re.compile(r"[0-9a-f]{32}")
FIELDS = "received router direction client bytes packets flow".split()
COLUMNS = ",".join(FIELDS)
MATCH = " AND ".join(f"a.{field}=r.{field}" for field in FIELDS)
APP_ID = 1179408463
UPLOAD = BoundedSemaphore(1)


def live_uri():
    return LIVE_PATH.as_uri() + "?mode=ro"


def retention_days():
    return int(os.environ.get("RETENTION_DAYS", "30"))


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack(source, output, remaining, limit=None, checksum=None):
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=128 * CHUNK)
    compressed = hashlib.sha256() if checksum is not None else None
    total = 0
    next_check = SPACE_CHECK
    with output.open("wb") as target:
        while not decoder.eof:
            if decoder.needs_input:
                block = source.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("Arquivo XZ incompleto.")
                remaining -= len(block)
                if compressed is not None:
                    compressed.update(block)
            else:
                block = b""
            data = decoder.decompress(block, max_length=CHUNK)
            if limit is not None and total >= next_check:
                # Leave headroom if the collector and archive volume share a filesystem.
                free = shutil.disk_usage(output.parent).free
                limit = min(limit, total + free - IMPORT_RESERVE)
                next_check = total + SPACE_CHECK
            total += len(data)
            if limit is not None and total > limit:
                raise ValueError("O arquivo descomprimido excede o espaço disponível.")
            target.write(data)
        if remaining or decoder.unused_data:
            raise ValueError("O ficheiro deve conter um único arquivo XZ.")
        if compressed is not None and compressed.hexdigest() != checksum:
            raise ValueError("O SHA-256 do arquivo não corresponde.")


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


def init_archive(db, unique_source=False):
    unique = "UNIQUE " if unique_source else ""
    db.executescript(f"""
        PRAGMA application_id={APP_ID};
        CREATE TABLE IF NOT EXISTS records (
            source_id INTEGER NOT NULL, received INTEGER NOT NULL,
            router TEXT NOT NULL, direction TEXT NOT NULL, client TEXT NOT NULL,
            bytes INTEGER NOT NULL, packets INTEGER NOT NULL, flow TEXT NOT NULL
        );
        CREATE {unique}INDEX IF NOT EXISTS source_row ON records(source_id);
        CREATE INDEX IF NOT EXISTS records_received ON records(received);
        CREATE TABLE IF NOT EXISTS routers(address TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS archive_info(day INTEGER, rows INTEGER);
    """)


def merge_day(raw, day):
    with closing(sqlite3.connect(raw, uri=True, timeout=10)) as db:
        init_archive(db)
        db.execute("ATTACH DATABASE ? AS live", (live_uri(),))
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


def publish(raw, destination, work, preset=9 | lzma.PRESET_EXTREME):
    candidate = work / "day.xz"
    with raw.open("rb") as source, candidate.open("wb") as target:
        with lzma.LZMAFile(target, "w", preset=preset) as packed:
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
    with closing(sqlite3.connect(LIVE_PATH, timeout=5)) as db:
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


def archive_day(day, destination, expire):
    if destination.exists() and not expire:
        return
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
                    if digest(stream) != checksum.read_text().partition(" ")[0]:
                        raise ValueError(f"SHA-256 inválido em {destination.name}")

        added = merge_day(raw, day)
        if not added and not destination.exists():
            return
        _, count = validate(raw)
        if added:
            publish(raw, destination, work)
            print(f"Arquivo verificado: {destination.name} · {count} registos", flush=True)
        if expire:
            expire_verified(raw, day)


def archive_pass():
    now = int(time())
    complete = ((now - 900) // DAY) * DAY
    cutoff = now - retention_days() * DAY
    with closing(sqlite3.connect(live_uri(), uri=True, timeout=5)) as source:
        oldest = source.execute("SELECT min(received) FROM records").fetchone()[0]
    if oldest is None:
        return

    for day in range(oldest // DAY * DAY, complete, DAY):
        date = datetime.fromtimestamp(day, UTC).date().isoformat()
        destination = ROOT / f"fluxo-{date}.sqlite3.xz"
        try:
            archive_day(day, destination, day + DAY <= cutoff)
        except (OSError, ValueError, EOFError, lzma.LZMAError, sqlite3.Error) as exc:
            print(f"Falha no arquivo {destination.name}: {exc}", file=sys.stderr, flush=True)


def worker():
    os.nice(10)
    while True:
        try:
            archive_pass()
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"Falha no arquivo diário: {exc}", file=sys.stderr, flush=True)
        sleep(900)


def archive_catalog():
    items = []
    for path in ROOT.iterdir():
        match = ARCHIVE_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match[1])
            metadata = path.stat(follow_symlinks=False)
        except (ValueError, FileNotFoundError):
            continue
        if stat.S_ISREG(metadata.st_mode) and metadata.st_size > 0:
            items.append({"date": day.isoformat(), "bytes": metadata.st_size})
    return sorted(items, key=lambda item: item["date"], reverse=True)


def open_regular(path):
    source = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
        source.close()
        raise ValueError("Arquivo inválido.")
    return source


def stage_import(source, length, expected_date=None, checksum=None):
    budget = shutil.disk_usage(ROOT).free - IMPORT_RESERVE
    if not 0 < length <= budget:
        raise ValueError("Escolhe um arquivo XZ com espaço suficiente no disco.")
    IMPORTS.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(IMPORTS.stat(follow_symlinks=False).st_mode):
        raise ValueError("Directório de importação indisponível.")
    token = uuid4().hex
    target = IMPORTS / f"{token}.sqlite3"
    with TemporaryDirectory(prefix=".upload-", dir=IMPORTS) as directory:
        path = Path(directory) / "view.sqlite3"
        unpack(source, path, length, budget, checksum)
        day, count = validate(path)
        iso_day = datetime.fromtimestamp(day, UTC).date().isoformat()
        if expected_date is not None and iso_day != expected_date:
            raise ValueError("A data do arquivo não corresponde ao ficheiro seleccionado.")
        os.replace(path, target)

    for old in IMPORTS.glob("*.sqlite3"):
        if old != target and IMPORT_TOKEN.fullmatch(old.stem):
            old.unlink(missing_ok=True)
    return {"id": token, "date": iso_day, "rows": count}


def open_saved_archive(day):
    name = f"fluxo-{day}.sqlite3.xz"
    if not ARCHIVE_NAME.fullmatch(name):
        raise ValueError("Arquivo inválido.")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("Arquivo inválido.") from exc

    path = ROOT / name
    checksum = None
    try:
        with open_regular(path.with_name(name + ".sha256")) as metadata:
            expected, _, _ = metadata.read(128).partition(b" ")
            if not re.fullmatch(rb"[0-9a-f]{64}", expected):
                raise ValueError("O SHA-256 do arquivo não corresponde.")
            checksum = expected.decode("ascii")
    except FileNotFoundError:
        pass
    with open_regular(path) as source:
        return stage_import(source, os.fstat(source.fileno()).st_size, day, checksum)


def handler(base, load_applications=panel.ApplicationNames):
    class ArchiveHandler(base):
        def do_POST(self):
            if self.path not in {"/api/import", "/api/archives/open"}:
                return super().do_POST()
            if not UPLOAD.acquire(blocking=False):
                return self.send_json(429, {"error": "Já está a decorrer uma importação."})
            try:
                length = int(self.headers.get("Content-Length", 0))
                content_type = self.headers.get_content_type()
                if self.path == "/api/archives/open":
                    if content_type != "application/json" or not 0 < length <= 128:
                        raise ValueError("Arquivo inválido.")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or not isinstance(body.get("date"), str):
                        raise ValueError("Arquivo inválido.")
                    result = open_saved_archive(body["date"])
                else:
                    if content_type != "application/x-xz":
                        raise ValueError("Escolhe um arquivo XZ com espaço suficiente no disco.")
                    result = stage_import(self.rfile, length)
                return self.send_json(200, result)
            except FileNotFoundError:
                return self.send_json(404, {"error": "O arquivo já não está disponível."})
            except (ValueError, OSError, EOFError, lzma.LZMAError, sqlite3.Error) as exc:
                return self.send_json(400, {"error": f"Importação recusada. {exc}"})
            finally:
                UPLOAD.release()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/api/archives":
                try:
                    return self.send_json(200, {"archives": archive_catalog()})
                except OSError:
                    return self.send_json(503, {"error": "Não foi possível listar os arquivos."})
            if url.path not in {"/api/view", "/api/export"}:
                return super().do_GET()

            try:
                args = {key: values[0] for key, values in parse_qs(
                    url.query, max_num_fields=16).items()}
                token = args.pop("archive", "")
                if not token:
                    if int(args.get("minutes", 60)) > retention_days() * 1440:
                        raise ValueError("O período excede a retenção em directo.")
                    return super().do_GET()
                if not IMPORT_TOKEN.fullmatch(token):
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
                    result = panel.query(db, args, export=export, now=day + DAY,
                                         load_applications=load_applications)
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
