"""Artemis-only skills management (Section 5.2).

Skills live in two scopes, both isolated from the operator's real config:
- workspace: ``profile.workspace_skills`` (workspace/.claude/skills), loaded by
  the "project" setting source.
- home: ``profile.home_skills`` (claude-home/skills), loaded by the "user"
  setting source once CLAUDE_CONFIG_DIR is relocated.

Each skill is a directory containing a SKILL.md. We never overwrite an existing
user skill; agent-suggested skills go to a separate ``skills-proposed`` area for
manual review before they are activated.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from ..profiles import Profile


class SkillRegistry:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    # ----- scope resolution ------------------------------------------------

    def _scope_dir(self, scope: str) -> Path:
        if scope == "home":
            return self.profile.home_skills
        if scope == "workspace":
            return self.profile.workspace_skills
        raise ValueError(f"unknown skill scope '{scope}'")

    @property
    def _proposed_dir(self) -> Path:
        return self.profile.workspace_skills.parent / "skills-proposed"

    # ----- read ------------------------------------------------------------

    def list(self) -> list[dict[str, str]]:
        """Scan both scopes for skill dirs that contain a SKILL.md."""
        out: list[dict[str, str]] = []
        for scope, base in (("workspace", self.profile.workspace_skills),
                            ("home", self.profile.home_skills)):
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if entry.is_dir() and (entry / "SKILL.md").exists():
                    out.append({"name": entry.name, "path": str(entry), "scope": scope})
        return out

    # ----- mutate ----------------------------------------------------------

    def add(self, name: str, source_path: Optional[str] = None,
            content: Optional[str] = None, scope: str = "workspace") -> Path:
        """Install a skill. Refuse to overwrite an existing user skill."""
        base = self._scope_dir(scope)
        dest = base / name
        if dest.exists():
            raise FileExistsError(f"skill '{name}' already exists in {scope}")
        base.mkdir(parents=True, exist_ok=True)
        if source_path is not None:
            src = Path(source_path)
            if not src.is_dir():
                raise NotADirectoryError(f"source '{source_path}' is not a directory")
            shutil.copytree(src, dest)
        elif content is not None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(content)
        else:
            raise ValueError("add requires either source_path or content")
        return dest

    def propose(self, name: str, content: str) -> Path:
        """Write a proposed skill for manual review. Does not activate it."""
        dest = self._proposed_dir / name
        if dest.exists():
            raise FileExistsError(f"proposed skill '{name}' already exists")
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "SKILL.md"
        path.write_text(content)
        return path

    def remove(self, name: str, scope: str = "workspace") -> bool:
        """Delete an installed skill. Returns True if it existed."""
        dest = self._scope_dir(scope) / name
        if not dest.exists():
            return False
        shutil.rmtree(dest)
        return True
