"""Append IPv4 after grouping, preserving regex matches and all node references."""
import asyncio
import ipaddress
import re
import socket
from app.merge import dedup_key, expected_ip


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
    addresses = await asyncio.gather(*(address(p) for p in nodes))
    mapping, used = {}, set()
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
        mapping[old] = name
        p["name"] = name
    for p in nodes:
        if p.get("dialer-proxy") in mapping:
            p["dialer-proxy"] = mapping[p["dialer-proxy"]]
    for group in doc.get("proxy-groups", []):
        if "proxies" in group:
            group["proxies"] = [mapping.get(name, name) for name in group["proxies"]]
    return {"ipv4_named": sum(ip is not None for ip in addresses),
            "ipv4_unavailable": sum(ip is None for ip in addresses)}
