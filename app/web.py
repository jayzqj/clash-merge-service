"""FastAPI app: admin UI + API.

Auth: HTTP Basic. Username defaults to ``jayzqj`` (env ADMIN_USER);
password comes ONLY from env ADMIN_PASSWORD or a file named by
ADMIN_PASSWORD_FILE — never from the repo, never from config.json.
The API returns full subscription URLs (including tokens) to the
logged-in admin — the user asked for this on 2026-11-12; the page
is login-protected, so only the admin sees them. The webhook value
is still never echoed: see ``Config.public_view``.

Static output (jay_filtered.yaml) is NOT served by this app — nginx
serves it as a static file. This app only serves /admin and /api/*.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import secrets
import time
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
# Strong references to background tasks: asyncio only keeps weak
# references, so a fire-and-forget create_task() result can be
# garbage-collected mid-run.
_TASKS: set[asyncio.Task] = set()
# /api/ini cache: (ini_url, fetched_at, ini_text). The admin page
# polls; hitting gist on every poll is pointless and rate-limited.
_INI_CACHE_TTL = 300
_ini_cache: tuple[str, float, str] | None = None
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _start_run() -> bool:
    """Start a full round in the background unless one is already
    running. Returns whether a round was actually started."""
    state = _state()
    if state.running or state.lock.locked():
        return False
    _spawn(run_pipeline(_cfg(), state))
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CFG, STATE
    CFG = Config.load()
    STATE = load_state(CFG)
    task = _spawn(scheduler_loop(CFG, STATE))
    log.info("clash-merge service started; next run %s", STATE.next_run)
    yield
    task.cancel()


app = FastAPI(title="clash-merge-service", lifespan=lifespan)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """CSRF guard for the Basic-auth API: browsers re-send Basic
    credentials automatically, so a cross-site form could otherwise
    POST to /api/*. Mutating API calls must carry the
    ``X-Requested-With`` header, which a cross-site page cannot set
    without a CORS preflight (and this app allows no CORS)."""
    if (request.method in _MUTATING and request.url.path.startswith("/api/")
            and request.headers.get("x-requested-with") != "XMLHttpRequest"):
        return JSONResponse(status_code=403,
                            content={"detail": "missing X-Requested-With header"})
    return await call_next(request)


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
    purity_workers: int | None = None
    purity_delay_screen: bool | None = None
    check_front_enabled: bool | None = None
    use_remote_subconverter: bool | None = None
    merged_public_url: str | None = None


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=8)


class SubscriptionPatch(BaseModel):
    enabled: bool | None = None
    url: str | None = None


def _changed_response(**extra) -> dict:
    started = _start_run()
    out = {"ok": True, "run_started": started,
           "next_run": _state().next_run, **extra}
    if not started:
        out["note"] = ("a round is already running; the change takes "
                       "effect from the next round")
    return out


@app.put("/api/config")
async def api_put_config(body: SettingsIn, request: Request,
                         _user: str = Depends(_check_auth)):
    cfg = _cfg()
    # Validate on a COPY first: a rejected save must not mutate the
    # live in-memory config (an earlier bug let interval_seconds=60
    # leak into next_run computation even though the save was refused).
    data = copy.deepcopy(cfg.data)
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
    if body.purity_workers is not None:
        data["purity_workers"] = body.purity_workers
    if body.purity_delay_screen is not None:
        data["purity_delay_screen"] = body.purity_delay_screen
    if body.check_front_enabled is not None:
        data["check_front_enabled"] = body.check_front_enabled
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
    return _changed_response()


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
    return _changed_response()


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
    return _changed_response()


@app.patch("/api/subscriptions/{sub_id}")
async def api_patch_subscription(sub_id: str, body: SubscriptionPatch,
                                _user: str = Depends(_check_auth)):
    cfg = _cfg()
    target = next((s for s in cfg.subscriptions if s.get("id") == sub_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    if body.url:
        url = body.url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=400, detail="URL must start with http(s)://")
        if any(s.get("url") == url and s is not target
               for s in cfg.subscriptions):
            raise HTTPException(status_code=400, detail="subscription already exists")
        target["url"] = url
    if body.enabled is not None:
        target["enabled"] = body.enabled
    cfg.save()
    reschedule(_state(), cfg)
    return _changed_response()


@app.post("/api/run")
async def api_run_now(_user: str = Depends(_check_auth)):
    if not _start_run():
        return {"ok": False, "error": "a round is already running"}
    return {"ok": True, "run_started": True}


@app.post("/api/reapply")
async def api_reapply(_user: str = Depends(_check_auth)):
    """Re-apply the current ini to the current published file,
    without re-testing purity. The result lands in /api/status
    (last_run, kind="reapply")."""
    from app.pipeline import reapply_rules
    state = _state()
    if state.running or state.lock.locked():
        return {"ok": False, "error": "a round is already running"}
    _spawn(reapply_rules(_cfg(), state))
    return {"ok": True, "reapply_started": True}


@app.get("/api/ini")
async def api_ini(refresh: bool = False, _user: str = Depends(_check_auth)):
    """The live ini's exclude_remarks (read-only), plus which nodes the
    last run filtered with it. The ini text is cached for 5 minutes
    (``?refresh=1`` bypasses the cache)."""
    global _ini_cache
    from app.converter import parse_ini
    from app.fetcher import fetch_text, make_client
    url = _cfg().ini_url
    now = time.monotonic()
    if (not refresh and _ini_cache and _ini_cache[0] == url
            and now - _ini_cache[1] < _INI_CACHE_TTL):
        ini_text = _ini_cache[2]
    else:
        try:
            async with make_client(timeout=30) as client:
                ini_text = await fetch_text(url, client)
        except Exception as e:  # noqa: BLE001 - surfaced to the admin page
            return {"ok": False, "error": str(e)[:300]}
        _ini_cache = (url, now, ini_text)
    ini = parse_ini(ini_text)
    last = (_state().last_run or {})
    return {"ok": True,
            "exclude_remarks": ini.exclude_regex,
            "prefilter_excluded": last.get("prefilter_excluded", 0),
            "prefilter_excluded_names":
                last.get("prefilter_excluded_names", [])}


@app.get("/api/availability")
async def api_availability(_user: str = Depends(_check_auth)):
    """Availability records: how many nodes are tracked, and which
    are currently omitted (3 consecutive failed rounds). Names only,
    no credentials."""
    from app.availability import load_availability, omitted_nodes
    records = load_availability(_cfg())
    return {"tracked": len(records), "omitted": omitted_nodes(records)}


@app.post("/api/availability/clear")
async def api_clear_availability(_user: str = Depends(_check_auth)):
    """Clear all availability records. Omitted nodes come back into
    the next round's output (and start counting failures from 0).
    Does NOT trigger a run."""
    from app.availability import clear_availability
    return {"ok": True, "cleared": clear_availability(_cfg())}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO — that would put the full
    # subscription URLs (tokens) and one line per node check into the
    # journal. Keep only warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    port = int(os.environ.get("PORT", "18080"))
    uvicorn.run("app.web:app", host="127.0.0.1", port=port, log_level="info")
