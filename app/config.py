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
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

DEFAULTS: dict = {
    "subscriptions": [],
    "interval_seconds": 3600,
    "notify_enabled": True,
    "wechat_webhook": "",
    "ini_url": "https://gist.githubusercontent.com/jayzqj/da3509f0267cbc7c8d55fe74e522d0d2/raw/custom_whitelist_jay5.ini",
    "subconverter_base": "https://suburl.v1.mk/",
    "use_remote_subconverter": False,
    "merged_public_url": "",
    "output_path": "/var/www/xiuyuan/jay_filtered.yaml",
    "purity_enabled": True,
    "purity_info_url": "https://my.123169.xyz/v1/info",
    "mihomo_bin": "mihomo",
    "mihomo_workdir": "/opt/clash-merge-service/mihomo",
    "mihomo_mixed_port": 7890,
    "mihomo_controller_port": 9090,
    "min_interval_seconds": 300,
}

# The dialer-proxy target group name. Must match the ini exactly:
# emoji U+1F6AA + one space + "ISP链式前置分组" (no spaces inside).
FRONT_GROUP = "🚪 ISP链式前置分组"
# Groups that the ini leaves as a REJECT placeholder and this service
# fills with the backend (静态) node names after conversion.
BACKEND_GROUPS = ["🎯 ISP链式后置分组", "🎯 链式代理后置分组"]

# Node-name keywords that group matching depends on. Renaming must
# preserve them (see app/merge.py).
KEYWORDS = ["静态", "家宽", "IEPL", "racknerd"]


def default_config_path() -> Path:
    env = os.environ.get("CLASH_MERGE_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config.json"


def mask_url(url: str) -> str:
    """Hide credential material in a URL for display/API responses.

    Keeps scheme + host + path, drops query values (tokens) and any
    userinfo. Never returns the raw URL.
    """
    if not url:
        return ""
    try:
        p = urlparse(url)
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        return urlunparse((p.scheme, netloc, p.path, "", "", ""))
    except Exception:
        return "<unparseable-url>"


def mask_webhook(url: str) -> str:
    if not url:
        return ""
    return "set" if url else ""


@dataclass
class Config:
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path = field(default_factory=default_config_path)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = Path(path) if path else default_config_path()
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            merged = copy.deepcopy(DEFAULTS)
            merged.update(raw)
            return cls(data=merged, path=p)
        return cls(data=copy.deepcopy(DEFAULTS), path=p)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
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

    # -- validation -----------------------------------------------------
    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.interval_seconds < self.min_interval_seconds:
            errors.append(
                f"interval_seconds must be >= {self.min_interval_seconds} "
                f"(a full purity round over ~270 nodes takes tens of minutes)")
        for s in self.subscriptions:
            url = (s.get("url") or "").strip()
            if not url:
                errors.append(f"subscription {s.get('id', '?')}: empty URL")
            elif not re.match(r"^https?://\S+$", url):
                errors.append(f"subscription {s.get('id', '?')}: not an http(s) URL")
        if self.notify_enabled and not self.wechat_webhook:
            errors.append("notify_enabled is on but wechat_webhook is empty")
        if not self.ini_url.startswith("https://"):
            errors.append("ini_url must be an https URL")
        if not self.output_path:
            errors.append("output_path is empty")
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
        return view

    def new_subscription_id(self) -> str:
        return "sub-" + uuid.uuid4().hex[:8]
