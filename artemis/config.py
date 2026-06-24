"""Global config, on-disk paths, and secret resolution.

Config lives at ``$ARTEMIS_HOME/config.yaml`` (default ``~/.artemis``). Secrets
(channel tokens, webhook secret) never land in the YAML: they resolve from env
first, then the OS keyring.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

KEYRING_SERVICE = "artemis"


def artemis_home() -> Path:
    """Root of all Artemis state. Override with ARTEMIS_HOME (used by Docker)."""
    return Path(os.environ.get("ARTEMIS_HOME", str(Path.home() / ".artemis"))).expanduser()


def config_path() -> Path:
    return artemis_home() / "config.yaml"


@dataclass
class BudgetConfig:
    per_task_usd: float = 1.0
    daily_usd: float = 20.0


@dataclass
class ApprovalConfig:
    timeout_seconds: int = 300
    default_action: str = "deny"  # action when an approval prompt times out


@dataclass
class WebhookConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    # The HMAC secret resolves from env/keyring under key "webhook_secret".


@dataclass
class GoalLoopConfig:
    enabled: bool = False
    interval_minutes: int = 30
    reflect_every: int = 5


@dataclass
class OpenAIServerConfig:
    """OpenAI-compatible HTTP endpoint (a drop-in LLM backend for other apps)."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8799
    require_auth: bool = True  # require Bearer key matching the stored secret


@dataclass
class GlobalConfig:
    default_profile: str = "default"
    default_approval_mode: str = "auto"
    default_model: str = "claude-sonnet-4-6"
    # Per channel, the sender ids permitted to command the agent. Empty list
    # means "deny all" for that channel; absent channel means not configured.
    channel_allowlists: dict[str, list[str]] = field(default_factory=dict)
    # Per-channel default conversation id for proactive delivery (cron/goal loop).
    channel_defaults: dict[str, str] = field(default_factory=dict)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    goal_loop: GoalLoopConfig = field(default_factory=GoalLoopConfig)
    openai_server: OpenAIServerConfig = field(default_factory=OpenAIServerConfig)
    log_level: str = "INFO"

    # ----- persistence -----------------------------------------------------

    @classmethod
    def load(cls) -> "GlobalConfig":
        p = config_path()
        if not p.exists():
            return cls()
        raw = yaml.safe_load(p.read_text()) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GlobalConfig":
        cfg = cls()
        for key, value in raw.items():
            if not hasattr(cfg, key):
                continue
            if key == "budgets":
                cfg.budgets = BudgetConfig(**(value or {}))
            elif key == "approval":
                cfg.approval = ApprovalConfig(**(value or {}))
            elif key == "webhook":
                cfg.webhook = WebhookConfig(**(value or {}))
            elif key == "goal_loop":
                cfg.goal_loop = GoalLoopConfig(**(value or {}))
            elif key == "openai_server":
                cfg.openai_server = OpenAIServerConfig(**(value or {}))
            else:
                setattr(cfg, key, value)
        return cfg

    def save(self) -> None:
        home = artemis_home()
        home.mkdir(parents=True, exist_ok=True)
        config_path().write_text(
            yaml.safe_dump(asdict(self), sort_keys=False, default_flow_style=False)
        )

    def sender_allowed(self, channel: str, sender_id: str) -> bool:
        """True if a sender may command the agent on a channel.

        CLI is always trusted (local). Other channels require an explicit
        allowlist entry; an unconfigured or empty allowlist denies.
        """
        if channel == "cli":
            return True
        allow = self.channel_allowlists.get(channel)
        if not allow:
            return False
        return str(sender_id) in {str(x) for x in allow}


# ---------------------------------------------------------------------------
# Secrets: env first (ARTEMIS_<PROFILE>_<KEY> or ARTEMIS_<KEY>), then keyring.
# ---------------------------------------------------------------------------

def _env_names(key: str, profile: Optional[str]) -> list[str]:
    k = key.upper()
    names = []
    if profile:
        names.append(f"ARTEMIS_{profile.upper()}_{k}")
    names.append(f"ARTEMIS_{k}")
    return names


def get_secret(key: str, profile: Optional[str] = None) -> Optional[str]:
    for name in _env_names(key, profile):
        val = os.environ.get(name)
        if val:
            return val
    try:
        import keyring

        username = f"{profile or 'global'}:{key}"
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


def set_secret(key: str, value: str, profile: Optional[str] = None) -> bool:
    """Store a secret in the OS keyring. Returns False if no keyring backend."""
    try:
        import keyring

        username = f"{profile or 'global'}:{key}"
        keyring.set_password(KEYRING_SERVICE, username, value)
        return True
    except Exception:
        return False


def ensure_api_key_env(profile: Optional[str] = None) -> None:
    """If ANTHROPIC_API_KEY is not in env but stored as a secret, load it.

    Lets headless/daemon runs use an API key saved during setup without the
    operator exporting it manually. Subscription auth does not need this.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    key = get_secret("anthropic_api_key", profile)
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


def delete_secret(key: str, profile: Optional[str] = None) -> None:
    try:
        import keyring

        username = f"{profile or 'global'}:{key}"
        keyring.delete_password(KEYRING_SERVICE, username)
    except Exception:
        pass
