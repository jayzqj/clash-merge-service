"""Subscription / rule downloads follow redirects, within limits."""
import asyncio
import unittest

import httpx

from app import fetcher
from app.fetcher import FetchError, fetch_subscription, fetch_text, internal_host

YAML = b"proxies:\n- {name: a, type: http, server: 1.2.3.4, port: 80}\n"


def run(coro):
    return asyncio.run(coro)


def client_for(routes):
    """routes: url -> (status, location or body). Records requested URLs."""
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        status, value = routes[url]
        if 300 <= status < 400:
            return httpx.Response(status, headers={"Location": value})
        return httpx.Response(status, content=value)

    return fetcher._client(10, transport=httpx.MockTransport(handler)), seen


async def fetch(client, url):
    async with client:
        return await fetch_subscription(url, client)


class RedirectTests(unittest.TestCase):
    def test_follows_redirect_chain(self):
        client, seen = client_for({
            "https://a.example/s/x": (302, "https://b.example/sub?token=t"),
            "https://b.example/sub?token=t": (301, "/final"),
            "https://b.example/final": (200, YAML)})
        nodes = run(fetch(client, "https://a.example/s/x"))
        self.assertEqual([n["name"] for n in nodes], ["a"])
        self.assertEqual(len(seen), 3)

    def test_too_many_redirects(self):
        routes = {f"https://a.example/{i}": (302, f"https://a.example/{i + 1}") for i in range(10)}
        client, seen = client_for(routes)
        with self.assertRaises(FetchError) as caught:
            run(fetch(client, "https://a.example/0"))
        self.assertIn("TooManyRedirects", caught.exception.reason)
        self.assertEqual(len(seen), fetcher.MAX_REDIRECTS + 1)

    def test_rejects_https_to_http(self):
        client, seen = client_for({
            "https://a.example/s/x": (302, "http://a.example/sub?token=secret"),
            "http://a.example/sub?token=secret": (200, YAML)})
        with self.assertRaises(FetchError) as caught:
            run(fetch(client, "https://a.example/s/x"))
        self.assertIn("http 明文", caught.exception.reason)
        self.assertNotIn("secret", caught.exception.reason)
        self.assertEqual(seen, ["https://a.example/s/x"])

    def test_rejects_internal_targets(self):
        for target in ("https://127.0.0.1:9090/configs", "https://localhost/x", "https://10.0.0.5/x",
                       "https://192.168.1.1/x", "https://169.254.169.254/latest", "https://[::1]/x",
                       "https://2130706433/x", "https://0x7f.1/x"):
            with self.subTest(target=target):
                client, seen = client_for({"https://a.example/s/x": (302, target)})
                with self.assertRaises(FetchError) as caught:
                    run(fetch(client, "https://a.example/s/x"))
                self.assertIn("本机或内网", caught.exception.reason)
                self.assertEqual(len(seen), 1)

    def test_rules_downloads_use_same_policy(self):
        client, _ = client_for({"https://gist.example/ini": (302, "http://gist.example/ini")})

        async def go():
            async with client:
                return await fetch_text("https://gist.example/ini", client)

        with self.assertRaises(FetchError):
            run(go())

    def test_local_source_may_redirect_locally(self):
        client, _ = client_for({
            "http://127.0.0.1:8000/a": (302, "http://127.0.0.1:8000/b"),
            "http://127.0.0.1:8000/b": (200, YAML)})
        self.assertEqual(len(run(fetch(client, "http://127.0.0.1:8000/a"))), 1)

    def test_make_client_follows_redirects(self):
        client = fetcher.make_client(5)
        try:
            self.assertTrue(client.follow_redirects)
            self.assertEqual(client.max_redirects, fetcher.MAX_REDIRECTS)
        finally:
            run(client.aclose())

    def test_internal_host(self):
        for host in ("localhost", "a.localhost", "127.0.0.1", "10.1.2.3", "172.16.0.1", "100.64.0.1",
                     "0.0.0.0", "::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1", "2130706433"):
            with self.subTest(host=host):
                self.assertTrue(internal_host(host))
        for host in ("example.com", "1.1.1.1", "123.com", "abc.de", "2606:4700::1111"):
            with self.subTest(host=host):
                self.assertFalse(internal_host(host))


if __name__ == "__main__":
    unittest.main()
