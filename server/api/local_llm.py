"""Builtin local LLM enable / status APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/local-llm", tags=["local-llm"])


@router.get("/status")
async def local_llm_status(request: Request) -> dict:
    svc = request.app.state.velora.local_llm
    if svc is None:
        raise HTTPException(500, "local llm service not initialized")
    return svc.status()


@router.post("/enable")
async def local_llm_enable(request: Request) -> dict:
    state = request.app.state.velora
    svc = state.local_llm
    if svc is None:
        raise HTTPException(500, "local llm service not initialized")
    try:
        return await svc.enable(state.store)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/disable")
async def local_llm_disable(request: Request) -> dict:
    state = request.app.state.velora
    svc = state.local_llm
    if svc is None:
        raise HTTPException(500, "local llm service not initialized")
    try:
        return await svc.disable(state.store)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/cancel-download")
async def local_llm_cancel_download(request: Request) -> dict:
    svc = request.app.state.velora.local_llm
    if svc is None:
        raise HTTPException(500, "local llm service not initialized")
    return svc.cancel_download()
