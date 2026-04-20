"""FastAPI application entry point.

Just the app object + router mounting + static file serving. All business
logic and routes live in the per-domain packages:
    - app/trips/routes.py → /api/trips/*, /api/vacationmap/*, conversations, messages
    - app/golf/routes.py  → /api/golf-library/*
"""

import base64
import logging
import os
import secrets
import sys
import traceback
from pathlib import Path

# Configure the root logger BEFORE any other imports that may call
# `logging.getLogger(...)`. Without this, uvicorn adds its own handlers first
# and third-party libs (SQLAlchemy, httpx, anthropic) end up with no handler,
# so their warnings / info messages vanish — which on Railway looks like "no
# logs at all". basicConfig is a no-op if handlers are already set, so if
# something upstream (pytest, ipython) did the setup we won't fight it.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Make absolutely sure early module-load prints hit Railway's log stream.
print("BOOT: app.main importing", file=sys.stderr, flush=True)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from .database import init_golf_db, init_trips_db  # noqa: E402
from .trips.routes import router as trips_router  # noqa: E402
from .golf.routes import router as golf_router  # noqa: E402
from .yearly.routes import router as yearly_router  # noqa: E402

# Unhandled-exception log: goes to stderr (so Railway / `uvicorn` console captures
# it) AND to an append-only `errors.log` for local dev. Ask "check latest error"
# to tail the file locally; on Railway, scroll the deploy logs.
_ERROR_LOG_PATH = Path(__file__).resolve().parent.parent / "errors.log"
_error_logger = logging.getLogger("vacationplanner.errors")
if not _error_logger.handlers:
    _formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _stream_handler = logging.StreamHandler(sys.stderr)
    _stream_handler.setFormatter(_formatter)
    _error_logger.addHandler(_stream_handler)
    try:
        _file_handler = logging.FileHandler(_ERROR_LOG_PATH)
        _file_handler.setFormatter(_formatter)
        _error_logger.addHandler(_file_handler)
    except OSError:
        # Read-only FS (some container setups) — stderr-only is fine.
        pass
    _error_logger.setLevel(logging.ERROR)
    _error_logger.propagate = False

app = FastAPI(title="Trip Planner Chatbot")


# ---------------------------------------------------------------------------
# HTTP Basic Auth — gates every request (API + static frontend)
# ---------------------------------------------------------------------------
#
# Credentials from env:
#   AUTH_USERS="felix:hunter2,guest:welcome"    # multi-user, comma-separated
#   or
#   AUTH_USERNAME=felix                          # single user
#   AUTH_PASSWORD=hunter2
#
# If neither is set, the middleware is a no-op (local dev convenience).
# Timing-safe comparison via `secrets.compare_digest`. Browsers cache Basic
# credentials until tab/window close, so there's no login page to build.


def _load_users() -> dict[str, str]:
    users: dict[str, str] = {}
    users_csv = os.environ.get("AUTH_USERS", "").strip()
    if users_csv:
        for pair in users_csv.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            u, p = pair.split(":", 1)
            u, p = u.strip(), p.strip()
            if u and p:
                users[u] = p
    u = os.environ.get("AUTH_USERNAME", "").strip()
    p = os.environ.get("AUTH_PASSWORD", "").strip()
    if u and p:
        users[u] = p
    return users


_AUTH_USERS = _load_users()
_AUTH_REALM = os.environ.get("AUTH_REALM", "Vacation Planner")


def _check_basic_auth(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    expected = _AUTH_USERS.get(user)
    if expected is None:
        # Still compare to avoid user-enumeration via timing.
        secrets.compare_digest(password, password)
        return False
    return secrets.compare_digest(password, expected)


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    # /healthz is a public liveness probe — Railway / curl need it to work
    # without credentials to distinguish "app is down" from "app is up but
    # returning 401".
    if request.url.path == "/healthz":
        return await call_next(request)
    if not _AUTH_USERS:
        return await call_next(request)
    header = request.headers.get("Authorization", "")
    if header and _check_basic_auth(header):
        return await call_next(request)
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_AUTH_REALM}"'},
        content=b"Authentication required",
        media_type="text/plain",
    )


@app.exception_handler(Exception)
async def _log_unhandled_exception(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # Belt-and-suspenders: write directly to stderr in addition to the logger,
    # so even if logging config is wrong (Railway, custom uvicorn setup) the
    # traceback still lands in the deploy logs.
    print(
        f"UNHANDLED {request.method} {request.url.path}\n{tb}",
        file=sys.stderr,
        flush=True,
    )
    _error_logger.error("%s %s\n%s", request.method, request.url.path, tb)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error_type": type(exc).__name__},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    print("BOOT: startup() begin", file=sys.stderr, flush=True)
    try:
        init_trips_db()
        print("BOOT: init_trips_db ok", file=sys.stderr, flush=True)
        init_golf_db()
        print("BOOT: init_golf_db ok", file=sys.stderr, flush=True)
    except Exception:
        # Without this, a startup exception is logged at DEBUG level by
        # uvicorn and the process just dies. Print the traceback directly so
        # Railway shows *why* the app never came up.
        print("BOOT: startup FAILED", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        raise
    print("BOOT: startup() done", file=sys.stderr, flush=True)


# Lightweight liveness probe. Bypasses auth so curl/browser can hit it without
# credentials to confirm the app is actually serving requests (vs. Railway's
# edge returning 5xx because the container is unhealthy).
@app.get("/healthz")
def healthz():
    return {"ok": True}


app.include_router(trips_router)
app.include_router(golf_router)
app.include_router(yearly_router)


# ---------------------------------------------------------------------------
# Static frontend serving
# ---------------------------------------------------------------------------

_frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if _frontend_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(str(_frontend_dir / "index.html"))
