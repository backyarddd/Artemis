"""OS detection and dispatch for Artemis service installation.

Picks launchd on macOS, systemd on Linux, else falls back to Docker artifacts.
Every entry point returns a human-readable string and never raises on a
missing tool.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Optional

from . import docker, launchd, systemd

# Project root holds pyproject.toml; Docker artifacts land here.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DOCKER_UP = (
    "Docker artifacts written to {root}.\n"
    "Next: copy .env.example to .env and fill in secrets, then run:\n"
    "  docker compose up -d --build"
)


def detect_os() -> str:
    sysname = platform.system().lower()
    if sysname == "darwin":
        return "darwin"
    if sysname == "linux":
        return "linux"
    return "other"


def default_method() -> str:
    os_name = detect_os()
    if os_name == "darwin":
        return "launchd"
    if os_name == "linux":
        return "systemd"
    return "docker"


def current_python() -> str:
    return sys.executable


def _resolve(method: Optional[str]) -> str:
    return method or default_method()


def install(method: Optional[str] = None, profile: Optional[str] = None) -> str:
    method = _resolve(method)
    try:
        if method == "launchd":
            return launchd.install(profile)
        if method == "systemd":
            return systemd.install(profile)
        if method == "docker":
            files = docker.generate(PROJECT_ROOT, profile or "default")
            listing = "\n".join(f"  {f}" for f in files)
            return _DOCKER_UP.format(root=PROJECT_ROOT) + "\n" + listing
        return f"unknown method: {method}"
    except Exception as exc:
        return f"install ({method}) failed: {exc}"


def status(method: Optional[str] = None) -> str:
    method = _resolve(method)
    try:
        if method == "launchd":
            return launchd.status()
        if method == "systemd":
            return systemd.status()
        if method == "docker":
            return "docker: run `docker compose ps` to inspect the service."
        return f"unknown method: {method}"
    except Exception as exc:
        return f"status ({method}) failed: {exc}"


def stop(method: Optional[str] = None) -> str:
    method = _resolve(method)
    try:
        if method == "launchd":
            return launchd.uninstall()
        if method == "systemd":
            cp = systemd._run(["systemctl", "--user", "stop", systemd.UNIT_NAME])
            if cp.returncode == 0:
                return f"stopped {systemd.UNIT_NAME}"
            return f"stop failed: {(cp.stderr or '').strip()}"
        if method == "docker":
            return "docker: run `docker compose down` to stop the service."
        return f"unknown method: {method}"
    except Exception as exc:
        return f"stop ({method}) failed: {exc}"


def restart(method: Optional[str] = None) -> str:
    method = _resolve(method)
    try:
        if method == "launchd":
            return launchd.restart()
        if method == "systemd":
            return systemd.restart()
        if method == "docker":
            return "docker: run `docker compose restart` to restart the service."
        return f"unknown method: {method}"
    except Exception as exc:
        return f"restart ({method}) failed: {exc}"


def uninstall(method: Optional[str] = None) -> str:
    method = _resolve(method)
    try:
        if method == "launchd":
            return launchd.uninstall()
        if method == "systemd":
            return systemd.uninstall()
        if method == "docker":
            return (
                "docker: run `docker compose down` and remove the generated "
                "Dockerfile / docker-compose.yml if no longer needed."
            )
        return f"unknown method: {method}"
    except Exception as exc:
        return f"uninstall ({method}) failed: {exc}"
