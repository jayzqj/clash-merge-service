"""The low-purity group: measured nodes with fraudScore <= 15
(excluding http-type nodes — the static chain nodes must not be
directly selectable), as a url-test group.

The group is NOT defined in the ini — its membership depends on the
measured scores, which the ini cannot see. It is built after
conversion, inserted right after ``🤖 国外AI`` (user's choice,
2026-11-13), and added as a member to ``🚀 手动选择`` and
``🤖 国外AI`` so it shows up as a selectable option in both.

``fill_linuxdo_group`` reuses the same member rule with its own
configurable threshold for the ini-defined linux.do url-test group.

The same builder serves the full run (scores from the purity round)
and re-apply (scores parsed back out of the display names with
``names.parse_score`` — it accepts both the old ``5%-`` and the new
``5% `` prefix).
"""
from __future__ import annotations

import logging

from app.names import parse_score

log = logging.getLogger(__name__)

GROUP_NAME = "🎯 纯净优选"
THRESHOLD = 15
PARENTS = ("🚀 手动选择", "🤖 国外AI")
TEST_URL = "https://cp.cloudflare.com/generate_204"
TEST_INTERVAL = 300
TEST_TOLERANCE = 300


def low_purity_members(proxies: list[dict],
                       scores_by_name: dict[str, int | None],
                       threshold: float = THRESHOLD) -> list[str]:
    """Final display names of nodes with a score <= threshold (default
    15), excluding http-type nodes. Input order is kept (the pipeline
    sorts by score ascending, so the purest come first)."""
    out = []
    for p in proxies:
        if str(p.get("type", "")).lower() == "http":
            continue
        score = scores_by_name.get(str(p.get("name", "")))
        if score is not None and score <= threshold:
            out.append(str(p["name"]))
    return out


def scores_from_names(proxies: list[dict]) -> dict[str, int | None]:
    """Scores parsed back out of display names (re-apply path)."""
    return {str(p.get("name", "")): parse_score(str(p.get("name", "")))
            for p in proxies}


def add_low_purity_group(doc: dict, proxies: list[dict],
                         scores_by_name: dict[str, int | None]) -> dict:
    """Build the group, insert it into doc["proxy-groups"] right
    after ``🤖 国外AI`` (after ``🚀 手动选择`` if 国外AI is missing,
    appended at the end if neither exists), and add its name to each
    parent group's member list. Returns stats for the run report."""
    members = low_purity_members(proxies, scores_by_name)
    group: dict = {"name": GROUP_NAME, "type": "url-test",
                   "proxies": members if members else ["REJECT"],
                   "url": TEST_URL, "interval": TEST_INTERVAL,
                   "tolerance": TEST_TOLERANCE}
    groups = doc.setdefault("proxy-groups", [])
    names = [str(g.get("name", "")) for g in groups]
    if GROUP_NAME in names:
        # Drop the old entry first so a re-insert also moves it
        # when the anchor changed (e.g. after 🚀 手动选择 -> after
        # 🤖 国外AI), instead of replacing it in place.
        groups.pop(names.index(GROUP_NAME))
        names = [str(g.get("name", "")) for g in groups]
    if "🤖 国外AI" in names:
        groups.insert(names.index("🤖 国外AI") + 1, group)
    elif "🚀 手动选择" in names:
        groups.insert(names.index("🚀 手动选择") + 1, group)
    else:
        groups.append(group)
    added_to = []
    for g in groups:
        if str(g.get("name", "")) in PARENTS:
            members_list = g.setdefault("proxies", [])
            if GROUP_NAME not in members_list:
                members_list.append(GROUP_NAME)
                added_to.append(str(g["name"]))
    return {"low_purity_group": GROUP_NAME,
            "low_purity_members": len(members),
            "low_purity_parents": added_to}


# linux.do group (docs/adr/0011): the ini defines the groups and rules;
# only the inner url-test group's members are filled here.
LINUXDO_KEYWORD = "LinuxDo"


def fill_linuxdo_group(doc: dict, proxies: list[dict],
                       scores_by_name: dict[str, int | None],
                       threshold: float) -> dict:
    """Replace the members of every url-test group whose name contains
    ``LinuxDo`` with the non-http nodes scoring <= threshold (REJECT
    when none qualify). Matched by name, like the backend groups, so
    the ini owns the name, position, test URL and rules. Select groups
    (the outer ``🐧 LinuxDo``) are left untouched. Returns scalar stats
    for the run report (the admin summary keeps only scalars)."""
    members = low_purity_members(proxies, scores_by_name, threshold)
    filled = []
    for g in doc.get("proxy-groups", []) or []:
        name = str(g.get("name", ""))
        if LINUXDO_KEYWORD in name and str(g.get("type", "")) == "url-test":
            g["proxies"] = list(members) if members else ["REJECT"]
            filled.append(name)
    if not filled:
        log.info("linuxdo: no url-test group named *%s* in the ini", LINUXDO_KEYWORD)
    return {"linuxdo_group": "、".join(filled),
            "linuxdo_members": len(members) if filled else 0,
            "linuxdo_threshold": threshold}
