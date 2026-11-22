"""Windows tray entry point. Linux continues to use main.py/systemd."""
import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser


def acquire_mutex(data_dir):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    name = "Local\\ClashMerge-" + hashlib.sha256(str(data_dir).lower().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle, ctypes.get_last_error() == 183


def choose_listener(preferred):
    if not 1024 <= preferred <= 65516:
        raise ValueError("服务端口必须在 1024–65516 之间")
    # Keep the bound socket until Uvicorn takes ownership, avoiding port races.
    for port in range(preferred, preferred + 20):
        sock = socket.socket()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
            return sock, port
        except OSError:
            sock.close()
    raise RuntimeError("本地服务端口被占用，请关闭旧实例后重试")


def prepare_data(data):
    from app.config import Config
    from app.runtime import resource_root
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config.load()
    # The binary lives beside this version of the app, never in the user's PATH.
    cfg.data["mihomo_bin"] = str(resource_root() / "vendor" / "clash-merge-mihomo.exe")
    if not Path(cfg.data["mihomo_bin"]).is_file():
        raise RuntimeError("缺少检测内核，请完整解压发布包，不要只复制主 EXE")
    workdir = Path(cfg.data["mihomo_workdir"])
    workdir.mkdir(parents=True, exist_ok=True)
    for name in ("geosite.dat", "geoip.dat", "geoip.metadb", "Country.mmdb"):
        src = resource_root() / "vendor" / name
        dst = workdir / name
        if src.is_file() and not dst.exists():
            shutil.copyfile(src, dst)
    # Moving/upgrading the portable folder must update its binary location.
    cfg.save()
    token_file = data / "subscription.token"
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    os.environ["CLASH_MERGE_SUB_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
    return cfg


def main():
    if os.name != "nt":
        raise SystemExit("Windows desktop only; on Linux run python main.py")
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--smoke-seconds", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.data_dir:
        os.environ["CLASH_MERGE_DATA_DIR"] = str(Path(args.data_dir).resolve())
    os.environ["CLASH_MERGE_DESKTOP"] = "1"
    from app.runtime import desktop_data_dir, desktop_url
    data = desktop_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    # A different shell's Linux config variable must not select the VPS config.
    os.environ["CLASH_MERGE_CONFIG"] = str(data / "config.json")
    kernel, mutex, exists = acquire_mutex(data)
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    if exists:
        try:
            for _ in range(30):
                try:
                    existing = json.loads((data / "desktop-session.json").read_text())
                    import httpx
                    with httpx.Client(trust_env=False, timeout=1) as client:
                        r = client.get(existing["url"] + "/api/status",
                                       cookies={"clash_merge_session": existing["token"]})
                        r.raise_for_status()
                    if not args.background:
                        webbrowser.open(existing["url"] + "/desktop/open?token=" + existing["token"])
                    return
                except Exception:
                    time.sleep(0.2)
            raise RuntimeError("另一个实例正在启动或退出，请稍后重试")
        finally:
            kernel.CloseHandle(mutex)
    logs = data / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / "desktop.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    listener = None
    icon = None
    try:
        prepare_data(data)
        os.environ["CLASH_MERGE_DESKTOP_TOKEN"] = secrets.token_urlsafe(32)
        saved_port = data / "desktop-port.json"
        preferred = args.port
        if saved_port.exists() and args.port == 18080:
            preferred = int(json.loads(saved_port.read_text())["port"])
        listener, port = choose_listener(preferred)
        os.environ["PORT"] = str(port)
        saved_port.write_text(json.dumps({"port": port}), encoding="utf-8")
        session = {"url": desktop_url(), "token": os.environ["CLASH_MERGE_DESKTOP_TOKEN"]}
        (data / "desktop-session.json").write_text(json.dumps(session), encoding="utf-8")
        from app import web
        from app.processes import stop_all
        import uvicorn
        import pystray
        from PIL import Image, ImageDraw
        config = uvicorn.Config(web.app, host="127.0.0.1", port=port, log_config=None,
                                access_log=False, loop="asyncio", timeout_graceful_shutdown=5)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        for _ in range(150):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("本地后台启动失败，请检查数据目录中的日志")
            time.sleep(0.1)
        else:
            raise RuntimeError("本地后台启动超时")

        def open_admin(*_):
            webbrowser.open(session["url"] + "/desktop/open?token=" + session["token"])

        quitting = threading.Event()

        def quit_app(*_):
            if quitting.is_set():
                return
            quitting.set()
            def cleanup():
                stop_all()
                server.should_exit = True
                thread.join(timeout=8)
                if icon:
                    icon.stop()
            threading.Thread(target=cleanup, daemon=True).start()

        def run_now(*_):
            import httpx
            try:
                with httpx.Client(trust_env=False, timeout=5) as client:
                    r = client.post(session["url"] + "/api/run", headers={"X-Requested-With": "XMLHttpRequest"},
                                    cookies={"clash_merge_session": session["token"]})
                    r.raise_for_status()
                    if not r.json().get("ok"):
                        icon.notify(r.json().get("error", "无法启动"), "Clash Merge")
            except Exception:
                logging.exception("tray run failed")
                icon.notify("无法启动，请打开后台查看状态", "Clash Merge")

        web.DESKTOP_EXIT = quit_app
        image = Image.new("RGBA", (64, 64), (12, 20, 34, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 56, 56), radius=12, outline=(53, 200, 244), width=4)
        draw.line((19, 25, 45, 25), fill="white", width=5)
        draw.line((19, 39, 36, 39), fill="white", width=5)
        icon = pystray.Icon("ClashMerge", image, "Clash Merge 本机检测", menu=pystray.Menu(
            pystray.MenuItem("打开配置页面", open_admin, default=True),
            pystray.MenuItem("立即检测", run_now),
            pystray.MenuItem("打开数据目录", lambda *_: os.startfile(data)),
            pystray.MenuItem("退出", quit_app)))
        if not args.background:
            open_admin()
        if args.smoke_seconds:
            threading.Timer(args.smoke_seconds, quit_app).start()
        icon.run()
        stop_all()
        server.should_exit = True
        thread.join(timeout=8)
    except Exception as exc:
        logging.exception("desktop startup failed")
        ctypes.windll.user32.MessageBoxW(None, str(exc) + "\n日志：" + str(logs), "Clash Merge", 0x10)
        raise
    finally:
        from app.processes import stop_all
        stop_all()
        if listener:
            listener.close()
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
