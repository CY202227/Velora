from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from server.api.auth import router
from server.config import Settings
from server.core.control import ControlMiddleware, initialize_token, read_token


def application():
    app = FastAPI()
    app.add_middleware(ControlMiddleware)
    app.include_router(router)
    app.state.velora = SimpleNamespace(settings=Settings(_env_file=None), control_token="a" * 64)
    @app.get("/api/private")
    async def private():
        return {"protected": True}
    return app


def test_local_control_key_is_stable_and_not_accepted_if_invalid(tmp_path):
    path = tmp_path / "control.token"
    first = initialize_token(path)
    assert len(first) == 64 and initialize_token(path) == first
    assert read_token(path) == first
    path.write_text("not a strong credential")
    with pytest.raises(ValueError):
        initialize_token(path)


@pytest.mark.asyncio
async def test_header_cookie_and_cross_origin_boundaries():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(), client=("127.0.0.1", 1)), base_url="http://test") as client:
        assert (await client.get("/api/private")).status_code == 401
        assert (await client.get("/api/auth/status")).json() == {"authenticated": False}
        assert (await client.get("/api/private", headers={"X-Velora-Token": "a" * 64})).status_code == 200
        assert (await client.get("/api/private", headers={"X-Velora-Token": "a" * 64, "Origin": "http://foreign"})).status_code == 401
        assert (await client.post("/api/auth/login", json={"token": "b" * 64})).status_code == 401
        response = await client.post("/api/auth/login", json={"token": "a" * 64}, headers={"Origin": "http://test"})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
        assert (await client.get("/api/private")).status_code == 200
        assert (await client.post("/api/auth/login", json={"token": "a" * 64}, headers={"Origin": "http://foreign"})).status_code == 403


@pytest.mark.asyncio
async def test_unicode_and_remote_login_are_rejected_without_server_error():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(), client=("remote", 1)), base_url="http://test") as client:
        assert (await client.post("/api/auth/login", json={"token": "a" * 64})).status_code == 403
        client.cookies.set("velora_control", "wrong")
        assert (await client.get("/api/private")).status_code == 401
