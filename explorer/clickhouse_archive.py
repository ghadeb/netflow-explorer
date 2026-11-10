"""Write verified, backwards-compatible daily archives from ClickHouse."""
from contextlib import closing
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep, time
import logging
import lzma
import os
import signal
import sqlite3
import sys

sys.path.insert(0, "/app")

from collector.clickhouse import ClickHouseError, Client, DAY, ingest_lock
import daily_archive as archives

LOG = logging.getLogger(__name__)
WINDOW = "received >= {start:UInt32} AND received < {end:UInt32}"
FIELDS = ("source_id", *archives.FIELDS)
COLUMNS = ",".join(FIELDS)
MATCH = " AND ".join(f"records.{field}=excluded.{field}" for field in archives.FIELDS)
UPSERT = f"""
    INSERT INTO records ({COLUMNS}) VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(source_id) DO UPDATE SET received=NULL WHERE NOT ({MATCH})
"""


def snapshot(client, params):
    return next(client.rows(f"SELECT count() AS rows, max(id) AS last FROM flows WHERE {WINDOW}", params))


def copy_snapshot(client, raw, params, day, expected):
    sql = f"""
        SELECT id AS source_id, received, IPv4NumToString(router) AS exporter,
               if(sent=1,'up','down') AS direction, IPv4NumToString(if(sent=1,src,dst)) AS client,
               bytes, packets, flow
        FROM flows WHERE {WINDOW} AND id <= {{last:UInt64}}
    """
    received_rows = 0
    with closing(sqlite3.connect(raw)) as db:
        archives.init_archive(db)
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS source_identity ON records(source_id)")
        with db:
            before = db.total_changes
            for batch in batched(client.rows(sql, {**params, "last": expected["last"]}, streaming=True), 4096):
                for row in batch:
                    row["router"] = row.pop("exporter")
                db.executemany(UPSERT, (tuple(row[field] for field in FIELDS) for row in batch))
                received_rows += len(batch)
            if received_rows != expected["rows"]:
                raise ValueError("The ClickHouse archive snapshot was incomplete.")
            added = db.total_changes - before
            if added:
                db.execute("INSERT OR IGNORE INTO routers SELECT DISTINCT router FROM records")
                db.execute("DELETE FROM archive_info")
                db.execute("INSERT INTO archive_info SELECT ?,count(*) FROM records", (day,))
    return added


def archive_day(client, day, expire, preset):
    date = datetime.fromtimestamp(day, UTC).date().isoformat()
    destination = archives.ROOT / f"fluxo-{date}.sqlite3.xz"
    if destination.exists() and not expire:
        return
    params = {"start": day, "end": day + DAY}
    expected = snapshot(client, params)
    if not expected["rows"]:
        return
    with TemporaryDirectory(prefix=".build-", dir=archives.ROOT) as folder:
        work = Path(folder)
        raw = work / "day.sqlite3"
        if destination.exists():
            checksum = destination.with_name(destination.name + ".sha256").read_text().split()[0]
            with destination.open("rb") as stream:
                archives.unpack(stream, raw, destination.stat().st_size, checksum=checksum)
            if archives.validate(raw)[0] != day:
                raise ValueError(f"Archive date differs from {destination.name}.")

        added = copy_snapshot(client, raw, params, day, expected)
        _, count = archives.validate(raw)
        if added:
            archives.publish(raw, destination, work, preset)
            LOG.info("Verified archive %s: %d records", destination.name, count)
        if expire:
            with ingest_lock():
                if snapshot(client, params) == expected:
                    client.execute(f"ALTER TABLE flows DROP PARTITION {day // DAY}")
                    LOG.info("Expired verified partition %s", date)


def archive_pass(client, now, days, preset):
    completed = ((now - 900) // DAY) * DAY
    cutoff = now - days * DAY
    parts = list(client.rows("""
        SELECT DISTINCT toUInt32(partition) AS day_index FROM system.parts
        WHERE database='netflow' AND table='flows' AND active ORDER BY day_index
    """))
    for row in parts:
        day = row["day_index"] * DAY
        if day >= completed:
            continue
        try:
            archive_day(client, day, day + DAY <= cutoff, preset)
        except (ClickHouseError, OSError, ValueError, IndexError, EOFError, lzma.LZMAError, sqlite3.Error):
            LOG.exception("ClickHouse archive failed for UTC day starting %d", day)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.nice(10)
    days = archives.retention_days()
    preset = int(os.getenv("CLICKHOUSE_ARCHIVE_XZ_LEVEL", "3"))
    if not 0 <= preset <= 9:
        raise ValueError("CLICKHOUSE_ARCHIVE_XZ_LEVEL must be between 0 and 9.")

    def stop(*_):
        raise SystemExit

    signal.signal(signal.SIGTERM, stop)
    client = Client(timeout=30)
    while True:
        try:
            archive_pass(client, int(time()), days, preset)
        except ClickHouseError:
            LOG.exception("ClickHouse archive scan failed")
        sleep(900)


if __name__ == "__main__":
    main()
