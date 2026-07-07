# Hosting the poller — Supabase + GitHub Actions

This hosts the **ingestion poller** (fetch → enrich → plan each cycle) with **no
always-on server**: GitHub Actions runs one poll cycle on a cron schedule, and
the data lives in a hosted **Supabase Postgres** database instead of the local
`catalyst.db`.

```
GitHub Actions (cron every ~15 min)
   └─ catalyst poll --once   ← one cycle, then exits
        └── reads/writes ──►  Supabase Postgres   ← the permanent DB
```

**Backend switch:** the store uses Postgres when the `DATABASE_URL` env var is
set (see `catalyst/pg.py`), and local SQLite otherwise. Nothing else in the code
changes — same `open_store()`, same functions.

> **Out of scope:** the Croo provider agent (`croo_agent.py`) opens a long-lived
> WebSocket and can't run on Actions' short-lived jobs. It needs a persistent
> host (a small VM / Fly / Render) pointed at the same `DATABASE_URL`. This guide
> covers ingestion only.

---

## 1. Create the Supabase project

1. Go to [supabase.com](https://supabase.com) → **New project**. Pick a region
   near you; note the database password you set.
2. Project Settings → **Database** → **Connection string** → **URI**. Use the
   **Session pooler** string (IPv4, works from GitHub Actions). It looks like:
   ```
   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```
   The direct (`db.<ref>.supabase.co`) connection is IPv6-only and won't work
   from Actions — use the pooler.

   **Password handling.** Rather than embed the password in the URL (which
   forces percent-encoding of any `@ : / # &` symbols), split it out:
   - `DATABASE_URL` = the DSN **without** the password:
     `postgresql://postgres.<ref>@aws-0-<region>.pooler.supabase.com:5432/postgres`
   - `DATABASE_PASSWORD` = the raw password (symbols need **no** encoding).

   The store passes `DATABASE_PASSWORD` to libpq as a separate parameter.

You don't need to create tables by hand: the schema (`catalyst/pg.py`
`_PG_SCHEMA`) is applied automatically the first time the code connects.

Free tier is plenty: 500 MB storage (your data is ~1 MB), and a project only
auto-pauses after **7 days** with no activity — the 15-minute poller keeps it
awake.

## 2. Migrate your existing data (optional but recommended)

Bring the posts/snapshots you've already collected up to Supabase:

```powershell
$env:DATABASE_URL = "postgresql://postgres.<ref>:<pw>@...pooler.supabase.com:5432/postgres"
uv run python scripts/migrate_to_pg.py catalyst.db
```

This creates the schema and copies `posts`, `actions`, `bias_snapshots`,
`alerts`, `monitor_fires`, `cycle_health`. It's idempotent (safe to re-run). The
row counts it prints double as a connection smoke test.

## 3. Add the GitHub secret

Push this repo to GitHub (public — public repos get **unlimited** Actions
minutes). Then: repo **Settings → Secrets and variables → Actions → New
repository secret**:

| Secret | Required | Notes |
|---|---|---|
| `DATABASE_URL` | **yes** | Session-pooler URI from step 1, **without** the password |
| `DATABASE_PASSWORD` | **yes** | the raw DB password (no URL-encoding needed) |
| `ANTHROPIC_API_KEY` | no | only if you enable `--llm` enrichment |
| `FRED_API_KEY` | no | only if you enable `--fred` macro series |

Secrets are encrypted and are **not** exposed by a public repo.

## 4. Run it

The workflow (`.github/workflows/poll.yml`) runs every 15 minutes. To test
immediately: repo **Actions → poll → Run workflow** (the `workflow_dispatch`
button). Watch the run; on success, open the Supabase **Table editor** and you'll
see rows landing in `posts` and `cycle_health`.

The cycle number continues across runs (it's read from `cycle_health` in
Postgres), so the ops sequence stays monotonic even though each run is a fresh
container.

### Tuning

- **Cadence:** edit the `cron:` in `poll.yml`. GitHub may fire scheduled runs a
  few minutes late under load — the pipeline tolerates jitter.
- **LLM enrichment:** a ready-made variant lives in
  `.github/workflows/poll-llm.yml` (runs `poll --once --llm`). It's
  manual-only by default — add the `ANTHROPIC_API_KEY` secret, then
  **Actions → poll (LLM) → Run workflow**. To make LLM the *scheduled* poller,
  uncomment its `schedule:` block and disable `poll.yml` so only one writes on
  cron (they share a concurrency group, so they'll never overlap regardless).
- **Local dev is unchanged:** with `DATABASE_URL` unset you're back on
  `catalyst.db` — no Postgres needed to develop or run tests.
