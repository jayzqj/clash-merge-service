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
import ipaddress
import re
import socket

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

# Redirects (review item 18): subscription short links ("/s/xxx") answer
# 301/302, and httpx does not follow redirects by default. Follow at most
# MAX_REDIRECTS hops, but never let a provider-controlled Location
# downgrade https to http or point the service at itself / the LAN
# (e.g. the local mihomo controller). Only literal addresses are
# checked; names are not resolved.
MAX_REDIRECTS = 5
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")

# Response limits (security review S1): every download is streamed and
# aborted past the limit, so a hostile or broken provider cannot make the
# service read hundreds of MB into a 512 MiB cgroup. The limit applies to
# the decoded body (after gzip), which also bounds compression bombs.
# ``fetch_max_mb`` in config.json overrides the default.
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_NODES_PER_SOURCE = 5000
# A subscription URL that yields fewer nodes than this is counted as a
# failed source (skipped and listed in ``sources_failed``). Not applied to
# node share links the admin entered directly. Default only:
# ``min_nodes_per_subscription`` in config.json (admin page) overrides it,
# 0 disables the check.
MIN_NODES_PER_SUBSCRIPTION = 5
_CHUNK = 64 * 1024


def internal_host(host: str) -> bool:
    """localhost, or a literal IP that is not globally routable
    (loopback, private, link-local, CGNAT, unspecified, multicast...).
    Non-standard numeric forms such as ``2130706433`` or ``0x7f.1``
    resolve to IPs too, so they count as internal."""
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return all(_NUMERIC_LABEL.fullmatch(label) for label in host.split("."))
    return not address.is_global or address.is_multicast


def _internal_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable: refuse rather than guess
    return not address.is_global or address.is_multicast


async def ensure_external(url: httpx.URL) -> None:
    """SSRF guard for a DIRECT request (security review S5): reject a
    target whose host is internal as a literal, OR whose name resolves
    to any internal address (defeats rebinding tricks like
    ``127.0.0.1.nip.io``). Not callable when a proxy is in use: the
    proxy resolves the name, so the service never sees the IP — the
    caller skips this check on the proxied route and the comment on
    ``_fetch`` records why."""
    host = url.host
    if internal_host(host):
        raise FetchError("fetch", "拒绝请求：目标是本机或内网地址")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return  # already a literal, internal_host checked it
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, url.port or (443 if url.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except OSError:
        raise FetchError("fetch", "拒绝请求：无法解析目标域名") from None
    for info in infos:
        if _internal_ip(info[4][0]):
            raise FetchError("fetch", "拒绝请求：目标域名解析到本机或内网地址")


async def check_redirect(response: httpx.Response) -> None:
    """httpx response hook: runs before every redirect is followed.
    Error texts never include the URLs (they may carry tokens)."""
    if not response.has_redirect_location:
        return
    source = response.request.url
    try:
        target = source.join(response.headers["location"])
    except Exception:  # noqa: BLE001 - malformed Location header
        raise FetchError("fetch", "拒绝跳转：跳转地址无效") from None
    if target.scheme not in ("http", "https"):
        raise FetchError("fetch", "拒绝跳转：跳转地址不是 http/https")
    if source.scheme == "https" and target.scheme != "https":
        raise FetchError("fetch", "拒绝跳转：https 地址跳转到了 http 明文地址")
    if internal_host(target.host) and not internal_host(source.host):
        raise FetchError("fetch", "拒绝跳转：跳转目标是本机或内网地址")
    # Resolve the redirect target too (defeats names like 127.0.0.1.nip.io),
    # unless this request goes through a proxy (the proxy owns DNS and the
    # service never sees the IP) or the source itself is local (a local
    # source may redirect locally, same rule as the literal check above).
    # read_body stamps every direct-route request's extensions.
    if response.request.extensions.get("merge_ssrf_check") and not internal_host(source.host):
        await ensure_external(target)


def _client(timeout: float, proxy=None, transport=None) -> httpx.AsyncClient:
    client = httpx.AsyncClient(timeout=timeout, proxy=None if transport else proxy,
                               transport=transport, trust_env=False,
                               verify=ssl.create_default_context(),
                               follow_redirects=True, max_redirects=MAX_REDIRECTS,
                               event_hooks={"response": [check_redirect]})
    # SSRF DNS checks (S5) are OFF by default and turned on only by
    # make_client for a real, unproxied client. A proxied route can't be
    # checked (the proxy owns DNS); a mock transport in tests opens no
    # socket, so resolving the host would be pointless and fail.
    client.merge_ssrf_check = False
    return client


def make_client(timeout: float = 60.0, cfg=None) -> httpx.AsyncClient:
    from app.runtime import desktop_mode
    proxy = (cfg.data.get("fetch_proxy_url") if cfg else None) or None
    if proxy is None and not desktop_mode():
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    client = _client(timeout, proxy)
    client.merge_direct_fallback = bool(proxy and cfg and cfg.data.get("fetch_direct_fallback", True))
    client.merge_route = "proxy" if proxy else "direct"
    client.merge_routes = {}
    client.merge_max_bytes = response_limit(cfg)
    client.merge_min_nodes = min_nodes(cfg)
    client.merge_ssrf_check = proxy is None
    return client


def response_limit(cfg=None) -> int:
    try:
        mb = int((cfg.data.get("fetch_max_mb") if cfg else None) or 0)
    except (TypeError, ValueError):
        mb = 0
    return mb * 1024 * 1024 if mb > 0 else MAX_RESPONSE_BYTES


def min_nodes(cfg=None) -> int:
    value = cfg.data.get("min_nodes_per_subscription") if cfg else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return MIN_NODES_PER_SUBSCRIPTION
    return value

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


def _too_large(limit: int) -> FetchError:
    return FetchError("fetch", f"响应超过 {limit // (1024 * 1024)} MB 上限，已中断下载")


async def read_body(client: httpx.AsyncClient, url: str, *, timeout: float,
                    headers=None, limit: int | None = None,
                    check_status: bool = True) -> tuple[int, bytes]:
    """GET ``url`` and return ``(status, body)``, reading at most ``limit``
    bytes. A declared Content-Length over the limit is refused before the
    body is read. With ``check_status`` an HTTP error status raises
    ``httpx.HTTPStatusError`` before the body is read."""
    if limit is None:
        limit = getattr(client, "merge_max_bytes", MAX_RESPONSE_BYTES)
    # SSRF guard (S5): stamp direct-route requests so check_redirect
    # resolves every provider-controlled redirect target. The FIRST URL is
    # NOT checked: it is admin-configured (subscription, ini, rule base),
    # and local sources such as a local subconverter are supported.
    # Skipped entirely when a proxy is in front (it owns DNS).
    extensions = {"merge_ssrf_check": True} if getattr(client, "merge_ssrf_check", False) else None
    async with client.stream("GET", url, timeout=timeout, headers=headers,
                             extensions=extensions) as resp:
        if check_status:
            resp.raise_for_status()
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise _too_large(limit)
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise _too_large(limit)
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks)


def _check_node_count(nodes: list[dict]) -> list[dict]:
    if len(nodes) > MAX_NODES_PER_SOURCE:
        raise FetchError("fetch", f"节点数 {len(nodes)} 超过单个订阅源上限 {MAX_NODES_PER_SOURCE}")
    return nodes


def _check_min_nodes(nodes: list[dict], minimum: int = MIN_NODES_PER_SUBSCRIPTION) -> list[dict]:
    if len(nodes) < minimum:
        raise FetchError("fetch", f"节点数 {len(nodes)} 少于 {minimum} 个，记为失败")
    return nodes


def _describe(exc: Exception) -> str:
    return exc.reason if isinstance(exc, FetchError) else type(exc).__name__


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
            nodes = _check_node_count(parse_node_source(url))
        except ValueError as exc:
            raise FetchError("fetch", str(exc)) from None
        if hasattr(client, "merge_routes"):
            client.merge_routes[url] = "local"
        return nodes
    minimum = getattr(client, "merge_min_nodes", MIN_NODES_PER_SUBSCRIPTION)
    return await _fetch(url, client, timeout,
                        lambda body: _check_min_nodes(_check_node_count(parse_subscription_body(body)), minimum),
                        {"User-Agent": "clash-verge/v2.0"})


async def _fetch(url, client, timeout, parse, headers=None):
    limit = getattr(client, "merge_max_bytes", MAX_RESPONSE_BYTES)

    async def attempt(active, route):
        for retry in range(2):
            try:
                _, body = await read_body(active, url, timeout=timeout,
                                          headers=headers, limit=limit)
                break
            except httpx.HTTPError as exc:
                transient = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (502, 503, 504))
                if retry or not transient:
                    raise
                await asyncio.sleep(0.3)
        value = parse(body)
        if hasattr(client, "merge_routes"):
            client.merge_routes[url] = route
        return value
    try:
        return await attempt(client, getattr(client, "merge_route", "direct"))
    except (httpx.HTTPError, FetchError) as first:
        if getattr(client, "merge_direct_fallback", False):
            try:
                async with _client(timeout) as direct:
                    # Inherit the SSRF-check setting from the primary
                    # client: the fallback is the same route kind (direct),
                    # and tests that inject a mock via the patched
                    # AsyncClient constructor have it disabled.
                    direct.merge_ssrf_check = getattr(client, "merge_ssrf_check", direct.merge_ssrf_check)
                    return await attempt(direct, "direct-fallback")
            except (httpx.HTTPError, FetchError) as second:
                raise FetchError("fetch", f"代理和直连均失败：{_describe(first)} / {_describe(second)}") from second
        if isinstance(first, FetchError):
            raise
        status = first.response.status_code if isinstance(first, httpx.HTTPStatusError) else None
        raise FetchError("fetch", f"{type(first).__name__}" + (f" HTTP {status}" if status else "")) from first


async def fetch_text(url: str, client: httpx.AsyncClient,
                     timeout: float = 30.0) -> str:
    return await _fetch(url, client, timeout, lambda body: body.decode("utf-8-sig"))
