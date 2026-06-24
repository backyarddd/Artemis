"""Docker artifacts for running the Artemis daemon in a container.

Generates a Dockerfile, docker-compose.yml, and .env.example. The image is
python:3.11-slim plus a Node 20 LTS (the SDK bundles a node-based `claude`
CLI), with the project synced via uv.
"""

from __future__ import annotations

from pathlib import Path

_DOCKERFILE = """# Artemis daemon image.
FROM python:3.11-slim

# git + curl for the SDK and tooling; Node 20 LTS for the bundled `claude` CLI.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends git curl ca-certificates \\
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

# uv drives the project install.
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY . /app
RUN uv sync --frozen

ENV ARTEMIS_HOME=/data
VOLUME ["/data"]

CMD ["uv", "run", "python", "-m", "artemis", "daemon", "run"]
"""

_COMPOSE = """services:
  artemis:
    build: .
    image: artemis:latest
    container_name: artemis
    env_file:
      - .env
    environment:
      ARTEMIS_HOME: /data
      ARTEMIS_PROFILE: {profile}
    volumes:
      - ./data:/data
    restart: unless-stopped
    # To expose the OpenAI-compatible endpoint, set openai_server.host=0.0.0.0
    # in config and uncomment:
    # ports:
    #   - "8799:8799"
"""

_ENV_EXAMPLE = """# Artemis container secrets. Copy to .env and fill in.
# Anthropic API key (skip the operator auth bridge when set).
ANTHROPIC_API_KEY=

# Channel tokens.
ARTEMIS_TELEGRAM_TOKEN=
ARTEMIS_DISCORD_TOKEN=

# Inbound webhook HMAC secret.
ARTEMIS_WEBHOOK_SECRET=

# OpenAI-compatible endpoint API key (Bearer). Auto-generated if blank.
ARTEMIS_OPENAI_API_KEY=
"""


def generate(dest_dir: Path, profile: str = "default") -> list[Path]:
    """Write Dockerfile, docker-compose.yml, and .env.example. Returns paths."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    files = {
        "Dockerfile": _DOCKERFILE,
        "docker-compose.yml": _COMPOSE.format(profile=profile),
        ".env.example": _ENV_EXAMPLE,
    }
    for name, content in files.items():
        path = dest_dir / name
        path.write_text(content)
        written.append(path)
    return written
