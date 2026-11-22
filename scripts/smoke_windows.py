"""Exercise the packaged EXE with local fixtures, without user subscriptions."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "windows-smoke"
EXE = ROOT / "dist" / "ClashMerge" / "ClashMerge.exe"
hits = []


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_HEAD(self):
        time.sleep(0.005)
        self.send_response(204)
        self.end_headers()

    def do_CONNECT(self):
        # Mihomo uses CONNECT for HTTP proxy nodes, even for an HTTP target.
        # The fixture destination is this same server; consume its tunneled GET.
        hits.append("CONNECT " + self.path)
        self.send_response(200, "Connection established")
        self.end_headers()
        self.handle_one_request()
        self.close_connection = True

    def do_GET(self):
        hits.append(self.path)
        path = urlsplit(self.path).path
        if path == "/subscription":
            body = yaml.safe_dump({"proxies": [{"name": "Windows fixture", "type": "http", "server": "127.0.0.1", "port": self.server.server_port}]}).encode()
        elif path == "/rules":
            body = b"[custom]\ncustom_proxy_group=Test`select`.*\nruleset=Test,[]FINAL\n"
        else:
            body = json.dumps({"ip": "203.0.113.1", "countryCode": "JP", "fraudScore": 5}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_for(callback, timeout=25):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            result = callback()
            if result:
                return result
        except (OSError, ValueError, httpx.HTTPError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Timed out waiting for desktop smoke test")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(keep_running=False):
    DATA.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture = f"http://127.0.0.1:{server.server_port}"
    mixed, controller = free_port(), free_port()
    cfg = {"subscriptions": [{"id": "fixture", "url": fixture + "/subscription", "enabled": True}],
           "ini_url": fixture + "/rules", "purity_info_url": fixture + "/info", "connectivity_url": fixture + "/ping",
           "notify_enabled": False, "execution_start": "00:00", "execution_end": "23:59",
           "purity_workers": 1, "purity_delay_screen": False, "interval_seconds": 86400,
           "mihomo_mixed_port": mixed, "mihomo_controller_port": controller}
    (DATA / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    # Avoid an obsolete bootstrap file from a previous smoke run.
    (DATA / "desktop-session.json").unlink(missing_ok=True)
    args = [str(EXE), "--background", "--data-dir", str(DATA), "--port", "18087"]
    proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
    client = httpx.Client(trust_env=False, timeout=5)
    session = None
    leave_running = False
    try:
        session = wait_for(lambda: json.loads((DATA / "desktop-session.json").read_text()))
        url = session["url"]
        wait_for(lambda: client.get(url + "/health").status_code == 200)
        assert client.get(url + "/api/config").status_code == 401
        response = client.get(url + "/desktop/open", params={"token": session["token"]}, follow_redirects=True)
        assert response.status_code == 200 and "desktopPanel" in response.text
        config = client.get(url + "/api/config").json()
        assert config["desktop"]["enabled"] and config["desktop"]["core_available"]
        token = (DATA / "subscription.token").read_text()
        second = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
        assert second.wait(timeout=15) == 0
        assert proc.poll() is None
        headers = {"X-Requested-With": "XMLHttpRequest"}
        assert client.post(url + "/api/run", headers=headers).json()["ok"]
        status = wait_for(lambda: (lambda s: s if not s["running"] and s.get("last_run") else None)(client.get(url + "/api/status").json()), timeout=45)
        report = status["last_run"]
        assert report["ok"], report
        assert report["purity_measured"] == 1, report
        published = client.get(config["subscription_url"])
        assert published.status_code == 200
        doc = yaml.safe_load(published.text)
        assert len(doc["proxies"]) == 1 and doc["proxies"][0]["name"].startswith("5% ")
        assert any(path.startswith("CONNECT ") for path in hits), "The measurement must traverse the test HTTP proxy"
        assert not (DATA / "output.tmp.yaml").exists()
        for port in (mixed, controller):
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0, "Mihomo port leaked"
        # Restore valid configurable URL before UI tests; keep this fixture data isolated.
        response = client.put(url + "/api/config", headers=headers, json={"run_now": False, "ini_url": "https://example.invalid/fixture.ini"})
        assert response.status_code == 200, response.text
        assert not client.get(url + "/health").json()["running"]
        print("PASS: packaged startup, cookie login, single instance, real Mihomo proxy measurement, YAML validation/publication, save-only and core cleanup", flush=True)
        if keep_running:
            leave_running = True
            print("UI fixture remains running on " + url, flush=True)
            return
        client.post(url + "/api/desktop/exit", headers=headers).raise_for_status()
        assert proc.wait(timeout=20) == 0
        proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
        session = wait_for(lambda: (lambda s: s if s["token"] != session["token"] else None)(json.loads((DATA / "desktop-session.json").read_text())))
        wait_for(lambda: client.get(session["url"] + "/health").status_code == 200)
        assert (DATA / "subscription.token").read_text() == token
        client.get(session["url"] + "/desktop/open", params={"token": session["token"]})
        client.post(session["url"] + "/api/desktop/exit", headers=headers).raise_for_status()
        assert proc.wait(timeout=20) == 0
        print("PASS: clean exit, restart and stable subscription token", flush=True)
    finally:
        server.shutdown()
        client.close()
        if not leave_running and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--exe", type=Path, default=EXE)
    args = parser.parse_args()
    EXE = args.exe.resolve()
    main(args.keep_running)
