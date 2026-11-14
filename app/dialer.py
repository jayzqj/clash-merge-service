"""dialer-proxy injection (AFTER subconverter conversion).

Rules (corrected per spec — the old "UDP nodes can't be front" claim
is wrong and discarded):
- hysteria2 CAN be a dialer-proxy target (front / first hop). The
  user's live chain uses a Japan hysteria2 node as front.
- hysteria2 itself CANNOT carry dialer-proxy: QUIC runs on UDP and
  cannot ride a TCP stream; mihomo silently skips it. So hysteria2
  is first-hop only.
- Backend = node name contains "静态". TCP backend nodes get
  ``dialer-proxy: <front group>`` (the ini's 前置 group — see
  ``front_group_from_ini``; the ini governs the name, the FRONT_GROUP
  constant below is only a fallback). UDP backend nodes
  (hysteria2 / tuic / wireguard) get none — direct only. Front and
  ordinary nodes get none.

Also fills the ini's REJECT-placeholder backend groups (any group
whose name contains "后置分组") with the backend node names,
because the ini leaves them as ``select []REJECT`` for "the
local script" to replace.
"""
from __future__ import annotations

import logging

from app.config import FRONT_GROUP

log = logging.getLogger(__name__)

UDP_TYPES = {"hysteria2", "tuic", "wireguard"}


def front_group_from_ini(ini) -> str:
    """The dialer-proxy target group, taken from the ini: the (single)
    group whose name contains ``前置``. The ini governs the name —
    the user renamed ``🚪 ISP链式前置分组`` to ``🚪 链式代理前置分组``
    on gist and the hardcoded constant went stale, leaving all
    backends pointing at a group that no longer exists in the file.
    Falls back to the FRONT_GROUP constant (and logs a warning) when
    the ini has zero or several 前置 groups."""
    candidates = [g.name for g in getattr(ini, "groups", [])
                  if "前置" in str(g.name)]
    if len(candidates) == 1:
        return candidates[0]
    log.warning("dialer: %d front groups in ini (%s), falling back to %r",
                len(candidates), candidates, FRONT_GROUP)
    return FRONT_GROUP


def is_backend(proxy: dict) -> bool:
    return "静态" in str(proxy.get("name", ""))


def is_udp(proxy: dict) -> bool:
    return str(proxy.get("type", "")).lower() in UDP_TYPES


def inject_dialer(proxies: list[dict], front_group: str = FRONT_GROUP) -> dict:
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
            p["dialer-proxy"] = front_group
    return stats


def fill_backend_groups(doc: dict, proxies: list[dict]) -> dict:
    """Replace the REJECT placeholder in backend groups with the
    backend node names. A backend group is any group in the doc whose
    name contains ``后置分组`` — matched by name, not by a hardcoded
    list, so renaming one in the ini (``🎯 ISP链式后置分组`` ->
    ``🎯 链式代理后置分组``) keeps working. Returns
    {group_name: member_count}."""
    backend_names = [str(p["name"]) for p in proxies if is_backend(p)]
    filled: dict[str, int] = {}
    for group in doc.get("proxy-groups", []) or []:
        if "后置分组" in str(group.get("name", "")):
            group["proxies"] = list(backend_names) if backend_names else ["REJECT"]
            group["type"] = "select"
            filled[group["name"]] = len(backend_names)
    return filled
