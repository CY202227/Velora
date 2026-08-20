"""Read-only skills list."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from server.core.persona.default import DEFAULT_PERSONA

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
async def list_skills(request: Request) -> dict[str, Any]:
    state = request.app.state.velora
    skills_mgr = getattr(state, "skills", None)
    if skills_mgr is None:
        return {"count": 0, "skills": [], "skills_dir": ""}
    items = skills_mgr.list_skills(DEFAULT_PERSONA.skill_names)
    return {
        "count": len(items),
        "skills_dir": str(skills_mgr.skills_dir),
        "skills": [
            {"name": s.name, "description": s.description, "path": s.path}
            for s in items
        ],
    }
