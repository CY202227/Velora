"""Local control credentials kept outside agent workspaces and URLs."""
import os
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse


def read_token(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("invalid local control credential file")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("invalid local control credential")
    return token


def initialize_token(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return read_token(path)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_hex(32))
        stream.flush()
        os.fsync(stream.fileno())
    return read_token(path)


def authorized(request, state):
    expected = getattr(state, "control_token", "")
    provided = request.headers.get("x-velora-token") or request.cookies.get("velora_control") or ""
    return bool(expected and secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")))


class ControlMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/") and scope["path"] not in (
            "/api/auth/login", "/api/auth/status",
        ):
            request = Request(scope, receive)
            state = getattr(request.app.state, "velora", None)
            if state is None:
                await JSONResponse({"detail": "service not ready"}, status_code=503)(scope, receive, send)
                return
            if state.settings.api_auth_enabled:
                origin = request.headers.get("origin")
                if not authorized(request, state) or (origin and origin.rstrip("/") != str(request.base_url).rstrip("/")):
                    await JSONResponse({"detail": "local access credential required"}, status_code=401)(scope, receive, send)
                    return
        await self.app(scope, receive, send)
