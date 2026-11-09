from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network, ip_address
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic, time
from urllib.parse import parse_qs, urlsplit
import csv
import io
import json
import logging
import sqlite3
import sys

import dns.exception

from ptr import configured_resolvers, lookup

ROOT = Path(__file__).parent
DB = "file:/data/flows.sqlite3?mode=ro"
SERVICE_FILE = ROOT.parent / "collector" / "port-services.json"
if not SERVICE_FILE.is_file():
    SERVICE_FILE = Path("/app/collector/port-services.json")
SERVICES = json.loads(SERVICE_FILE.read_text())
PROTOCOLS = {"1": "ICMP", "6": "TCP", "17": "UDP", "33": "DCCP",
             "47": "GRE", "50": "ESP", "51": "AH", "132": "SCTP"}
WINDOWS = {15, 60, 360, 1440, 10080, 43200}
EXPORT_LIMIT = 50_000
QUERIES = BoundedSemaphore(2)
LOOKUPS = BoundedSemaphore(4)
NETWORK_PAGE_SIZE = 12
LOG = logging.getLogger("fluxo.ptr")

TOTALS = """
    coalesce(sum(CASE WHEN direction='up' THEN bytes ELSE 0 END),0) AS up,
    coalesce(sum(CASE WHEN direction='down' THEN bytes ELSE 0 END),0) AS down,
    coalesce(sum(bytes),0) AS total, coalesce(sum(packets),0) AS packets,
    count(*) AS flows
"""
PAIR = "src || ' → ' || dst"
PORT = "CASE WHEN dport>0 THEN proto || '/' || dport END"
ASN = "CASE WHEN dasn>0 THEN 'AS' || dasn END"
CHARTS = {"src": "src", "dst": "dst", "proto": "proto",
          "port": PORT, "pair": PAIR, "asn": ASN}
GROUPS = {
    "src": "src", "dst": "dst", "pair": PAIR,
    "srcnet": "subnet(src,:prefix)", "dstnet": "subnet(dst,:prefix)",
    "port": PORT, "asn": ASN,
}
SORTS = {"total": "total DESC", "up": "up DESC", "down": "down DESC",
         "packets": "packets DESC", "recent": "received DESC"}


def protocol(value):
    value = str(value or "").upper()
    return PROTOCOLS.get(value, value or "Desconhecido")


def service(proto, port):
    return SERVICES.get(f"{str(proto).lower()}/{port}", "")


def subnet(ip, prefix):
    try:
        return str(IPv4Network((ip, prefix), strict=False))
    except (TypeError, ValueError):
        return None


def ipv4_number(address):
    try:
        return int(IPv4Address(address))
    except (TypeError, ValueError):
        return None


def reported_hop(value):
    try:
        address = ip_address(value)
    except (TypeError, ValueError):
        return None
    return None if address.is_unspecified else str(address)


def flow_observation(db, args, params):
    flow_id = int(args["flow_id"])
    received = int(args.get("flow_received", 0))
    if not (1 <= flow_id <= params["ceiling"] and params["start"] <= received < params["end"]):
        raise ValueError("Este fluxo já não está disponível. Actualiza o painel.")
    row = db.execute("""
        SELECT received, router, direction, bytes, packets, flow
        FROM records WHERE rowid=? AND received=?
    """, (flow_id, received)).fetchone()
    if row is None:
        raise ValueError("Este fluxo já não está disponível. Actualiza o painel.")
    observation = dict(row)
    raw = json.loads(observation.pop("flow"))
    for name, field in (("src", "src_addr"), ("dst", "dst_addr"), ("sport", "src_port"),
                        ("dport", "dst_port"), ("sasn", "src_as"), ("dasn", "dst_as"),
                        ("in_if", "in_if"), ("out_if", "out_if"),
                        ("started", "time_flow_start_ns"), ("ended", "time_flow_end_ns")):
        observation[name] = raw.get(field)
    observation["next_hop"] = reported_hop(raw.get("next_hop"))
    observation["bgp_next_hop"] = reported_hop(raw.get("bgp_next_hop"))
    observation["proto"] = protocol(raw.get("proto"))
    observation["service"] = service(observation["proto"], observation["dport"])
    return observation


def destination_map(db, base, params, target, page):
    host = "/" not in target
    try:
        network = IPv4Network(target, strict=False)
    except ValueError as exc:
        raise ValueError("Indica um endereço IPv4 ou uma rede IPv4 em formato CIDR.") from exc
    params = dict(params, address=str(network.network_address),
                  first=int(network.network_address), last=int(network.broadcast_address),
                  map_size=NETWORK_PAGE_SIZE, map_offset=(page - 1) * NETWORK_PAGE_SIZE)
    peer = "CASE WHEN src=:address THEN dst ELSE src END" if host else "dst"
    match = "(src=:address OR dst=:address)" if host else "ipv4_number(src) BETWEEN :first AND :last"
    rows = [dict(row) for row in db.execute(base + f"""
        SELECT {peer} AS peer, {TOTALS}, count(*) OVER () AS matches,
               sum(sum(bytes)) OVER () AS network_total, max(sum(bytes)) OVER () AS peak,
               sum(CASE WHEN src=:address THEN bytes ELSE 0 END) AS from_host,
               sum(CASE WHEN src!=:address THEN bytes ELSE 0 END) AS to_host
        FROM filtered
        WHERE src IS NOT NULL AND dst IS NOT NULL AND {match}
        GROUP BY 1
        ORDER BY total DESC, peer
        LIMIT :map_size OFFSET :map_offset
    """, params)]
    summary = rows[0] if rows else {"matches": 0, "network_total": 0, "peak": 0}
    return {"target": str(network.network_address) if host else str(network),
            "host": host, "rows": rows, "matches": summary["matches"],
            "total": summary["network_total"], "peak": summary["peak"],
            "page": page, "size": NETWORK_PAGE_SIZE,
            "end": params["end"], "ceiling": params["ceiling"]}


def connect(path=DB):
    db = sqlite3.connect(path, uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.create_function("proto_name", 1, protocol, deterministic=True)
    db.create_function("service_name", 2, service, deterministic=True)
    db.create_function("subnet", 2, subnet, deterministic=True)
    db.create_function("ipv4_number", 1, ipv4_number, deterministic=True)
    return db


def query(db, args, export=False, now=None):
    now = int(time()) if now is None else now
    minutes = int(args.get("minutes", 60))
    size = int(args.get("size", 25))
    page = int(args.get("page", 1))
    prefix = int(args.get("prefix", 24))
    group = args.get("group", "flow")
    sort = args.get("sort", "total")
    direction = args.get("direction", "")
    q = args.get("q", "").strip().lower()
    end = int(args.get("end", now))
    ceiling = int(args.get("ceiling", db.execute(
        "SELECT coalesce(max(rowid),0) FROM records"
    ).fetchone()[0]))
    if (minutes not in WINDOWS or size not in {25, 50, 100}
            or not 1 <= page < 2**53 or not 0 <= prefix <= 32
            or group not in {"flow", *GROUPS} or sort not in SORTS
            or direction not in {"", "up", "down"} or len(q) > 160
            or not now - 3600 <= end <= now or not 0 <= ceiling < 2**63):
        raise ValueError("Filtros inválidos ou consulta antiga. Actualiza o painel.")

    router = args.get("router", "")
    if router:
        router = str(IPv4Address(router))
    start = end - minutes * 60
    step = max(60, ((minutes + 59) // 60) * 60)
    params = dict(start=start, end=end, ceiling=ceiling, router=router,
                  direction=direction, q=q, prefix=prefix, step=step,
                  limit=EXPORT_LIMIT + 1 if export else size,
                  offset=0 if export else (page - 1) * size)

    field, sep, value = q.partition(":")
    filters = {**CHARTS, "router": "router", "sasn": "sasn", "dasn": "dasn"}
    if sep and field in filters:
        params["q"] = value.strip()
        match = f"lower(CAST(({filters[field]}) AS TEXT)) = :q"
    elif q:
        match = """instr(lower(printf('%s %s %s %s %s %s AS%s AS%s %s',
                  router, src, dst, sport, dport, proto, sasn, dasn,
                  service_name(proto,dport))), :q) > 0"""
    else:
        match = "1"

    base = f"""
    WITH base AS (
        SELECT rowid AS id, received, router, direction, bytes, packets,
            json_extract(flow,'$.src_addr') AS src,
            json_extract(flow,'$.dst_addr') AS dst,
            CAST(json_extract(flow,'$.src_port') AS INTEGER) AS sport,
            CAST(json_extract(flow,'$.dst_port') AS INTEGER) AS dport,
            proto_name(json_extract(flow,'$.proto')) AS proto,
            nullif(CAST(json_extract(flow,'$.src_as') AS INTEGER),0) AS sasn,
            nullif(CAST(json_extract(flow,'$.dst_as') AS INTEGER),0) AS dasn
        FROM records
        WHERE received>=:start AND received<:end AND rowid<=:ceiling
          AND (:router='' OR router=:router) AND (:direction='' OR direction=:direction)
    ), filtered AS (SELECT * FROM base WHERE {match})
    """
    if not export:
        if "flow_id" in args:
            return flow_observation(db, args, params)
        if "network" in args:
            return destination_map(db, base, params, args["network"], page)

    totals = dict(db.execute(base + f"SELECT {TOTALS} FROM filtered", params).fetchone())
    if export and group == "flow" and totals["flows"] > EXPORT_LIMIT:
        raise ValueError("A exportação excede 50 000 linhas. Reduz o período ou aplica um filtro.")

    if group == "flow":
        table = """SELECT id,received,router,src,dst,sport,dport,proto,sasn,dasn,packets,direction,
                   CASE WHEN direction='up' THEN bytes ELSE 0 END AS up,
                   CASE WHEN direction='down' THEN bytes ELSE 0 END AS down,
                   bytes AS total FROM filtered"""
        tie = "received DESC, id DESC"
    else:
        table = f"""SELECT coalesce({GROUPS[group]},'Sem dados') AS key,
                    {TOTALS}, max(received) AS received, count(*) OVER() AS groups
                    FROM filtered GROUP BY 1"""
        tie = "key"

    rows = [dict(row) for row in db.execute(
        base + table + f" ORDER BY {SORTS[sort]}, {tie} LIMIT :limit OFFSET :offset", params
    )]
    if export and len(rows) > EXPORT_LIMIT:
        raise ValueError("A exportação excede 50 000 linhas. Reduz o período ou aplica um filtro.")
    for row in rows:
        if group == "flow":
            row["service"] = service(row["proto"], row["dport"])
        elif group == "port" and "/" in row["key"]:
            proto, port = row["key"].split("/", 1)
            row["service"] = service(proto, port)

    result = {
        "rows": rows, "totals": totals, "end": end, "ceiling": ceiling, "page": page, "size": size,
        "matches": totals["flows"] if group == "flow" else (rows[0]["groups"] if rows else 0),
        "routers": [r[0] for r in db.execute("SELECT address FROM routers ORDER BY address")],
    }
    if export or args.get("charts") == "0":
        return result

    bins = {at: {"at": at, "up": 0, "down": 0} for at in range(start, end, step)}
    for row in db.execute(base + f"""
        SELECT :start + ((received-:start)/:step)*:step AS at, {TOTALS}
        FROM filtered GROUP BY 1
    """, params):
        bins[row["at"]] = dict(row)

    chart_sql = " UNION ALL ".join(
        f"""SELECT * FROM (
              SELECT '{name}' AS chart, ({expr}) AS key, {TOTALS}
              FROM filtered WHERE ({expr}) IS NOT NULL
              GROUP BY 2 ORDER BY total DESC, key LIMIT 8
            )"""
        for name, expr in CHARTS.items()
    )
    charts = {name: [] for name in CHARTS}
    for row in db.execute(base + chart_sql, params):
        item = dict(row)
        if item["chart"] == "port":
            proto, port = item["key"].split("/", 1)
            item["service"] = service(proto, port)
        charts[item["chart"]].append(item)

    heat = {
        row["slot"]: row["total"]
        for row in db.execute(base + """
            SELECT ((CAST(strftime('%w',received,'unixepoch') AS INTEGER)+6)%7)*24
                 + CAST(strftime('%H',received,'unixepoch') AS INTEGER) AS slot,
                   sum(bytes) AS total
            FROM filtered GROUP BY 1
        """, params)
    }
    return {**result, "series": list(bins.values()), "charts": charts, "heat": heat}


def csv_bytes(rows, group):
    fields = (
        ["received_utc","router","direction","src","dst","sport","dport","proto",
         "service","sasn","dasn","up","down","total","packets"]
        if group == "flow" else
        ["key","service","up","down","total","packets","flows","received_utc"]
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        output = {**row, "received_utc": datetime.fromtimestamp(row["received"], UTC).isoformat()}
        for key in fields:
            value = output.get(key)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                output[key] = "'" + value
        writer.writerow(output)
    return buffer.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    timeout = 25

    def log_message(self, *_):
        pass

    def send(self, status, content_type, payload, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, payload):
        self.send(status, "application/json; charset=utf-8", json.dumps(payload).encode())

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            return self.send(200, "text/html; charset=utf-8", (ROOT / "index.html").read_bytes())
        if url.path in {"/i18n.js", "/visuals.js"}:
            return self.send(200, "application/javascript; charset=utf-8",
                             (ROOT / url.path[1:]).read_bytes())
        if url.path == "/visuals.css":
            return self.send(200, "text/css; charset=utf-8", (ROOT / "visuals.css").read_bytes())
        if url.path not in {"/api/view", "/api/export"}:
            return self.send_json(404, {"error": "Página inexistente."})
        if not QUERIES.acquire(blocking=False):
            return self.send_json(429, {"error": "O explorador está ocupado. Tenta novamente."})
        export = url.path == "/api/export"
        try:
            args = {key: values[0] for key, values in parse_qs(url.query, max_num_fields=16).items()}
            with closing(connect()) as db:
                deadline = monotonic() + 15
                db.set_progress_handler(lambda: monotonic() >= deadline, 10_000)
                db.execute("BEGIN")
                result = query(db, args, export=export)
        except (ValueError, OverflowError) as exc:
            return self.send_json(400, {"error": str(exc)})
        except sqlite3.Error as exc:
            print(f"SQLite: {exc}", file=sys.stderr)
            return self.send_json(503, {"error": "Consulta indisponível. Experimenta um período mais curto."})
        finally:
            QUERIES.release()
        if export:
            return self.send(200, "text/csv; charset=utf-8",
                             csv_bytes(result["rows"], args.get("group", "flow")), "fluxo.csv")
        self.send_json(200, result)

    def do_POST(self):
        if self.path != "/api/ptr":
            return self.send_json(404, {"error": "Página inexistente."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 1 <= length <= 100 or self.headers.get_content_type() != "application/json":
                raise ValueError
            ip = str(IPv4Address(json.loads(self.rfile.read(length))["ip"]))
        except (ValueError, TypeError, KeyError, TimeoutError):
            return self.send_json(400, {"error": "IPv4 inválido."})
        if not LOOKUPS.acquire(blocking=False):
            return self.send_json(429, {"error": "DNS ocupado."})
        try:
            name = lookup(ip, configured_resolvers())
        except ValueError as exc:
            LOG.warning("Invalid PTR_DNS configuration: %s", exc)
            return self.send_json(503, {"error": "Configuração PTR_DNS inválida."})
        except dns.exception.Timeout as exc:
            LOG.warning("PTR lookup timed out: %s", exc)
            return self.send_json(504, {"error": "O servidor DNS não respondeu a tempo."})
        except dns.exception.DNSException as exc:
            LOG.warning("PTR lookup failed: %s", exc)
            return self.send_json(502, {"error": "DNS indisponível."})
        finally:
            LOOKUPS.release()
        self.send_json(200, {"name": name})


def check():
    now = 1_800_000_000
    with closing(connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE records(received,router,direction,client,bytes,packets,flow);
            CREATE TABLE routers(address);
            INSERT INTO routers VALUES ('192.0.2.1');
        """)
        sent = dict(src_addr="10.0.0.25", dst_addr="198.51.100.8",
                    src_port=50000, dst_port=443, proto="TCP", dst_as=64500)
        received = dict(src_addr="198.51.100.8", dst_addr="10.0.0.25",
                        src_port=443, dst_port=50000, proto=6, src_as=64500)
        for direction, size, flow in [("up", 4, sent), ("down", 30, received)]:
            db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                       (now-1, "192.0.2.1", direction, "10.0.0.25", size, 1, json.dumps(flow)))
        view = query(db, {}, now=now)
        assert view["totals"]["up"] == 4 and view["totals"]["down"] == 30
        assert view["rows"][0]["sasn"] == 64500 and view["rows"][1]["service"] == "https"
        assert view["charts"]["asn"][0]["key"] == "AS64500"
        grouped = query(db, {"group":"srcnet", "q":"port:TCP/443"}, now=now)
        assert grouped["rows"][0]["key"] == "10.0.0.0/24"
        assert "10.0.0.25" in csv_bytes(view["rows"], "flow").decode("utf-8-sig")
    with closing(connect()) as live:
        row = live.execute("SELECT received, json_extract(flow,'$.src_addr') FROM records ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("O volume existe, mas a tabela records está vazia.")
    print("OK: testes de fluxos, gráficos, filtros e CSV. SQLite activo legível.")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        print("Fluxo Explorer a escutar em 8000")
        ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
