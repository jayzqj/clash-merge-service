"""Node display-name normalization.

Goal: every node shows a flag emoji in Clash Verge, in one uniform
shape ``{score}% {flag} {country} {rest}`` — e.g.
``9%-台湾 A01 高带宽优化`` becomes ``9% 🇹🇼 台湾 A01 高带宽优化``,
and a static node ``3%-US-静态31🇺🇸「梅萨」 168.158.231.49`` becomes
``3% 🇺🇸 美国 静态31 168.158.231.49``.

Rules (deliberately conservative — the ini's group regexes match on
substrings of these names, so normalization must never delete a word
a regex relies on):
- Static nodes (``{CC}-静态{N}{flag}「{city}」 {IP}``) are rebuilt as
  ``{flag} {country-CN} 静态{N} {IP}``: the country code becomes its
  Chinese name, the ``「city」`` annotation is dropped, the flag and
  the IP stay. ``静态`` itself is kept, so the backend groups and
  dialer-proxy detection still match.
- Ordinary nodes: if a flag emoji is already present the name is
  left alone; otherwise the flag is inserted immediately before the
  country word found in the name (``台湾`` -> ``🇹🇼 台湾``). All
  other words stay in place, so ini group regexes (including the
  ``$``-anchored front-group patterns) keep matching exactly as
  before.
- If the name carries no recognizable country, the purity record's
  country code (ippure ``countryCode`` of the exit IP) is used as a
  fallback and the flag is prepended, e.g. ``非洲约翰内斯堡`` with
  exit country ZA becomes ``🇿🇦 非洲约翰内斯堡``.
- The score prefix is NOT added here; ``merge.prefix_purity_names``
  adds ``{score}% `` (percent + space) afterwards. ``normalize_name``
  strips a leading score prefix first (``5%-`` or ``5% ``), so it is
  safe to run on already-prefixed names too.
"""
from __future__ import annotations

import re

# Country code -> Chinese name (short form, as used in node names).
CC_TO_CN = {
    "US": "美国", "TW": "台湾", "JP": "日本", "SG": "新加坡", "HK": "香港",
    "KR": "韩国", "IN": "印度", "AU": "澳大利亚", "CA": "加拿大",
    "GB": "英国", "DE": "德国", "FR": "法国", "NL": "荷兰", "ES": "西班牙",
    "BR": "巴西", "SA": "沙特", "PL": "波兰", "SK": "斯洛伐克",
    "MY": "马来西亚", "NO": "挪威", "SE": "瑞典", "ZA": "南非",
    "IT": "意大利", "VN": "越南", "TH": "泰国", "RU": "俄罗斯",
    "TR": "土耳其", "AE": "阿联酋", "PH": "菲律宾", "ID": "印尼",
    "MX": "墨西哥", "AR": "阿根廷", "CL": "智利", "EG": "埃及",
    "NG": "尼日利亚", "KE": "肯尼亚", "IL": "以色列", "UA": "乌克兰",
    "CZ": "捷克", "AT": "奥地利", "CH": "瑞士", "FI": "芬兰",
    "DK": "丹麦", "BE": "比利时", "IE": "爱尔兰", "PT": "葡萄牙",
    "GR": "希腊", "NZ": "新西兰",
}
# Chinese name -> country code (longest names first for matching).
CN_TO_CC = {cn: cc for cc, cn in CC_TO_CN.items()}
CN_TO_CC["印度尼西亚"] = "ID"
_CN_WORDS = sorted(CN_TO_CC, key=len, reverse=True)

_FLAG_RX = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_SCORE_RX = re.compile(r"^\d+%[- ]")
_STATIC_RX = re.compile(r"^([A-Z]{2})-静态(\d+)\s*(.*?)\s*$")
_IP_RX = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s*$")
_CITY_RX = re.compile(r"「[^」]*」")


def flag_for_cc(cc: str) -> str:
    """'US' -> '🇺🇸'. Any two ASCII letters work."""
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha() or not cc.isascii():
        return ""
    base = 0x1F1E6
    return chr(base + ord(cc[0]) - ord("A")) + chr(base + ord(cc[1]) - ord("A"))


def parse_score(name: str) -> int | None:
    """Score from a display name: accepts both the old ``5%-`` and
    the new ``5% `` prefix (re-apply reads files written by either
    version)."""
    m = re.match(r"^(\d+)%[- ]", str(name or ""))
    return int(m.group(1)) if m else None


def strip_score_prefix(name: str) -> str:
    return _SCORE_RX.sub("", str(name or ""), count=1)


def _normalize_static(base: str) -> str | None:
    """``US-静态31🇺🇸「梅萨」 168.158.231.49`` ->
    ``🇺🇸 美国 静态31 168.158.231.49``. Returns None when the name
    does not look like a static node."""
    m = _STATIC_RX.match(base)
    if not m:
        return None
    cc, num, tail = m.group(1), m.group(2), m.group(3)
    flag = _FLAG_RX.search(tail)
    flag = flag.group(0) if flag else flag_for_cc(cc)
    ip_m = _IP_RX.search(tail)
    ip = ip_m.group(1) if ip_m else ""
    cn = CC_TO_CN.get(cc, cc)
    out = f"{flag} {cn} 静态{num}" if flag else f"{cn} 静态{num}"
    return f"{out} {ip}" if ip else out


def normalize_name(name: str, country_code: str | None = None) -> str:
    """Normalize one node's base name (score prefix stripped first).
    Never raises: on anything unexpected the name is returned as-is
    (minus a score prefix)."""
    try:
        base = strip_score_prefix(name)
        if "静态" in base:
            rebuilt = _normalize_static(base)
            if rebuilt is not None:
                return rebuilt
        if _FLAG_RX.search(base):
            return base
        for word in _CN_WORDS:
            idx = base.find(word)
            if idx >= 0:
                flag = flag_for_cc(CN_TO_CC[word])
                if not flag:
                    return base
                return base[:idx] + flag + " " + base[idx:]
        if country_code:
            flag = flag_for_cc(country_code)
            if flag:
                return f"{flag} {base}"
        return base
    except Exception:  # noqa: BLE001 - a name must never break a run
        return strip_score_prefix(name)
