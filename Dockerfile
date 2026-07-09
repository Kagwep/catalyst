# Catalyst — Croo provider agent (always-on).
#
# This image runs the long-lived WebSocket listener that can't live on GitHub
# Actions: it holds a persistent connection to the Croo Network, and on each
# paid order runs the deterministic pipeline against the SAME Supabase Postgres
# the poller writes to, then delivers the canonical `catalyst.signals` payload.
#
# Extras baked in:
#   pg  — REQUIRED here: the provider reads the hosted Postgres (DATABASE_URL).
#   llm — OPTIONAL enrichment path. Installed so it *can* run, but it stays OFF
#         at runtime unless you invoke enrichment AND set ANTHROPIC_API_KEY.
#         The provider itself never enriches; this only matters if you also run
#         `catalyst poll ... --llm` from this same image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 1) Dependency layer (cached until the lockfile changes).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra pg --extra llm

# 2) Project layer.
COPY . .
RUN uv sync --frozen --extra pg --extra llm

# Outbound WS client — no port to EXPOSE. DATABASE_URL selects the Postgres
# backend automatically; --db is ignored when it's set.
CMD ["uv", "run", "catalyst", "croo-provider"]
