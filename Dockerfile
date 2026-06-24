# Artemis daemon image.
FROM python:3.11-slim

# git + curl for the SDK and tooling; Node 20 LTS for the bundled `claude` CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv drives the project install.
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY . /app
RUN uv sync --frozen

ENV ARTEMIS_HOME=/data
VOLUME ["/data"]

CMD ["uv", "run", "python", "-m", "artemis", "daemon", "run"]
