"""Exercise the pilot in a disposable Linux Compose project."""
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep, time
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
    args = {"q": "src:203.0.113.55", "charts": 0}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        for port in (2055, 2056):
            udp.sendto(packet(2, "203.0.113.55", "198.51.100.9", 443, 7000,
                              app_id=app_id, domain=19), ("127.0.0.1", port))
        deadline = monotonic() + 10
        while monotonic() < deadline:
            result = view(**args)
            if len(result["rows"]) == 2:
                break
            sleep(0.3)
        rows = {row["direction"]: row for row in result["rows"]}
        assert rows["up"]["application_id"] == app_id.hex(), rows
        assert rows["up"]["application_name"] is None, rows
        assert rows["down"]["application_id"] is None, rows

        udp.sendto(application_table(3, app_id, "different-domain", domain=20), ("127.0.0.1", 2055))
        udp.sendto(application_table(4, app_id, "test-app", domain=19), ("127.0.0.1", 2055))
        deadline = monotonic() + 10
        while monotonic() < deadline:
            rows = {row["direction"]: row for row in view(**args)["rows"]}
            if rows["up"]["application_name"] == "test-app":
                break
            sleep(0.3)
        assert rows["up"]["application_name"] == "test-app", rows
        assert rows["down"]["application_id"] is None, rows
        assert b"test-app" in api("api/export", {"q": args["q"]})


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
    assert view(**scope, network="203.0.113.25", charts=0)["rows"][0]["to_host"] > 0
    assert view(**scope, network="203.0.113.0/24", charts=0)["rows"][0]["peer"] == "198.51.100.9"
    assert b"https" in api("api/export", {**scope, "direction": "up"})
    test_nbar()

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
    print("OK: UDP, TLS, ClickHouse views and CSV, idempotent retry, verified expiry and archive reopen")


if __name__ == "__main__":
    main()
