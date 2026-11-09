from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock
import json
import sys

import dns.exception
import dns.resolver

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "explorer"))
import ptr
import server

NOW = 1_800_000_000
ROUTER = "192.0.2.1"


class ExplorerTest(TestCase):
    def setUp(self):
        self.db = server.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE records(received,router,direction,client,bytes,packets,flow);
            CREATE TABLE routers(address);
            INSERT INTO routers VALUES ('192.0.2.1');
        """)

    def add_flow(self, src="10.20.30.15", dst="198.51.100.1", size=400,
                 direction="up", **fields):
        data = dict(src_addr=src, dst_addr=dst, src_port=45000, dst_port=443,
                    proto="TCP", **fields)
        with self.db:
            return self.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                                   (NOW-5, ROUTER, direction, src, size, 2,
                                    json.dumps(data))).lastrowid

    def test_single_flow_has_only_reported_hops(self):
        row = self.add_flow(next_hop="192.0.2.8", bgp_next_hop="0.0.0.0", in_if=6)
        flow = server.query(self.db, {"flow_id": str(row), "flow_received": str(NOW-5)}, now=NOW)
        self.assertEqual(flow["src"], "10.20.30.15")
        self.assertEqual(flow["dst"], "198.51.100.1")
        self.assertEqual(flow["next_hop"], "192.0.2.8")
        self.assertIsNone(flow["bgp_next_hop"])
        self.assertEqual(flow["in_if"], 6)
        self.assertIsNone(flow["out_if"])

    def test_stale_flow_reference_does_not_pick_another_record(self):
        row = self.add_flow()
        with self.assertRaisesRegex(ValueError, "fluxo"):
            server.query(self.db, {"flow_id": str(row), "flow_received": str(NOW-10)}, now=NOW)

    def test_network_map_aggregates_sources_and_pages_destinations(self):
        for offset in range(1, 15):
            self.add_flow(dst=f"198.51.100.{offset}", size=offset)
        self.add_flow(src="10.20.30.200", dst="198.51.100.14", size=10)
        self.add_flow(src="10.20.31.1", dst="198.51.100.200", size=1000)
        args = {"network": "10.20.30.20/24"}
        first = server.query(self.db, args, now=NOW)
        second = server.query(self.db, {**args, "page": "2"}, now=NOW)
        self.assertEqual(first["target"], "10.20.30.0/24")
        self.assertEqual(first["matches"], 14)
        self.assertEqual(first["rows"][0]["peer"], "198.51.100.14")
        self.assertEqual(first["rows"][0]["total"], 24)
        self.assertEqual(first["total"], sum(range(1, 15)) + 10)
        self.assertEqual(len(first["rows"]) + len(second["rows"]), 14)
        self.assertFalse({row["peer"] for row in first["rows"]} & {row["peer"] for row in second["rows"]})

    def test_network_map_respects_query_filters_and_window(self):
        self.add_flow(dst="198.51.100.1", direction="down")
        self.add_flow(dst="198.51.100.2")
        data = server.query(self.db, {"network":"10.20.30.0/24", "direction":"up",
                                     "q":"dst:198.51.100.2"}, now=NOW)
        self.assertEqual([row["peer"] for row in data["rows"]], ["198.51.100.2"])

    def test_host_map_groups_both_directions_by_peer(self):
        self.add_flow(src="10.20.30.1", dst="198.51.100.3", size=20)
        self.add_flow(src="198.51.100.3", dst="10.20.30.1", size=50, direction="down")
        data = server.query(self.db, {"network": "10.20.30.1"}, now=NOW)
        self.assertTrue(data["host"])
        self.assertEqual(data["matches"], 1)
        peer = data["rows"][0]
        self.assertEqual((peer["peer"], peer["from_host"], peer["to_host"]),
                         ("198.51.100.3", 20, 50))


class PtrTest(TestCase):
    def test_tries_second_resolver_after_negative_first(self):
        first, second = Mock(), Mock()
        first.resolve_address.side_effect = dns.resolver.NXDOMAIN
        second.resolve_address.return_value = [Mock(target="router.example.test.")]
        self.assertEqual(ptr.lookup("192.0.2.2", (first, second)), "router.example.test")

    def test_distinguishes_absence_from_unreachable_resolver(self):
        missing, timed_out = Mock(), Mock()
        missing.resolve_address.side_effect = dns.resolver.NoAnswer
        timed_out.resolve_address.side_effect = dns.exception.Timeout
        self.assertIsNone(ptr.lookup("192.0.2.2", (missing,)))
        with self.assertRaises(dns.exception.Timeout):
            ptr.lookup("192.0.2.2", (missing, timed_out))
