"""Per-profile MCP server management (Section 5.3).

The profile's ``mcp.json`` is the single source of truth for the strict,
Artemis-only MCP set the engine loads (see engine.load_mcp_servers). We store
servers under the ``{"mcpServers": {name: config}}`` shape the SDK expects.

Recognized config shapes (SDK TypedDicts):
- stdio: ``{type?: 'stdio', command, args?, env?}`` (default when no type)
- sse:   ``{type: 'sse', url, headers?}``
- http:  ``{type: 'http', url, headers?}``
"""

from __future__ import annotations

import json
import shutil
from typing import Any

import httpx

from ..profiles import Profile

_TEST_TIMEOUT = 3.0


class McpRegistry:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.path = profile.mcp_json

    # ----- io --------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            return {}
        servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
        return servers if isinstance(servers, dict) else {}

    def _write(self, servers: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"mcpServers": servers}, indent=2))

    # ----- read ------------------------------------------------------------

    def list(self) -> dict[str, Any]:
        return self._read()

    # ----- mutate ----------------------------------------------------------

    @staticmethod
    def _validate(config: dict[str, Any]) -> str:
        """Return the resolved server type or raise ValueError on a bad shape."""
        if not isinstance(config, dict):
            raise ValueError("mcp config must be an object")
        kind = (config.get("type") or "stdio").strip().lower()
        if kind == "stdio":
            if not config.get("command"):
                raise ValueError("stdio mcp config requires 'command'")
        elif kind in ("sse", "http"):
            if not config.get("url"):
                raise ValueError(f"{kind} mcp config requires 'url'")
        else:
            raise ValueError(f"unrecognized mcp config type '{kind}'")
        return kind

    def add(self, name: str, config: dict[str, Any]) -> None:
        self._validate(config)
        servers = self._read()
        servers[name] = config
        self._write(servers)

    def remove(self, name: str) -> bool:
        servers = self._read()
        if name not in servers:
            return False
        del servers[name]
        self._write(servers)
        return True

    # ----- connectivity ----------------------------------------------------

    async def test(self, name: str) -> tuple[bool, str]:
        """Best-effort connectivity check. Never raises."""
        servers = self._read()
        config = servers.get(name)
        if config is None:
            return False, f"no mcp server '{name}'"
        try:
            kind = self._validate(config)
        except ValueError as exc:
            return False, str(exc)
        try:
            if kind == "stdio":
                return await self._test_stdio(config)
            return await self._test_url(config)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    async def _test_stdio(self, config: dict[str, Any]) -> tuple[bool, str]:
        import asyncio

        command = str(config.get("command", ""))
        resolved = shutil.which(command)
        if not resolved:
            return False, f"command '{command}' not found on PATH"
        # Launch briefly; an MCP server should stay up waiting on stdio. If it
        # exits immediately with a non-zero code, treat that as a crash.
        args = [str(a) for a in (config.get("args") or [])]
        # Merge over the current environment so the child keeps PATH/HOME etc;
        # replacing it outright causes spurious test failures.
        import os
        cfg_env = config.get("env")
        env = {**os.environ, **cfg_env} if isinstance(cfg_env, dict) else None
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except Exception as exc:
            return False, f"spawn failed: {exc}"
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=_TEST_TIMEOUT)
        except asyncio.TimeoutError:
            # Still running after the window: good sign for a stdio server.
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return True, f"command '{command}' launched and stayed up"
        if code == 0:
            return True, f"command '{command}' ran and exited cleanly"
        return False, f"command '{command}' exited immediately with code {code}"

    async def _test_url(self, config: dict[str, Any]) -> tuple[bool, str]:
        url = str(config.get("url", ""))
        headers = config.get("headers") if isinstance(config.get("headers"), dict) else None
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT, follow_redirects=True) as client:
            try:
                resp = await client.head(url, headers=headers)
                if resp.status_code >= 400:
                    resp = await client.get(url, headers=headers)
            except httpx.HTTPError:
                resp = await client.get(url, headers=headers)
            return True, f"{url} reachable (HTTP {resp.status_code})"
