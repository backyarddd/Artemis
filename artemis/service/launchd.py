"""macOS LaunchAgent for the Artemis daemon.

Renders and installs a per-user LaunchAgent that runs
``<python> -m artemis daemon run``. KeepAlive restarts on crash and survives
sleep/wake; the daemon handles its own clean SIGTERM shutdown.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

LABEL = "com.artemis.agent"

# Extra dirs so the bundled `claude` CLI and common tools resolve.
_BASE_PATH_DIRS = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _service_path(python: str) -> str:
    """PATH for the unit: venv bin dir first, then common system dirs."""
    venv_bin = str(Path(python).parent)
    dirs = [venv_bin] + [d for d in _BASE_PATH_DIRS if d != venv_bin]
    return ":".join(dirs)


def _log_paths(artemis_home: str) -> tuple[str, str]:
    home = Path(artemis_home)
    return str(home / "daemon.out.log"), str(home / "daemon.err.log")


def render_plist(python: str, artemis_home: str, profile: Optional[str]) -> str:
    """Return a valid LaunchAgent plist as text."""
    out_log, err_log = _log_paths(artemis_home)
    env = {
        "ARTEMIS_HOME": artemis_home,
        "PATH": _service_path(python),
    }
    if profile:
        env["ARTEMIS_PROFILE"] = profile

    env_lines = "\n".join(
        f"        <key>{escape(k)}</key>\n        <string>{escape(v)}</string>"
        for k, v in env.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{escape(LABEL)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{escape(python)}</string>
        <string>-m</string>
        <string>artemis</string>
        <string>daemon</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>EnvironmentVariables</key>
    <dict>
{env_lines}
    </dict>
    <key>WorkingDirectory</key>
    <string>{escape(artemis_home)}</string>
    <key>StandardOutPath</key>
    <string>{escape(out_log)}</string>
    <key>StandardErrorPath</key>
    <string>{escape(err_log)}</string>
</dict>
</plist>
"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Run a command, never raising. Returns the completed process."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # tool missing, timeout, etc.
        cp = subprocess.CompletedProcess(args, 1, "", str(exc))
        return cp


def _uid() -> int:
    return os.getuid()


def install(profile: Optional[str] = None) -> str:
    """Write the plist and (re)load it. Returns human-readable status."""
    from ..config import artemis_home
    import sys

    home = artemis_home()
    home.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_plist(sys.executable, str(home), profile))

    lines = [f"wrote {path}"]
    # Unload any prior copy first; ignore failures (may not be loaded).
    _run(["launchctl", "unload", str(path)])

    # Prefer `load -w`; fall back to modern bootstrap on failure.
    loaded = _run(["launchctl", "load", "-w", str(path)])
    if loaded.returncode == 0:
        lines.append("loaded via launchctl load -w")
    else:
        boot = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)])
        if boot.returncode == 0:
            lines.append("loaded via launchctl bootstrap")
        else:
            err = (loaded.stderr or boot.stderr or "").strip()
            lines.append(f"load failed: {err or 'unknown error'}")
    lines.append(f"logs: {home / 'daemon.out.log'} / {home / 'daemon.err.log'}")
    return "\n".join(lines)


def uninstall() -> str:
    path = plist_path()
    lines = []
    if path.exists():
        _run(["launchctl", "unload", "-w", str(path)])
        _run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"])
        try:
            path.unlink()
            lines.append(f"removed {path}")
        except OSError as exc:
            lines.append(f"could not remove {path}: {exc}")
    else:
        lines.append(f"no plist at {path}")
    return "\n".join(lines)


def status() -> str:
    cp = _run(["launchctl", "list"])
    if cp.returncode != 0:
        return f"launchctl list failed: {(cp.stderr or '').strip()}"
    matches = [ln for ln in cp.stdout.splitlines() if LABEL in ln]
    if not matches:
        return f"{LABEL}: not loaded"
    return "\n".join(matches)


def restart() -> str:
    """Kickstart the running service, falling back to unload/load."""
    kick = _run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{LABEL}"])
    if kick.returncode == 0:
        return f"restarted {LABEL} via kickstart -k"
    path = plist_path()
    _run(["launchctl", "unload", str(path)])
    loaded = _run(["launchctl", "load", "-w", str(path)])
    if loaded.returncode == 0:
        return f"restarted {LABEL} via unload/load"
    return f"restart failed: {(kick.stderr or loaded.stderr or '').strip()}"
