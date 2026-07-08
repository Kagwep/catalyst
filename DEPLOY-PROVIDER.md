# Hosting the Croo provider agent — Railway

The provider (`catalyst croo-provider`) holds a **persistent WebSocket** to the
Croo Network, so — unlike the [poller](HOSTING.md) — it can't run on GitHub
Actions' short-lived jobs. It needs a small always-on host. This guide uses
**Railway**, but the `Dockerfile` works on any container host (Fly, Render,
Hetzner + `docker run`, etc.).

```
Railway (always-on container)
  catalyst croo-provider
    ├── WS  ⇄  wss://api.croo.network/ws     ← paid orders arrive here
    └── reads ──►  Supabase Postgres          ← the SAME DB the poller writes
```

The provider is **read-only** against the store: the poller ingests + enriches
into Supabase on its cron; the provider just reads the freshest rows and runs
the deterministic `signal → bias → planner` math on each `ORDER_PAID`. No
network ingest happens in the paid path, so delivery stays inside the SLA.

---

## 1. Prerequisites

- The poller is already writing to Supabase (see `HOSTING.md`). The provider
  points at the **same** `DATABASE_URL`.
- A registered Croo service + funded provider AA wallet. Service id on file:
  `784122a0-b943-4b95-8d66-f7c7896e7eba`.
- A Railway account (Hobby plan, ~$5/mo of usage — a single WS process sits
  well under it).

## 2. Create the Railway service

1. Railway → **New Project** → **Deploy from GitHub repo** → pick `catalyst`.
2. Railway auto-detects `railway.toml` + `Dockerfile` and builds the image.
   No start command to type — it's in `railway.toml`.
3. Set the environment variables (next section), then **Deploy**.

> **Do not scale replicas.** `numReplicas` is pinned to 1 in `railway.toml`.
> Two replicas would open two WS connections on the same SDK key and could
> double-deliver an order.

## 3. Environment variables

### Required

| Var                 | What                                                        |
| ------------------- | ---------------------------------------------------------- |
| `CROO_API_URL`      | `https://api.croo.network`                                 |
| `CROO_WS_URL`       | `wss://api.croo.network/ws`                                 |
| `CROO_SDK_KEY`      | this provider agent's SDK key                              |
| `DATABASE_URL`      | Supabase **Session pooler** DSN, **no password** (see `HOSTING.md` §1) |
| `DATABASE_PASSWORD` | the DB password, raw (passed out-of-band, no URL-encoding) |

### Optional

| Var                 | What                                                                   |
| ------------------- | ---------------------------------------------------------------------- |
| `BASE_RPC_URL`      | Base RPC override (defaults to the SDK's; gas is platform-sponsored)   |
| `ANTHROPIC_API_KEY` | Optional. When set, turns on the provider's grounded narration (`summary`/`catalyst_notes`/`layer_notes`). Unset → deterministic delivery. See §4. |
| `CROO_EVENTS_SERVICE_ID` | Optional. The `service_id` of the registered `catalyst.events` feed. When set, the provider ALSO serves that second service (routes by `order.service_id`). Unset → signals-only. |

The provider needs `CROO_*` + the two `DATABASE_*` vars and nothing else.
`--db` is ignored whenever `DATABASE_URL` is set, so the provider always talks
to Supabase.

## 4. The two optional LLM paths (both keyed, both off without a key)

`ANTHROPIC_API_KEY` is **always optional**. Without it everything runs
deterministically. There are two *separate* places a key can be used — don't
confuse them:

**a) Grounded narration (runs on THIS provider box).** At delivery time the
provider can add plain-language `summary`, `catalyst_notes`, and `layer_notes`
to the payload — a readable interpretation for the agents/humans consuming it.
It is a **presentation layer only**: it restates the numbers the pipeline
already computed and can never change or invent them (it's handed only the
computed facts, and any note about a catalyst/layer that isn't really present is
dropped). It turns **on automatically when `ANTHROPIC_API_KEY` is set**, off
otherwise. Controls: `--no-present` forces it off even with a key;
`--present-model claude-haiku-4-5` picks a cheaper model (default
`claude-opus-4-8`). One call per delivered order → bounded cost on the paid path.

**b) Post enrichment (runs on the POLLER, not here).** LLM scoring of individual
posts during ingest. Only activates with `catalyst poll ... --llm` **and** a key.
The provider never enriches — it reads already-enriched rows. Relevant here only
if you also run the poller off this same image (e.g. a second Railway service for
a clean, non-Actions IP for sources that block datacenter ranges).

So: no key → deterministic delivery, no narrative fields, everything works. Key
present → the provider adds grounded narration; `--llm` (poller) adds post
scoring. Nothing is hardcoded on or off.

## 5. Verify

- Railway **Deploy logs** should show:
  `croo provider listening (db=…, coverage=all)`
- A full paid round-trip needs a **second agent as buyer** — Croo blocks
  self-orders (`cannot negotiate own service`). Fund a second AA wallet with a
  few cents of USDC (gas is sponsored) and order via
  `scripts/croo_smoke_requester.py` with `CROO_REQUESTER_SDK_KEY` set.
- On delivery the logs show `delivered order <id> (<n> actions)` and Croo
  auto-settles the escrow to the provider wallet.

## 6. Smoke test before going live

Deploy once with the no-op pipeline to prove auth + WS + accept/deliver against
the real backend without trusting the pipeline:

```
uv run catalyst croo-provider --no-op
```

It accepts every order and delivers a static probe payload. Swap back to the
default start command once the round-trip is green.
