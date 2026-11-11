"""Exercise the pilot in a disposable Linux Compose project."""
from datetime import UTC, datetime
from ipaddress import IPv4Address
from threading import Event
from time import monotonic, sleep, time, time_ns
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import ssl
import sys

sys.path[:0] = ["/painel", "/tmp"]
from smoke_udp import application_table, packet
import socket

from collector.clickhouse import Client, DAY, Writer, encode_batch
from clickhouse_archive import archive_pass
import clickhouse_view
import daily_archive

TLS = ssl.create_default_context(cafile="/tls/cert.pem")


def api(path, args=None, body=None):
    url = "https://127.0.0.1:8000/" + path
    if args:
        url += "?" + urlencode(args)
    headers = {"Content-Type": "application/json"} if body else {}
    req = Request(url, data=json.dumps(body).encode() if body else None, headers=headers)
    with urlopen(req, context=TLS, timeout=20) as response:
        return response.read()


def view(**args):
    return json.loads(api("api/view", args))


def wait_for_flows():
    deadline = monotonic() + 30
    while monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(packet(1, "203.0.113.25", "198.51.100.9", 443, 4000), ("127.0.0.1", 2055))
            udp.sendto(packet(1, "198.51.100.9", "203.0.113.25", 55000, 30000), ("127.0.0.1", 2056))
        sleep(1.5)
        data = view()
        if data["totals"]["up"] and data["totals"]["down"]:
            return data
    raise AssertionError("Flows did not arrive on both UDP ports.")


def synthetic(day, size):
    raw = json.dumps({
        "type": "NETFLOW_V9", "sampler_address": "127.0.0.1",
        "time_received_ns": (day + 3600) * 10**9, "src_addr": "203.0.113.25",
        "dst_addr": "198.51.100.9", "src_port": 45000, "dst_port": 443,
        "proto": "TCP", "bytes": size, "packets": 1, "dst_as": 64500,
    })
    return ("up", raw)


def test_nbar():
    app_id = bytes.fromhex("0d00002a")
    down_app_id = bytes.fromhex("0d00002b")
    args = {"q": "src:203.0.113.55", "charts": 0}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        for port in (2055, 2056):
            udp.sendto(packet(2, "203.0.113.55", "198.51.100.9", 443, 7000,
                              app_id=app_id if port == 2055 else down_app_id,
                              domain=19), ("127.0.0.1", port))
        deadline = monotonic() + 10
        while monotonic() < deadline:
            result = view(**args)
            if len(result["rows"]) == 2:
                break
            sleep(0.3)
        rows = {row["direction"]: row for row in result["rows"]}
        assert rows["up"]["application_id"] == app_id.hex(), rows
        assert rows["up"]["application_name"] is None, rows
        assert rows["down"]["application_id"] == down_app_id.hex(), rows
        assert rows["down"]["application_name"] is None, rows

        # Cisco can send the manufacturer-wide name table in a separate domain.
        for sequence, domain, name in ((3, 20, "different-domain"), (4, 19, "test-app")):
            udp.sendto(application_table(sequence, app_id, name, domain=domain), ("127.0.0.1", 2055))
            deadline = monotonic() + 10
            while monotonic() < deadline:
                rows = {row["direction"]: row for row in view(**args)["rows"]}
                if rows["up"]["application_name"] == name:
                    break
                sleep(0.3)
            assert rows["up"]["application_name"] == name, rows
        udp.sendto(application_table(3, down_app_id, "down-test-app", domain=19), ("127.0.0.1", 2056))
        deadline = monotonic() + 10
        while monotonic() < deadline:
            data = view(**args)
            rows = {row["direction"]: row for row in data["rows"]}
            if rows["down"]["application_name"] == "down-test-app":
                break
            sleep(0.3)
        assert rows["down"]["application_name"] == "down-test-app", rows
        assert rows["up"]["application_name"] == "test-app", rows
        down = rows["down"]
        observation = view(end=data["end"], ceiling=data["ceiling"], flow_id=down["id"],
                           flow_received=down["received"], charts=0)
        assert observation["application_id"] == down_app_id.hex(), observation
        assert observation["application_name"] == "down-test-app", observation
        assert b"test-app" in api("api/export", {"q": args["q"], "direction": "up"})
        assert b"down-test-app" in api("api/export", {"q": args["q"], "direction": "down"})


def test_iana_application_names():
    expected = {
        "01000001": "ICMP",
        "030000a1": "snmp",
        "030000b3": "bgp",
        "03000202": "shell",
    }
    source = "203.0.113.56"
    args = {"q": f"src:{source}", "charts": 0}
    deadline = monotonic() + 15
    sequence = 5
    names = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        while monotonic() < deadline:
            for app_id in expected.keys() - names.keys():
                udp.sendto(packet(sequence, source, "198.51.100.9", 443, 7000,
                                  app_id=bytes.fromhex(app_id), domain=21), ("127.0.0.1", 2055))
                sequence += 1
            sleep(1.5)
            data = view(**args)
            names = {row["application_id"]: row["application_name"] for row in data["rows"]}
            if names.keys() == expected.keys():
                break
    assert names == expected, data
    row = next(row for row in data["rows"] if row["application_id"] == "030000b3")
    observation = view(end=data["end"], ceiling=data["ceiling"], flow_id=row["id"],
                       flow_received=row["received"], charts=0)
    assert observation["application_name"] == "bgp", observation
    assert b"snmp" in api("api/export", {"q": args["q"]})


def test_many_distinct_pairs():
    class LimitedMemoryClient(Client):
        def rows(self, sql, params=None, *, streaming=False, settings=None):
            limits = {
                "max_memory_usage": 144000000,
                "max_bytes_to_read": 120000000,
                "query_plan_optimize_lazy_materialization": 0,
                **(settings or {}),
            }
            return super().rows(sql, params, streaming=streaming, settings=limits)

    now, first_id, count = int(time()), time_ns(), 400_000
    client = LimitedMemoryClient(timeout=30)
    batch_size = 100_000
    for offset in range(0, count, batch_size):
        client.execute("""
            INSERT INTO flows
            SELECT {first_id:UInt64} + number, {now:UInt32} - 60 - 3600 * (number % 2),
                   IPv4StringToNum('192.0.2.1'), 3405774848 + number, 3325256704 + number,
                   number % 2, 45000, 443, 0, 64500, number + 1, 1, 'TCP',
                   concat(if(number % 2, '{"application_id":"01000001","observation_domain_id":19,', '{'),
                          '"pad":"',
                          repeat('x', 1024), '"}')
            FROM numbers({offset:UInt64}, {batch:UInt64})
        """, params={"first_id": first_id, "now": now, "offset": offset, "batch": batch_size})

    scope = {"minutes": "10080", "router": "192.0.2.1"}
    ceiling = first_id + count - 1
    data = clickhouse_view.query(client, scope, ceiling, {"192.0.2.1"}, now=now)
    total = count * (count + 1) // 2
    assert data["totals"]["flows"] == count
    assert data["totals"]["total"] == total
    assert sum(data["heat"].values()) == total and len(data["heat"]) == 2
    assert [row["total"] for row in data["series"] if row.get("total")] == [total]
    pairs = data["charts"]["pair"]
    assert [row["total"] for row in pairs] == list(range(count, count - 8, -1))
    assert pairs[0]["key"] == f"{IPv4Address(3405774848 + count - 1)} → {IPv4Address(3325256704 + count - 1)}"
    assert data["charts"]["port"][0]["service"] == "https"
    assert data["rows"][0]["application_name"] == "ICMP", data["rows"][0]
    assert data["rows"][1]["application_id"] is None, data["rows"][1]
    for group in ("flow", "pair"):
        page = clickhouse_view.query(client, {**scope, "charts": "0", "page": "2", "group": group},
                                     ceiling, {"192.0.2.1"}, now=now)
        assert page["matches"] == count and len(page["rows"]) == 25
        assert [row["total"] for row in page["rows"]] == list(range(count - 25, count - 50, -1))
    network = clickhouse_view.query(client, {**scope, "network": "203.0.0.0/8"},
                                    ceiling, {"192.0.2.1"}, now=now)
    assert network["aggregation_prefix"] == 16 and len(network["rows"]) <= 12, network
    assert network["total"] == total and sum(row["flows"] for row in network["rows"]) == count
    assert "page" not in network
    print("OK: exact live pages, totals and rankings for 400,000 flows with a 144 MB query limit")


def main():
    data = wait_for_flows()
    assert data["charts"]["port"], data
    assert sum(row["up"] + row["down"] for row in data["series"]) == data["totals"]["total"]
    assert sum(data["heat"].values()) == data["totals"]["total"]
    assert any(row.get("service") == "https" for row in data["charts"]["port"]), data
    scope = {key: data[key] for key in ("end", "ceiling")}
    assert view(**scope, q="https", group="srcnet", charts=0)["matches"] == 1
    assert view(**scope, q="port:TCP/443", group="port", charts=0)["rows"][0]["service"] == "https"
    assert view(**scope, router="127.0.0.1", direction="up", charts=0)["totals"]["down"] == 0
    row = next(row for row in data["rows"] if row["direction"] == "up")
    assert isinstance(row["id"], str)
    observation = view(**scope, flow_id=row["id"], flow_received=row["received"], charts=0)
    assert observation["src"] == row["src"]
    assert view(flow_id=row["id"], flow_received=row["received"], end=1)["src"] == row["src"]
    assert view(**scope, q="198.51.100.9", charts=0)["matches"] == 2
    assert view(**scope, network="203.0.113.25", charts=0)["rows"][0]["to_host"] > 0
    assert view(**scope, network="203.0.113.0/24", charts=0)["rows"][0]["peer"] == "198.51.100.9"
    assert b"https" in api("api/export", {**scope, "direction": "up"})
    test_nbar()
    test_iana_application_names()

    now = int(time())
    old = (now // DAY - 35) * DAY
    day = datetime.fromtimestamp(old, UTC).date().isoformat()
    client = Client(timeout=30)
    codec_query = """
        SELECT compression_codec FROM system.columns
        WHERE database='netflow' AND table='flows' AND name='flow'
    """
    assert next(client.rows(codec_query))["compression_codec"] == "CODEC(ZSTD(1))"
    client.execute("ALTER TABLE flows MODIFY COLUMN flow CODEC(LZ4)")
    writer = Writer()
    assert next(client.rows(codec_query))["compression_codec"] == "CODEC(ZSTD(1))"
    payload, count = encode_batch([synthetic(old, 500)] * 1000, {"127.0.0.1"}, writer.next_id)
    assert count == 1000
    settings = {"insert_deduplication_token": str(writer.next_id), "async_insert": 0}
    for _ in range(2):
        client.execute("INSERT INTO flows FORMAT RowBinary", settings=settings, data=payload)
    assert next(client.rows("SELECT count() AS n FROM flows WHERE received < {cutoff:UInt32}",
                            {"cutoff": old + DAY}))["n"] == 1000
    archive_pass(client, now, 30, 0)
    assert next(client.rows("SELECT count() AS n FROM flows WHERE received < {cutoff:UInt32}",
                            {"cutoff": old + DAY}))["n"] == 0
    assert any(entry["date"] == day for entry in daily_archive.archive_catalog())
    imported = json.loads(api("api/archives/open", body={"date": day}))
    history = view(minutes=1440, archive=imported["id"])
    assert history["totals"]["flows"] == 1000 and history["totals"]["total"] == 500000

    # A later batch for an expired day must extend the existing archive.
    assert Writer().ingest([synthetic(old, 123)], {"127.0.0.1"}, Event()) == 1
    archive_pass(client, now, 30, 0)
    imported = json.loads(api("api/archives/open", body={"date": day}))
    history = view(minutes=1440, archive=imported["id"])
    assert history["totals"]["flows"] == 1001 and history["totals"]["total"] == 500123
    test_many_distinct_pairs()
    print("OK: UDP, TLS, ClickHouse views and CSV, idempotent retry, verified expiry and archive reopen")


if __name__ == "__main__":
    main()
