"""Discover Anthropic-style SKILL.md packages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SkillInfo:
    name: str
    description: str
    path: str  # absolute path to SKILL.md


_FRONT_NAME = re.compile(r"(?im)^name:\s*(.+)$")
_FRONT_DESC = re.compile(r"(?im)^description:\s*(.+)$")
_H1 = re.compile(r"(?m)^#\s+(.+)$")


def _parse_skill_md(text: str, fallback_name: str) -> tuple[str, str]:
    name = fallback_name
    description = ""
    if text.lstrip().startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            fm = text[3:end]
            m = _FRONT_NAME.search(fm)
            if m:
                name = m.group(1).strip().strip("\"'")
            m = _FRONT_DESC.search(fm)
            if m:
                description = m.group(1).strip().strip("\"'")
            body = text[end + 3 :]
        else:
            body = text
    else:
        body = text
    if not description:
        h1 = _H1.search(body)
        if h1:
            description = h1.group(1).strip()
        else:
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:200]
                    break
    if not description:
        description = f"Skill {name}"
    return name, description


class SkillManager:
    def __init__(self, skills_dir: str | Path) -> None:
        self.skills_dir = Path(skills_dir)

    def list_skills(self, names: list[str] | None = None) -> list[SkillInfo]:
        """None = all; [] = none; list = filter by folder/name."""
        if names is not None and len(names) == 0:
            return []
        root = self.skills_dir
        if not root.is_dir():
            return []
        found: list[SkillInfo] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            md = child / "SKILL.md"
            if not md.is_file():
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, desc = _parse_skill_md(text, child.name)
            if names is not None and name not in names and child.name not in names:
                continue
            found.append(
                SkillInfo(
                    name=name,
                    description=desc,
                    path=str(md.resolve()),
                )
            )
        return found

    def build_skills_prompt(self, names: list[str] | None = None) -> str:
        skills = self.list_skills(names)
        if not skills:
            return ""
        lines = [
            "你可以使用以下 Skills（按需用 read_file 打开 SKILL.md 全文后再遵循其中步骤）：",
        ]
        for s in skills:
            lines.append(f"- {s.name}: {s.description}")
            lines.append(f"  path: {s.path}")
        return "\n".join(lines)
