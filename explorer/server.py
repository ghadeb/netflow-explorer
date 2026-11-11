from contextlib import closing
from datetime import UTC, datetime
from heapq import nsmallest
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
import re
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
APP_ID_PATTERN = re.compile(r"(?:[0-9a-f]{2}){2,16}")
# RFC 6759 assigns these selectors at device or manufacturer scope.
NAMED_APP_ENGINES = ("06", "0d", "14")  # User-defined, PANA-L7, PANA-L7-PEN.
EXPORTER_APP_ENGINES = {"02", "04", "0c", *NAMED_APP_ENGINES}
WINDOWS = {15, 60, 360, 1440, 10080, 43200}
EXPORT_LIMIT = 50_000
QUERIES = BoundedSemaphore(2)
LOOKUPS = BoundedSemaphore(4)
MAP_NODE_LIMIT = 12
MAP_PREFIXES = (32, 24, 16, 8, 3)
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


def iana_application_name(app_id):
    if len(app_id) == 4 and app_id.startswith("01") or (
            len(app_id) == 8 and app_id.startswith("010000")):
        return PROTOCOLS.get(str(int(app_id[-2:], 16)))
    if len(app_id) == 6 and app_id.startswith("03") or (
            len(app_id) == 8 and app_id.startswith("0300")):
        # RFC 6759 §4.4 uses the TCP registration for IANA-L4 port names.
        return service("tcp", int(app_id[-4:], 16)) or None
    return None


def iana_application_ids(name):
    ids = {f"01{int(number):0{width}x}" for number, label in PROTOCOLS.items()
           if label.casefold() == name for width in (2, 6)}
    for key, label in SERVICES.items():
        if key.startswith("tcp/") and label.casefold() == name:
            port = int(key.split("/", 1)[1])
            ids.update((f"03{port:04x}", f"03{port:06x}"))
    return ids


def valid_application_id(app_id, domain):
    return (isinstance(app_id, str) and APP_ID_PATTERN.fullmatch(app_id)
            and not app_id.startswith("00") and isinstance(domain, int) and 0 <= domain < 2**32)


class ApplicationNames:
    def __init__(self, names=None):
        self.names = names if names is not None else {}
        self.exporter_names = {}
        for (router, _domain, app_id), name in self.names.items():
            if app_id[:2] in EXPORTER_APP_ENGINES:
                key = (router, app_id)
                if self.exporter_names.setdefault(key, name) != name:
                    self.exporter_names[key] = None

    def resolve(self, router, domain, app_id):
        if not valid_application_id(app_id, domain):
            return None
        return (self.names.get((router, domain, app_id))
                or self.exporter_names.get((router, app_id))
                or iana_application_name(app_id))

    def label_rows(self, rows):
        for row in rows:
            domain = row.pop("application_domain", 0)
            app_id = row.get("application_id")
            if not valid_application_id(app_id, domain):
                row["application_id"] = None
                continue
            row["application_name"] = self.resolve(ipv4_number(row["router"]), domain, app_id)
            if app_id[:2] in NAMED_APP_ENGINES:
                name = row["application_name"]
                row["application_status"] = (
                    "unmapped" if not name else "unknown" if name.casefold() == "unknown" else "named"
                )

    def search_keys(self, name, named_only=False):
        exact = {key for key, label in self.names.items() if label.casefold() == name
                 and (not named_only or key[2][:2] in NAMED_APP_ENGINES)}
        exporters = {key for key, label in self.exporter_names.items()
                     if label and label.casefold() == name
                     and (not named_only or key[1][:2] in NAMED_APP_ENGINES)}
        iana = set() if named_only else iana_application_ids(name)
        overrides = {key for key in self.names if key not in exact
                     and ((key[0], key[2]) in exporters or key[2] in iana)}
        return exact, exporters, iana, overrides


def application_charts(rows, names):
    apps = {}
    for row in rows:
        router, domain, app_id = row["router_num"], row["application_domain"], row["application_id"]
        name = names.resolve(router, domain, app_id)
        if not name or name.casefold() == "unknown":
            continue
        summary = apps.setdefault(name.casefold(), {"key": name, "flows": 0, "total": 0})
        summary["key"] = min(summary["key"], name)
        summary["flows"] += row["flows"]
        summary["total"] += row["total"]
    return {
        "app_count": nsmallest(20, apps.values(), key=lambda r: (-r["flows"], -r["total"], r["key"])),
        "app_volume": nsmallest(20, apps.values(), key=lambda r: (-r["total"], -r["flows"], r["key"])),
        "app_total": sum(app["total"] for app in apps.values()),
    }


def port_charts(rows):
    services = {}

    def ports():
        for item in rows:
            row = dict(item)
            proto, port = row.pop("proto"), row.pop("dport")
            row["key"] = f"{proto}/{port}"
            row["service"] = name = service(proto, port)
            if name:
                summary = services.setdefault(name, {"key": name, "flows": 0, "total": 0})
                summary["flows"] += row["flows"]
                summary["total"] += row["total"]
            yield row

    rank = lambda row: (-row["flows"], -row["total"], row["key"])
    return {"port": nsmallest(20, ports(), key=rank),
            "service": nsmallest(8, services.values(), key=rank)}


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


def flow_observation(db, args, ceiling, load_applications):
    flow_id = int(args["flow_id"])
    received = int(args.get("flow_received", 0))
    if not (1 <= flow_id <= min(ceiling, 2**63 - 1) and 0 < received < 2**32):
        raise ValueError("Este fluxo já não está disponível. Actualiza o painel.")
    row = db.execute("""
        SELECT received, router, direction, bytes, packets, flow
        FROM records WHERE rowid=? AND received=?
    """, (flow_id, received)).fetchone()
    if row is None:
        raise ValueError("Este fluxo já não está disponível. Actualiza o painel.")
    observation = observation_from_row(row)
    if observation.get("application_id"):
        load_applications().label_rows([observation])
    return observation


def observation_from_row(row):
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
    if raw.get("application_id"):
        observation["application_id"] = raw["application_id"]
        observation["application_domain"] = raw.get("observation_domain_id", 0)
    return observation


def map_network(target):
    try:
        return IPv4Network(target, strict=False)
    except ValueError as exc:
        raise ValueError("Indica um endereço IPv4 ou uma rede IPv4 em formato CIDR.") from exc


def map_mask(prefix):
    return (0xffffffff << (32 - prefix)) & 0xffffffff


def map_result(network, host, prefix, rows, params):
    for row in rows:
        address = str(IPv4Address(row.pop("peer_num")))
        row["peer"] = address if prefix == 32 else f"{address}/{prefix}"
    rows.sort(key=lambda row: (-row["total"], row["peer"]))
    return {"target": str(network.network_address) if host else str(network),
            "host": host, "rows": rows, "matches": len(rows),
            "aggregation_prefix": None if prefix == 32 else prefix,
            "total": sum(row["total"] for row in rows),
            "peak": rows[0]["total"] if rows else 0,
            "end": params["end"], "ceiling": str(params["ceiling"])}


def destination_map(db, base, params, target):
    host = "/" not in target
    network = map_network(target)
    params = dict(params, address=str(network.network_address),
                  first=int(network.network_address), last=int(network.broadcast_address),
                  map_limit=MAP_NODE_LIMIT + 1)
    peer = "CASE WHEN src=:address THEN dst ELSE src END" if host else "dst"
    match = "(src=:address OR dst=:address)" if host else "ipv4_number(src) BETWEEN :first AND :last"
    eligible = f"FROM filtered WHERE src IS NOT NULL AND dst IS NOT NULL AND ipv4_number({peer}) IS NOT NULL AND {match}"
    key = f"ipv4_number({peer}) & :map_mask"
    for prefix in MAP_PREFIXES:
        params["map_mask"] = map_mask(prefix)
        groups = db.execute(f"{base}SELECT DISTINCT {key} {eligible} LIMIT :map_limit", params).fetchall()
        if len(groups) <= MAP_NODE_LIMIT:
            break
    rows = [dict(row) for row in db.execute(base + f"""
        SELECT {key} AS peer_num, {TOTALS},
               sum(CASE WHEN src=:address THEN bytes ELSE 0 END) AS from_host,
               sum(CASE WHEN src!=:address THEN bytes ELSE 0 END) AS to_host
        {eligible} GROUP BY peer_num
    """, params)]
    return map_result(network, host, prefix, rows, params)


def connect(path=DB):
    db = sqlite3.connect(path, uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.create_function("proto_name", 1, protocol, deterministic=True)
    db.create_function("service_name", 2, service, deterministic=True)
    db.create_function("subnet", 2, subnet, deterministic=True)
    db.create_function("ipv4_number", 1, ipv4_number, deterministic=True)
    return db


def query_scope(args, now, ceiling, export):
    minutes = int(args.get("minutes", 60))
    size = int(args.get("size", 25))
    page = int(args.get("page", 1))
    prefix = int(args.get("prefix", 24))
    group = args.get("group", "flow")
    sort = args.get("sort", "total")
    direction = args.get("direction", "")
    q = args.get("q", "").strip().lower()
    end = int(args.get("end", now))
    if (minutes not in WINDOWS or size not in {25, 50, 100}
            or not 1 <= page < 2**53 or not 0 <= prefix <= 32
            or group not in {"flow", *GROUPS} or sort not in SORTS
            or direction not in {"", "up", "down"}
            or len(q) > (255 + q.index(":") + 1 if q.startswith(("app:", "nbar:")) else 160)
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

    return params, group, sort, page, size


def query(db, args, export=False, now=None, load_applications=ApplicationNames):
    now = int(time()) if now is None else now
    ceiling = int(args["ceiling"]) if "ceiling" in args else db.execute(
        "SELECT coalesce(max(rowid),0) FROM records"
    ).fetchone()[0]
    if not export and "flow_id" in args:
        return flow_observation(db, args, ceiling, load_applications)
    params, group, sort, page, size = query_scope(args, now, ceiling, export)
    start, end, step, q = (params[key] for key in ("start", "end", "step", "q"))

    field, sep, value = q.partition(":")
    application_names = None
    filters = {**CHARTS, "router": "router", "sasn": "sasn", "dasn": "dasn"}
    if sep and field in filters:
        params["q"] = value.strip()
        match = f"lower(CAST(({filters[field]}) AS TEXT)) = :q"
    elif sep and field == "service":
        params["q"] = value.strip()
        match = "(:q<>'' AND lower(service_name(proto,dport))=:q)"
    elif sep and field in {"app", "nbar"}:
        application_names = load_applications()
        requested_name = value.strip().casefold()

        def application_matches(router, domain, app_id):
            if field == "nbar" and (not isinstance(app_id, str) or app_id[:2] not in NAMED_APP_ENGINES):
                return False
            name = application_names.resolve(ipv4_number(router), domain, app_id)
            return bool(name and name.casefold() == requested_name)

        db.create_function("application_matches", 3, application_matches, deterministic=True)
        match = "application_matches(router, application_domain, application_id)"
    elif ipv4_number(q) is not None:
        match = "(:q=src OR :q=dst OR :q=router)"
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
            nullif(CAST(json_extract(flow,'$.dst_as') AS INTEGER),0) AS dasn,
            json_extract(flow,'$.application_id') AS application_id,
            coalesce(json_extract(flow,'$.observation_domain_id'),0) AS application_domain
        FROM records
        WHERE received>=:start AND received<:end AND rowid<=:ceiling
          AND (:router='' OR router=:router) AND (:direction='' OR direction=:direction)
    ), filtered AS (SELECT * FROM base WHERE {match})
    """
    if not export and "network" in args:
        return destination_map(db, base, params, args["network"])

    totals = dict(db.execute(base + f"SELECT {TOTALS} FROM filtered", params).fetchone())
    if export and group == "flow" and totals["flows"] > EXPORT_LIMIT:
        raise ValueError("A exportação excede 50 000 linhas. Reduz o período ou aplica um filtro.")

    if group == "flow":
        table = """SELECT id,received,router,src,dst,sport,dport,proto,sasn,dasn,packets,direction,
                   application_id, application_domain,
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
    if group == "flow" and any(row.get("application_id") for row in rows):
        if application_names is None:
            application_names = load_applications()
        application_names.label_rows(rows)
    for row in rows:
        if group == "flow":
            row.pop("application_domain", None)
            row["id"] = str(row["id"])
            row["service"] = service(row["proto"], row["dport"])
        elif group == "port" and "/" in row["key"]:
            proto, port = row["key"].split("/", 1)
            row["service"] = service(proto, port)

    result = {
        "rows": rows, "totals": totals, "end": end, "ceiling": str(ceiling), "page": page, "size": size,
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
        for name, expr in CHARTS.items() if name != "port"
    )
    charts = {name: [] for name in CHARTS if name != "port"}
    for row in db.execute(base + chart_sql, params):
        charts[row["chart"]].append(dict(row))
    charts.update(port_charts(db.execute(base + f"""
        SELECT proto, dport, {TOTALS},
               count(CASE WHEN direction='up' THEN 1 END) AS up_flows,
               count(CASE WHEN direction='down' THEN 1 END) AS down_flows
        FROM filtered WHERE dport>0 GROUP BY proto, dport
    """, params)))
    if application_names is None:
        application_names = load_applications()
    app_engines = ",".join(map(repr, NAMED_APP_ENGINES))
    app_rows = db.execute(base + f"""
        SELECT ipv4_number(router) AS router_num, application_domain, application_id,
               count(*) AS flows, sum(bytes) AS total
        FROM filtered WHERE substr(application_id,1,2) IN ({app_engines})
        GROUP BY router, application_domain, application_id
    """, params) if application_names.names else ()
    charts.update(application_charts(app_rows, application_names))

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
         "service","application_id","application_name","sasn","dasn","up","down","total","packets"]
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
        if url.path == "/logo.png":
            return self.send(200, "image/png", (ROOT / "logo.png").read_bytes())
        if url.path == "/license":
            license_path = ROOT / "LICENSE"
            if not license_path.is_file():
                license_path = ROOT.parent / "LICENSE"
            return self.send(200, "text/plain; charset=utf-8", license_path.read_bytes())
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
        sent = dict(src_addr="203.0.113.25", dst_addr="198.51.100.8",
                    src_port=50000, dst_port=443, proto="TCP", dst_as=64500)
        received = dict(src_addr="198.51.100.8", dst_addr="203.0.113.25",
                        src_port=443, dst_port=50000, proto=6, src_as=64500)
        for direction, size, flow in [("up", 4, sent), ("down", 30, received)]:
            db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                       (now-1, "192.0.2.1", direction, "203.0.113.25", size, 1, json.dumps(flow)))
        view = query(db, {}, now=now)
        assert view["totals"]["up"] == 4 and view["totals"]["down"] == 30
        assert view["rows"][0]["sasn"] == 64500 and view["rows"][1]["service"] == "https"
        assert view["charts"]["asn"][0]["key"] == "AS64500"
        grouped = query(db, {"group":"srcnet", "q":"port:TCP/443"}, now=now)
        assert grouped["rows"][0]["key"] == "203.0.113.0/24"
        assert "203.0.113.25" in csv_bytes(view["rows"], "flow").decode("utf-8-sig")
    with closing(connect()) as live:
        row = live.execute("SELECT received, json_extract(flow,'$.src_addr') FROM records ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("O volume existe, mas a tabela records está vazia.")
    print("OK: testes de fluxos, gráficos, filtros e CSV. SQLite activo legível.")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        print("NetFlow Explorer listening on port 8000")
        ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
