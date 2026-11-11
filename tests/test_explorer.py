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

    def add_flow(self, src="203.0.113.15", dst="198.51.100.1", size=400,
                 direction="up", dport=443, **fields):
        data = dict(src_addr=src, dst_addr=dst, src_port=45000, dst_port=dport,
                    proto="TCP", **fields)
        with self.db:
            return self.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                                   (NOW-5, ROUTER, direction, src, size, 2,
                                    json.dumps(data))).lastrowid

    def test_single_flow_has_only_reported_hops(self):
        row = self.add_flow(next_hop="192.0.2.8", bgp_next_hop="0.0.0.0", in_if=6)
        flow = server.query(self.db, {"flow_id": str(row), "flow_received": str(NOW-5)}, now=NOW)
        self.assertEqual(flow["src"], "203.0.113.15")
        self.assertEqual(flow["dst"], "198.51.100.1")
        self.assertEqual(flow["next_hop"], "192.0.2.8")
        self.assertIsNone(flow["bgp_next_hop"])
        self.assertEqual(flow["in_if"], 6)
        self.assertIsNone(flow["out_if"])

    def test_stale_flow_reference_does_not_pick_another_record(self):
        row = self.add_flow()
        with self.assertRaisesRegex(ValueError, "fluxo"):
            server.query(self.db, {"flow_id": str(row), "flow_received": str(NOW-10)}, now=NOW)

    def test_flow_map_opens_an_old_dashboard_row(self):
        row = self.add_flow()
        data = server.query(self.db, {"flow_id": str(row), "flow_received": str(NOW-5)}, now=NOW+7200)
        self.assertEqual(data["received"], NOW-5)

    def test_ip_search_matches_either_endpoint_exactly(self):
        self.add_flow(src="203.0.113.1")
        self.add_flow(dst="203.0.113.1")
        self.add_flow(src="203.0.113.10")
        data = server.query(self.db, {"q": "203.0.113.1"}, now=NOW)
        self.assertEqual(data["matches"], 2)

    def test_application_filter_respects_exporter_domains_and_iana_fallback(self):
        self.add_flow(application_id="0d00002a", observation_domain_id=6)
        self.add_flow(application_id="0d00002a", observation_domain_id=256)
        self.add_flow(application_id="0d00002a", observation_domain_id=257)
        self.add_flow(application_id="030001bb")
        self.add_flow()
        router = server.ipv4_number(ROUTER)
        catalog = server.ApplicationNames({
            (router, 6, "0d00002a"): "syslog",
            (router, 256, "0d00002a"): "custom",
        })
        names = {"load_applications": lambda: catalog}
        data = server.query(self.db, {"q": "app:SYSLOG"}, now=NOW, **names)
        self.assertEqual(data["matches"], 1)
        self.assertEqual(data["rows"][0]["application_name"], "syslog")
        self.assertEqual(data["rows"][0]["application_status"], "named")
        data = server.query(self.db, {"q": "app:https"}, now=NOW, **names)
        self.assertEqual(data["matches"], 1)
        self.assertEqual(data["rows"][0]["application_name"], "https")
        self.assertNotIn("application_status", data["rows"][0])
        self.assertEqual(server.query(self.db, {"q": "nbar:https"}, now=NOW, **names)["matches"], 0)

    def test_nbar_rankings_separate_same_port_apps_and_exclude_unknown_ids(self):
        router = server.ipv4_number(ROUTER)
        catalog = {(router, 0, f"0d{index:06x}"): f"app-{index:02d}" for index in range(2, 24)}
        catalog[router, 0, "0d000001"] = "unknown"
        catalog[router, 0, "0d000002"] = "facebook"
        catalog[router, 0, "0d000003"] = "netflix"
        for (_, _, app_id), _ in catalog.items():
            self.add_flow(application_id=app_id, size=1_000 if app_id == "0d000003" else 400)
        self.add_flow(application_id="0d000002", direction="down")
        self.add_flow(application_id="0d000099")
        self.add_flow(application_id="030001bb")
        names = {"load_applications": lambda: server.ApplicationNames(catalog)}
        data = server.query(self.db, {}, now=NOW, **names)
        self.assertEqual(len(data["charts"]["app_count"]), 20)
        self.assertEqual(len(data["charts"]["app_volume"]), 20)
        self.assertEqual(data["charts"]["app_count"][0]["key"], "facebook")
        self.assertEqual(data["charts"]["app_volume"][0]["key"], "netflix")
        self.assertEqual(data["charts"]["app_total"], 9_800)
        statuses = {row["application_id"]: row.get("application_status") for row in data["rows"]}
        self.assertEqual(statuses["0d000001"], "unknown")
        self.assertEqual(statuses["0d000099"], "unmapped")
        self.assertIsNone(statuses["030001bb"])
        filtered = server.query(self.db, {"q": "nbar:Facebook", "direction": "down"}, now=NOW, **names)
        self.assertEqual(filtered["matches"], 1)
        self.assertEqual(filtered["charts"]["app_count"], [{"key": "facebook", "flows": 1, "total": 400}])

    def test_top_ports_use_record_count_and_service_filter(self):
        self.add_flow(dport=443, size=1_000_000)
        self.add_flow(dport=53, direction="up")
        self.add_flow(dport=53, direction="down")
        for port in range(50_000, 50_020):
            self.add_flow(dport=port)
        data = server.query(self.db, {}, now=NOW)
        ports = data["charts"]["port"]
        self.assertEqual(len(ports), 20)
        self.assertEqual((ports[0]["key"], ports[0]["up_flows"], ports[0]["down_flows"]),
                         ("TCP/53", 1, 1))
        self.assertEqual(data["charts"]["service"][0]["key"], "domain")
        self.assertEqual(server.query(self.db, {"q": "service:domain"}, now=NOW)["matches"], 2)

    def test_table_page_jump_reuses_snapshot_and_reports_page_size(self):
        for size in range(1, 52):
            self.add_flow(size=size)
        args = {"charts": "0", "size": "25"}
        first = server.query(self.db, args, now=NOW)
        self.add_flow(size=10_000)

        snapshot = {"end": first["end"], "ceiling": first["ceiling"]}
        third = server.query(self.db, {**args, **snapshot, "page": "3"}, now=NOW)
        self.assertEqual((third["page"], third["size"], third["matches"]), (3, 25, 51))
        self.assertEqual([row["total"] for row in third["rows"]], [1])

        far = server.query(self.db, {**args, **snapshot, "page": "1000001"}, now=NOW)
        self.assertEqual(far["page"], 1_000_001)
        self.assertEqual(far["rows"], [])

    def test_network_map_aggregates_destinations_without_losing_bytes(self):
        for offset in range(1, 15):
            self.add_flow(dst=f"198.51.100.{offset}", size=offset)
        self.add_flow(src="203.0.113.200", dst="198.51.100.14", size=10)
        self.add_flow(dst="198.51.101.3", size=7)
        self.add_flow(src="192.0.2.251", dst="198.51.100.200", size=1000)
        args = {"network": "203.0.113.20/24"}
        data = server.query(self.db, args, now=NOW)
        self.assertEqual(data["target"], "203.0.113.0/24")
        self.assertEqual(data["aggregation_prefix"], 24)
        self.assertEqual([(r["peer"], r["total"]) for r in data["rows"]],
                         [("198.51.100.0/24", 115), ("198.51.101.0/24", 7)])
        self.assertEqual(data["total"], 122)
        self.assertNotIn("page", data)

    def test_network_map_respects_query_filters_and_window(self):
        self.add_flow(dst="198.51.100.1", direction="down")
        self.add_flow(dst="198.51.100.2")
        data = server.query(self.db, {"network":"203.0.113.0/24", "direction":"up",
                                     "q":"dst:198.51.100.2"}, now=NOW)
        self.assertEqual([row["peer"] for row in data["rows"]], ["198.51.100.2"])

    def test_host_map_groups_both_directions_by_peer(self):
        self.add_flow(src="203.0.113.1", dst="198.51.100.3", size=20)
        self.add_flow(src="198.51.100.3", dst="203.0.113.1", size=50, direction="down")
        data = server.query(self.db, {"network": "203.0.113.1"}, now=NOW)
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
