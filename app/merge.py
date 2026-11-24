"""Merge + dedup + purity ordering.

Dedup key (per spec): server + port + password/UUID/path identity
fields. It must NOT be server+port alone: real subscriptions contain
several nodes sharing one server+port (e.g. Los Angeles 1/2, Tokyo
1-3, several vless nodes on one Cloudflare IP) and port-only dedup
would delete live nodes. The key is implemented as the whole proxy
dict minus ``name`` — see ``dedup_key`` for why a fixed field list
is not enough (vless nodes on a shared IP differ only in
``servername`` / ``ws-opts``).

Renaming: only ever appends a suffix to resolve a name clash; it
never rewrites the original name, so grouping keywords (静态, 家宽,
IEPL, country/region names, racknerd) are always preserved.
``prefix_purity_names`` additionally prepends the measured purity
score (``"{score}%-{name}"``) after the purity round; the prefix is
only a display prefix — every grouping regex in the ini searches
the name (``.*`` / substring match), so a leading ``"5%-"`` cannot
break group matching. Unmeasured nodes keep their original name
(no score to show); final names are de-duplicated again in case
two same-name nodes share one score.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import re

IP_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s*$")
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")


def internal_server(server: str) -> bool:
    """True when ``server`` is a LITERAL internal address (security
    review S6). A subscription node whose server points at the box
    itself or the LAN (e.g. ``127.0.0.1``, ``10.x``, ``::1``, or a
    non-standard numeric form like ``2130706433``) would, during the
    purity round, make mihomo open a connection to that address — a
    port scan of the VPS by proxy — and would then be published to
    clients. Names are NOT resolved here (that needs DNS and belongs to
    the fetch path); only literals are judged, matching
    ``fetcher.internal_host``."""
    host = (server or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return False  # a node with no server is dropped elsewhere
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Dotted all-numeric forms (2130706433, 0x7f.1) resolve to IPs.
        # Numeric forms (2130706433, 0x7f.1, 0177.0.0.1) resolve to IPs
        # too; a label of all-digits/hex is numeric, a real hostname has
        # at least one non-numeric label. Matches fetcher.internal_host.
        if all(_NUMERIC_LABEL.fullmatch(label) for label in host.split(".")):
            return True
        return False  # a real hostname: not judged here
    return not address.is_global or address.is_multicast


def unique_name(base: str, used: set[str]) -> str:
    """``base`` if free, else ``base (2)``, ``base (3)``... checking the
    name actually produced, so a node that is literally called
    ``HK (2)`` can never collide with a generated suffix."""
    if base not in used:
        return base
    index = 2
    while f"{base} ({index})" in used:
        index += 1
    return f"{base} ({index})"


def dedup_key(proxy: dict) -> str:
    """Identity of a node = every connection field except its display
    name (and dialer-proxy, which is injected later).

    The spec's key is "server + port + password/UUID/path and other
    identity fields". A fixed field list is not enough: verified
    2026-11-12 against the live subscriptions, the vless nodes sharing
    one Cloudflare IP differ ONLY in ``servername`` and
    ``ws-opts.path``/``Host`` — a key of server+port+uuid+top-level
    ``path`` would wrongly collapse 10 distinct nodes into 1 (their
    ws path lives in ``ws-opts``, not ``path``). Conversely the
    hysteria2 nodes sharing a server+port (LA 1/2, Tokyo 1-3) are
    byte-identical in every field except ``name`` — same password,
    sni and fingerprint — i.e. the same endpoint listed twice, so
    they correctly collapse to one node under this key as well as
    under the spec's key. Whole-dict-minus-name is the only key that
    gets both cases right; over-dedup kills live nodes, under-dedup
    only leaves a redundant entry, so the key errs toward keeping."""
    ident = {k: v for k, v in proxy.items()
             if k not in ("name", "dialer-proxy")}
    return json.dumps(ident, sort_keys=True, ensure_ascii=False, default=str)


def expected_ip(proxy: dict) -> str | None:
    """IP a node is expected to exit from: trailing IP in its name,
    else its server if the server itself is an IP. Nodes are
    identified by IP, never by country label (labels drift between
    runs; the old task once missed AU for exactly this reason)."""
    m = IP_RE.search(str(proxy.get("name", "")))
    if m:
        return m.group(1)
    server = str(proxy.get("server", ""))
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", server):
        return server
    return None


def merge_proxies(sources: list[tuple[str, list[dict]]],
                  trusted: frozenset[str] | set[str] = frozenset()) -> tuple[list[dict], dict]:
    """Merge proxies from ``[(source_id, proxies), ...]``.

    Returns (merged, stats). Duplicate identities are dropped (first
    occurrence wins); same-name/different-identity nodes get a
    `` (n)`` suffix appended, keywords intact.

    ``trusted`` holds source ids whose nodes may point at the box or the
    LAN: nodes the admin entered directly (share links / YAML) and local
    subscription URLs. Only nodes from a remote provider are excluded by
    ``internal_server`` (security review S6).
    """
    merged: list[dict] = []
    seen_keys: set[tuple] = set()
    used_names: set[str] = set()
    stats = {"total_in": 0, "dedup_removed": 0, "renamed": 0,
             "internal_server_excluded": 0, "internal_server_names": []}
    for source_id, proxies in sources:
        for p in proxies:
            stats["total_in"] += 1
            if source_id not in trusted and internal_server(str(p.get("server", ""))):
                stats["internal_server_excluded"] += 1
                if len(stats["internal_server_names"]) < 50:
                    stats["internal_server_names"].append(str(p.get("name", ""))[:80])
                continue
            key = dedup_key(p)
            if key in seen_keys:
                stats["dedup_removed"] += 1
                continue
            seen_keys.add(key)
            node = copy.deepcopy(p)
            node.pop("dialer-proxy", None)  # re-injected after conversion
            name = str(node.get("name", ""))
            final = unique_name(name, used_names)
            used_names.add(final)
            if final != name:
                node["name"] = final
                stats["renamed"] += 1
            merged.append(node)
    return merged, stats


def sort_by_purity(proxies: list[dict], purity: dict) -> list[dict]:
    """Order current/cached scores ascending, then nodes with no score.
    Optional threshold filtering is handled separately by the pipeline."""
    def sort_key(item: tuple[int, dict]):
        idx, p = item
        rec = purity.get(dedup_key(p))
        if rec and rec.get("score") is not None:
            return (0, rec["score"], idx)
        return (1, 0, idx)
    return [p for _i, p in sorted(enumerate(proxies), key=sort_key)]


def prefix_purity_names(proxies: list[dict], purity: dict) -> tuple[list[dict], dict]:
    """Prepend the measured purity score to each node's display name:
    ``"日本02 三网高速"`` with fraudScore 5 becomes
    ``"5% 🇹🇼 日本02 三网高速"`` (the base name is normalized by
    ``names.normalize_name`` before this runs, so the flag and the
    Chinese country name are already in place). Cached scores also get
    a prefix; nodes with no score keep their name. Run AFTER the round (records
    are keyed by the pre-rename identity) and BEFORE conversion, so
    grouping and dialer-proxy injection see the final names. Final
    names are made unique: two same-name nodes with the same score
    get a ``" (n)"`` suffix on the later one.
    """
    stats = {"purity_prefixed": 0, "unmeasured_kept": 0, "purity_reused": 0}
    used_names: set[str] = set()
    out: list[dict] = []
    for p in proxies:
        node = copy.deepcopy(p)
        rec = purity.get(dedup_key(p))
        if rec and rec.get("score") is not None:
            node["name"] = f"{int(rec['score'])}% {node.get('name', '')}"
            stats["purity_prefixed"] += 1
            if rec.get("score_source") == "history":
                stats["purity_reused"] += 1
        else:
            stats["unmeasured_kept"] += 1
        node["name"] = unique_name(str(node.get("name", "")), used_names)
        used_names.add(node["name"])
        out.append(node)
    return out, stats
