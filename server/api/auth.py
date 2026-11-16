"""Authenticate the local terminal and management interface."""
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server.core.control import authorized

router = APIRouter(prefix="/api/auth", tags=["local access"])


@router.get("/status")
async def status(request: Request):
    state = request.app.state.velora
    return {"authenticated": not state.settings.api_auth_enabled or authorized(request, state)}


class Login(BaseModel):
    token: str = Field(min_length=64, max_length=64)


@router.post("/login")
async def login(body: Login, request: Request):
    origin = request.headers.get("origin")
    if (not request.client or request.client.host not in ("127.0.0.1", "::1")
            or (origin and origin.rstrip("/") != str(request.base_url).rstrip("/"))):
        raise HTTPException(403, "local same-origin login required")
    expected = request.app.state.velora.control_token
    if not expected or not secrets.compare_digest(body.token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "invalid access credential")
    response = JSONResponse({"authenticated": True})
    response.set_cookie("velora_control", expected, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/api", max_age=86400)
    return response
