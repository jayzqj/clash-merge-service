"""Security review S6: subscription nodes whose server is a literal
internal address are excluded at merge time and counted in the report."""
import unittest

from app.merge import internal_server, merge_proxies


def node(name, server, port=443):
    return {"name": name, "type": "http", "server": server, "port": port}


class InternalServerPredicateTests(unittest.TestCase):
    def test_internal_literals(self):
        for server in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
                       "100.64.0.1", "169.254.169.254", "0.0.0.0", "::1",
                       "[::1]", "fe80::1", "localhost", "box.localhost",
                       "2130706433", "0x7f.1", "224.0.0.1"):
            with self.subTest(server=server):
                self.assertTrue(internal_server(server))

    def test_public_and_names_are_not_excluded(self):
        for server in ("1.1.1.1", "93.184.216.34", "example.com",
                       "sub.example.org", "2606:4700::1111", ""):
            with self.subTest(server=server):
                self.assertFalse(internal_server(server))


class MergeExclusionTests(unittest.TestCase):
    def test_internal_nodes_dropped_and_counted(self):
        merged, stats = merge_proxies([("s1", [
            node("good-hk", "93.184.216.34"),
            node("scan-controller", "127.0.0.1", 9090),
            node("scan-admin", "127.0.0.1", 18080),
            node("lan", "192.168.1.1"),
            node("good-name", "edge.example.com"),
        ])])
        names = [p["name"] for p in merged]
        self.assertEqual(sorted(names), ["good-hk", "good-name"])
        self.assertEqual(stats["internal_server_excluded"], 3)
        self.assertEqual(sorted(stats["internal_server_names"]),
                         ["lan", "scan-admin", "scan-controller"])
        self.assertEqual(stats["total_in"], 5)

    def test_no_internal_nodes_leaves_stats_zero(self):
        merged, stats = merge_proxies([("s1", [node("a", "1.1.1.1"), node("b", "edge.example.com")])])
        self.assertEqual(len(merged), 2)
        self.assertEqual(stats["internal_server_excluded"], 0)
        self.assertEqual(stats["internal_server_names"], [])

    def test_trusted_source_keeps_local_nodes(self):
        merged, stats = merge_proxies([
            ("remote", [node("scan", "127.0.0.1", 9090)]),
            ("local", [node("my-local-proxy", "127.0.0.1", 7897)]),
        ], trusted={"local"})
        self.assertEqual([p["name"] for p in merged], ["my-local-proxy"])
        self.assertEqual(stats["internal_server_names"], ["scan"])


if __name__ == "__main__":
    unittest.main()
