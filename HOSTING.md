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
| `BLUESKY_HANDLE` | **yes** | your Bluesky handle, e.g. `you.bsky.social` — see below |
| `BLUESKY_APP_PASSWORD` | **yes** | a Bluesky **app password**, never the account password |
| `ANTHROPIC_API_KEY` | no | only if you enable `--llm` enrichment |
| `FRED_API_KEY` | no | only if you enable `--fred` macro series |

Secrets are encrypted and are **not** exposed by a public repo.

**Why Bluesky needs auth here but not locally:** the public Bluesky AppView
(`public.api.bsky.app`) serves an HTML 403 to datacenter/cloud IPs, and GitHub
Actions runners are datacenter IPs. With the two `BLUESKY_*` secrets set, the
adapter logs in to the PDS and makes authenticated XRPC calls, which are not
IP-blocked. Create the app password at Bluesky **Settings → Privacy and
Security → App Passwords**.

**Derivs on Actions runners:** `fapi.binance.com` returns 451 and Bybit 403s
from US datacenter IPs (where the runners sit), so the derivs layer runs a
provider chain — **Binance → Bybit → Kraken Futures → Hyperliquid**, all
keyless. Hosted cycles fall through until one answers; nothing to configure.
Set `DERIVS_PROVIDER=binance|bybit|kraken|hyperliquid` to force one. If a
hosted cycle still shows derivs as `source_silent`, the run log carries the
combined per-provider error.

**Known loss on Actions runners (auth can't fix it):** farside.co.uk (ETF
flows) is behind Cloudflare bot protection that 403s datacenter IPs. The
poller degrades gracefully — the source skips and the ops layer flags it
`source_silent` — but hosted cycles run without the flows layer until that
source is replaced or the poller moves to a box with a clean IP.

## 4. Run it

The workflow (`.github/workflows/poll.yml`) has a `*/15` cron. To test
immediately: repo **Actions → poll → Run workflow** (the `workflow_dispatch`
button). Watch the run; on success, open the Supabase **Table editor** and you'll
see rows landing in `posts` and `cycle_health`.

> **GitHub's cron is best-effort and it shows.** On this repo the scheduler
> delivered ~1 run/hour with multi-hour dead zones, while manual dispatches ran
> instantly every time. For a real 15-minute cadence, set up the Supabase
> pinger in §5 — the GitHub cron then just becomes a harmless backup.

The cycle number continues across runs (it's read from `cycle_health` in
Postgres), so the ops sequence stays monotonic even though each run is a fresh
container.

## 5. Exact 15-minute cadence (Supabase pinger)

GitHub treats `workflow_dispatch` API calls like your manual clicks — immediate
and reliable. So Supabase `pg_cron` (exact) fires every 15 minutes → calls the
`poll-dispatch` Edge Function (`supabase/functions/poll-dispatch/index.ts`) →
which POSTs to GitHub's API to dispatch `poll.yml`. Setup, once:

1. **Create a fine-grained GitHub PAT** — github.com → Settings → Developer
   settings → Personal access tokens → Fine-grained tokens → Generate new.
   Repository access: **only this repo**. Repository permissions: **Actions →
   Read and write**. Nothing else. Copy the `github_pat_…` value.

2. **Generate a shared secret** for the function (any random string):

   ```powershell
   uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. **Deploy the function + set its secrets** (Supabase CLI via npx; log in
   with `npx supabase login` first):

   ```powershell
   npx supabase functions deploy poll-dispatch --project-ref tqkwztozcoyekpacwqnq
   npx supabase secrets set --project-ref tqkwztozcoyekpacwqnq GH_PAT=github_pat_… DISPATCH_SECRET=<step-2-value>
   ```

   (`supabase/config.toml` sets `verify_jwt = false` for this function — it
   authenticates with the `x-dispatch-secret` header instead, so no Supabase
   key ever appears in SQL. No-CLI alternative: paste `index.ts` into
   Dashboard → Edge Functions → Deploy via editor, toggle **Verify JWT off**,
   and add the two secrets under Edge Functions → Secrets.)

4. **Smoke-test the function** — expect `{"dispatched": true, …}` and a new
   `poll` run appearing on GitHub's Actions tab:

   ```powershell
   curl.exe -s -X POST -H "x-dispatch-secret: <step-2-value>" https://tqkwztozcoyekpacwqnq.supabase.co/functions/v1/poll-dispatch
   ```

5. **Schedule it**: open `supabase/poll_cron.sql`, replace `<DISPATCH_SECRET>`
   with the step-2 value, and run it in the Supabase **SQL editor**. It enables
   `pg_cron` + `pg_net` and (re)creates the `poll-dispatch-15m` job. The file's
   footer comments show how to verify, watch responses, and unschedule.

Afterward every quarter-hour lands a `workflow_dispatch` run; the concurrency
group in `poll.yml` already prevents overlap with any straggling GitHub-cron
run. Free-tier note: pg_cron + pg_net + Edge Functions are all on the free
plan, and ~2,900 function calls/month is far inside the 500K quota.

### Tuning

- **Cadence:** edit the schedule in `supabase/poll_cron.sql` (the reliable
  clock) — and optionally the `cron:` in `poll.yml` (the best-effort backup).
- **LLM enrichment:** a ready-made variant lives in
  `.github/workflows/poll-llm.yml` (runs `poll --once --llm`). It's
  manual-only by default — add the `ANTHROPIC_API_KEY` secret, then
  **Actions → poll (LLM) → Run workflow**. To make LLM the *scheduled* poller,
  uncomment its `schedule:` block and disable `poll.yml` so only one writes on
  cron (they share a concurrency group, so they'll never overlap regardless).
- **Local dev is unchanged:** with `DATABASE_URL` unset you're back on
  `catalyst.db` — no Postgres needed to develop or run tests.
