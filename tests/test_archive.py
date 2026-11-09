from contextlib import closing, redirect_stderr
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from io import BytesIO, StringIO
import hashlib
import json
import lzma
import sys

from collector.storage import open_db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "explorer"))
import daily_archive as archives

TODAY = 21_000 * archives.DAY
NOW = TODAY + 3600


class DailyArchiveTest(TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        self.volume = root / "archive"
        self.volume.mkdir()
        live = root / "live.sqlite3"
        self.db = open_db(live)
        self.addCleanup(self.db.close)
        settings = patch.multiple(archives, ROOT=self.volume, LIVE_PATH=live, time=lambda: NOW)
        settings.start()
        self.addCleanup(settings.stop)
        env = patch.dict("os.environ", {"RETENTION_DAYS": "30"})
        env.start()
        self.addCleanup(env.stop)

    def add_record(self, day):
        self.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)", (
            day + 3600, "192.0.2.1", "up", "192.0.2.2", 100, 1,
            '{"src_addr":"192.0.2.2","dst_addr":"198.51.100.1","proto":"TCP","dst_port":443}'
        ))
        self.db.commit()

    def file_for(self, day):
        return self.volume / f"fluxo-{datetime.fromtimestamp(day, UTC).date().isoformat()}.sqlite3.xz"

    def test_archives_completed_days_but_expires_only_outside_retention(self):
        expired = TODAY - 31 * archives.DAY
        retained = TODAY - 30 * archives.DAY
        yesterday = TODAY - archives.DAY
        for day in (expired, retained, yesterday, TODAY):
            self.add_record(day)

        # Exercise the real XZ writer and reader without allocating the extreme encoder.
        xz_file = lzma.LZMAFile

        def low_memory_xz(*args, **kwargs):
            if kwargs.get("preset") is not None:
                kwargs["preset"] = 0
            return xz_file(*args, **kwargs)

        with patch.object(lzma, "LZMAFile", side_effect=low_memory_xz):
            archives.archive_pass()
            archives.archive_pass()

        days = [row[0] for row in self.db.execute("SELECT received-3600 FROM records ORDER BY received")]
        self.assertEqual(days, [retained, yesterday, TODAY])
        self.assertFalse(self.file_for(TODAY).exists())
        for day in (expired, retained, yesterday):
            archive = self.file_for(day)
            self.assertTrue(archive.with_name(archive.name + ".sha256").is_file())
            raw = self.volume / "import.sqlite3"
            with archive.open("rb") as stream:
                archives.unpack(stream, raw, archive.stat().st_size)
            self.assertEqual(archives.validate(raw), (day, 1))
            with closing(archives.open_archive(raw)) as db:
                view = archives.panel.query(db, {"minutes": "1440"}, now=day + archives.DAY)
                self.assertEqual(view["matches"], 1)

    def test_imported_day_serves_a_historical_network_map(self):
        day = TODAY - archives.DAY
        self.add_record(day)
        raw = self.volume / "day.sqlite3"
        archives.merge_day(raw, day)
        packed = lzma.compress(raw.read_bytes(), preset=0)

        request = archives.handler(object)()
        request.path = "/api/import"
        request.headers = Message()
        request.headers["Content-Type"] = "application/x-xz"
        request.headers["Content-Length"] = str(len(packed))
        request.rfile = BytesIO(packed)
        request.send_json = Mock()
        with patch.object(archives, "IMPORTS", self.volume / ".imports"):
            request.do_POST()
            code, result = request.send_json.call_args.args
            self.assertEqual(code, 200, result)

            request.path = "/api/view?minutes=1440&network=192.0.2.0/24&archive=" + result["id"]
            request.do_GET()
            code, result = request.send_json.call_args.args
            self.assertEqual(code, 200, result)
            self.assertEqual(result["rows"][0]["peer"], "198.51.100.1")

    def make_saved(self, day):
        raw = self.volume / "day.sqlite3"
        archives.merge_day(raw, day)
        payload = lzma.compress(raw.read_bytes(), preset=0)
        raw.unlink()
        file = self.file_for(day)
        file.write_bytes(payload)
        sha256 = hashlib.sha256(payload).hexdigest()
        file.with_name(file.name + ".sha256").write_text(f"{sha256}  {file.name}\n")
        return file

    def test_catalog_lists_only_daily_files_newest_first(self):
        older = self.file_for(TODAY - 2 * archives.DAY)
        recent = self.file_for(TODAY - archives.DAY)
        older.write_bytes(b"older")
        recent.write_bytes(b"recent")
        self.file_for(TODAY).symlink_to(recent)
        self.file_for(TODAY + archives.DAY).mkdir()
        (self.volume / "fluxo-2027-02-30.sqlite3.xz").write_bytes(b"invalid date")
        (self.volume / "upload.sqlite3.xz").write_bytes(b"unrelated")
        request = archives.handler(object)()
        request.path = "/api/archives"
        request.send_json = Mock()
        request.do_GET()
        code, result = request.send_json.call_args.args
        self.assertEqual(code, 200)
        self.assertEqual(result["archives"], [
            {"date": datetime.fromtimestamp(day, UTC).date().isoformat(), "bytes": size}
            for day, size in ((TODAY - archives.DAY, 6), (TODAY - 2 * archives.DAY, 5))
        ])

    def test_open_saved_archive_stages_separate_read_only_copy(self):
        day = TODAY - archives.DAY
        self.add_record(day)
        self.add_record(TODAY)
        file = self.make_saved(day)
        original = file.read_bytes()
        live_before = list(self.db.execute("SELECT * FROM records"))
        request = archives.handler(object)()
        request.path = "/api/archives/open"
        request.send_json = Mock()

        def select(selected):
            body = json.dumps({"date": selected}).encode()
            request.headers = Message()
            request.headers["Content-Type"] = "application/json"
            request.headers["Content-Length"] = str(len(body))
            request.rfile = BytesIO(body)
            request.do_POST()
            return request.send_json.call_args.args

        iso_day = datetime.fromtimestamp(day, UTC).date().isoformat()
        imports = self.volume / ".imports"
        with patch.object(archives, "IMPORTS", imports):
            code, result = select(iso_day)
            self.assertEqual(code, 200, result)
            self.assertEqual(result["date"], iso_day)
            copy = imports / (result["id"] + ".sqlite3")
            with closing(archives.open_archive(copy)) as db:
                self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
            self.assertEqual(select("../live")[0], 400)
            file.with_name(file.name + ".sha256").write_text("0" * 64 + "  " + file.name)
            code, result = select(iso_day)
            self.assertEqual(code, 400)
            self.assertIn("SHA-256", result["error"])
            self.assertEqual(list(imports.iterdir()), [copy])

        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(list(self.db.execute("SELECT * FROM records")), live_before)
        self.add_record(TODAY)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 3)

    def test_failed_daily_export_preserves_sqlite_records(self):
        expired = TODAY - 31 * archives.DAY
        self.add_record(expired)
        with (patch.object(archives, "publish", side_effect=OSError("disk full")),
              redirect_stderr(StringIO()) as log):
            archives.archive_pass()
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        self.assertFalse(self.file_for(expired).exists())
        self.assertIn("disk full", log.getvalue())
