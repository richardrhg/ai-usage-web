from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .alerts import DnsResolver
from .config import Settings, load_settings
from .crypto import CredentialCipher
from .database import Database
from .routers import alerts, auth, oauth, usage
from .usage_service import UsageService


async def _background_poller_loop(svc: UsageService, interval_seconds: int) -> None:
    # Startup proactive check: immediately refresh expired/expiring tokens and sync snapshots.
    try:
        await svc.poll_all_active_credentials()
    except Exception:
        pass

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await svc.poll_all_active_credentials()
        except Exception:
            pass


def create_app(
    settings: Settings | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    dns_resolver: DnsResolver | None = None,
) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cipher = CredentialCipher(cfg.secret_key)
        db = Database(cfg.db_path, cipher)
        http_client = httpx.AsyncClient(timeout=20.0, transport=http_transport)
        svc = UsageService(
            db=db,
            settings=cfg,
            http_client=http_client,
            dns_resolver=dns_resolver,
        )

        app.state.settings = cfg
        app.state.db = db
        app.state.http_client = http_client
        app.state.usage_service = svc

        poller_task: asyncio.Task[None] | None = None
        if cfg.enable_background_poller:
            poller_task = asyncio.create_task(
                _background_poller_loop(svc, cfg.poll_interval_seconds)
            )
        else:
            # Even when periodic loop is disabled (e.g. in deterministic tests),
            # run the startup token refresh check once on boot!
            await svc.token_manager.refresh_expiring_on_startup()

        try:
            yield
        finally:
            if poller_task is not None:
                poller_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poller_task
            await http_client.aclose()

    app = FastAPI(
        title="AI Usage Web API",
        version="2.1.0",
        description=(
            "Multi-user remote AI subscription usage monitor supporting Claude, ChatGPT (Codex), "
            "and Google Gemini OAuth with Single-flight automatic token refresh, 24h SQLite trends, "
            "burn-rate exhaustion prediction, and proactive Discord/Slack/Telegram webhook alerts."
        ),
        lifespan=lifespan,
    )

    app.include_router(auth.router)
    app.include_router(oauth.router)
    app.include_router(usage.router)
    app.include_router(alerts.router)

    dist_dir = cfg.frontend_dist_dir
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        index_file = dist_dir / "index.html"
        if not index_file.is_file():
            raise HTTPException(
                status_code=404,
                detail="Frontend build not found. Run `npm run build` in frontend/ or use Vite dev server.",
            )
        candidate = (dist_dir / full_path).resolve()
        if full_path and candidate.is_file() and str(candidate).startswith(str(dist_dir.resolve())):
            return FileResponse(str(candidate))
        return FileResponse(str(index_file))

    return app


app = create_app()
