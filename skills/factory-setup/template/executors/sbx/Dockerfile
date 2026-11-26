# Starting point for a `docker-sandbox` (`sbx`) executor (ADR-0029).
# Extends `sbx/shell-image` (`agent` user in `/home/agent`, with `git`, `gh`, `uv`, `python3`, `nodejs`, and `npm`).
# Pre-installing `pyyaml` and `jinja2` lets `factory begin/end` run without hitting PyPI on every sandbox start.
FROM docker.io/sbx/shell-image:latest
USER root
RUN uv pip install --system --break-system-packages pyyaml jinja2 \
 && mkdir -p /work /repos /opt/factory && chown -R agent:agent /work /repos /opt/factory
# Install your harness CLI below (nodejs and npm are already installed):
# RUN npm install -g <your harness CLI>
USER agent
WORKDIR /work
