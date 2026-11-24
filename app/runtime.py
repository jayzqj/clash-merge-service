"""Platform paths, the published-output location and the subscription token."""
import os
import re
import secrets
import sys
from pathlib import Path

TOKEN_FILE = "subscription.token"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def desktop_mode() -> bool:
    return os.environ.get("CLASH_MERGE_DESKTOP") == "1"


def resource_root() -> Path:
    return Path(__file__).resolve().parent.parent


def desktop_data_dir() -> Path:
    explicit = os.environ.get("CLASH_MERGE_DATA_DIR")
    if explicit:
        return Path(explicit).resolve()
    return Path("D:/ClashMerge")


def desktop_url() -> str:
    return f"http://127.0.0.1:{int(os.environ.get('PORT', '18080'))}"


def output_root(config_path) -> Path:
    """The only directory the published YAML may be written to.

    Linux: ``<dir of config.json>/output`` (``/var/lib/clash-merge/output``
    in the hardened deployment). Desktop: the data directory itself, so
    existing ``<data>/jay_filtered.yaml`` installs keep working."""
    base = Path(config_path).parent
    return base if desktop_mode() else base / "output"


def default_output_path(config_path) -> Path:
    return output_root(config_path) / "jay_filtered.yaml"


def valid_token(token: str) -> bool:
    return bool(token) and _TOKEN_RE.fullmatch(token) is not None


def ensure_subscription_token(directory) -> str:
    """Read ``<directory>/subscription.token``, creating it (mode 0600)
    on first start. The token is the only secret in the subscription URL."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TOKEN_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32))
    token = path.read_text(encoding="utf-8").strip()
    if not valid_token(token):
        raise RuntimeError(f"{path} 内容无效（应为 32–128 位 URL 安全字符），删除该文件后重启可重新生成")
    return token


def public_base() -> str:
    """Linux public origin from env ``CLASH_MERGE_PUBLIC_BASE``, e.g.
    ``https://xiuyuan.9527653.xyz``. Empty when unset or not an https origin."""
    base = os.environ.get("CLASH_MERGE_PUBLIC_BASE", "").strip().rstrip("/")
    return base if re.fullmatch(r"https://[^/\s?#@]+", base) else ""


def subscription_url() -> str:
    """Full private subscription URL, or "" when it cannot be built."""
    token = os.environ.get("CLASH_MERGE_SUB_TOKEN", "")
    base = desktop_url() if desktop_mode() else public_base()
    if not base or not valid_token(token):
        return ""
    return base + "/subscription/" + token + "/jay_filtered.yaml"


def defaults_for_platform(defaults: dict) -> dict:
    import copy
    result = copy.deepcopy(defaults)
    if desktop_mode():
        data = desktop_data_dir()
        result.update(output_path=str(data / "jay_filtered.yaml"),
                      mihomo_workdir=str(data / "mihomo"),
                      mihomo_bin=str(resource_root() / "vendor" / "clash-merge-mihomo.exe"),
                      mihomo_mixed_port=17890, mihomo_controller_port=19090,
                      notify_enabled=False, purity_workers=2)
    return result


def autorun_command() -> str:
    import subprocess
    args = ([sys.executable] if getattr(sys, "frozen", False) else
            [sys.executable, str(resource_root() / "desktop.py")])
    return subprocess.list2cmdline(args + ["--background", "--data-dir", str(desktop_data_dir())])


def autorun_enabled() -> bool:
    if not desktop_mode() or os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            return winreg.QueryValueEx(key, "ClashMerge")[0] == autorun_command()
    except OSError:
        return False


def set_autorun(enabled: bool) -> None:
    if not desktop_mode() or os.name != "nt":
        raise RuntimeError("开机启动仅支持 Windows 桌面版")
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "ClashMerge", 0, winreg.REG_SZ, autorun_command())
        else:
            try:
                winreg.DeleteValue(key, "ClashMerge")
            except FileNotFoundError:
                pass
