from contextlib import closing, redirect_stderr
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from io import BytesIO, StringIO
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

    def test_failed_daily_export_preserves_sqlite_records(self):
        expired = TODAY - 31 * archives.DAY
        self.add_record(expired)
        with (patch.object(archives, "publish", side_effect=OSError("disk full")),
              redirect_stderr(StringIO()) as log):
            archives.archive_pass()
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        self.assertFalse(self.file_for(expired).exists())
        self.assertIn("disk full", log.getvalue())
