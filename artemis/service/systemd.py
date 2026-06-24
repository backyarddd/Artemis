"""Linux systemd user unit for the Artemis daemon.

Renders and installs ``~/.config/systemd/user/artemis.service`` running
``<python> -m artemis daemon run`` with Restart=on-failure. Needs
``loginctl enable-linger $USER`` to run without an active login session.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

UNIT_NAME = "artemis.service"

_BASE_PATH_DIRS = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def _service_path(python: str) -> str:
    venv_bin = str(Path(python).parent)
    dirs = [venv_bin] + [d for d in _BASE_PATH_DIRS if d != venv_bin]
    return ":".join(dirs)


def render_unit(python: str, artemis_home: str, profile: Optional[str]) -> str:
    """Return the systemd user unit file as text."""
    home = Path(artemis_home)
    env_lines = [
        f'Environment="ARTEMIS_HOME={artemis_home}"',
        f'Environment="PATH={_service_path(python)}"',
    ]
    if profile:
        env_lines.append(f'Environment="ARTEMIS_PROFILE={profile}"')
    env_block = "\n".join(env_lines)
    out_log = home / "daemon.out.log"
    err_log = home / "daemon.err.log"
    return f"""[Unit]
Description=Artemis autonomous agent daemon
After=network.target

[Service]
Type=simple
ExecStart={python} -m artemis daemon run
Restart=on-failure
RestartSec=10
WorkingDirectory={artemis_home}
{env_block}
StandardOutput=append:{out_log}
StandardError=append:{err_log}

[Install]
WantedBy=default.target
"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def install(profile: Optional[str] = None) -> str:
    """Write the unit, reload, then enable + start it."""
    from ..config import artemis_home
    import sys

    home = artemis_home()
    home.mkdir(parents=True, exist_ok=True)
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(sys.executable, str(home), profile))

    lines = [f"wrote {path}"]
    reload_cp = _run(["systemctl", "--user", "daemon-reload"])
    if reload_cp.returncode != 0:
        lines.append(f"daemon-reload failed: {(reload_cp.stderr or '').strip()}")
    enable = _run(["systemctl", "--user", "enable", "--now", UNIT_NAME])
    if enable.returncode == 0:
        lines.append(f"enabled + started {UNIT_NAME}")
    else:
        lines.append(f"enable --now failed: {(enable.stderr or '').strip()}")
    lines.append(
        "note: run `loginctl enable-linger $USER` so the service runs without "
        "an active login session."
    )
    return "\n".join(lines)


def uninstall() -> str:
    path = unit_path()
    lines = []
    _run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
    if path.exists():
        try:
            path.unlink()
            lines.append(f"removed {path}")
        except OSError as exc:
            lines.append(f"could not remove {path}: {exc}")
    else:
        lines.append(f"no unit at {path}")
    _run(["systemctl", "--user", "daemon-reload"])
    return "\n".join(lines)


def status() -> str:
    cp = _run(["systemctl", "--user", "status", UNIT_NAME])
    out = (cp.stdout or cp.stderr or "").strip()
    return out or f"{UNIT_NAME}: no status available"


def restart() -> str:
    cp = _run(["systemctl", "--user", "restart", UNIT_NAME])
    if cp.returncode == 0:
        return f"restarted {UNIT_NAME}"
    return f"restart failed: {(cp.stderr or '').strip()}"
