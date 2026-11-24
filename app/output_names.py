"""Append IPv4 after grouping, preserving regex matches and all node references."""
import asyncio
import ipaddress
import logging
import re
import socket
from app.fetcher import FetchError
from app.merge import dedup_key, expected_ip

log = logging.getLogger(__name__)

# mihomo built-in outbounds; a proxy must not take one of these names.
BUILTIN_OUTBOUNDS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}


def ipv4(value):
    try:
        address = ipaddress.ip_address(str(value))
        return str(address) if address.version == 4 else None
    except ValueError:
        return None


async def append_ipv4_names(doc, records):
    semaphore = asyncio.Semaphore(8)
    cache = {}

    async def resolve(host):
        async with semaphore:
            try:
                answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                    host, None, family=socket.AF_INET, type=socket.SOCK_STREAM), timeout=3)
                return answers[0][4][0] if answers else None
            except (OSError, asyncio.TimeoutError):
                return None

    async def address(p):
        rec = records.get(dedup_key(p), {})
        # A current measured exit is more informative than a CDN entry address.
        value = ipv4(rec.get("exit_ip")) if rec.get("measured") else None
        value = value or ipv4(expected_ip(p)) or ipv4(p.get("server"))
        if value: return value
        host = str(p.get("server", ""))
        try:
            ipaddress.ip_address(host)
            return None  # IPv6 literal: do not append it or resolve it.
        except ValueError:
            pass
        if host not in cache: cache[host] = asyncio.create_task(resolve(host))
        return await cache[host]

    nodes = doc.get("proxies", [])
    old_names = [p["name"] for p in nodes]
    if len(set(old_names)) != len(old_names):
        # Earlier steps guarantee unique names. If that ever breaks, the
        # name -> name mapping below would silently drop a node from its
        # groups, so fail the round loudly instead.
        seen, dups = set(), []
        for name in old_names:
            if name in seen and name not in dups:
                dups.append(name)
            seen.add(name)
        raise FetchError("names", "节点名称重复：" + "、".join(dups[:5]))
    # Names a proxy may not take: groups and built-ins. References to them
    # in groups / dialer-proxy keep pointing at the group or built-in.
    reserved = BUILTIN_OUTBOUNDS | {str(g.get("name", "")) for g in doc.get("proxy-groups", [])}
    addresses = await asyncio.gather(*(address(p) for p in nodes))
    mapping, used, conflicts = {}, set(reserved), 0
    for p, ip in zip(nodes, addresses):
        old = p["name"]
        # Strip only an existing trailing IPv4, preserving grouping keywords.
        base = re.sub(r"\s+\d+\.\d+\.\d+\.\d+$", "", old) if ip else old
        name = f"{base} {ip}" if ip else base
        index = 2
        while name in used:
            name = f"{base} ({index})" + (f" {ip}" if ip else "")
            index += 1
        used.add(name)
        if old in reserved:
            conflicts += 1
            log.warning("node name %r equals a group or built-in name; renamed to %r", old, name)
        else:
            mapping[old] = name
        p["name"] = name
    for p in nodes:
        if p.get("dialer-proxy") in mapping:
            p["dialer-proxy"] = mapping[p["dialer-proxy"]]
    for group in doc.get("proxy-groups", []):
        if "proxies" in group:
            group["proxies"] = [mapping.get(name, name) for name in group["proxies"]]
    return {"ipv4_named": sum(ip is not None for ip in addresses),
            "ipv4_unavailable": sum(ip is None for ip in addresses),
            "name_conflicts_renamed": conflicts}
