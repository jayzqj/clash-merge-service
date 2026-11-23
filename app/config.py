"""Persistent configuration.

Config lives in a single JSON file (default ``config.json`` next to the
repo root, override with env ``CLASH_MERGE_CONFIG``). It is NEVER
committed: real subscription URLs carry tokens and the webhook URL is
sensitive, so ``config.json`` is gitignored and only
``config.example.json`` (placeholders) is in the repo.

Admin credentials are NOT stored here. They come from the environment
(``ADMIN_USER`` / ``ADMIN_PASSWORD``, or ``ADMIN_PASSWORD_FILE``
pointing at a file containing the password) — see ``app/web.py``.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from app.runtime import defaults_for_platform, desktop_mode, desktop_data_dir

# Upper bounds enforced by validate(). 8 mihomo instances already
# saturate a 2 GB VPS under MemoryMax=512M; a day is the longest
# sensible interval.
MAX_PURITY_WORKERS = 8
MAX_INTERVAL_SECONDS = 86400

DEFAULTS: dict = {
    "subscriptions": [],
    "interval_seconds": 3600,
    "execution_start": "08:00",
    "execution_end": "23:59",
    "notify_enabled": True,
    "wechat_webhook": "",
    "ini_url": "https://gist.githubusercontent.com/jayzqj/da3509f0267cbc7c8d55fe74e522d0d2/raw/custom_whitelist_jay5.ini",
    "subconverter_base": "https://suburl.v1.mk/",
    "use_remote_subconverter": False,
    "merged_public_url": "",
    "output_path": "/var/www/xiuyuan/jay_filtered.yaml",
    "purity_enabled": True,
    "purity_filter_enabled": False,
    "purity_threshold": 15,
    "purity_info_url": "https://my.123169.xyz/v1/info",
    "purity_workers": 1,
    # Batch vs sequential connectivity testing; both are mandatory.
    "purity_delay_screen": True,
    "purity_delay_url": "https://cp.cloudflare.com/generate_204",  # legacy field
    # Optional extra front candidate. It must pass the same tests as subscription nodes.
    "check_front": None,
    "check_front_enabled": False,
    "mihomo_bin": "mihomo",
    "mihomo_workdir": "/opt/clash-merge-service/mihomo",
    "mihomo_mixed_port": 7890,
    "mihomo_controller_port": 9090,
    "min_interval_seconds": 300,
    "fetch_proxy_url": "",
    "fetch_direct_fallback": True,
    "connectivity_url": "https://cp.cloudflare.com/generate_204",
    "connectivity_timeout_ms": 10000,
    "connectivity_rounds": 3,
    "excluded_protocols": [],
}

# The dialer-proxy target group name — FALLBACK only. The ini
# governs the real name (see app/dialer.py::front_group_from_ini):
# the pipeline takes the ini's (single) 前置 group, so renaming it
# on gist just works. This constant is used only when the ini has
# zero or several 前置 groups.
FRONT_GROUP = "🚪 ISP链式前置分组"

# Node-name keywords that group matching depends on. Renaming must
# preserve them (see app/merge.py).
KEYWORDS = ["静态", "家宽", "IEPL", "racknerd"]


def default_config_path() -> Path:
    env = os.environ.get("CLASH_MERGE_CONFIG")
    if env:
        return Path(env)
    if desktop_mode():
        return desktop_data_dir() / "config.json"
    return Path(__file__).resolve().parent.parent / "config.json"


def mask_webhook(url: str) -> str:
    return "set" if url else ""


def mask_check_front(front) -> dict:
    """check_front for display/API: identity only, never secrets
    (password, uuid, token, private key)."""
    if not isinstance(front, dict):
        return {}
    return {"configured": True,
            "name": str(front.get("name", "")),
            "type": str(front.get("type", "")),
            "server": str(front.get("server", "")),
            "port": front.get("port", "")}


@dataclass
class Config:
    data: dict = field(default_factory=lambda: defaults_for_platform(DEFAULTS))
    path: Path = field(default_factory=default_config_path)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = Path(path) if path else default_config_path()
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            merged = defaults_for_platform(DEFAULTS)
            merged.update(raw)
            return cls(data=merged, path=p)
        return cls(data=defaults_for_platform(DEFAULTS), path=p)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # -- accessors ------------------------------------------------------
    @property
    def subscriptions(self) -> list[dict]:
        return self.data.get("subscriptions", [])

    @property
    def enabled_subscriptions(self) -> list[dict]:
        return [s for s in self.subscriptions if s.get("enabled", True)]

    @property
    def interval_seconds(self) -> int:
        return int(self.data.get("interval_seconds", 3600))

    @property
    def notify_enabled(self) -> bool:
        return bool(self.data.get("notify_enabled", True))

    @property
    def wechat_webhook(self) -> str:
        return self.data.get("wechat_webhook", "")

    @property
    def ini_url(self) -> str:
        return self.data.get("ini_url", "")

    @property
    def subconverter_base(self) -> str:
        return (self.data.get("subconverter_base") or "https://suburl.v1.mk/").rstrip("/") + "/"

    @property
    def output_path(self) -> str:
        return self.data.get("output_path", "")

    @property
    def min_interval_seconds(self) -> int:
        return int(self.data.get("min_interval_seconds", 300))

    @property
    def purity_workers(self) -> int:
        try:
            return max(1, min(int(self.data.get("purity_workers", 1) or 1),
                              MAX_PURITY_WORKERS))
        except (TypeError, ValueError):
            return 1

    @property
    def check_front(self) -> dict | None:
        front = self.data.get("check_front")
        return front if isinstance(front, dict) else None

    @property
    def check_front_enabled(self) -> bool:
        return bool(self.data.get("check_front_enabled", False))

    # -- validation -----------------------------------------------------
    def validate(self) -> list[str]:
        errors: list[str] = []
        for key, default in (("execution_start", "08:00"), ("execution_end", "23:59")):
            value = self.data.get(key, default)
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                errors.append(f"{key} 必须是 HH:MM 格式的时间（00:00–23:59）")
        if self.data.get("execution_start", "08:00") == self.data.get("execution_end", "23:59"):
            errors.append("执行时段的开始和结束时间不能相同；全天执行请设置 00:00–23:59")
        try:
            interval = int(self.data.get("interval_seconds", 3600))
        except (TypeError, ValueError):
            interval = 0
        if interval < self.min_interval_seconds:
            errors.append(
                f"interval_seconds must be >= {self.min_interval_seconds} "
                f"(a full purity round over ~270 nodes takes tens of minutes)")
        elif interval > MAX_INTERVAL_SECONDS:
            errors.append(
                f"interval_seconds must be <= {MAX_INTERVAL_SECONDS} (1 day)")
        try:
            workers = int(self.data.get("purity_workers", 1) or 1)
        except (TypeError, ValueError):
            workers = 0
        if not 1 <= workers <= MAX_PURITY_WORKERS:
            errors.append(
                f"purity_workers must be between 1 and {MAX_PURITY_WORKERS}")
        threshold = self.data.get("purity_threshold", 15)
        if (isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold) or not 0 <= threshold <= 100):
            errors.append("纯净度预留必须是 0–100 之间的数字")
        if (self.data.get("purity_filter_enabled", False)
                and not self.data.get("purity_enabled", True)):
            errors.append("开启纯净度过滤时必须同时启用纯净度检测")
        front = self.data.get("check_front")
        if front is not None:
            if not isinstance(front, dict):
                errors.append("check_front must be a proxy object or null")
            else:
                for key in ("name", "type", "server", "port"):
                    if not front.get(key):
                        errors.append(f"check_front is missing {key}")
        if self.check_front_enabled and not isinstance(front, dict):
            errors.append("check_front_enabled is on but check_front is not "
                          "configured in config.json")
        for s in self.subscriptions:
            url = (s.get("url") or "").strip()
            if not url:
                errors.append(f"subscription {s.get('id', '?')}: empty URL")
            else:
                from app.node_links import validate_source
                try:
                    validate_source(url)
                except ValueError as exc:
                    errors.append(f"subscription {s.get('id', '?')}: {exc}")
        if self.notify_enabled and not self.wechat_webhook:
            errors.append("notify_enabled is on but wechat_webhook is empty")
        if not self.ini_url.startswith("https://"):
            errors.append("ini_url must be an https URL")
        if not self.output_path:
            errors.append("output_path is empty")
        proxy = self.data.get("fetch_proxy_url", "")
        if not re.fullmatch(r"https?://\S+", str(self.data.get("connectivity_url", ""))):
            errors.append("连通性测试地址须为 http:// 或 https:// 地址")
        timeout = self.data.get("connectivity_timeout_ms", 10000)
        rounds = self.data.get("connectivity_rounds", 3)
        if type(rounds) is not int or rounds not in (3, 5):
            errors.append("连通性检测次数须为 3 或 5")
        protocols = self.data.get("excluded_protocols", [])
        if not isinstance(protocols, list) or any(not isinstance(p, str) or not re.fullmatch(r"[a-z0-9-]{1,32}", p) for p in protocols):
            errors.append("排除协议须为协议名称列表")
        if type(timeout) is not int or not 1000 <= timeout <= 30000:
            errors.append("连通性超时必须为 1000–30000 毫秒")
        if proxy and not re.fullmatch(r"https?://\S+", proxy):
            errors.append("订阅拉取代理须为 http:// 或 https:// 地址，留空表示直连")
        return errors

    def public_view(self) -> dict:
        """Config as returned from the admin API (login required).

        Subscription URLs are shown IN FULL, including tokens, because
        the user asked for it (2026-11-12): the admin page is only
        reachable after HTTP Basic login as the admin user, so the
        full addresses are only visible to the admin. The Enterprise
        WeChat webhook is still never echoed (shown only as "set"/"").
        """
        view = copy.deepcopy(self.data)
        view["wechat_webhook"] = mask_webhook(view.get("wechat_webhook", ""))
        # The check front carries its node's password: never echo it.
        view["check_front"] = mask_check_front(view.get("check_front"))
        return view

    def new_subscription_id(self) -> str:
        return "sub-" + uuid.uuid4().hex[:8]
