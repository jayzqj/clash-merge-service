"""FastAPI app: admin UI + API.

Auth: HTTP Basic. Username defaults to ``jayzqj`` (env ADMIN_USER);
password comes ONLY from env ADMIN_PASSWORD or a file named by
ADMIN_PASSWORD_FILE — never from the repo, never from config.json.
The API returns full subscription URLs (including tokens) to the
logged-in admin — the user asked for this on 2026-11-12; the page
is login-protected, so only the admin sees them. The webhook value
is still never echoed: see ``Config.public_view``.

The published YAML is served by THIS process at
``/subscription/<token>/jay_filtered.yaml`` on both Linux and Windows;
the token lives in ``subscription.token`` next to config.json. nginx
only reverse-proxies that path, it never serves the file statically.
The Windows launcher additionally enables local session authentication.
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app.config import Config
from app.pipeline import ServiceState, load_state, reschedule, run_pipeline, scheduler_loop
from app import runtime
from app.node_links import validate_source, PROTOCOLS

log = logging.getLogger(__name__)
security = HTTPBasic(auto_error=False)
DESKTOP_EXIT = None

ADMIN_USER = os.environ.get("ADMIN_USER", "jayzqj")
_admin_password = os.environ.get("ADMIN_PASSWORD", "")
if not _admin_password and os.environ.get("ADMIN_PASSWORD_FILE"):
    try:
        _admin_password = Path(os.environ["ADMIN_PASSWORD_FILE"]).read_text(
            encoding="utf-8").strip()
    except OSError as e:
        log.warning("ADMIN_PASSWORD_FILE unreadable: %s", e)


def _check_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    if runtime.desktop_mode():
        token = os.environ.get("CLASH_MERGE_DESKTOP_TOKEN", "")
        if token and secrets.compare_digest(request.cookies.get("clash_merge_session", ""), token):
            return "desktop"
        raise HTTPException(status_code=401, detail="请从系统托盘重新打开配置页面")
    if not _admin_password:
        raise HTTPException(status_code=503,
                            detail="admin password not configured on server")
    if credentials is None:
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"})
    ip = _client_ip(request)
    retry_after = _LOGIN_LIMITER.locked(ip)
    if retry_after:
        raise HTTPException(status_code=429, detail="too many failed logins, try again later",
                            headers={"Retry-After": str(retry_after)})
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
    pw_ok = secrets.compare_digest(credentials.password, _admin_password)
    if not (user_ok and pw_ok):
        if _LOGIN_LIMITER.fail(ip):
            log.warning("auth: failed admin login from %s; locked for %d s", ip, LOGIN_LOCK_SECONDS)
        else:
            log.warning("auth: failed admin login from %s", ip)
        raise HTTPException(
            status_code=401, detail="unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"clash-merge admin\""})
    _LOGIN_LIMITER.reset(ip)
    return credentials.username


# Login throttling (security review S2): the admin UI is on the public
# internet behind HTTP Basic, which browsers and scripts can retry without
# limit. After LOGIN_MAX_FAILURES consecutive wrong passwords from one IP,
# that IP gets 429 for LOGIN_LOCK_SECONDS, even with the right password.
# State is in memory only: a restart clears it, which is acceptable.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60
_LOGIN_TRACKED_MAX = 10000


class LoginLimiter:
    def __init__(self, max_failures=LOGIN_MAX_FAILURES, lock_seconds=LOGIN_LOCK_SECONDS,
                 clock=time.monotonic):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.clock = clock
        # ip -> [consecutive failures, locked until (0 = not locked), last seen]
        self._entries: dict[str, list] = {}

    def locked(self, ip: str) -> int:
        """Seconds left on the lock, 0 when not locked."""
        entry = self._entries.get(ip)
        if not entry or not entry[1]:
            return 0
        left = entry[1] - self.clock()
        if left <= 0:
            del self._entries[ip]
            return 0
        return max(1, int(left + 0.999))

    def fail(self, ip: str) -> bool:
        """Record a failure; True when this failure starts a lock."""
        now = self.clock()
        if ip not in self._entries and len(self._entries) >= _LOGIN_TRACKED_MAX:
            self._prune(now)
        entry = self._entries.setdefault(ip, [0, 0.0, now])
        entry[0] += 1
        entry[2] = now
        if entry[0] >= self.max_failures:
            entry[0] = 0
            entry[1] = now + self.lock_seconds
            return True
        return False

    def reset(self, ip: str) -> None:
        self._entries.pop(ip, None)

    def _prune(self, now: float) -> None:
        stale = [ip for ip, (_, until, seen) in self._entries.items()
                 if (until and until <= now) or (not until and now - seen > self.lock_seconds)]
        for ip in stale:
            del self._entries[ip]
        if len(self._entries) >= _LOGIN_TRACKED_MAX:
            # Still full of live entries: drop the oldest unlocked ones.
            for ip, _ in sorted(((ip, e[2]) for ip, e in self._entries.items() if not e[1]),
                                key=lambda item: item[1])[:len(self._entries) // 2 or 1]:
                del self._entries[ip]


_LOGIN_LIMITER = LoginLimiter()


def _client_ip(request: Request) -> str:
    """The service listens on 127.0.0.1 only, behind nginx, which sets
    X-Real-IP. The header is trusted only from a loopback peer."""
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1", "localhost", "testclient"):
        forwarded = request.headers.get("x-real-ip", "").strip()
        if forwarded:
            return forwarded[:64]
    return peer or "unknown"


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
    from app.schedule import in_execution_window
    if not in_execution_window(_cfg()):
        reschedule(state, _cfg())
        return False
    _spawn(run_pipeline(_cfg(), state))
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CFG, STATE
    CFG = Config.load()
    if not runtime.desktop_mode():
        # Desktop sets this in desktop.prepare_data().
        os.environ["CLASH_MERGE_SUB_TOKEN"] = runtime.ensure_subscription_token(CFG.path.parent)
        for error in CFG.output_path_errors():
            log.error("config: %s — rounds will fail until config.json is fixed", error)
        if not runtime.public_base():
            log.warning("CLASH_MERGE_PUBLIC_BASE is unset or not an https origin; "
                        "the admin page and notifications cannot show the subscription URL")
    STATE = load_state(CFG)
    task = _spawn(scheduler_loop(CFG, STATE))
    log.info("clash-merge service started; next run %s", STATE.next_run)
    yield
    pending = list(_TASKS)
    for pending_task in pending:
        pending_task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


# Security review S4: the interactive API docs (/docs, /redoc) and the
# OpenAPI schema (/openapi.json) are unauthenticated. nginx does not
# proxy them, so they are unreachable from the public site, but they are
# open to anyone who can reach the service directly (localhost, desktop).
# Nothing in this app consumes them, so turn them off entirely.
app = FastAPI(title="clash-merge-service", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


def _validation_message(err: dict) -> str:
    """One readable line per pydantic error, e.g. ``ini_url：长度不能超过 2048 个字符``."""
    loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
    field = ".".join(loc) or "请求"
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "string_too_long":
        text = f"长度不能超过 {ctx.get('max_length')} 个字符"
    elif kind == "string_too_short":
        text = f"长度不能少于 {ctx.get('min_length')} 个字符"
    elif kind == "too_long":
        text = f"最多 {ctx.get('max_length')} 项"
    elif kind == "missing":
        text = "缺少此字段"
    else:
        text = str(err.get("msg", "格式不正确"))
    return f"{field}：{text}"


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Request-body validation failures (field types, S7 length/count
    bounds) answer 400 ``{"errors": [...]}`` — the same shape as
    ``Config.validate`` failures — so the admin page shows a readable
    message instead of FastAPI's raw 422 detail list. Input values are
    never echoed back (they may be URLs with tokens)."""
    return JSONResponse(status_code=400,
                        content={"errors": [_validation_message(e) for e in exc.errors()]})


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """CSRF guard for the Basic-auth API: browsers re-send Basic
    credentials automatically, so a cross-site form could otherwise
    POST to /api/*. Mutating API calls must carry the
    ``X-Requested-With`` header, which a cross-site page cannot set
    without a CORS preflight (and this app allows no CORS)."""
    if runtime.desktop_mode():
        if request.url.hostname not in ("127.0.0.1", "localhost"):
            return JSONResponse(status_code=403, content={"detail": "local access only"})
        origin = request.headers.get("origin")
        if origin and origin != runtime.desktop_url():
            return JSONResponse(status_code=403, content={"detail": "untrusted origin"})
    if (request.method in _MUTATING and request.url.path.startswith("/api/")
            and request.headers.get("x-requested-with") != "XMLHttpRequest"):
        return JSONResponse(status_code=403,
                            content={"detail": "missing X-Requested-With header"})
    return await call_next(request)


# Security response headers (security review S3). CSP: the admin page
# serves exactly one inline <script> (signed per-request with a nonce,
# so 'unsafe-inline' script is NOT allowed) and inline styles (the
# <style> block and two static style= attributes carry no user data, so
# 'unsafe-inline' style is acceptable). Everything else is denied; the
# page loads no external script, font or image, and talks only to its
# own origin. frame-ancestors/X-Frame-Options stop click-jacking.
def _csp(nonce: str) -> str:
    return ("default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request.state.csp_nonce = secrets.token_urlsafe(16)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _csp(request.state.csp_nonce))
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


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
async def admin_page(request: Request, _user: str = Depends(_check_auth)):
    nonce = getattr(request.state, "csp_nonce", "") or secrets.token_urlsafe(16)
    return HTMLResponse(_admin_html(nonce))


_ADMIN_HTML: tuple[str, str] | None = None


def _admin_html(nonce: str) -> str:
    """admin.html with the single inline <script> tagged with the CSP
    nonce. The file is read once; the exact tag ``<script>`` must occur
    exactly once (asserted at load) so a later edit that adds another
    script can never silently ship unsigned (and thus CSP-blocked)."""
    global _ADMIN_HTML
    if _ADMIN_HTML is None:
        raw = (Path(__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
        if raw.count("<script>") != 1 or "<script " in raw:
            raise RuntimeError("admin.html must contain exactly one unattributed <script> tag "
                               "for CSP nonce injection")
        head, tail = raw.split("<script>", 1)
        _ADMIN_HTML = (head, tail)
    head, tail = _ADMIN_HTML
    return f'{head}<script nonce="{nonce}">{tail}'


@app.get("/api/status")
async def api_status(summary: bool = False, _user: str = Depends(_check_auth)):
    status = _state().status()
    if summary and status.get("last_run"):
        last = status["last_run"]
        compact = {key: value for key, value in last.items() if not isinstance(value, (list, dict))}
        compact["excluded_protocols"] = last.get("excluded_protocols", [])
        compact["source_routes"] = last.get("source_routes", [])[:30]
        compact["sources_failed_count"] = len(last.get("sources_failed", []))
        compact["sources_failed"] = [{"source": item.get("name") or item.get("id") or "订阅源",
            "error": str(item.get("error", ""))[:250]} for item in last.get("sources_failed", [])[:10]]
        compact["tested_chains_count"] = len(last.get("tested_chains", []))
        compact["front_candidates_count"] = len(last.get("front_candidates", []))
        status["last_run"] = compact
    return status


@app.get("/api/config")
async def api_get_config(summary: bool = False, _user: str = Depends(_check_auth)):
    view = _cfg().public_view()
    if summary:
        from app.node_links import is_node_source
        for source in view.get('subscriptions', []):
            value = source.get('url', '')
            source['url'] = '本地节点配置 / 分享链接' if is_node_source(value) else value[:160] + ('…' if len(value)>160 else '')
    view["available_protocols"] = sorted(set(((_state().last_run or {}).get("protocols_seen", [])))
        | set(view.get("excluded_protocols", []))
        | PROTOCOLS)
    view["subscription_url"] = runtime.subscription_url()
    view["desktop"] = {"enabled": runtime.desktop_mode()}
    if runtime.desktop_mode():
        from app.resources import resource_recommendation
        view["desktop"].update(autorun=runtime.autorun_enabled(),
                               data_dir=str(runtime.desktop_data_dir()),
                               core_available=Path(_cfg().data["mihomo_bin"]).is_file(),
                               resources=await asyncio.to_thread(resource_recommendation))
    return view


@app.get("/desktop/open")
async def desktop_open(token: str):
    expected = os.environ.get("CLASH_MERGE_DESKTOP_TOKEN", "")
    if not runtime.desktop_mode() or not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid desktop session")
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("clash_merge_session", expected, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/subscription/{token}/jay_filtered.yaml")
async def subscription(token: str):
    expected = os.environ.get("CLASH_MERGE_SUB_TOKEN", "")
    if (not runtime.valid_token(expected)
            or not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))):
        raise HTTPException(status_code=404, detail="not found")
    cfg = _cfg()
    if cfg.output_path_errors():
        raise HTTPException(status_code=404, detail="not found")
    output = Path(cfg.output_path)
    if not output.is_file():
        raise HTTPException(status_code=404, detail="尚未生成订阅，请添加订阅源并完成一轮检测")
    return FileResponse(output, media_type="application/yaml",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff"})


class AutorunIn(BaseModel):
    enabled: bool = Field(strict=True)


@app.put("/api/desktop/autorun")
async def desktop_autorun(body: AutorunIn, _user: str = Depends(_check_auth)):
    if not runtime.desktop_mode():
        raise HTTPException(status_code=404, detail="desktop only")
    try:
        runtime.set_autorun(body.enabled)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="无法修改当前用户开机启动设置") from exc
    return {"ok": True, "enabled": runtime.autorun_enabled()}


@app.post("/api/desktop/exit")
async def desktop_exit(_user: str = Depends(_check_auth)):
    if not runtime.desktop_mode() or DESKTOP_EXIT is None:
        raise HTTPException(status_code=404, detail="desktop only")
    asyncio.get_running_loop().call_later(0.5, DESKTOP_EXIT)
    return {"ok": True}


# Field bounds (security review S7): the admin is trusted, so these are
# guard rails against a fat-finger or a runaway config file, not an
# attacker. URLs and names are length-capped; lists and the subscription
# total are count-capped (see MAX_SUBSCRIPTIONS in the add handler).
_MAX_URL = 2048
_MAX_PROTOCOLS = 64
# A real deployment has a handful of subscription sources; this only
# stops the config file from growing without bound.
MAX_SUBSCRIPTIONS = 200


class SettingsIn(BaseModel):
    run_now: bool = Field(default=True, strict=True)
    interval_seconds: int | None = None
    execution_start: str | None = None
    execution_end: str | None = None
    notify_enabled: bool | None = None
    wechat_webhook: str | None = Field(default=None, max_length=_MAX_URL)  # empty/absent = keep current
    ini_url: str | None = Field(default=None, max_length=_MAX_URL)
    subconverter_base: str | None = Field(default=None, max_length=_MAX_URL)
    output_path: str | None = None  # read-only: accepted only when unchanged
    purity_enabled: bool | None = None
    purity_filter_enabled: bool | None = None
    purity_threshold: float | None = Field(default=None, ge=0, le=100, strict=True)
    purity_workers: int | None = None
    purity_delay_screen: bool | None = None
    check_front_enabled: bool | None = None
    use_remote_subconverter: bool | None = None
    merged_public_url: str | None = Field(default=None, max_length=_MAX_URL)
    fetch_proxy_url: str | None = Field(default=None, max_length=_MAX_URL)
    fetch_direct_fallback: bool | None = None
    connectivity_url: str | None = Field(default=None, max_length=_MAX_URL)
    connectivity_timeout_ms: int | None = None
    connectivity_rounds: int | None = Field(default=None, strict=True)
    excluded_protocols: list[str] | None = Field(default=None, max_length=_MAX_PROTOCOLS)
    check_front: dict | None = None


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=8, max_length=_MAX_URL)
    name: str = Field(default="", max_length=100)


class SubscriptionPatch(BaseModel):
    enabled: bool | None = None
    url: str | None = Field(default=None, max_length=_MAX_URL)
    name: str | None = Field(default=None, max_length=100)


def _changed_response(**extra) -> dict:
    started = _start_run()
    out = {"ok": True, "run_started": started,
           "next_run": _state().next_run, **extra}
    if not started:
        from app.schedule import in_execution_window
        out["outside_execution_window"] = not in_execution_window(_cfg())
        out["note"] = ("当前不在任务执行时段内，已安排到允许的时间执行"
                       if out["outside_execution_window"] else
                       "a round is already running; the change takes effect from the next round")
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
    if body.execution_start is not None:
        data["execution_start"] = body.execution_start
    if body.execution_end is not None:
        data["execution_end"] = body.execution_end
    if body.notify_enabled is not None:
        data["notify_enabled"] = body.notify_enabled
    if body.wechat_webhook:
        data["wechat_webhook"] = body.wechat_webhook.strip()
    if body.ini_url is not None:
        data["ini_url"] = body.ini_url.strip()
    if body.subconverter_base is not None:
        data["subconverter_base"] = body.subconverter_base.strip()
    if body.output_path is not None and body.output_path.strip() != cfg.output_path:
        return JSONResponse(status_code=400, content={
            "errors": ["输出文件路径只能在服务器的 config.json 中修改"]})
    if body.purity_enabled is not None:
        data["purity_enabled"] = body.purity_enabled
    if body.purity_filter_enabled is not None:
        data["purity_filter_enabled"] = body.purity_filter_enabled
    if body.purity_threshold is not None:
        data["purity_threshold"] = body.purity_threshold
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
    if body.fetch_proxy_url is not None:
        data["fetch_proxy_url"] = body.fetch_proxy_url.strip()
    for key in ("fetch_direct_fallback", "connectivity_url", "connectivity_timeout_ms", "connectivity_rounds", "excluded_protocols"):
        if getattr(body, key) is not None:
            data[key] = getattr(body, key)
    if "check_front" in body.model_fields_set:
        data["check_front"] = body.check_front
    trial = Config(data=data, path=cfg.path)
    errors = trial.validate()
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    cfg.data = data
    cfg.save()
    if not body.run_now:
        reschedule(_state(), cfg, preserve_next=True)
        return {"ok": True, "run_started": False, "saved_only": True,
                "next_run": _state().next_run,
                "note": "已保存，下轮执行时使用新配置"}
    reschedule(_state(), cfg)
    # Legacy/default action still saves and triggers a round within its window.
    return _changed_response()


@app.post("/api/subscriptions")
async def api_add_subscription(body: SubscriptionIn,
                               _user: str = Depends(_check_auth)):
    cfg = _cfg()
    if len(cfg.subscriptions) >= MAX_SUBSCRIPTIONS:
        raise HTTPException(status_code=400,
                            detail=f"订阅源数量已达上限 {MAX_SUBSCRIPTIONS}")
    try:
        url = validate_source(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if any(s.get("url") == url for s in cfg.subscriptions):
        raise HTTPException(status_code=400, detail="subscription already exists")
    cfg.data.setdefault("subscriptions", []).append(
        {"id": cfg.new_subscription_id(), "url": url,
         "name": body.name.strip(), "enabled": True})
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
        try:
            url = validate_source(body.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if any(s.get("url") == url and s is not target
               for s in cfg.subscriptions):
            raise HTTPException(status_code=400, detail="subscription already exists")
        target["url"] = url
    if body.enabled is not None:
        target["enabled"] = body.enabled
    if body.name is not None:
        target["name"] = body.name.strip()
    cfg.save()
    # A display-name edit changes no pipeline inputs or schedule.
    if body.url is None and body.enabled is None:
        return {"ok": True, "run_started": False,
                "next_run": _state().next_run}
    reschedule(_state(), cfg)
    return _changed_response()


@app.post("/api/run")
async def api_run_now(_user: str = Depends(_check_auth)):
    if not _start_run():
        from app.schedule import in_execution_window
        if not in_execution_window(_cfg()):
            return {"ok": False, "outside_execution_window": True,
                    "next_run": _state().next_run,
                    "error": f"当前不在任务执行时段内，下次执行：{_state().next_run}（北京时间）"}
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
async def api_ini(refresh: bool = False, summary: bool = False, _user: str = Depends(_check_auth)):
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
            async with make_client(timeout=30, cfg=_cfg()) as client:
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
                [] if summary else last.get("prefilter_excluded_names", [])}


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
    # No access log: request paths carry the subscription token.
    uvicorn.run("app.web:app", host="127.0.0.1", port=port, log_level="info",
                access_log=False)
