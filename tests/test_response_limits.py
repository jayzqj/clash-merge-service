"""Security review S1: downloads are size-limited and node counts capped."""
import asyncio
import unittest

import httpx

from app import fetcher
from app.config import Config
from app.converter import IniData, fetch_base
from app.fetcher import FetchError, fetch_subscription, fetch_text, read_body


def run(coro):
    return asyncio.run(coro)


def client_with(handler, limit=None):
    client = fetcher._client(10, transport=httpx.MockTransport(handler))
    if limit is not None:
        client.merge_max_bytes = limit
    return client


def nodes_yaml(count):
    lines = ["proxies:"]
    lines += [f"- {{name: n{i}, type: http, server: 1.2.3.4, port: {1000 + i}}}" for i in range(count)]
    return ("\n".join(lines) + "\n").encode()


class EndlessBody(httpx.AsyncByteStream):
    """A body without Content-Length that never ends on its own."""

    def __init__(self):
        self.sent = 0

    async def __aiter__(self):
        while True:
            self.sent += 65536
            yield b"a" * 65536


class ResponseLimitTests(unittest.TestCase):
    def test_declared_length_over_limit_is_refused(self):
        client = client_with(lambda r: httpx.Response(200, content=b"x" * 2048), limit=1024)

        async def go():
            async with client:
                return await fetch_subscription("https://a.example/sub", client)

        with self.assertRaises(FetchError) as caught:
            run(go())
        self.assertIn("上限", caught.exception.reason)

    def test_unbounded_stream_is_aborted(self):
        body = EndlessBody()
        client = client_with(lambda r: httpx.Response(200, stream=body), limit=256 * 1024)

        async def go():
            async with client:
                return await fetch_text("https://a.example/ini", client)

        with self.assertRaises(FetchError) as caught:
            run(go())
        self.assertIn("上限", caught.exception.reason)
        self.assertLess(body.sent, 1024 * 1024)

    def test_body_within_limit_is_read(self):
        client = client_with(lambda r: httpx.Response(200, content=b"hello"), limit=1024)

        async def go():
            async with client:
                return await read_body(client, "https://a.example/x", timeout=5)

        self.assertEqual(run(go()), (200, b"hello"))

    def test_error_status_still_raises_http_error(self):
        client = client_with(lambda r: httpx.Response(404, content=b"nope"))

        async def go():
            async with client:
                return await read_body(client, "https://a.example/x", timeout=5)

        with self.assertRaises(httpx.HTTPStatusError):
            run(go())

    def test_node_count_cap(self):
        old = fetcher.MAX_NODES_PER_SOURCE
        fetcher.MAX_NODES_PER_SOURCE = 6
        try:
            for count, ok in ((6, True), (7, False)):
                with self.subTest(count=count):
                    client = client_with(lambda r, c=count: httpx.Response(200, content=nodes_yaml(c)))

                    async def go():
                        async with client:
                            return await fetch_subscription("https://a.example/sub", client)

                    if ok:
                        self.assertEqual(len(run(go())), 6)
                    else:
                        with self.assertRaises(FetchError) as caught:
                            run(go())
                        self.assertIn("上限", caught.exception.reason)
        finally:
            fetcher.MAX_NODES_PER_SOURCE = old

    def test_subscription_with_fewer_than_min_nodes_fails(self):
        self.assertEqual(fetcher.MIN_NODES_PER_SUBSCRIPTION, 5)
        for count, ok in ((1, False), (4, False), (5, True)):
            with self.subTest(count=count):
                client = client_with(lambda r, c=count: httpx.Response(200, content=nodes_yaml(c)))

                async def go():
                    async with client:
                        return await fetch_subscription("https://a.example/sub", client)

                if ok:
                    self.assertEqual(len(run(go())), count)
                else:
                    with self.assertRaises(FetchError) as caught:
                        run(go())
                    self.assertIn("少于 5", caught.exception.reason)

    def test_rule_base_is_limited(self):
        client = client_with(lambda r: httpx.Response(200, content=b"x" * 4096), limit=1024)
        ini = IniData(base_url="https://a.example/base.yaml")

        async def go():
            async with client:
                return await fetch_base(client, ini)

        with self.assertRaises(FetchError) as caught:
            run(go())
        self.assertEqual(caught.exception.step, "convert")
        self.assertIn("上限", caught.exception.reason)

    def test_limit_from_config(self):
        cfg = Config()
        self.assertEqual(fetcher.response_limit(cfg), fetcher.MAX_RESPONSE_BYTES)
        cfg.data["fetch_max_mb"] = 5
        self.assertEqual(fetcher.response_limit(cfg), 5 * 1024 * 1024)
        client = fetcher.make_client(5, cfg=cfg)
        try:
            self.assertEqual(client.merge_max_bytes, 5 * 1024 * 1024)
        finally:
            run(client.aclose())


if __name__ == "__main__":
    unittest.main()
