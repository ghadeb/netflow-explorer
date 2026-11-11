"""Serve the existing dashboard API over the live ClickHouse table."""
from ipaddress import IPv4Address, IPv4Network
from threading import BoundedSemaphore
from time import time
from urllib.parse import parse_qs, urlsplit
import logging

from collector.clickhouse import APPLICATION_ID, ClickHouseError, Client
import server as panel

LOG = logging.getLogger(__name__)
QUERIES = BoundedSemaphore(1)
TOTALS = """
    sumIf(bytes, sent=1) AS up, sumIf(bytes, sent=0) AS down,
    sum(bytes) AS total, sum(packets) AS packets, count() AS flows
"""
PORT = "if(dport>0, concat(proto,'/',toString(dport)), '')"
ASN = "if(dasn>0, concat('AS',toString(dasn)), '')"
PAIR = "concat(src_ip,' → ',dst_ip)"
CHARTS = {"src": "src_ip", "dst": "dst_ip", "proto": "proto",
          "port": PORT, "pair": PAIR, "asn": ASN}
GROUPS = {
    **CHARTS,
    "srcnet": "concat(IPv4NumToString(bitAnd(src_num,{mask:UInt32})), '/', toString({prefix:UInt8}))",
    "dstnet": "concat(IPv4NumToString(bitAnd(dst_num,{mask:UInt32})), '/', toString({prefix:UInt8}))",
}
FLOW_COLUMNS = """
    SELECT id, received, router AS router_num, src AS src_num, dst AS dst_num,
           IPv4NumToString(router) AS router_ip, IPv4NumToString(src) AS src_ip,
           IPv4NumToString(dst) AS dst_ip, sport, dport, proto, sasn, dasn,
           bytes, packets, sent
"""
FLOW_WINDOW = """
    FROM flows
    WHERE received >= {start:UInt32} AND received < {end:UInt32} AND id <= {ceiling:UInt64}
      AND ({router:String}='' OR router={router_num:UInt32})
      AND ({direction:String}='' OR sent=({direction:String}='up'))
"""
RANKING_KEYS = {
    "src": ("src_num", "IPv4NumToString(src_num)"),
    "dst": ("dst_num", "IPv4NumToString(dst_num)"),
    "pair": ("src_num, dst_num", "concat(IPv4NumToString(src_num),' → ',IPv4NumToString(dst_num))"),
    "port": ("proto, dport", PORT),
    "asn": ("dasn", ASN),
}
RANKING_SETTINGS = {
    "max_bytes_before_external_group_by": 64000000,
    "max_bytes_before_external_sort": 64000000,
}
# RFC 6759: engine 13 (PANA-L7) identifies a manufacturer-wide application.
PANA_L7_PREFIX = "0d"
EXPORT_ERROR = "A exportação excede 50 000 linhas. Reduz o período ou aplica um filtro."


def resolve_applications(client, rows):
    pending = []
    for row in rows:
        domain = row.pop("application_domain", 0)
        app_id = row.get("application_id")
        if (row["direction"] == "up" and isinstance(app_id, str)
                and APPLICATION_ID.fullmatch(app_id) and not app_id.startswith("00")
                and isinstance(domain, int) and 0 <= domain < 2**32):
            pending.append((row, (int(IPv4Address(row["router"])), domain, app_id)))
        else:
            row["application_id"] = None
    if not pending:
        return
    routers = sorted({key[0] for _, key in pending})
    names = {
        (entry["router"], entry["domain"], entry["application_id"]): entry["application_name"]
        for entry in client.rows("""
            SELECT router, domain, application_id, argMax(name, seen_at) AS application_name
            FROM application_names WHERE router IN {routers:Array(UInt32)}
            GROUP BY router, domain, application_id
        """, {"routers": str(routers)})
    }
    exporter_names = {}
    for (router, _domain, app_id), name in names.items():
        if len(app_id) == 8 and app_id.startswith(PANA_L7_PREFIX):
            key = (router, app_id)
            if exporter_names.setdefault(key, name) != name:
                exporter_names[key] = None
    for row, (router, domain, app_id) in pending:
        row["application_name"] = names.get((router, domain, app_id), exporter_names.get((router, app_id)))


def search_predicate(params):
    field, sep, value = params["q"].partition(":")
    aliases = {**CHARTS, "router": "router_ip", "sasn": "sasn", "dasn": "dasn"}
    if sep and field in aliases:
        params["q"] = value.strip()
        predicate = f"lowerUTF8(toString({aliases[field]})) = {{q:String}}"
    elif params["q"]:
        # Service labels are local reference data. Compare their port keys in SQL.
        ports = [key for key, label in panel.SERVICES.items() if params["q"] in label.lower()]
        services = ",".join("'" + key.replace("'", "\\'") + "'" for key in ports)
        predicate = """positionCaseInsensitiveUTF8(
            concat(router_ip,' ',src_ip,' ',dst_ip,' ',toString(sport),' ',toString(dport),
                   ' ',proto,' AS',toString(sasn),' AS',toString(dasn)), {q:String}) > 0"""
        if services:
            predicate += " OR lower(" + PORT + ") IN (" + services + ")"
    else:
        predicate = "1"
    return predicate


def filtered(predicate, *, include_flow=False):
    columns = FLOW_COLUMNS + (", flow" if include_flow else "")
    return "WITH filtered AS (SELECT * FROM (" + columns + FLOW_WINDOW + ") WHERE " + predicate + ") "


def destination_map(client, base, params, target, page):
    host = "/" not in target
    try:
        network = IPv4Network(target, strict=False)
    except ValueError as exc:
        raise ValueError("Indica um endereço IPv4 ou uma rede IPv4 em formato CIDR.") from exc
    params = dict(params, first=int(network.network_address), last=int(network.broadcast_address),
                  map_offset=(page - 1) * panel.NETWORK_PAGE_SIZE)
    peer = "if(src_num={first:UInt32},dst_ip,src_ip)" if host else "dst_ip"
    match = "(src_num={first:UInt32} OR dst_num={first:UInt32})" if host else "src_num BETWEEN {first:UInt32} AND {last:UInt32}"
    sql = base + f"""
        SELECT *, count() OVER () AS matches, sum(total) OVER () AS network_total,
               max(total) OVER () AS peak
        FROM (
            SELECT {peer} AS peer, {TOTALS},
                   sumIf(bytes, src_num={{first:UInt32}}) AS from_host,
                   sumIf(bytes, src_num!={{first:UInt32}}) AS to_host
            FROM filtered WHERE {match} GROUP BY peer
        )
        ORDER BY total DESC, peer LIMIT {panel.NETWORK_PAGE_SIZE} OFFSET {{map_offset:UInt64}}
    """
    rows = list(client.rows(sql, params))
    summary = rows[0] if rows else {"matches": 0, "network_total": 0, "peak": 0}
    return {"target": str(network.network_address) if host else str(network),
            "host": host, "rows": rows, "matches": summary["matches"],
            "total": summary["network_total"], "peak": summary["peak"], "page": page,
            "size": panel.NETWORK_PAGE_SIZE, "end": params["end"], "ceiling": str(params["ceiling"])}


def observation(client, args, params):
    flow_id = int(args["flow_id"])
    received = int(args.get("flow_received", 0))
    error = "Este fluxo já não está disponível. Actualiza o painel."
    if not (1 <= flow_id <= params["ceiling"] and params["start"] <= received < params["end"]):
        raise ValueError(error)
    rows = list(client.rows("""
        SELECT received, IPv4NumToString(router) AS router_ip,
               if(sent=1,'up','down') AS direction, bytes, packets, flow
        FROM flows WHERE received={received:UInt32} AND id={id:UInt64} LIMIT 1
    """, {"id": flow_id, "received": received}))
    if not rows:
        raise ValueError(error)
    row = rows[0]
    row["router"] = row.pop("router_ip")
    result = panel.observation_from_row(row)
    resolve_applications(client, [result])
    return result


def service_label(row):
    if "/" in row["key"]:
        proto, port = row["key"].split("/", 1)
        row["service"] = panel.service(proto, port)


def chart_data(client, base, params):
    # Each time bucket can cover several hours. Sum the same grouped rows for
    # totals, the timeline and the UTC heatmap.
    sql = base + f"""
        SELECT {{start:UInt32}}+intDiv(received-{{start:UInt32}},{{step:UInt32}})*{{step:UInt32}} AS slot,
               (toDayOfWeek(toDateTime(received,'UTC'))-1)*24+toHour(toDateTime(received,'UTC')) AS hour_slot,
               {TOTALS}
        FROM filtered GROUP BY slot, hour_slot
    """
    totals = dict.fromkeys(("up", "down", "total", "packets", "flows"), 0)
    bins = {at: {"at": at, "up": 0, "down": 0} for at in range(params["start"], params["end"], params["step"])}
    heat = {}
    for row in client.rows(sql, params):
        bucket = bins[row["slot"]]
        hour = row["hour_slot"]
        heat[hour] = heat.get(hour, 0) + row["total"]
        for metric in totals:
            totals[metric] += row[metric]
            bucket[metric] = bucket.get(metric, 0) + row[metric]

    # Run the rankings serially so high-cardinality address and pair groups do
    # not retain their aggregate tables at the same time.
    charts = {}
    for name, expression in CHARTS.items():
        group, key = RANKING_KEYS.get(name, ("key", expression))
        ranking = base + f"""
            SELECT {key} AS key, {TOTALS}
            FROM filtered GROUP BY {group} HAVING key != ''
            ORDER BY total DESC, key LIMIT 8
        """
        charts[name] = list(client.rows(ranking, params, settings=RANKING_SETTINGS))
    for row in charts["port"]:
        service_label(row)
    return {"totals": totals, "series": list(bins.values()), "charts": charts, "heat": heat}


def query(client, args, committed_id, routers, export=False, now=None):
    now = int(time()) if now is None else now
    ceiling = min(int(args.get("ceiling", committed_id)), committed_id)
    params, group, sort, page, size = panel.query_scope(args, now, ceiling, export)
    params["router_num"] = int(IPv4Address(params["router"])) if params["router"] else 0
    params["mask"] = (0xffffffff << (32 - params["prefix"])) & 0xffffffff
    if not export and "flow_id" in args:
        return observation(client, args, params)
    predicate = search_predicate(params)
    base = filtered(predicate)
    if not export and "network" in args:
        return destination_map(client, base, params, args["network"], page)

    charts = chart_data(client, base, params) if not export and args.get("charts") != "0" else {}
    totals = charts["totals"] if charts else next(client.rows(base + f"SELECT {TOTALS} FROM filtered", params))
    if group == "flow":
        if export and totals["flows"] > panel.EXPORT_LIMIT:
            raise ValueError(EXPORT_ERROR)
        table = """SELECT toString(id) AS flow_id, received, router_ip AS router, src_ip AS src,
                   dst_ip AS dst, sport, dport, proto, nullIf(sasn,0) AS sasn,
                   nullIf(dasn,0) AS dasn, packets, if(sent=1,'up','down') AS direction,
                   if(sent=1,bytes,0) AS up, if(sent=0,bytes,0) AS down, bytes AS total, flow
                   FROM filtered"""
        tie = "received DESC, id DESC"
    else:
        table = f"""SELECT *, count() OVER () AS groups FROM (
                    SELECT if(empty(group_key),'Sem dados',group_key) AS key, {TOTALS},
                           max(received) AS received
                    FROM (SELECT *, {GROUPS[group]} AS group_key FROM filtered)
                    GROUP BY key)"""
        tie = "key"

    table += f" ORDER BY {panel.SORTS[sort]}, {tie} LIMIT {{limit:UInt64}} OFFSET {{offset:UInt64}}"
    if group == "flow":
        table = f"""
            SELECT * EXCEPT (flow),
                   if(direction='up', JSONExtractString(flow, 'application_id'), '') AS application_id,
                   JSONExtractUInt(flow, 'observation_domain_id') AS application_domain
            FROM ({table})
            ORDER BY {panel.SORTS[sort]}, received DESC, toUInt64(flow_id) DESC
        """
    sql = (filtered(predicate, include_flow=True) if group == "flow" else base) + table
    rows = list(client.rows(sql, params))
    if export and len(rows) > panel.EXPORT_LIMIT:
        raise ValueError(EXPORT_ERROR)
    if group == "flow":
        resolve_applications(client, rows)
    for row in rows:
        if group == "flow":
            row["id"] = row.pop("flow_id")
            row["service"] = panel.service(row["proto"], row["dport"])
        elif group == "port":
            service_label(row)
    return {
        "rows": rows, "totals": totals, "end": params["end"], "ceiling": str(ceiling),
        "page": page, "size": size, **charts,
        "matches": totals["flows"] if group == "flow" else (rows[0]["groups"] if rows else 0),
        "routers": sorted(routers),
    }


def handler(base, storage):
    class ClickHouseHandler(base):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/healthz":
                try:
                    list(Client(timeout=2).rows("SELECT 1 FROM flows LIMIT 1"))
                except ClickHouseError:
                    return self.send_json(503, {"ok": False})
                return self.send_json(200, {"ok": True})
            if url.path not in {"/api/view", "/api/export"}:
                return super().do_GET()
            if not QUERIES.acquire(blocking=False):
                return self.send_json(429, {"error": "O explorador está ocupado. Tenta novamente."})
            export = url.path == "/api/export"
            try:
                args = {key: values[0] for key, values in parse_qs(url.query, max_num_fields=16).items()}
                result = query(Client(), args, storage.committed_id, self.server.exporters, export=export)
            except (ValueError, OverflowError) as exc:
                return self.send_json(400, {"error": str(exc)})
            except ClickHouseError as exc:
                LOG.warning("ClickHouse query failed: %s", exc)
                return self.send_json(503, {"error": "Consulta indisponível. Experimenta um período mais curto."})
            finally:
                QUERIES.release()
            if export:
                return self.send(200, "text/csv; charset=utf-8",
                                 panel.csv_bytes(result["rows"], args.get("group", "flow")), "fluxo.csv")
            return self.send_json(200, result)

    return ClickHouseHandler
