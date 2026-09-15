"""Velora FastAPI entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.api import (
    chat,
    debug,
    local_llm as local_llm_api,
    mcp as mcp_api,
    memory,
    personas as personas_api,
    reminders,
    sessions,
    settings as settings_api,
    skills as skills_api,
)
from server.app_state import build_app_state, sync_optional_tools
from server.config import get_settings
from server.core.memory.sidecar import start_memory_sidecar
from server.core.settings_store import load_settings

_STATIC = Path(__file__).resolve().parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("velora")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    state = build_app_state(settings)
    await state.store.init()
    await load_settings(state.store, state.settings, state.provider)
    sync_optional_tools(state.tools, state.settings)
    # memory client may need URL from persisted settings
    state.memory.base_url = state.settings.atom_memory_base_url.rstrip("/")
    state.memory.api_key = state.settings.atom_memory_api_key
    state.memory_sidecar = start_memory_sidecar(state.settings)
    if state.local_llm is not None:
        state.local_llm.try_autostart()
        state.provider.use_local_sampling = state.settings.local_llm_enabled
    state.consolidate_job.start()
    state.reminder_job.start()
    await state.mcp.start(state.tools)
    try:
        await state.memory.ensure_space(state.settings.memory_space_uid)
    except Exception:
        logger.warning(
            "atom-memory unreachable at %s — chat will degrade without recall",
            state.settings.atom_memory_base_url,
        )
    app.state.velora = state
    mcp_st = state.mcp.status()
    logger.info(
        "Velora ready on %s:%s (llm=%s model=%s sidecar=%s local_llm=%s reminders=%s tz=%s mcp_tools=%s)",
        state.settings.host,
        state.settings.port,
        state.settings.llm_base_url,
        state.settings.llm_model,
        bool(
            state.memory_sidecar and state.memory_sidecar.started_by_us),
        bool(
            state.local_llm
            and state.local_llm.sidecar
            and state.local_llm.sidecar.started_by_us
        ),
        state.settings.reminders_enabled,
        state.settings.user_timezone,
        mcp_st.get("tool_count", 0),
    )
    yield
    await state.mcp.stop()
    await state.reminder_job.stop()
    await state.consolidate_job.stop()
    if state.local_llm is not None:
        state.local_llm.sidecar.stop()
    if state.memory_sidecar is not None:
        state.memory_sidecar.stop()
    await state.store.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Velora", version="0.1.0", lifespan=lifespan)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(reminders.router)
    app.include_router(personas_api.router)
    app.include_router(mcp_api.router)
    app.include_router(skills_api.router)
    app.include_router(settings_api.router)
    app.include_router(local_llm_api.router)
    app.include_router(debug.router)

    if _STATIC.is_dir():
        assets_dir = _STATIC / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="assets",
            )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "velora"}

    @app.get("/")
    async def desk_index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
