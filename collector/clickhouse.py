"""Direct ClickHouse ingestion using HTTP and typed RowBinary batches."""
from contextlib import contextmanager
from fcntl import LOCK_EX, flock
from http.client import HTTPException
from ipaddress import IPv4Address
from pathlib import Path
import re
from struct import Struct
from time import time_ns
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import logging
import os

from collector.storage import parse_event

LOG = logging.getLogger(__name__)
DAY = 86400
INGEST_LOCK = Path("/archives/.clickhouse-ingest.lock")
PROTOCOLS = {"1": "ICMP", "6": "TCP", "17": "UDP", "33": "DCCP",
             "47": "GRE", "50": "ESP", "51": "AH", "132": "SCTP"}
ROW = Struct("<QIIIIBHHIIQQ")
DDL = """
CREATE TABLE IF NOT EXISTS flows (
    id UInt64,
    received UInt32,
    router UInt32,
    src UInt32,
    dst UInt32,
    sent UInt8,
    sport UInt16,
    dport UInt16,
    sasn UInt32,
    dasn UInt32,
    bytes UInt64,
    packets UInt64,
    proto String,
    flow String CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY intDiv(received, 86400)
ORDER BY (received, router, id)
SETTINGS non_replicated_deduplication_window = 256
"""
APPLICATION_OPTIONS = """
CREATE TABLE IF NOT EXISTS application_names (
    router UInt32,
    domain UInt32,
    application_id String,
    name String,
    seen_at UInt64
)
ENGINE = ReplacingMergeTree(seen_at)
ORDER BY (router, domain, application_id)
"""
APPLICATION_COLUMNS = """
ALTER TABLE flows
    ADD COLUMN IF NOT EXISTS application_id String
        MATERIALIZED JSONExtractString(flow, 'application_id'),
    ADD COLUMN IF NOT EXISTS application_domain UInt64
        MATERIALIZED JSONExtractUInt(flow, 'observation_domain_id')
"""
APPLICATION_ID = re.compile(r"(?:[0-9a-f]{2}){2,16}")
APPLICATION_CACHE_LIMIT = 32768


class ClickHouseError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class Client:
    def __init__(self, timeout=15):
        self.endpoint = os.environ["CLICKHOUSE_URL"].rstrip("/")
        self.headers = {
            "X-ClickHouse-User": os.environ.get("CLICKHOUSE_USER", "netflow"),
            "X-ClickHouse-Key": os.environ["CLICKHOUSE_PASSWORD"],
        }
        self.timeout = timeout

    @contextmanager
    def request(self, sql, params=None, settings=None, data=None):
        options = {"database": "netflow", "wait_end_of_query": "1", **(settings or {})}
        options.update({f"param_{key}": value for key, value in (params or {}).items()})
        if data is not None:
            options["query"] = sql
        req = Request(f"{self.endpoint}/?{urlencode(options)}",
                      data=sql.encode() if data is None else data, headers=self.headers)
        try:
            with urlopen(req, timeout=self.timeout) as response:
                yield response
        except HTTPError as exc:
            message = exc.read(400).decode(errors="replace").strip()
            raise ClickHouseError(f"HTTP {exc.code}: {message}", exc.code == 429 or exc.code >= 500) from exc
        except (OSError, HTTPException) as exc:
            raise ClickHouseError(str(exc), retryable=True) from exc

    def execute(self, sql, *, params=None, settings=None, data=None):
        with self.request(sql, params, settings, data) as response:
            result = response.read()
        if result.strip():
            raise ClickHouseError("Unexpected ClickHouse command response.")

    def rows(self, sql, params=None, *, streaming=False, settings=None):
        query_settings = {
            "output_format_json_quote_64bit_integers": 0,
            "max_threads": 1,
            "max_execution_time": 0 if streaming else 12,
            "wait_end_of_query": 0 if streaming else 1,
            **(settings or {}),
        }
        with self.request(sql + " FORMAT JSONEachRow", params, query_settings) as response:
            try:
                for line in response:
                    yield json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ClickHouseError("Invalid or interrupted ClickHouse response.") from exc


@contextmanager
def ingest_lock():
    with INGEST_LOCK.open("a") as lock:
        flock(lock, LOCK_EX)
        yield


def write_string(buffer, value):
    raw = value.encode()
    length = len(raw)
    while length >= 128:
        buffer.append((length & 127) | 128)
        length >>= 7
    buffer.append(length)
    buffer.extend(raw)


def unsigned(value, bits):
    try:
        value = int(value)
    except (ValueError, TypeError, OverflowError):
        return 0
    return value if 0 <= value < 2**bits else 0


def add_application_option(raw, allowed, applications):
    try:
        option = json.loads(raw)
        if option["type"] != "NETFLOW_V9_APPLICATION" or option["sampler_address"] not in allowed:
            return
        application_id = option["application_id"]
        if not APPLICATION_ID.fullmatch(application_id) or application_id.startswith("00"):
            return
        name = option["application_name"].strip()
        if not 0 < len(name.encode()) <= 255 or not name.isprintable():
            return
        seen_at = int(option["time_received_ns"])
        domain = int(option["observation_domain_id"])
        if not 0 < seen_at < 2**64 or not 0 <= domain < 2**32:
            return
        key = (int(IPv4Address(option["sampler_address"])), domain, application_id)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return
    if key not in applications or seen_at >= applications[key][1]:
        applications[key] = (name, seen_at)


def encode_batch(events, allowed, first_id, applications=None):
    data = bytearray()
    count = 0
    for direction, raw in events:
        parsed = parse_event(direction, raw, allowed)
        if parsed is None:
            if applications is not None:
                add_application_option(raw, allowed, applications)
            continue
        row, flow = parsed
        received, router, _, _, size, packets, _, src, _, _ = row
        if received >= 2**32:
            continue
        proto = str(flow.get("proto") or "").upper()
        data.extend(ROW.pack(first_id + count, received, int(IPv4Address(router)), src,
                             int(IPv4Address(flow["dst_addr"])), direction == "up",
                             unsigned(flow.get("src_port"), 16), unsigned(flow.get("dst_port"), 16),
                             unsigned(flow.get("src_as"), 32), unsigned(flow.get("dst_as"), 32),
                             size, packets))
        write_string(data, PROTOCOLS.get(proto, proto or "Desconhecido"))
        write_string(data, raw)
        count += 1
    return bytes(data), count


class Writer:
    def __init__(self):
        self.client = Client(timeout=8)
        startup = Client(timeout=60)
        startup.execute(DDL)
        startup.execute(APPLICATION_OPTIONS)
        startup.execute(APPLICATION_COLUMNS)
        codec = next(startup.rows("""
            SELECT compression_codec FROM system.columns
            WHERE database='netflow' AND table='flows' AND name='flow'
        """))["compression_codec"]
        if codec != "CODEC(ZSTD(1))":
            startup.execute("ALTER TABLE flows MODIFY COLUMN flow CODEC(ZSTD(1))")
        self.committed_id = next(startup.rows("SELECT max(id) AS last FROM flows", streaming=True))["last"]
        self.next_id = max(time_ns(), self.committed_id + 1)
        self.application_cache = {}

    def close(self):
        pass

    def ingest(self, events, allowed, stopped):
        first = self.next_id
        applications = {}
        payload, count = encode_batch(events, allowed, first, applications)
        if not count and not applications:
            return 0
        with ingest_lock():
            self.store_applications(applications, stopped)
            if count:
                self.insert("INSERT INTO flows FORMAT RowBinary", payload, stopped,
                            insert_deduplication_token=str(first))
        if count:
            self.next_id += count
            self.committed_id = self.next_id - 1
        return count

    def store_applications(self, applications, stopped):
        updates = {}
        for key, (name, seen_at) in applications.items():
            previous = self.application_cache.get(key)
            if previous is None or seen_at >= previous[1]:
                updates[key] = (name, seen_at)
        changed = [
            {"router": router, "domain": domain, "application_id": app_id,
             "name": name, "seen_at": seen_at}
            for (router, domain, app_id), (name, seen_at) in updates.items()
            if self.application_cache.get((router, domain, app_id), (None, 0))[0] != name
        ]
        if changed:
            payload = "\n".join(json.dumps(row) for row in changed).encode()
            self.insert("INSERT INTO application_names FORMAT JSONEachRow", payload, stopped)
        if len(self.application_cache) + len(updates) > APPLICATION_CACHE_LIMIT:
            self.application_cache.clear()
        self.application_cache.update(updates)

    def insert(self, sql, payload, stopped, **settings):
        delay = 0.5
        while True:
            try:
                self.client.execute(sql, settings={"async_insert": 0, **settings}, data=payload)
                return
            except ClickHouseError as exc:
                if not exc.retryable or stopped.is_set():
                    raise
                LOG.warning("ClickHouse insert pending, retry in %.1fs: %s", delay, exc)
                if stopped.wait(delay):
                    raise
                delay = min(delay * 2, 5)
