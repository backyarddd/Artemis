"""Multi-persona profiles: each is a fully isolated Artemis identity.

A profile owns its CLAUDE_CONFIG_DIR (claude-home), workspace (cwd), MCP set,
skills, commands, sqlite db, and channel bindings. The active profile resolves
from ARTEMIS_PROFILE env, else the global config default.

Auth bridge: the SDK CLI authenticates from the credentials file inside
CLAUDE_CONFIG_DIR. We copy ONLY the operator's ``claudeAiOauth`` block (never
their MCP oauth tokens) into the isolated claude-home so the agent reuses the
operator's subscription without seeing the rest of their Claude config. When
ANTHROPIC_API_KEY is set we skip bridging and let the SDK use the key.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import artemis_home

KEYCHAIN_SERVICE = "Claude Code-credentials"


@dataclass
class Profile:
    name: str
    root: Path

    @property
    def claude_home(self) -> Path:
        return self.root / "claude-home"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def mcp_json(self) -> Path:
        return self.root / "mcp.json"

    @property
    def settings_json(self) -> Path:
        return self.root / "settings.json"

    @property
    def soul_md(self) -> Path:
        return self.root / "soul.md"

    @property
    def goals_md(self) -> Path:
        return self.root / "goals.md"

    @property
    def commands_dir(self) -> Path:
        return self.root / "commands"

    @property
    def db_path(self) -> Path:
        return self.root / "artemis.db"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def workspace_claude_md(self) -> Path:
        return self.workspace / "CLAUDE.md"

    @property
    def workspace_skills(self) -> Path:
        return self.workspace / ".claude" / "skills"

    @property
    def home_skills(self) -> Path:
        return self.claude_home / "skills"

    @property
    def credentials_file(self) -> Path:
        return self.claude_home / ".credentials.json"

    @property
    def state_file(self) -> Path:
        """Artemis runtime state (allowlist, active mode) for this profile."""
        return self.root / "state.json"

    def exists(self) -> bool:
        return self.root.is_dir() and self.soul_md.exists()


# ---------------------------------------------------------------------------
# Operator credential discovery (read-only, used for the auth bridge).
# ---------------------------------------------------------------------------

def _read_keychain_blob() -> Optional[dict]:
    if platform.system() != "Darwin":
        return None
    user = os.environ.get("USER") or ""
    args = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE]
    if user:
        args += ["-a", user]
    args += ["-w"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout.strip())
    except Exception:
        return None


def _read_operator_credentials_file() -> Optional[dict]:
    p = Path.home() / ".claude" / ".credentials.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def read_operator_oauth() -> Optional[dict]:
    """Return the operator's ``claudeAiOauth`` block, or None if not logged in."""
    blob = _read_keychain_blob() or _read_operator_credentials_file()
    if not blob:
        return None
    oauth = blob.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def read_operator_identity() -> dict:
    """Operator account identity from ~/.claude.json (no secrets)."""
    p = Path.home() / ".claude.json"
    out: dict = {}
    if p.exists():
        try:
            src = json.loads(p.read_text())
            for k in ("oauthAccount", "userID", "hasCompletedOnboarding"):
                if k in src:
                    out[k] = src[k]
        except Exception:
            pass
    out.setdefault("hasCompletedOnboarding", True)
    return out


def operator_logged_in() -> bool:
    return read_operator_oauth() is not None


# ---------------------------------------------------------------------------
# Profile management.
# ---------------------------------------------------------------------------

DEFAULT_SOUL = """# Artemis Persona

You are {name}, an autonomous always-on agent.

## Identity
- Calm, terse, and decisive. No filler. No em dashes.
- You operate inside an isolated workspace and never touch the operator's
  personal machine config.

## Operating rules
- Prefer the smallest correct action. State assumptions, then act.
- Surface risky actions for approval rather than guessing.
- Keep a clear audit trail; report cost and outcome plainly.
- When you notice a repeated workflow, propose a reusable command.
"""

DEFAULT_CLAUDE_MD = """# Workspace operating context

This workspace belongs to the Artemis agent profile "{name}". Treat it as the
project root. Standing context and persona live in soul.md (appended to the
system prompt). Add project notes here.

## Rules
- Stay inside this workspace. Do not write outside it without approval.
- No em dashes. Conventional commits. No AI attribution in git.
"""

DEFAULT_GOALS = """# Standing goals for the autonomous loop

List one actionable goal per bullet. The goal loop picks the next actionable
item each heartbeat. Keep them concrete and bounded.

# - Triage anything new in the workspace inbox and summarize it.
"""


class ProfileManager:
    def __init__(self) -> None:
        self.profiles_dir = artemis_home() / "profiles"

    def path_for(self, name: str) -> Profile:
        return Profile(name=name, root=self.profiles_dir / name)

    def get(self, name: str) -> Profile:
        prof = self.path_for(name)
        if not prof.exists():
            raise FileNotFoundError(f"profile '{name}' does not exist")
        return prof

    def list(self) -> list[str]:
        if not self.profiles_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.profiles_dir.iterdir()
            if (p / "soul.md").exists()
        )

    def active_name(self) -> str:
        from .config import GlobalConfig

        return os.environ.get("ARTEMIS_PROFILE") or GlobalConfig.load().default_profile

    def active(self) -> Profile:
        return self.get(self.active_name())

    def create(self, name: str, persona: Optional[str] = None, overwrite: bool = False) -> Profile:
        """Scaffold a profile tree. Idempotent: never clobbers existing soul/goals."""
        prof = self.path_for(name)
        for d in (prof.claude_home, prof.workspace, prof.commands_dir,
                  prof.logs_dir, prof.workspace_skills, prof.home_skills):
            d.mkdir(parents=True, exist_ok=True)

        persona = persona or name
        self._seed(prof.soul_md, DEFAULT_SOUL.format(name=persona), overwrite)
        self._seed(prof.workspace_claude_md, DEFAULT_CLAUDE_MD.format(name=persona), overwrite)
        self._seed(prof.goals_md, DEFAULT_GOALS, overwrite)
        self._seed(prof.settings_json, json.dumps({"includeCoAuthoredBy": False}, indent=2), overwrite)
        self._seed(prof.mcp_json, json.dumps({"mcpServers": {}}, indent=2), overwrite)
        return prof

    @staticmethod
    def _seed(path: Path, content: str, overwrite: bool) -> None:
        if overwrite or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    # ----- auth bridge -----------------------------------------------------

    def bridge_auth(self, prof: Profile) -> str:
        """Make the operator's subscription usable inside the isolated home.

        Returns a short status string. No-op (api_key) when ANTHROPIC_API_KEY is
        set. Writes only the ``claudeAiOauth`` block plus account identity.
        """
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api_key"

        oauth = read_operator_oauth()
        if not oauth:
            return "no_session"

        prof.claude_home.mkdir(parents=True, exist_ok=True)
        creds = {"claudeAiOauth": oauth}
        prof.credentials_file.write_text(json.dumps(creds))
        os.chmod(prof.credentials_file, 0o600)

        identity = read_operator_identity()
        identity_path = prof.claude_home / ".claude.json"
        existing = {}
        if identity_path.exists():
            try:
                existing = json.loads(identity_path.read_text())
            except Exception:
                existing = {}
        existing.update(identity)
        identity_path.write_text(json.dumps(existing))
        os.chmod(identity_path, 0o600)
        return "bridged"
