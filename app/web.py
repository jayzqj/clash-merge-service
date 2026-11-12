"""FastAPI app: admin UI + API.

Auth: HTTP Basic. Username defaults to ``jayzqj`` (env ADMIN_USER);
password comes ONLY from env ADMIN_PASSWORD or a file named by
ADMIN_PASSWORD_FILE — never from the repo, never from config.json.
If no password is configured the admin routes fail closed (503).
The API never returns real subscription URLs or the webhook value:
see ``Config.public_view`` (masked URLs, webhook shown only as
"set"/empty).

Static output (jay_filtered.yaml) is NOT served by this app — nginx
serves it as a static file. This app only serves /admin and /api/*.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app.config import Config
from app.pipeline import ServiceState, load_state, reschedule, run_pipeline, scheduler_loop

log = logging.getLogger(__name__)
security = HTTPBasic()

ADMIN_USER = os.environ.get("ADMIN_USER", "jayzqj")
_admin_password = os.environ.get("ADMIN_PASSWORD", "")
if not _admin_password and os.environ.get("ADMIN_PASSWORD_FILE"):
    try:
        _admin_password = Path(os.environ["ADMIN_PASSWORD_FILE"]).read_text(
            encoding="utf-8").strip()
    except OSError as e:
        log.warning("ADMIN_PASSWORD_FILE unreadable: %s", e)


def _check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not _admin_password:
        raise HTTPException(status_code=503,
                            detail="admin password not configured on server")
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
    pw_ok = secrets.compare_digest(credentials.password, _admin_password)
    if not (user_ok and pw_ok):
        raise HTTPException(
            status_code=401, detail="unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"clash-merge admin\""})
    return credentials.username


CFG: Config | None = None
STATE: ServiceState | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CFG, STATE
    CFG = Config.load()
    STATE = load_state(CFG)
    task = asyncio.create_task(scheduler_loop(CFG, STATE))
    log.info("clash-merge service started; next run %s", STATE.next_run)
    yield
    task.cancel()


app = FastAPI(title="clash-merge-service", lifespan=lifespan)


def _cfg() -> Config:
    assert CFG is not None
    return CFG


def _state() -> ServiceState:
    assert STATE is not None
    return STATE


@app.get("/health")
async def health():
    return {"ok": True, "running": _state().running}


@app.get("/admin")
async def admin_page(_user: str = Depends(_check_auth)):
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


@app.get("/api/status")
async def api_status(_user: str = Depends(_check_auth)):
    return _state().status()


@app.get("/api/config")
async def api_get_config(_user: str = Depends(_check_auth)):
    return _cfg().public_view()


class SettingsIn(BaseModel):
    interval_seconds: int | None = None
    notify_enabled: bool | None = None
    wechat_webhook: str | None = None  # empty/absent = keep current
    ini_url: str | None = None
    subconverter_base: str | None = None
    output_path: str | None = None
    purity_enabled: bool | None = None
    use_remote_subconverter: bool | None = None
    merged_public_url: str | None = None


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=8)


@app.put("/api/config")
async def api_put_config(body: SettingsIn, request: Request,
                         _user: str = Depends(_check_auth)):
    cfg = _cfg()
    # Validate on a COPY first: a rejected save must not mutate the
    # live in-memory config (an earlier bug let interval_seconds=60
    # leak into next_run computation even though the save was refused).
    import copy as _copy
    data = _copy.deepcopy(cfg.data)
    if body.interval_seconds is not None:
        data["interval_seconds"] = body.interval_seconds
    if body.notify_enabled is not None:
        data["notify_enabled"] = body.notify_enabled
    if body.wechat_webhook:
        data["wechat_webhook"] = body.wechat_webhook.strip()
    if body.ini_url is not None:
        data["ini_url"] = body.ini_url.strip()
    if body.subconverter_base is not None:
        data["subconverter_base"] = body.subconverter_base.strip()
    if body.output_path is not None:
        data["output_path"] = body.output_path.strip()
    if body.purity_enabled is not None:
        data["purity_enabled"] = body.purity_enabled
    if body.use_remote_subconverter is not None:
        data["use_remote_subconverter"] = body.use_remote_subconverter
    if body.merged_public_url is not None:
        data["merged_public_url"] = body.merged_public_url.strip()
    trial = Config(data=data, path=cfg.path)
    errors = trial.validate()
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    cfg.data = data
    cfg.save()
    reschedule(_state(), cfg)
    # Spec: saving any change triggers an immediate full run.
    asyncio.create_task(run_pipeline(cfg, _state()))
    return {"ok": True, "run_started": True,
            "next_run": _state().next_run}


@app.post("/api/subscriptions")
async def api_add_subscription(body: SubscriptionIn,
                               _user: str = Depends(_check_auth)):
    cfg = _cfg()
    url = body.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must start with http(s)://")
    if any(s.get("url") == url for s in cfg.subscriptions):
        raise HTTPException(status_code=400, detail="subscription already exists")
    cfg.data.setdefault("subscriptions", []).append(
        {"id": cfg.new_subscription_id(), "url": url, "enabled": True})
    cfg.save()
    reschedule(_state(), cfg)
    asyncio.create_task(run_pipeline(cfg, _state()))
    return {"ok": True, "run_started": True}


@app.delete("/api/subscriptions/{sub_id}")
async def api_delete_subscription(sub_id: str,
                                  _user: str = Depends(_check_auth)):
    cfg = _cfg()
    before = len(cfg.subscriptions)
    cfg.data["subscriptions"] = [s for s in cfg.subscriptions
                                 if s.get("id") != sub_id]
    if len(cfg.subscriptions) == before:
        raise HTTPException(status_code=404, detail="subscription not found")
    cfg.save()
    reschedule(_state(), cfg)
    asyncio.create_task(run_pipeline(cfg, _state()))
    return {"ok": True, "run_started": True}


@app.patch("/api/subscriptions/{sub_id}")
async def api_patch_subscription(sub_id: str, body: dict,
                                _user: str = Depends(_check_auth)):
    cfg = _cfg()
    target = next((s for s in cfg.subscriptions if s.get("id") == sub_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    if "enabled" in body:
        target["enabled"] = bool(body["enabled"])
    if body.get("url"):
        url = str(body["url"]).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=400, detail="URL must start with http(s)://")
        target["url"] = url
    cfg.save()
    reschedule(_state(), cfg)
    asyncio.create_task(run_pipeline(cfg, _state()))
    return {"ok": True, "run_started": True}


@app.post("/api/run")
async def api_run_now(_user: str = Depends(_check_auth)):
    state = _state()
    if state.running:
        return {"ok": False, "error": "a round is already running"}
    asyncio.create_task(run_pipeline(_cfg(), state))
    return {"ok": True, "run_started": True}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("PORT", "18080"))
    uvicorn.run("app.web:app", host="127.0.0.1", port=port, log_level="info")
