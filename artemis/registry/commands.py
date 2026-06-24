"""Parameterized markdown command templates (Section 5.1).

A command is a `.md` file: optional YAML frontmatter between `---` fences,
then a body that is the prompt template with `{arg}` placeholders. The
orchestrator renders a command to a prompt and builds a Task from `resolve`.

User-authored commands live directly under `profile.commands_dir`. Commands an
agent proposes are written to `commands_dir/pending/` and only become active
once an operator approves them. We never auto-overwrite a user command.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ..models import ApprovalMode
from ..profiles import Profile


@dataclass
class Command:
    name: str
    description: str = ""
    args: list[dict[str, Any]] = field(default_factory=list)  # {name,type,default,required}
    allowed_tools: Optional[list[str]] = None
    approval_mode: Optional[str] = None
    default_channel: Optional[str] = None
    body: str = ""
    path: Optional[Path] = None
    pending: bool = False


def _parse(path: Path, pending: bool) -> Command:
    """Parse one command markdown file. Robust to missing/blank frontmatter."""
    text = path.read_text()
    meta: dict[str, Any] = {}
    body = text
    if text.lstrip().startswith("---"):
        # Split on the fence lines; tolerate leading whitespace before the open.
        stripped = text.lstrip()
        rest = stripped[3:]
        end = rest.find("\n---")
        if end != -1:
            front = rest[:end]
            body = rest[end + 4:]
            if body.startswith("\n"):
                body = body[1:]
            try:
                loaded = yaml.safe_load(front)
                if isinstance(loaded, dict):
                    meta = loaded
            except yaml.YAMLError:
                meta = {}
    return Command(
        name=meta.get("name") or path.stem,
        description=meta.get("description") or "",
        args=list(meta.get("args") or []),
        allowed_tools=meta.get("allowed_tools"),
        approval_mode=meta.get("approval_mode"),
        default_channel=meta.get("default_channel"),
        body=body.strip(),
        path=path,
        pending=pending,
    )


class CommandRegistry:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.dir = profile.commands_dir
        self.pending_dir = self.dir / "pending"

    # ----- load ------------------------------------------------------------

    def load_all(self) -> dict[str, Command]:
        """Parse every *.md under commands_dir and commands_dir/pending."""
        out: dict[str, Command] = {}
        for path in sorted(self.dir.glob("*.md")):
            cmd = _parse(path, pending=False)
            out[cmd.name] = cmd
        if self.pending_dir.is_dir():
            for path in sorted(self.pending_dir.glob("*.md")):
                cmd = _parse(path, pending=True)
                # Active command of the same name wins over a pending one.
                out.setdefault(cmd.name, cmd)
        return out

    def list(self) -> list[Command]:
        return list(self.load_all().values())

    def get(self, name: str) -> Optional[Command]:
        return self.load_all().get(name)

    # ----- render ----------------------------------------------------------

    def render(self, name: str, args: dict[str, Any]) -> str:
        """Merge args over declared defaults and substitute `{arg}` tokens.

        Raises ValueError when a required arg is missing. Stray braces in the
        body are left intact (no str.format), so templates never crash.
        """
        cmd = self.get(name)
        if cmd is None:
            raise ValueError(f"unknown command '{name}'")
        merged: dict[str, Any] = {}
        for decl in cmd.args:
            aname = decl.get("name")
            if not aname:
                continue
            if "default" in decl:
                merged[aname] = decl["default"]
        merged.update({k: v for k, v in (args or {}).items() if v is not None})
        for decl in cmd.args:
            aname = decl.get("name")
            if decl.get("required") and (aname not in merged or merged[aname] is None):
                raise ValueError(f"command '{name}' requires arg '{aname}'")
        body = cmd.body
        for decl in cmd.args:
            aname = decl.get("name")
            if aname is None:
                continue
            value = "" if merged.get(aname) is None else str(merged.get(aname))
            body = body.replace("{" + aname + "}", value)
        return body

    def resolve(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Render plus the dispatch metadata the orchestrator needs."""
        cmd = self.get(name)
        if cmd is None:
            raise ValueError(f"unknown command '{name}'")
        # No declared mode means "let the orchestrator decide" (None), not AUTO.
        mode = ApprovalMode.coerce(cmd.approval_mode, None) if cmd.approval_mode else None
        return {
            "prompt": self.render(name, args),
            "allowed_tools": cmd.allowed_tools,
            "approval_mode": mode or None,
            "default_channel": cmd.default_channel,
        }

    # ----- mutate ----------------------------------------------------------

    def create(self, name: str, description: str, body: str,
               args: Optional[list[dict[str, Any]]] = None,
               allowed_tools: Optional[list[str]] = None,
               approval_mode: Optional[str] = None,
               default_channel: Optional[str] = None,
               pending: bool = False, overwrite: bool = False) -> Path:
        """Write a command md file. Refuse to clobber a user command."""
        active = self.dir / f"{name}.md"
        if active.exists() and not overwrite:
            raise FileExistsError(f"command '{name}' already exists (active)")
        target_dir = self.pending_dir if pending else self.dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.md"
        if path.exists() and not overwrite:
            raise FileExistsError(f"command '{name}' already exists")
        meta: dict[str, Any] = {"name": name, "description": description}
        if args:
            meta["args"] = args
        if allowed_tools is not None:
            meta["allowed_tools"] = allowed_tools
        if approval_mode is not None:
            meta["approval_mode"] = approval_mode
        if default_channel is not None:
            meta["default_channel"] = default_channel
        front = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False).strip()
        path.write_text(f"---\n{front}\n---\n\n{body.strip()}\n")
        return path

    def approve(self, name: str) -> Path:
        """Move a pending command into the active commands dir."""
        src = self.pending_dir / f"{name}.md"
        if not src.exists():
            raise FileNotFoundError(f"no pending command '{name}'")
        dest = self.dir / f"{name}.md"
        if dest.exists():
            raise FileExistsError(f"command '{name}' already exists; will not overwrite")
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return dest

    def remove(self, name: str) -> bool:
        """Delete a command (active or pending). Returns True if anything went."""
        removed = False
        for path in (self.dir / f"{name}.md", self.pending_dir / f"{name}.md"):
            if path.exists():
                path.unlink()
                removed = True
        return removed


# ---------------------------------------------------------------------------
# Starter pack: small, editable commands seeded once per profile.
# ---------------------------------------------------------------------------

_STARTER: list[dict[str, Any]] = [
    {
        "name": "research",
        "description": "Research a topic and report findings with sources.",
        "args": [{"name": "topic", "type": "string", "required": True}],
        "body": (
            "Research the topic: {topic}.\n"
            "Gather reputable sources, cross-check key claims, and report a\n"
            "concise summary with the sources you relied on."
        ),
    },
    {
        "name": "ship",
        "description": "Commit and push a scoped change.",
        "args": [{"name": "scope", "type": "string", "required": True}],
        "approval_mode": "auto",
        "body": (
            "Make the scoped change: {scope}.\n"
            "Then commit it with a conventional message and push the branch.\n"
            "Keep the diff minimal and run the tests before pushing."
        ),
    },
    {
        "name": "standup",
        "description": "Summarize recent activity.",
        "args": [],
        "body": (
            "Summarize recent activity in this workspace: what changed, what is\n"
            "in flight, and what is blocked. Keep it to a short bulleted list."
        ),
    },
    {
        "name": "track",
        "description": "Run a monitoring task against a target.",
        "args": [{"name": "target", "type": "string", "required": True}],
        "body": (
            "Check the current state of: {target}.\n"
            "Report what changed since the last check and flag anything that\n"
            "needs attention."
        ),
    },
    {
        "name": "digest",
        "description": "Summarize new items into a short digest.",
        "args": [],
        "body": (
            "Collect new items since the last digest and summarize them into a\n"
            "short, skimmable digest grouped by theme."
        ),
    },
]


def seed_starter_pack(profile: Profile) -> None:
    """Write the starter commands if absent. Never overwrites existing files."""
    reg = CommandRegistry(profile)
    reg.dir.mkdir(parents=True, exist_ok=True)
    for spec in _STARTER:
        if (reg.dir / f"{spec['name']}.md").exists():
            continue
        reg.create(
            name=spec["name"],
            description=spec["description"],
            body=spec["body"],
            args=spec.get("args"),
            approval_mode=spec.get("approval_mode"),
        )
