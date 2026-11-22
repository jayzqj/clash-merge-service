"""Platform paths and desktop-only settings; Linux defaults remain unchanged."""
import os
import sys
from pathlib import Path


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


def subscription_url() -> str:
    if desktop_mode():
        return desktop_url() + "/subscription/" + os.environ["CLASH_MERGE_SUB_TOKEN"] + "/jay_filtered.yaml"
    return "https://xiuyuan.9527653.xyz/jay_filtered.yaml"


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
