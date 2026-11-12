"""dialer-proxy injection (AFTER subconverter conversion).

Rules (corrected per spec — the old "UDP nodes can't be front" claim
is wrong and discarded):
- hysteria2 CAN be a dialer-proxy target (front / first hop). The
  user's live chain uses a Japan hysteria2 node as front.
- hysteria2 itself CANNOT carry dialer-proxy: QUIC runs on UDP and
  cannot ride a TCP stream; mihomo silently skips it. So hysteria2
  is first-hop only.
- Backend = node name contains "静态". TCP backend nodes get
  ``dialer-proxy: 🚪 ISP链式前置分组`` (exact string, emoji + one
  space, no spaces inside "ISP链式前置分组"). UDP backend nodes
  (hysteria2 / tuic / wireguard) get none — direct only. Front and
  ordinary nodes get none.

Also fills the ini's REJECT-placeholder backend groups
(🎯 ISP链式后置分组 / 🎯 链式代理后置分组) with the backend node
names, because the ini leaves them as ``select []REJECT`` for "the
local script" to replace.
"""
from __future__ import annotations

from app.config import BACKEND_GROUPS, FRONT_GROUP

UDP_TYPES = {"hysteria2", "tuic", "wireguard"}
TCP_TYPES = {"ss", "ssr", "vmess", "vless", "trojan", "socks5", "http",
             "anytls", "snell", "tuic-tcp"}


def is_backend(proxy: dict) -> bool:
    return "静态" in str(proxy.get("name", ""))


def is_udp(proxy: dict) -> bool:
    return str(proxy.get("type", "")).lower() in UDP_TYPES


def inject_dialer(proxies: list[dict]) -> dict:
    """Mutate proxy dicts in place. Returns counts."""
    stats = {"backend_total": 0, "backend_tcp_injected": 0,
             "backend_udp_no_dialer": 0, "front_or_ordinary": 0}
    for p in proxies:
        if not is_backend(p):
            stats["front_or_ordinary"] += 1
            p.pop("dialer-proxy", None)
            continue
        stats["backend_total"] += 1
        if is_udp(p):
            stats["backend_udp_no_dialer"] += 1
            p.pop("dialer-proxy", None)
        else:
            stats["backend_tcp_injected"] += 1
            p["dialer-proxy"] = FRONT_GROUP
    return stats


def fill_backend_groups(doc: dict, proxies: list[dict]) -> dict:
    """Replace the REJECT placeholder in backend groups with the
    backend node names. Returns {group_name: member_count}."""
    backend_names = [str(p["name"]) for p in proxies if is_backend(p)]
    filled: dict[str, int] = {}
    for group in doc.get("proxy-groups", []) or []:
        if group.get("name") in BACKEND_GROUPS:
            group["proxies"] = list(backend_names) if backend_names else ["REJECT"]
            group["type"] = "select"
            filled[group["name"]] = len(backend_names)
    return filled
