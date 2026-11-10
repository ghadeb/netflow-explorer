"""Direct ClickHouse ingestion using HTTP and typed RowBinary batches."""
from contextlib import contextmanager
from fcntl import LOCK_EX, flock
from http.client import HTTPException
from ipaddress import IPv4Address
from pathlib import Path
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
    flow String
)
ENGINE = MergeTree
PARTITION BY intDiv(received, 86400)
ORDER BY (received, router, id)
SETTINGS non_replicated_deduplication_window = 256
"""


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

    def rows(self, sql, params=None, *, streaming=False):
        settings = {
            "output_format_json_quote_64bit_integers": 0,
            "max_threads": 1,
            "max_execution_time": 0 if streaming else 12,
            "wait_end_of_query": 0 if streaming else 1,
        }
        with self.request(sql + " FORMAT JSONEachRow", params, settings) as response:
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


def encode_batch(events, allowed, first_id):
    data = bytearray()
    count = 0
    for direction, raw in events:
        parsed = parse_event(direction, raw, allowed)
        if parsed is None:
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
        self.committed_id = next(startup.rows("SELECT max(id) AS last FROM flows", streaming=True))["last"]
        self.next_id = max(time_ns(), self.committed_id + 1)

    def close(self):
        pass

    def ingest(self, events, allowed, stopped):
        first = self.next_id
        payload, count = encode_batch(events, allowed, first)
        if not count:
            return 0
        with ingest_lock():
            delay = 0.5
            while True:
                try:
                    self.client.execute(
                        "INSERT INTO flows FORMAT RowBinary",
                        settings={"insert_deduplication_token": str(first), "async_insert": 0},
                        data=payload,
                    )
                    break
                except ClickHouseError as exc:
                    if not exc.retryable or stopped.is_set():
                        raise
                    LOG.warning("ClickHouse batch of %d pending, retry in %.1fs: %s", count, delay, exc)
                    if stopped.wait(delay):
                        raise
                    delay = min(delay * 2, 5)
        self.next_id += count
        self.committed_id = self.next_id - 1
        return count
