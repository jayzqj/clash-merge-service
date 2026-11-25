"""Security review S5: direct requests reject hosts that RESOLVE to an
internal address, not just literal internal IPs. DNS is stubbed so the
test touches no real network."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from app import fetcher
from app.fetcher import FetchError, ensure_external, fetch_subscription, make_client

YAML = b"proxies:\n" + b"".join(
    b"- {name: %s, type: http, server: 1.2.3.4, port: %d}\n" % (n.encode(), 80 + i)
    for i, n in enumerate("abcde"))


def run(coro):
    return asyncio.run(coro)


def fake_getaddrinfo(mapping):
    async def _resolve(host, port, *args, **kwargs):
        if host not in mapping:
            raise OSError("name does not resolve")
        return [(2, 1, 6, "", (ip, port)) for ip in mapping[host]]
    return _resolve


class EnsureExternalTests(unittest.IsolatedAsyncioTestCase):
    async def _with_dns(self, mapping, url):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo(mapping)):
            await ensure_external(httpx.URL(url))

    async def test_name_resolving_to_public_ip_is_allowed(self):
        await self._with_dns({"sub.example.org": ["93.184.216.34"]},
                             "https://sub.example.org/x")

    async def test_name_resolving_to_loopback_is_rejected(self):
        with self.assertRaises(FetchError) as caught:
            await self._with_dns({"127.0.0.1.nip.io": ["127.0.0.1"]},
                                 "https://127.0.0.1.nip.io/x")
        self.assertIn("内网", caught.exception.reason)

    async def test_name_resolving_to_private_ip_is_rejected(self):
        with self.assertRaises(FetchError):
            await self._with_dns({"rebind.example": ["93.184.216.34", "10.0.0.5"]},
                                 "https://rebind.example/x")

    async def test_unresolvable_name_is_rejected(self):
        with self.assertRaises(FetchError) as caught:
            await self._with_dns({}, "https://nope.invalid/x")
        self.assertIn("解析", caught.exception.reason)

    async def test_literal_internal_ip_rejected_without_dns(self):
        with self.assertRaises(FetchError):
            await ensure_external(httpx.URL("https://127.0.0.1/x"))
        # A public literal needs no DNS and passes.
        await ensure_external(httpx.URL("https://1.1.1.1/x"))


class ClientFlagTests(unittest.TestCase):
    def test_direct_client_checks_proxied_client_does_not(self):
        # Desktop mode never reads an environment proxy, so make_client
        # builds a genuine direct client regardless of this machine's
        # HTTPS_PROXY. That must enable the SSRF DNS check.
        with patch("app.runtime.desktop_mode", return_value=True):
            direct = make_client(5)
        try:
            self.assertTrue(direct.merge_ssrf_check)
        finally:
            run(direct.aclose())

        # An explicit proxy means the proxy owns DNS: no check.
        class Cfg:
            data = {"fetch_proxy_url": "http://127.0.0.1:7897"}
        proxied = make_client(5, cfg=Cfg())
        try:
            self.assertFalse(proxied.merge_ssrf_check)
        finally:
            run(proxied.aclose())

    def test_mock_transport_client_does_not_check(self):
        client = fetcher._client(5, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        try:
            self.assertFalse(client.merge_ssrf_check)
        finally:
            run(client.aclose())


class RedirectDnsTests(unittest.IsolatedAsyncioTestCase):
    """With the DNS check ON (as for a real direct client): the
    admin-configured first URL may be local, a local source may redirect
    locally, but a public source's redirect may not resolve internally."""

    def client(self, routes):
        def handler(request):
            status, value = routes[str(request.url)]
            if 300 <= status < 400:
                return httpx.Response(status, headers={"Location": value})
            return httpx.Response(status, content=value)
        client = fetcher._client(5, transport=httpx.MockTransport(handler))
        client.merge_ssrf_check = True  # behave like a real unproxied client
        return client

    async def fetch(self, routes, url, dns):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo(dns)):
            async with self.client(routes) as client:
                return await fetch_subscription(url, client)

    async def test_local_first_url_is_allowed(self):
        nodes = await self.fetch({"http://127.0.0.1:8000/sub": (200, YAML)},
                                 "http://127.0.0.1:8000/sub", {})
        self.assertEqual(len(nodes), 5)

    async def test_local_source_may_redirect_locally(self):
        nodes = await self.fetch({"http://127.0.0.1:8000/a": (302, "http://127.0.0.1:8000/b"),
                                  "http://127.0.0.1:8000/b": (200, YAML)},
                                 "http://127.0.0.1:8000/a", {})
        self.assertEqual(len(nodes), 5)

    async def test_public_source_redirect_to_rebinding_name_rejected(self):
        with self.assertRaises(FetchError) as caught:
            await self.fetch({"https://a.example/s/x": (302, "https://rebind.example/sub")},
                             "https://a.example/s/x",
                             {"rebind.example": ["127.0.0.1"]})
        self.assertIn("内网", caught.exception.reason)

    async def test_public_source_redirect_to_public_name_allowed(self):
        nodes = await self.fetch({"https://a.example/s/x": (302, "https://b.example/sub"),
                                  "https://b.example/sub": (200, YAML)},
                                 "https://a.example/s/x",
                                 {"b.example": ["93.184.216.35"]})
        self.assertEqual(len(nodes), 5)


if __name__ == "__main__":
    unittest.main()
