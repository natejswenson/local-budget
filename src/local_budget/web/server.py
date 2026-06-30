"""Local dashboard server — app assembly, auth middleware, /health, and serve().

Binds 127.0.0.1 by default — loopback is the security boundary for a single-user
local app. Every /api/* route is gated by a bearer token IF `LOCAL_BUDGET_API_TOKEN`
is set (required when binding a non-loopback host). The HTTP routes themselves live
in `routes.py` and are mounted via `include_router` (prospector F-4).
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from . import routes

_STATIC = Path(__file__).resolve().parent / "static"
_API_TOKEN = os.environ.get("LOCAL_BUDGET_API_TOKEN")

# Minimum bearer length accepted on a non-loopback bind (design I3/§3.3 — siege S7).
# A typo'd/weak token must never gate full bank PII on the LAN; the recommended
# generator is `secrets.token_urlsafe(32)` (43 chars).
_MIN_TOKEN_LEN = 32

# Raw-file ingestion routes — the I2 boundary (design §3.7). When
# LOCAL_BUDGET_NO_INTAKE is set (the container sets it), these return 403 so raw
# bank exports (full account numbers) can never be ingested inside the container.
# This is the COMPLETE set: raw-file ingestion is a closed list. Dashboard edit
# routes stay live (interactive-dashboard decision) — they touch no raw file.
_INTAKE_ROUTES = frozenset({"/api/upload", "/api/intake/run", "/api/intake/undo"})
_NO_INTAKE = os.environ.get("LOCAL_BUDGET_NO_INTAKE", "").lower() in ("1", "true", "yes")


def create_app() -> FastAPI:
    app = FastAPI(title="local-budget", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        path = request.url.path
        # Auth is the OUTER gate. Contract (design §6): 401 = missing/invalid token
        # → the browser re-prompts for the token; 403 = raw-intake-blocked in this
        # deployment → the browser shows an error and does NOT re-auth.
        if path.startswith("/api/") and _API_TOKEN:
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token, _API_TOKEN):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        if _NO_INTAKE and path in _INTAKE_ROUTES:
            return JSONResponse(
                {"detail": "raw-file intake is disabled in this deployment (run it on the host CLI)"},
                status_code=403,
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        # Cheap liveness probe — unauthenticated (not under /api/) and touches no
        # DB or external service, so Traefik / `docker ps` can probe without the
        # token and never contend on SQLite (design I6).
        return {"status": "ok"}

    app.include_router(routes.api)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    app.mount("/", StaticFiles(directory=_STATIC), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8770) -> None:
    import uvicorn

    if host != "127.0.0.1":
        if not _API_TOKEN:
            raise SystemExit(
                "Refusing to bind a non-loopback host without LOCAL_BUDGET_API_TOKEN set "
                "(financial data). Set the token or bind 127.0.0.1."
            )
        if len(_API_TOKEN) < _MIN_TOKEN_LEN:
            raise SystemExit(
                f"Refusing to bind a non-loopback host with a weak LOCAL_BUDGET_API_TOKEN "
                f"(<{_MIN_TOKEN_LEN} chars) — it gates full bank data on the LAN. Generate one "
                f'with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
    db.init_schema()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
