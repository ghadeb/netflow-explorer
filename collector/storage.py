"""Normalised flow records and minute aggregates, committed together."""
from ipaddress import IPv4Address
import json
import sqlite3

WINDOWS = (15, 60, 360, 1440, 10080, 43200)
MAX_INTEGER = (1 << 63) - 1
PORT_PROTOCOLS = {"6": "tcp", "17": "udp", "33": "dccp", "132": "sctp"}


def open_db(path):
    db = connect(path)
    db.execute("PRAGMA auto_vacuum = INCREMENTAL")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            received INTEGER NOT NULL, router TEXT NOT NULL,
            direction TEXT NOT NULL, client TEXT NOT NULL,
            bytes INTEGER NOT NULL, packets INTEGER NOT NULL, flow TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS records_received ON records(received);
        CREATE TABLE IF NOT EXISTS minute_clients (
            minute INTEGER NOT NULL, router TEXT NOT NULL, direction TEXT NOT NULL,
            client TEXT NOT NULL, bytes INTEGER NOT NULL, flows INTEGER NOT NULL,
            PRIMARY KEY (minute, router, direction, client)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS minute_router ON minute_clients(router, minute);
        CREATE TABLE IF NOT EXISTS minute_ports (
            minute INTEGER NOT NULL, router TEXT NOT NULL, direction TEXT NOT NULL,
            client TEXT NOT NULL, source INTEGER NOT NULL,
            protocol TEXT NOT NULL, port INTEGER NOT NULL, bytes INTEGER NOT NULL,
            PRIMARY KEY (minute, router, direction, client, source, protocol, port)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS ports_router ON minute_ports(router, minute);
        CREATE TABLE IF NOT EXISTS routers (
            address TEXT PRIMARY KEY, last_seen INTEGER NOT NULL
        ) WITHOUT ROWID;
    """)
    return db


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def parse_event(direction, raw, allowed):
    try:
        flow = json.loads(raw)
        if direction not in ("up", "down") or flow["type"] != "NETFLOW_V9":
            return None
        router = str(IPv4Address(flow["sampler_address"]))
        if router not in allowed:
            return None
        source = str(IPv4Address(flow["src_addr"]))
        destination = str(IPv4Address(flow["dst_addr"]))
        received = int(flow["time_received_ns"]) // 10**9
        size = int(flow["bytes"])
        packets = int(flow.get("packets", 0))
        if received <= 0 or not 0 <= size <= MAX_INTEGER or not 0 <= packets <= MAX_INTEGER:
            return None
        client = source if direction == "up" else destination
        protocol = str(flow.get("proto", "")).lower()
        protocol = PORT_PROTOCOLS.get(protocol, protocol)
        port = int(flow.get("dst_port", 0))
        if protocol not in PORT_PROTOCOLS.values() or not 1 <= port <= 65535:
            port = 0
        return (received, router, direction, client, size, packets, raw, int(IPv4Address(source)), protocol, port), flow
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None


def ingest(db, events, allowed):
    rows = [parsed[0] for direction, raw in events if (parsed := parse_event(direction, raw, allowed))]
    counts = {}
    ports = {}
    seen = {}
    for received, router, direction, client, size, _, _, source, protocol, port in rows:
        key = received // 60 * 60, router, direction, client
        amount, flows = counts.get(key, (0, 0))
        counts[key] = amount + size, flows + 1
        port_key = (*key, source, protocol, port)
        ports[port_key] = ports.get(port_key, 0) + size
        seen[router] = max(received, seen.get(router, 0))
    with db:
        db.executemany("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)", (row[:7] for row in rows))
        db.executemany("""
            INSERT INTO minute_clients VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET bytes = bytes + excluded.bytes, flows = flows + excluded.flows
        """, ((*key, *values) for key, values in counts.items()))
        db.executemany("""
            INSERT INTO minute_ports VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET bytes = bytes + excluded.bytes
        """, ((*key, value) for key, value in ports.items()))
        db.executemany("""
            INSERT INTO routers VALUES (?, ?)
            ON CONFLICT DO UPDATE SET last_seen = max(last_seen, excluded.last_seen)
        """, seen.items())
    return len(rows)


def prune(db, cutoff):
    with db:
        db.execute("DELETE FROM records WHERE received < ?", (cutoff,))
        db.execute("DELETE FROM minute_clients WHERE minute < ?", (cutoff,))
        db.execute("DELETE FROM minute_ports WHERE minute < ?", (cutoff,))
        db.execute("DELETE FROM routers WHERE last_seen < ?", (cutoff,))
    db.execute("PRAGMA incremental_vacuum(128)")


def dashboard(db, now, minutes, router, *, detail=False, prefix=None):
    if minutes not in WINDOWS:
        raise ValueError("intervalo inválido")
    if router:
        router = str(IPv4Address(router))
    if prefix is not None and not 0 <= prefix <= 32:
        raise ValueError("prefixo inválido")
    end = (now // 60 + 1) * 60
    start = end - minutes * 60
    step = {15: 60, 60: 60, 360: 300, 1440: 900, 10080: 7200, 43200: 21600}[minutes]
    where = "minute >= ? AND minute < ?"
    args = [start, end]
    if router:
        where += " AND router = ?"
        args.append(router)

    # Both queries share a WAL read snapshot while the collector keeps writing.
    db.execute("BEGIN")
    try:
        routers = [row["address"] for row in db.execute("SELECT address FROM routers ORDER BY address")]
        last_seen = db.execute(
            "SELECT max(last_seen) FROM routers" + (" WHERE address = ?" if router else ""),
            (router,) if router else (),
        ).fetchone()[0]
        series = {
            at: {"at": at, "up": 0, "down": 0}
            for at in range(start, end, step)
        }
        totals = {"up": 0, "down": 0, "flows": 0}
        for row in db.execute(f"""
            SELECT ((minute - ?) / ?) AS slot, direction, sum(bytes) AS bytes, sum(flows) AS flows
            FROM minute_clients WHERE {where} GROUP BY slot, direction
        """, (start, step, *args)):
            series[start + row["slot"] * step][row["direction"]] = row["bytes"]
            totals[row["direction"]] += row["bytes"]
            totals["flows"] += row["flows"]
        talkers = _talkers(db, where, args, detail, prefix)
    finally:
        db.rollback()
    return {"routers": routers, "totals": totals, "talkers": talkers,
            "series": list(series.values()), "bucket_seconds": step, "last_seen": last_seen}


def _talkers(db, where, args, detail, prefix):
    table = "minute_ports" if detail or prefix is not None else "minute_clients"
    dimensions = "ip, protocol, port" if detail else "ip"
    address = "client"
    if prefix is not None:
        address = "source & ?"
        args = [(0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF, *args]
    columns = ", protocol, port" if detail else ""
    rows = db.execute(f"""
        SELECT {address} AS ip {columns},
               sum(CASE WHEN direction = 'up' THEN bytes ELSE 0 END) AS up,
               sum(CASE WHEN direction = 'down' THEN bytes ELSE 0 END) AS down
        FROM {table} WHERE {where}
        GROUP BY {dimensions} ORDER BY sum(bytes) DESC, {dimensions} LIMIT 20
    """, args)
    talkers = [dict(row) for row in rows]
    if prefix is not None:
        for row in talkers:
            row["ip"] = f"{IPv4Address(row['ip'])}/{prefix}"
    return talkers
