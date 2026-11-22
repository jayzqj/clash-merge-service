"""Current-round connectivity is mandatory; score history cannot override it."""
from app.merge import dedup_key
from app.dialer import is_backend, is_udp


def filter_connected(proxies, results):
    kept = [p for p in proxies if results.get(dedup_key(p), {}).get("connected") is True]
    failures = [{"name": p.get("name", ""),
                 "reason": results.get(dedup_key(p), {}).get("connectivity_error", "本轮未完成连通验证")}
                for p in proxies if results.get(dedup_key(p), {}).get("connected") is not True]
    return kept, {"connectivity_total": len(proxies), "connectivity_passed": len(kept),
                  "connectivity_excluded": len(proxies) - len(kept), "connectivity_failures": failures}


def retain_chain_dependencies(proxies, results):
    keys = {dedup_key(p) for p in proxies}
    kept = []
    for p in proxies:
        if is_backend(p) and not is_udp(p):
            front = results.get(dedup_key(p), {}).get("front_key")
            if front not in keys:
                continue
        kept.append(p)
    return kept, len(proxies) - len(kept)


def pin_tested_chains(proxies, results):
    names = {dedup_key(p): p["name"] for p in proxies}
    chains = []
    for p in proxies:
        if is_backend(p) and not is_udp(p):
            rec = results.get(dedup_key(p), {})
            front = names.get(rec.get("front_key"))
            if not front:
                raise ValueError("后置节点缺少本轮已测通的前置")
            p["dialer-proxy"] = front
            chains.append({"node": p["name"], "front": front})
    return chains
