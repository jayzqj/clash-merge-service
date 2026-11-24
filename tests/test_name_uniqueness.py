"""Final node names are unique and never take a group / built-in name."""
import asyncio
import unittest

from app.fetcher import FetchError
from app.merge import merge_proxies, prefix_purity_names, dedup_key, unique_name
from app.output_names import append_ipv4_names


def node(name, server, port=443):
    return {"name": name, "type": "ss", "server": server, "port": port,
            "cipher": "aes-128-gcm", "password": "test"}


class UniqueNameTests(unittest.TestCase):
    def test_unique_name(self):
        self.assertEqual(unique_name("HK", set()), "HK")
        self.assertEqual(unique_name("HK", {"HK"}), "HK (2)")
        self.assertEqual(unique_name("HK", {"HK", "HK (2)"}), "HK (3)")

    def test_merge_does_not_collide_with_literal_suffix(self):
        merged, stats = merge_proxies([
            ("a", [node("HK", "1.1.1.1"), node("HK", "1.1.1.2")]),
            ("b", [node("HK (2)", "1.1.1.3"), node("HK", "1.1.1.4")])])
        names = [p["name"] for p in merged]
        self.assertEqual(len(set(names)), 4, names)
        self.assertEqual(names[:2], ["HK", "HK (2)"])
        self.assertEqual(stats["renamed"], 3)

    def test_prefix_same_score_and_literal_suffix(self):
        ps = [node("X", "2.2.2.1"), node("X", "2.2.2.2"), node("X (2)", "2.2.2.3")]
        purity = {dedup_key(p): {"score": 5} for p in ps}
        out, _ = prefix_purity_names(ps, purity)
        names = [p["name"] for p in out]
        self.assertEqual(len(set(names)), 3, names)
        self.assertEqual(names[0], "5% X")


class Ipv4NameTests(unittest.TestCase):
    def run_doc(self, doc):
        return asyncio.run(append_ipv4_names(doc, {}))

    def test_group_and_builtin_names_are_not_taken(self):
        doc = {
            "proxies": [node("A", "2001:db8::1"), node("DIRECT", "2001:db8::2"),
                        node("HK", "3.3.3.3")],
            "proxy-groups": [{"name": "A", "type": "select", "proxies": ["DIRECT"]},
                             {"name": "G", "type": "select", "proxies": ["A", "DIRECT", "HK"]}]}
        stats = self.run_doc(doc)
        names = [p["name"] for p in doc["proxies"]]
        self.assertEqual(names, ["A (2)", "DIRECT (2)", "HK 3.3.3.3"])
        self.assertEqual(doc["proxy-groups"][1]["proxies"], ["A", "DIRECT", "HK 3.3.3.3"])
        self.assertEqual(doc["proxy-groups"][0]["proxies"], ["DIRECT"])
        self.assertEqual(stats["name_conflicts_renamed"], 2)

    def test_same_base_with_and_without_ip_stay_unique(self):
        doc = {"proxies": [node("JP", "4.4.4.4"), node("JP 4.4.4.4", "4.4.4.4", 8443),
                           node("JP", "2001:db8::3")],
               "proxy-groups": []}
        with self.assertRaises(FetchError):
            self.run_doc(doc)  # "JP" twice: earlier steps must have made it unique
        doc["proxies"][2]["name"] = "JP (2)"
        self.run_doc(doc)
        names = [p["name"] for p in doc["proxies"]]
        self.assertEqual(len(set(names)), 3, names)

    def test_duplicate_input_names_fail_loudly(self):
        doc = {"proxies": [node("Z", "5.5.5.5"), node("Z", "5.5.5.6")],
               "proxy-groups": [{"name": "G", "type": "select", "proxies": ["Z"]}]}
        with self.assertRaises(FetchError) as caught:
            self.run_doc(doc)
        self.assertEqual(caught.exception.step, "names")


if __name__ == "__main__":
    unittest.main()
