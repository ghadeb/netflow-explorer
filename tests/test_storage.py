import json
from pathlib import Path
import tempfile
import unittest

from collector.storage import dashboard, ingest, open_db, prune

NOW = 1_800_000_060
ROUTER = "192.0.2.1"
OTHER = "192.0.2.2"
CLIENT = "203.0.113.25"


def flow(direction, count, router=ROUTER, at=NOW, client=CLIENT, **extra):
    remote = "198.51.100.8"
    src, dst = (client, remote) if direction == "up" else (remote, client)
    data = {
        "type": "NETFLOW_V9",
        "time_received_ns": at * 10**9,
        "sampler_address": router,
        "src_addr": src,
        "dst_addr": dst,
        "bytes": count,
        "packets": 1,
        **extra,
    }
    return direction, json.dumps(data)


class StorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = open_db(Path(temporary.name) / "flows.sqlite3")
        self.addCleanup(self.db.close)

    def test_combines_upload_source_and_download_destination(self):
        events = [flow("up", 4), flow("down", 30), flow("up", 19, client="203.0.113.26")]
        self.assertEqual(ingest(self.db, events, {ROUTER}), 3)
        view = dashboard(self.db, NOW, 60, ROUTER)
        self.assertEqual(view["totals"], {"up": 23, "down": 30, "flows": 3})
        self.assertEqual(view["talkers"][0], {"ip": CLIENT, "up": 4, "down": 30})
        self.assertEqual(view["series"][-1]["down"], 30)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 3)

    def test_filter_router_and_reject_unknown_exporter(self):
        events = [flow("up", 4), flow("down", 30, OTHER), flow("up", 5, "192.0.2.3")]
        self.assertEqual(ingest(self.db, events, {ROUTER, OTHER}), 2)
        view = dashboard(self.db, NOW, 15, OTHER)
        self.assertEqual(view["totals"]["down"], 30)
        self.assertEqual(view["totals"]["up"], 0)
        self.assertEqual(view["routers"], [ROUTER, OTHER])

    def test_retention_prunes_records_and_totals(self):
        events = [flow("up", 60, at=NOW - 30 * 86400 - 60), flow("up", 5)]
        ingest(self.db, events, {ROUTER})
        prune(self.db, NOW - 30 * 86400)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT sum(bytes) FROM minute_clients").fetchone()[0], 5)

    def test_invalid_json_and_non_ipv4_are_skipped(self):
        events = [("up", "{"), flow("up", 2, client="2001:db8::1"), flow("up", 4)]
        self.assertEqual(ingest(self.db, events, {ROUTER}), 1)
        self.assertEqual(dashboard(self.db, NOW, 15, None)["totals"]["up"], 4)

    def test_destination_ports_remain_literal_and_protocols_separate(self):
        events = [
            flow("up", 4, proto="TCP", dst_port=443),
            flow("down", 30, proto="TCP", dst_port=55000),
            flow("up", 8, proto="UDP", dst_port=443),
        ]
        ingest(self.db, events, {ROUTER})
        view = dashboard(self.db, NOW, 15, None, detail=True)
        rows = [(row["ip"], row["protocol"], row["port"]) for row in view["talkers"]]
        self.assertEqual(rows, [(CLIENT, "tcp", 55000), (CLIENT, "udp", 443), (CLIENT, "tcp", 443)])

    def test_network_groups_actual_source_in_both_directions(self):
        ingest(self.db, [
            flow("up", 4),
            flow("up", 8, client="203.0.113.31"),
            flow("down", 30),
        ], {ROUTER})
        view = dashboard(self.db, NOW, 15, None, prefix=24)
        self.assertEqual(view["talkers"], [
            {"ip": "198.51.100.0/24", "up": 0, "down": 30},
            {"ip": "203.0.113.0/24", "up": 12, "down": 0},
        ])
