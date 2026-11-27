# Starting point for a `docker` executor (ADR-0029). The engine needs git, gh, uv (or python3 + pyyaml/jinja2) and
# your harness CLIs; the engine is copied in at run time, so this image rebuilds only when this file changes.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates gh nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv pyyaml jinja2
# RUN npm install -g <your harness CLI>   (nodejs and npm are installed above)
# A non-root user: harnesses refuse to skip permission prompts as root.
RUN useradd -m -d /home/factory factory && mkdir -p /work /repos /opt/factory && chown -R factory /work /repos /opt/factory
USER factory
WORKDIR /work
