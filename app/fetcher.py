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

import httpx
import yaml

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


def make_client(timeout: float = 60.0) -> httpx.AsyncClient:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    return httpx.AsyncClient(timeout=timeout, proxy=proxy, trust_env=False,
                             verify=ssl.create_default_context())

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
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise FetchError("fetch", "empty response body")
    for marker in PLAIN_TEXT_ERROR_MARKERS:
        if marker in text[:500]:
            raise FetchError("fetch", f"provider error text in body ({marker!r})")
    try:
        return _parse_proxies(text)
    except FetchError:
        raise
    except yaml.YAMLError:
        pass
    # Some providers return base64-encoded YAML.
    try:
        decoded = base64.b64decode(text, validate=False).decode("utf-8")
        return _parse_proxies(decoded)
    except (binascii.Error, UnicodeDecodeError, yaml.YAMLError, FetchError) as e:
        raise FetchError("fetch", f"response is not parseable Clash YAML: {e}")


async def fetch_subscription(url: str, client: httpx.AsyncClient,
                            timeout: float = 60.0) -> list[dict]:
    try:
        resp = await client.get(url, timeout=timeout,
                                headers={"User-Agent": "clash-verge/v2.0"})
    except httpx.HTTPError as e:
        raise FetchError("fetch", f"HTTP request failed: {e}")
    if resp.status_code != 200:
        body = resp.text[:200].strip()
        raise FetchError("fetch", f"HTTP {resp.status_code} (body: {body!r})")
    return parse_subscription_body(resp.content)


async def fetch_text(url: str, client: httpx.AsyncClient,
                     timeout: float = 30.0) -> str:
    try:
        resp = await client.get(url, timeout=timeout)
    except httpx.HTTPError as e:
        raise FetchError("fetch", f"HTTP request failed for {url}: {e}")
    if resp.status_code != 200:
        raise FetchError("fetch", f"HTTP {resp.status_code} fetching {url}")
    return resp.text
