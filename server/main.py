"""Velora FastAPI entrypoint."""

from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager, AsyncExitStack

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from fastapi.staticfiles import StaticFiles

from server.api import (
    chat,
    auth as auth_api,
    files as files_api,
    execution as execution_api,
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
from server.core.control import initialize_token, ControlMiddleware

_STATIC = Path(__file__).resolve().parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("velora")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from server.core.runtime_lease import RuntimeLease
    settings = get_settings()
    with RuntimeLease(settings.database_url, Path(settings.control_token_path).parent):
        async with owned_lifespan(app):
            yield


@asynccontextmanager
async def owned_lifespan(app: FastAPI):
    settings = get_settings()
    state = build_app_state(settings)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(state.store.close)
        def stop_sidecars():
            if state.memory_sidecar is not None:
                state.memory_sidecar.stop()
            if state.local_llm is not None:
                state.local_llm.sidecar.stop()
        cleanup.callback(stop_sidecars)
        cleanup.push_async_callback(state.consolidate_job.stop)
        cleanup.push_async_callback(state.reminder_job.stop)
        cleanup.push_async_callback(state.mcp.stop)
        if state.execution is not None:
            cleanup.push_async_callback(state.execution.close)
        if state.tasks is not None:
            cleanup.push_async_callback(state.tasks.close)
        await state.store.init()
        await state.router.execution_store.reconcile()
        if state.settings.api_auth_enabled:
            state.control_token = initialize_token(state.settings.control_token_path)
        if state.tasks is not None:
            expired_runs = await state.tasks.expired_run_ids()
            from server.core.agent.maintenance import RuntimeArchive
            from server.core.execution.artifacts import OutputStore
            try:
                await RuntimeArchive(state.store._session_factory, settings.runtime_archive_dir, settings.runtime_archive_storage_bytes,
                    outputs=OutputStore(settings.execution_output_dir), receipts=state.router.execution_store.receipts).prune(expired_runs)
                await asyncio.to_thread(state.router.snapshots.prune, expired_runs, state.settings.task_history_days)
                await state.tasks.prune()
            except (OSError, ValueError):
                logger.exception("Runtime history maintenance stopped; unarchived evidence retained")
        await load_settings(state.store, state.settings, state.provider)
        sync_optional_tools(state.tools, state.settings)
        # memory client may need URL from persisted settings
        state.memory.base_url = state.settings.atom_memory_base_url.rstrip("/")
        state.memory.api_key = state.settings.atom_memory_api_key
        startup = asyncio.create_task(asyncio.to_thread(start_memory_sidecar, state.settings))
        try:
            state.memory_sidecar = await asyncio.shield(startup)
        except asyncio.CancelledError:
            dependency = await startup
            dependency.stop()
            raise
        try:
            await state.memory.ensure_space(state.settings.memory_space_uid)
        except Exception as exc:
            if state.settings.memory_required:
                raise RuntimeError("atom-memory dependency is not ready; Velora startup stopped") from exc
            logger.warning("Explicit degraded mode: atom-memory unavailable at %s", state.settings.atom_memory_base_url)
        if state.local_llm is not None:
            state.local_llm.try_autostart()
            state.provider.use_local_sampling = state.settings.local_llm_enabled
        state.consolidate_job.start()
        state.reminder_job.start()
        await state.mcp.start(state.tools)
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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Velora", version="0.1.0", lifespan=lifespan)
    @app.exception_handler(SQLAlchemyError)
    async def persistence_unavailable(request, exc):
        logger.error("Runtime persistence unavailable (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"result": {"status": "failed", "reason": "persistence_unavailable",
            "message": "运行记录暂时无法保存，无法确认结果；请检查存储，不要重复执行可能已有副作用的操作。"}})
    app.add_middleware(ControlMiddleware)
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
    app.include_router(auth_api.router)
    app.include_router(files_api.router)
    app.include_router(execution_api.router)
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
