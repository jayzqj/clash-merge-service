"""Fetch one Clash subscription and validate it is really a config.

Validation is deliberately stricter than "HTTP 200": a known failure
mode (2026-11-11) is a provider returning HTTP 400 with the plain-text
body ``Subscription source #10 could not be fetched or contains no
valid node info.`` — and in general any non-YAML / empty response must
not be treated as a subscription.

Per-source tolerance (user's decision 2026-11-13, after one source
returned HTTP 502 and another "No nodes were found!"): a failed
source is SKIPPED and the run continues with the remaining sources;
only when ALL sources fail does the run fail (and keep the last good
output file). The pipeline records each skipped source (URL + reason)
in the run report as ``sources_failed`` — shown on the admin page,
never in the WeChat notification.
"""
from __future__ import annotations

import base64
import binascii
import asyncio
import re

import httpx
import yaml
from app.node_links import is_node_source, parse_node_source, decode64, parse_clash_nodes, normalize_links

# httpx must NOT read proxy settings from the environment implicitly:
# this dev VM's NO_PROXY contains bracketed IPv6 entries that httpx
# 0.28 fails to parse ("Invalid port: ':1]'"), which breaks every
# client construction. Take the proxy explicitly (if set) instead.
# SSL verification uses the *system* CA store, not certifi: the dev
# VM's egress proxy MITMs TLS with a CA that lives in /etc/ssl/certs
# (hatch-egress-ca.pem), so certifi-based verification fails here.
# The system store also verifies real public certs on the VPS.
import os
import ssl


def make_client(timeout: float = 60.0, cfg=None) -> httpx.AsyncClient:
    from app.runtime import desktop_mode
    proxy = (cfg.data.get("fetch_proxy_url") if cfg else None) or None
    if proxy is None and not desktop_mode():
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    client = httpx.AsyncClient(timeout=timeout, proxy=proxy, trust_env=False,
                              verify=ssl.create_default_context())
    client.merge_direct_fallback = bool(proxy and cfg and cfg.data.get("fetch_direct_fallback", True))
    client.merge_route = "proxy" if proxy else "direct"
    client.merge_routes = {}
    return client

PLAIN_TEXT_ERROR_MARKERS = (
    "could not be fetched",
    "contains no valid node info",
    "Subscription source",
)


class FetchError(Exception):
    def __init__(self, step: str, reason: str):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


def _parse_proxies(text: str) -> list[dict]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise FetchError("fetch", "response is not a YAML mapping")
    proxies = data.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        raise FetchError("fetch", "parsed YAML has no proxies (0 nodes)")
    good = []
    for p in proxies:
        if not isinstance(p, dict):
            continue
        if not p.get("name") or not p.get("server") or not p.get("port"):
            continue
        good.append(p)
    if not good:
        raise FetchError("fetch", "proxies present but none have name/server/port")
    return good


def parse_subscription_body(content: bytes) -> list[dict]:
    """Parse raw subscription bytes; handles plain YAML and base64 YAML."""
    text = normalize_links(content.decode("utf-8", errors="replace"))
    if not text:
        raise FetchError("fetch", "empty response body")
    for marker in PLAIN_TEXT_ERROR_MARKERS:
        if marker in text[:500]:
            raise FetchError("fetch", f"provider error text in body ({marker!r})")
    for candidate in (text,):
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', candidate):
            try:
                return parse_node_source(candidate)
            except ValueError as exc:
                raise FetchError("fetch", str(exc)) from None
    try:
        return parse_clash_nodes(text)
    except (ValueError, yaml.YAMLError):
        pass
    try:
        decoded = decode64(text)
        return parse_node_source(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError, yaml.YAMLError, FetchError):
        raise FetchError("fetch", "响应不是有效的 Clash YAML 或节点分享链接") from None


async def fetch_subscription(url: str, client: httpx.AsyncClient,
                            timeout: float = 60.0) -> list[dict]:
    if is_node_source(url):
        try:
            nodes = parse_node_source(url)
        except ValueError as exc:
            raise FetchError("fetch", str(exc)) from None
        if hasattr(client, "merge_routes"):
            client.merge_routes[url] = "local"
        return nodes
    return await _fetch(url, client, timeout, parse_subscription_body,
                        {"User-Agent": "clash-verge/v2.0"})


async def _fetch(url, client, timeout, parse, headers=None):
    async def attempt(active, route):
        for retry in range(2):
            try:
                resp = await active.get(url, timeout=timeout, headers=headers)
                resp.raise_for_status()
                break
            except httpx.HTTPError as exc:
                transient = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (502, 503, 504))
                if retry or not transient:
                    raise
                await asyncio.sleep(0.3)
        value = parse(resp.content)
        if hasattr(client, "merge_routes"):
            client.merge_routes[url] = route
        return value
    try:
        return await attempt(client, getattr(client, "merge_route", "direct"))
    except (httpx.HTTPError, FetchError) as first:
        if getattr(client, "merge_direct_fallback", False):
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                             verify=ssl.create_default_context()) as direct:
                    return await attempt(direct, "direct-fallback")
            except (httpx.HTTPError, FetchError) as second:
                raise FetchError("fetch", f"代理和直连均失败：{type(first).__name__} / {type(second).__name__}") from second
        if isinstance(first, FetchError):
            raise
        status = first.response.status_code if isinstance(first, httpx.HTTPStatusError) else None
        raise FetchError("fetch", f"{type(first).__name__}" + (f" HTTP {status}" if status else "")) from first


async def fetch_text(url: str, client: httpx.AsyncClient,
                     timeout: float = 30.0) -> str:
    return await _fetch(url, client, timeout, lambda body: body.decode("utf-8-sig"))
