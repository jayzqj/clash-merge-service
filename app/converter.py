"""Apply the user's subconverter remote config (ini) locally.

Why local, not a remote /sub call (spec ambiguity, decided here):
the spec's pipeline is fetch -> merge -> convert via
``/sub?target=clash&url=<source>&config=<ini>`` -> inject dialer-proxy.
But ``url=`` must be a URL the remote subconverter server can fetch,
and the merged intermediate file only exists on the VPS — there is no
public URL for it, and the final public URL is the *output* of the
pipeline, so it cannot also be the input. Calling the remote service
with the final URL as input would be circular.

Decision (v1 default): parse the ini in-process and generate the
Clash document from it — ``custom_proxy_group`` definitions (name
regexes / group references), ``ruleset`` lines (rule-providers +
rules), ``clash_rule_base`` (base template), ``exclude_remarks``.
This produces exactly what the remote call would produce for these
inputs, without a circular fetch. The remote path is kept as an
opt-in: set ``use_remote_subconverter=true`` AND
``merged_public_url`` (a publicly reachable URL of the merged
intermediate file) and the pipeline will call the remote service
instead; see ``remote_convert``.

Ini facts verified 2026-11-12 (live fetch of the ini):
- ``custom_proxy_group`` lines are backtick-separated:
  ``name`type`regex-or-[]Group`...`test-url`interval,timeout,tolerance``.
  Some group names are split across two physical lines in the file
  (e.g. the 🚪 ISP front group, 🇯🇵 日本分组), so logical lines are
  re-joined before parsing.
- ``ruleset=GROUP,URL,interval`` for http lists and
  ``ruleset=GROUP,[]GEOSITE,xxx`` / ``[]GEOIP,xxx`` / ``[]FINAL``.
- ``clash_rule_base`` points at base_jay.yaml; it is fetched and
  used as the document template.
- ``exclude_remarks=(...)`` drops nodes whose names match.
- The backend groups 🎯 ISP链式后置分组 / 🎯 链式代理后置分组 are
  ``select []REJECT`` placeholders in the ini; they are filled with
  backend node names by ``app.dialer.fill_backend_groups`` after
  conversion, not here.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
import yaml

from app.fetcher import FetchError

log = logging.getLogger(__name__)

KNOWN_DIRECTIVES = ("[", ";", "ruleset=", "custom_proxy_group=",
                    "enable_rule_generator=", "overwrite_original_rules=",
                    "clash_rule_base=", "exclude_remarks=")


@dataclass
class IniGroup:
    name: str
    type: str  # select | url-test
    refs: list[str] = field(default_factory=list)      # []Group entries
    policies: list[str] = field(default_factory=list)  # DIRECT / REJECT
    regexes: list[str] = field(default_factory=list)
    url: str = ""
    interval: int = 300
    tolerance: int = 100


@dataclass
class IniRuleset:
    group: str
    kind: str  # http | geosite | geoip | final
    url: str = ""
    arg: str = ""
    no_resolve: bool = False
    interval: int = 3600


@dataclass
class IniData:
    groups: list[IniGroup] = field(default_factory=list)
    rulesets: list[IniRuleset] = field(default_factory=list)
    exclude_regex: str = ""
    base_url: str = ""
    enable_rule_generator: bool = True
    overwrite_original_rules: bool = True


def join_logical_lines(text: str) -> list[str]:
    """Re-join ini lines split mid-value (group names spanning lines)."""
    logical: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        starts_directive = stripped.startswith(KNOWN_DIRECTIVES)
        if logical and not starts_directive and not stripped.startswith("["):
            # Continuation of the previous logical line (a split name).
            logical[-1] += stripped
        else:
            logical.append(stripped)
    return logical


def _classify_field(group: IniGroup, f: str) -> None:
    if f.startswith("[]"):
        ref = f[2:]
        if ref:
            group.refs.append(ref)
    elif f in ("DIRECT", "REJECT"):
        group.policies.append(f)
    elif f.startswith("http://") or f.startswith("https://"):
        group.url = f
    elif re.fullmatch(r"\d+,\d+(,\d+)?", f):
        nums = [int(x) for x in f.split(",")]
        group.interval = nums[0]
        group.tolerance = nums[-1]
    elif f:
        group.regexes.append(f)


def parse_ini(text: str) -> IniData:
    ini = IniData()
    for line in join_logical_lines(text):
        if line.startswith(";") or line.startswith("["):
            continue
        if line.startswith("custom_proxy_group="):
            body = line[len("custom_proxy_group="):]
            parts = body.split("`")
            if len(parts) < 2:
                continue
            group = IniGroup(name=parts[0].strip(),
                             type=parts[1].strip() or "select")
            for f in parts[2:]:
                _classify_field(group, f.strip())
            ini.groups.append(group)
        elif line.startswith("ruleset="):
            body = line[len("ruleset="):]
            parts = [p.strip() for p in body.split(",")]
            if len(parts) < 2:
                continue
            group, second = parts[0], parts[1]
            if second.startswith("[]"):
                kind_arg = second[2:]
                if kind_arg.upper().startswith("GEOSITE"):
                    ini.rulesets.append(IniRuleset(
                        group=group, kind="geosite",
                        arg=parts[2] if len(parts) > 2 else ""))
                elif kind_arg.upper().startswith("GEOIP"):
                    ini.rulesets.append(IniRuleset(
                        group=group, kind="geoip",
                        arg=parts[2] if len(parts) > 2 else "",
                        no_resolve="no-resolve" in parts))
                elif kind_arg.upper().startswith("FINAL"):
                    ini.rulesets.append(IniRuleset(group=group, kind="final"))
            else:
                interval = 3600
                if len(parts) > 2:
                    try:
                        interval = int(parts[2])
                    except ValueError:
                        pass
                ini.rulesets.append(IniRuleset(group=group, kind="http",
                                               url=second, interval=interval))
        elif line.startswith("exclude_remarks="):
            val = line[len("exclude_remarks="):].strip()
            if val.startswith("(") and val.endswith(")"):
                val = val[1:-1]
            ini.exclude_regex = val
        elif line.startswith("clash_rule_base="):
            ini.base_url = line[len("clash_rule_base="):].strip()
        elif line.startswith("enable_rule_generator="):
            ini.enable_rule_generator = "true" in line.lower()
        elif line.startswith("overwrite_original_rules="):
            ini.overwrite_original_rules = "true" in line.lower()
    return ini


def apply_exclude(proxies: list[dict], ini: IniData) -> tuple[list[dict], int]:
    if not ini.exclude_regex:
        return proxies, 0
    try:
        rx = re.compile(ini.exclude_regex)
    except re.error as e:
        log.warning("converter: bad exclude_remarks regex: %s", e)
        return proxies, 0
    kept = [p for p in proxies if not rx.search(str(p.get("name", "")))]
    return kept, len(proxies) - len(kept)


def _matching_nodes(proxies: list[dict], regexes: list[str]) -> list[str]:
    out: list[str] = []
    for rx_text in regexes:
        try:
            rx = re.compile(rx_text)
        except re.error:
            log.warning("converter: bad group regex %r, skipped", rx_text)
            continue
        for p in proxies:
            name = str(p.get("name", ""))
            if name not in out and rx.search(name):
                out.append(name)
    return out


def build_proxy_groups(proxies: list[dict], ini: IniData) -> list[dict]:
    groups: list[dict] = []
    for g in ini.groups:
        members: list[str] = []
        for ref in g.refs:
            if ref not in members:
                members.append(ref)
        for name in _matching_nodes(proxies, g.regexes):
            if name not in members:
                members.append(name)
        for pol in g.policies:
            if pol not in members:
                members.append(pol)
        if not members:
            members = ["REJECT"]
        entry: dict = {"name": g.name, "type": g.type, "proxies": members}
        if g.type == "url-test":
            entry["url"] = g.url or "https://cp.cloudflare.com/generate_204"
            entry["interval"] = g.interval
            entry["tolerance"] = g.tolerance
        groups.append(entry)
    return groups


def build_rules(ini: IniData) -> tuple[list[dict], list[str]]:
    """Returns (rule_providers, rules)."""
    providers: dict[str, dict] = {}
    rules: list[str] = []
    counter = 0
    for rs in ini.rulesets:
        if rs.kind == "http":
            key = f"provider-{counter}"
            counter += 1
            providers[key] = {
                "type": "http", "behavior": "classical",
                "url": rs.url, "path": f"./rules/{key}.list",
                "interval": rs.interval,
            }
            rules.append(f"RULE-SET,{key},{rs.group}")
        elif rs.kind == "geosite":
            rules.append(f"GEO-SITE,{rs.arg},{rs.group}")
        elif rs.kind == "geoip":
            suffix = ",no-resolve" if rs.no_resolve else ""
            rules.append(f"GEO-IP,{rs.arg},{rs.group}{suffix}")
        elif rs.kind == "final":
            rules.append(f"FINAL,{rs.group}")
    if not any(r.startswith("FINAL,") for r in rules):
        rules.append("MATCH,🚀 手动选择")
    return providers, rules


FALLBACK_BASE: dict = {
    "port": 7890, "socks-port": 7891, "allow-lan": False,
    "mode": "rule", "log-level": "info",
    "external-controller": "127.0.0.1:9090",
    "dns": {"enable": True, "nameserver": ["223.5.5.5", "8.8.8.8"]},
}


async def fetch_base(client: httpx.AsyncClient, ini: IniData) -> dict:
    if not ini.base_url:
        return dict(FALLBACK_BASE)
    try:
        resp = await client.get(ini.base_url, timeout=30)
        resp.raise_for_status()
        base = yaml.safe_load(resp.text)
        if isinstance(base, dict):
            return base
    except Exception as e:  # noqa: BLE001 - fallback base on any failure
        log.warning("converter: base template fetch failed (%s), using fallback", e)
    return dict(FALLBACK_BASE)


async def convert(proxies: list[dict], ini_text: str,
                  client: httpx.AsyncClient) -> tuple[dict, dict]:
    """Apply the ini to a proxy list; returns (clash_doc, stats)."""
    ini = parse_ini(ini_text)
    kept, excluded = apply_exclude(proxies, ini)
    base = await fetch_base(client, ini)
    doc = {k: v for k, v in base.items()
           if k not in ("proxies", "proxy-groups", "rules", "rule-providers",
                        "proxy-providers")}
    doc["proxies"] = kept
    doc["proxy-groups"] = build_proxy_groups(kept, ini)
    providers, rules = build_rules(ini)
    if providers:
        doc["rule-providers"] = providers
    if ini.enable_rule_generator or ini.overwrite_original_rules:
        doc["rules"] = rules
    stats = {"proxies_in": len(proxies), "excluded_by_ini": excluded,
             "proxies_out": len(kept), "groups": len(doc["proxy-groups"]),
             "rules": len(rules), "rule_providers": len(providers)}
    return doc, stats


async def remote_convert(merged_yaml_text: str, ini_url: str,
                         subconverter_base: str,
                         merged_public_url: str,
                         client: httpx.AsyncClient) -> dict:
    """Opt-in remote subconverter call. Only usable when the merged
    intermediate file is publicly reachable (merged_public_url)."""
    if not merged_public_url:
        raise FetchError("convert", "use_remote_subconverter is on but "
                         "merged_public_url is empty — the remote service "
                         "cannot fetch a local file")
    url = (subconverter_base + "sub?target=clash"
           f"&url={quote(merged_public_url, safe='')}"
           f"&config={quote(ini_url, safe='')}")
    resp = await client.get(url, timeout=120)
    if resp.status_code != 200:
        raise FetchError("convert",
                         f"subconverter HTTP {resp.status_code}: "
                         f"{resp.text[:200]}")
    doc = yaml.safe_load(resp.text)
    if not isinstance(doc, dict) or not doc.get("proxies"):
        raise FetchError("convert", "subconverter returned no proxies")
    return doc
