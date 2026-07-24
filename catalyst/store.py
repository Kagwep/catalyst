"""SQLite persistence for normalized posts — stdlib sqlite3, no dependencies.

Posts are keyed by their URI. Re-fetching a known post updates its engagement
metrics (which change over time) but preserves the original fetched_at stamp.
The schema/column names match the Node version, so existing catalyst.db files
remain readable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import Post

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    uri           TEXT PRIMARY KEY,
    cid           TEXT,
    source        TEXT NOT NULL,
    url           TEXT,
    text          TEXT,
    created_at    TEXT,
    indexed_at    TEXT,
    author_did    TEXT,
    author_handle TEXT,
    author_name   TEXT,
    likes         INTEGER DEFAULT 0,
    reposts       INTEGER DEFAULT 0,
    replies       INTEGER DEFAULT 0,
    quotes        INTEGER DEFAULT 0,
    raw           TEXT,
    fetched_at    TEXT NOT NULL,
    sentiment_score  REAL,
    sentiment_label  TEXT,
    assets           TEXT,
    catalyst         TEXT,
    sentiment_model  TEXT,
    enriched_at      TEXT,
    event            TEXT,
    severity         TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_indexed_at ON posts(indexed_at);
CREATE INDEX IF NOT EXISTS idx_posts_source     ON posts(source);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT NOT NULL,
    action      TEXT NOT NULL,
    direction   TEXT,
    confidence  REAL,
    horizon     TEXT,
    score       REAL,
    catalysts   TEXT,
    rationale   TEXT,
    freshness_minutes REAL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at);

CREATE TABLE IF NOT EXISTS bias_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,        -- the "now" at which the bias was computed
    layer     TEXT NOT NULL,        -- 'macro' | 'flows' | 'supply'
    asset     TEXT,                 -- ticker, or '*' for market-wide (macro)
    bias      REAL,
    label     TEXT,
    evidence  REAL
);
CREATE INDEX IF NOT EXISTS idx_bias_snap ON bias_snapshots(layer, asset, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT NOT NULL,
    action       TEXT NOT NULL,
    confidence   REAL,
    horizon      TEXT,
    sinks        TEXT,                -- comma-joined sinks that accepted it
    delivered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_delivered ON alerts(delivered_at);

CREATE TABLE IF NOT EXISTS monitor_fires (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor   TEXT NOT NULL,          -- the monitor name
    kind      TEXT NOT NULL,          -- 'proposal' | 'event'
    ref       TEXT NOT NULL,          -- proposal: 'asset:action' | event: post uri
    fired_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_fires ON monitor_fires(monitor, kind, fired_at);

CREATE TABLE IF NOT EXISTS cycle_health (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle       INTEGER,
    started_at  TEXT NOT NULL,
    duration_ms REAL,
    fetched     INTEGER,
    inserted    INTEGER,
    enriched    INTEGER,
    llm_calls   INTEGER,
    actions     INTEGER,
    notable     INTEGER,
    error       TEXT,               -- cycle-level error (NULL when the cycle ran clean)
    per_source  TEXT,               -- JSON {source: fetched_count}
    summary     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycle_health ON cycle_health(started_at);

CREATE TABLE IF NOT EXISTS score_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle       INTEGER,             -- cycle_health.cycle (NULL for direct calls)
    ts          TEXT NOT NULL,       -- the cycle "now" (same ts as bias_snapshots)
    asset       TEXT NOT NULL,
    sentiment   REAL,
    strength    REAL,
    score       REAL,
    direction   TEXT,
    mentions    INTEGER,
    velocity    REAL,
    catalysts   TEXT,                -- JSON list
    sources     TEXT,                -- JSON list
    latest_at   TEXT,
    action      TEXT,                -- planner outcome this cycle (buy/sell/watch/NULL)
    confidence  REAL,
    horizon     TEXT,
    layers      TEXT,                -- JSON: per-layer {label, bias, effect, weight}
    price_at_score REAL,             -- spot at record time (NULL if oracle unavailable)
    catalyst_text  TEXT,             -- the "what happened" digest behind this signal
                                     -- (events/headlines) — durable corpus for later
                                     -- embeddings; NULL when no catalyst-bearing rows
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_score_snap_ts_asset ON score_snapshots(ts, asset);
CREATE INDEX IF NOT EXISTS idx_score_snap_asset ON score_snapshots(asset, ts);

CREATE TABLE IF NOT EXISTS score_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL,  -- score_snapshots.id
    asset         TEXT NOT NULL,     -- denormalized for the resolution query
    scored_at     TEXT NOT NULL,     -- = snapshot ts
    horizon_hours REAL NOT NULL,
    due_at        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | no_price
    attempts      INTEGER DEFAULT 0,
    resolved_at   TEXT,
    entry_px      REAL,
    exit_px       REAL,
    ret           REAL,              -- exit/entry - 1 (the label)
    btc_ret       REAL               -- BTC return over the same window (baseline)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_snap_h ON score_outcomes(snapshot_id, horizon_hours);
CREATE INDEX IF NOT EXISTS idx_outcome_due ON score_outcomes(status, due_at);

CREATE TABLE IF NOT EXISTS market_moves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT NOT NULL,
    detected_at  TEXT NOT NULL,
    window_hours REAL NOT NULL,
    start_px     REAL,
    end_px       REAL,
    ret          REAL NOT NULL,      -- signed move over the window
    catalysts    TEXT,               -- JSON list of catalysts active in the window
    evidence     TEXT,               -- JSON sample [{uri, catalyst, event, sentiment_score}]
    signal_score REAL,               -- latest score_snapshots.score in the window
    action       TEXT,               -- any buy/sell emitted in the window
    explained    INTEGER DEFAULT 0   -- 1 = catalysts/actions covered it; 0 = unexplained
);
CREATE INDEX IF NOT EXISTS idx_moves_asset ON market_moves(asset, detected_at);
"""

# Columns added after the initial release — applied to pre-existing DBs.
_ENRICH_COLUMNS = {
    "sentiment_score": "REAL",
    "sentiment_label": "TEXT",
    "assets": "TEXT",
    "catalyst": "TEXT",
    "sentiment_model": "TEXT",
    "enriched_at": "TEXT",
    "event": "TEXT",
    "severity": "TEXT",
}

_INSERT = """
INSERT INTO posts (
    uri, cid, source, url, text, created_at, indexed_at,
    author_did, author_handle, author_name,
    likes, reposts, replies, quotes, raw, fetched_at
) VALUES (
    :uri, :cid, :source, :url, :text, :created_at, :indexed_at,
    :author_did, :author_handle, :author_name,
    :likes, :reposts, :replies, :quotes, :raw, :fetched_at
)
ON CONFLICT(uri) DO UPDATE SET
    likes   = excluded.likes,
    reposts = excluded.reposts,
    replies = excluded.replies,
    quotes  = excluded.quotes,
    raw     = excluded.raw
"""


def open_store(path: str = "catalyst.db"):
    """Open (or create) the database and ensure the schema exists.

    Backend is chosen by the ``DATABASE_URL`` environment variable: when set
    (e.g. a Supabase Postgres DSN for hosted runs) the returned connection is a
    psycopg proxy shaped like ``sqlite3.Connection``; otherwise it's a local
    SQLite file at ``path``. Every store function below works with either.
    """
    from . import pg

    url = pg.database_url()
    if url:
        return pg.open_pg(url)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


# Columns added to score_snapshots after its initial release (additive migration).
_SCORE_SNAPSHOT_COLUMNS = {"catalyst_text": "TEXT"}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns missing from pre-existing tables (additive only)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
    for col, decl in _ENRICH_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} {decl}")
    snap_cols = {row["name"] for row in conn.execute("PRAGMA table_info(score_snapshots)")}
    for col, decl in _SCORE_SNAPSHOT_COLUMNS.items():
        if col not in snap_cols:
            conn.execute(f"ALTER TABLE score_snapshots ADD COLUMN {col} {decl}")
    conn.commit()


def _count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM posts").fetchone()[0]


def save_posts(conn: sqlite3.Connection, posts: Iterable[Post]) -> dict[str, int]:
    """Upsert posts. Returns {inserted, updated, total}."""
    posts = [p for p in posts if getattr(p, "uri", None)]
    fetched_at = datetime.now(timezone.utc).isoformat()

    before = _count(conn)
    with conn:  # transaction: commits on success, rolls back on exception
        for p in posts:
            row = p.to_row()
            row["fetched_at"] = fetched_at
            conn.execute(_INSERT, row)
    after = _count(conn)

    inserted = after - before
    return {"inserted": inserted, "updated": len(posts) - inserted, "total": len(posts)}


def fetch_unenriched(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    source: str | None = None,
    reenrich: bool = False,
) -> list[dict]:
    """Rows that still need scoring (sentiment_model IS NULL), newest-first.

    `reenrich=True` returns all rows regardless of prior scoring.
    """
    sql = "SELECT uri, text, source, author_handle FROM posts"
    clauses: list[str] = []
    params: dict[str, object] = {}
    if not reenrich:
        clauses.append("sentiment_model IS NULL")
    if source:
        clauses.append("source = :source")
        params["source"] = source
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY indexed_at DESC"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def save_enrichments(conn: sqlite3.Connection, items: Iterable[tuple[str, object]]) -> int:
    """Write (uri, Enrichment) pairs back to their posts. Returns rows updated."""
    enriched_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    with conn:
        for uri, e in items:
            cur = conn.execute(
                "UPDATE posts SET sentiment_score=?, sentiment_label=?, assets=?, "
                "catalyst=?, sentiment_model=?, enriched_at=?, event=?, severity=? "
                "WHERE uri=?",
                (
                    e.sentiment_score,
                    e.sentiment_label,
                    json.dumps(e.assets),
                    e.catalyst,
                    e.model,
                    enriched_at,
                    getattr(e, "event", None),
                    getattr(e, "severity", None),
                    uri,
                ),
            )
            updated += cur.rowcount
    return updated


def save_actions(conn: sqlite3.Connection, actions: Iterable[object]) -> int:
    """Append planner actions to the audit trail. Returns rows written."""
    n = 0
    with conn:
        for a in actions:
            conn.execute(
                "INSERT INTO actions (asset, action, direction, confidence, horizon, "
                "score, catalysts, rationale, freshness_minutes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    a.asset, a.action, a.direction, a.confidence, a.horizon, a.score,
                    json.dumps(a.catalysts), a.rationale, a.freshness_minutes, a.created_at,
                ),
            )
            n += 1
    return n


def save_bias_snapshots(
    conn: sqlite3.Connection, ts: str, *, regime=None, flow_bias=None, supply_bias=None,
    market_bias=None, derivs_bias=None,
) -> int:
    """Persist the bias each layer computed at `ts` — the point-in-time history.

    Sources that only serve "now" (staking) become backtestable from here on; for
    flows/macro this is a revision-proof audit alongside their dated-input replay.
    """
    rows: list[tuple] = []
    if regime is not None:
        rows.append(("macro", "*", regime.score, regime.label, regime.evidence))
    for layer, biases in (("flows", flow_bias), ("supply", supply_bias),
                          ("market", market_bias), ("derivs", derivs_bias)):
        for asset, b in (biases or {}).items():
            rows.append((layer, asset, b.bias, b.label, getattr(b, "evidence", 0.0)))
    if rows:
        with conn:
            conn.executemany(
                "INSERT INTO bias_snapshots (ts, layer, asset, bias, label, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(ts, *r) for r in rows],
            )
    return len(rows)


def fetch_bias_snapshots(
    conn: sqlite3.Connection, *, layer: str | None = None, asset: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Bias snapshots, oldest-first. `before` (ISO ts) gives the point-in-time view."""
    sql = "SELECT ts, layer, asset, bias, label, evidence FROM bias_snapshots"
    clauses: list[str] = []
    params: dict[str, object] = {}
    if layer:
        clauses.append("layer = :layer")
        params["layer"] = layer
    if asset:
        clauses.append("asset = :asset")
        params["asset"] = asset
    if before:
        clauses.append("ts <= :before")
        params["before"] = before
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def save_cycle_health(conn: sqlite3.Connection, health) -> int:
    """Persist one poll cycle's structured health record. Returns 1."""
    with conn:
        conn.execute(
            "INSERT INTO cycle_health (cycle, started_at, duration_ms, fetched, inserted, "
            "enriched, llm_calls, actions, notable, error, per_source, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (health.cycle, health.started_at, health.duration_ms, health.fetched,
             health.inserted, health.enriched, health.llm_calls, health.actions,
             health.notable, health.error, json.dumps(health.per_source), health.summary),
        )
    return 1


def last_cycle_number(conn: sqlite3.Connection) -> int:
    """The highest cycle number recorded so far (0 if none).

    Lets the poll loop CONTINUE the cycle sequence across process restarts and
    repeated `--once` invocations (e.g. a hosted cron poller) instead of resetting
    to 1 each run — so the cycle number is a stable, monotonic ops identifier."""
    return conn.execute("SELECT COALESCE(MAX(cycle), 0) FROM cycle_health").fetchone()[0]


def fetch_recent_health(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    """Recent cycle-health rows, newest-first (`per_source` decoded to a dict)."""
    rows = conn.execute(
        "SELECT cycle, started_at, duration_ms, fetched, inserted, enriched, llm_calls, "
        "actions, notable, error, per_source, summary FROM cycle_health "
        "ORDER BY id DESC LIMIT :limit",
        {"limit": limit},
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["per_source"] = json.loads(d["per_source"]) if d["per_source"] else {}
        except json.JSONDecodeError:
            d["per_source"] = {}
        out.append(d)
    return out


def source_freshness(conn: sqlite3.Connection) -> dict:
    """Per-source {last_indexed_at, count} across stored posts — for `status`."""
    rows = conn.execute(
        "SELECT source, MAX(indexed_at) AS last, COUNT(*) AS n FROM posts GROUP BY source"
    ).fetchall()
    return {r["source"]: {"last_indexed_at": r["last"], "count": r["n"]} for r in rows}


def save_alerts(
    conn: sqlite3.Connection, actions: Iterable[object], *, sinks: str = "",
    now: datetime | None = None,
) -> int:
    """Record delivered alerts (the alert-layer de-dupe history). Returns rows written."""
    delivered_at = (now or datetime.now(timezone.utc)).isoformat()
    n = 0
    with conn:
        for a in actions:
            conn.execute(
                "INSERT INTO alerts (asset, action, confidence, horizon, sinks, delivered_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a.asset, a.action, a.confidence, a.horizon, sinks, delivered_at),
            )
            n += 1
    return n


def fetch_recent_alerts(
    conn: sqlite3.Connection, *, within_minutes: float, now: datetime | None = None
) -> list[dict]:
    """Alerts delivered in the last `within_minutes` — feeds the alert-layer de-dupe."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=within_minutes)).isoformat()
    rows = conn.execute(
        "SELECT asset, action, delivered_at FROM alerts WHERE delivered_at >= :cutoff",
        {"cutoff": cutoff},
    ).fetchall()
    return [dict(r) for r in rows]


def save_monitor_fires(
    conn: sqlite3.Connection, monitor: str, kind: str, refs: Iterable[str],
    *, now: datetime | None = None,
) -> int:
    """Record that `monitor` fired on `refs` (the per-monitor de-dupe history).

    `kind` is 'proposal' (ref = 'asset:action') or 'event' (ref = post uri).
    Returns rows written."""
    fired_at = (now or datetime.now(timezone.utc)).isoformat()
    n = 0
    with conn:
        for ref in refs:
            conn.execute(
                "INSERT INTO monitor_fires (monitor, kind, ref, fired_at) VALUES (?, ?, ?, ?)",
                (monitor, kind, ref, fired_at),
            )
            n += 1
    return n


def fetch_recent_monitor_fires(
    conn: sqlite3.Connection, monitor: str, kind: str, *,
    within_minutes: float, now: datetime | None = None,
) -> set[str]:
    """The `ref`s a monitor already fired on in the last `within_minutes` — feeds
    the per-monitor, per-trigger de-dupe so a restart doesn't re-alert."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=within_minutes)).isoformat()
    rows = conn.execute(
        "SELECT ref FROM monitor_fires WHERE monitor = :m AND kind = :k AND fired_at >= :cutoff",
        {"m": monitor, "k": kind, "cutoff": cutoff},
    ).fetchall()
    return {r["ref"] for r in rows}


def fetch_recent_actions(
    conn: sqlite3.Connection, *, within_minutes: float, now: datetime | None = None
) -> list[dict]:
    """Actions emitted in the last `within_minutes` — feeds the planner cooldown."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=within_minutes)).isoformat()
    rows = conn.execute(
        "SELECT asset, action, confidence, created_at FROM actions WHERE created_at >= :cutoff",
        {"cutoff": cutoff},
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_macro(conn: sqlite3.Connection) -> list[dict]:
    """Enriched macro posts (catalyst='macro' or source='macro') — the regime input.

    These have no ticker, so they're excluded from `fetch_enriched`; the macro
    layer reads them separately to compute a market-wide risk regime.
    """
    rows = conn.execute(
        "SELECT uri, source, author_handle, indexed_at, text, sentiment_score, catalyst "
        "FROM posts WHERE sentiment_model IS NOT NULL "
        "AND (catalyst = 'macro' OR source = 'macro')"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_flows(conn: sqlite3.Connection) -> list[dict]:
    """Flow posts (source='flows') — per-asset ETF net-flow records.

    Flows carry their signal in `raw` (asset + net_usd), not sentiment, so unlike
    macro they need no enrichment; the flows layer reads them to compute per-asset
    bias. `assets` is empty, so they're excluded from `fetch_enriched`.
    """
    rows = conn.execute(
        "SELECT uri, source, indexed_at, text, raw FROM posts WHERE source = 'flows'"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_onchain(conn: sqlite3.Connection) -> list[dict]:
    """On-chain tier posts (source 'onchain'=unlocks, 'staking'=ETH queue).

    These carry their signal in `raw` (kind + asset + amounts), read by the
    supply-bias layer. Unlock posts also ride the normal enrich→signal path as a
    catalyst, but the bias is computed straight from `raw`.
    """
    rows = conn.execute(
        "SELECT uri, source, indexed_at, text, raw FROM posts "
        "WHERE source IN ('onchain', 'staking')"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_derivs(conn: sqlite3.Connection) -> list[dict]:
    """Derivatives-layer posts (source='derivs') — perp funding + open interest.

    Carries the signal in `raw` (kind='funding'|'oi', asset, funding_rate/oi_usd),
    read by the positioning-bias layer. Text uses the exchange symbol, so `assets`
    is empty and these never enter the signal layer.
    """
    rows = conn.execute(
        "SELECT uri, source, indexed_at, text, raw FROM posts WHERE source = 'derivs'"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_market(conn: sqlite3.Connection) -> list[dict]:
    """Market-layer posts (source='market') — the Fear & Greed series.

    Carries its value in `raw` (kind='fng'); the market layer reads it alongside
    price technicals to compute a per-asset momentum bias.
    """
    rows = conn.execute(
        "SELECT uri, source, indexed_at, text, raw FROM posts WHERE source = 'market'"
    ).fetchall()
    return [dict(r) for r in rows]


# The news/text sources whose sentiment feeds the signal layer. Numeric data
# feeds (derivs, market, onchain, staking, defillama, snapshot) also get an asset
# ticker — a derivs "BTCUSDT open interest" post tags BTC — but they carry no
# directional view, so counting them as sentiment dilutes the news read toward
# neutral. They already feed their OWN bias layers (fetch_derivs/market/onchain/…),
# so we exclude them here. macro/flows are already excluded (they carry no ticker).
# predictions (Polymarket/Kalshi odds shifts) and hyperliquid (listings, funding
# regime flips) ARE directional events, so they count as news: LLM-enriched,
# signal inputs, and recorded on the learning path (score_snapshots/outcomes).
# exchange (Binance/Upbit listing announcements) and telegram (announcement +
# fast-news channels) are likewise directional text events, so they count too.
NEWS_SOURCES = ("bluesky", "rss", "github", "predictions", "hyperliquid", "exchange", "telegram")


def fetch_enriched(
    conn: sqlite3.Connection, *, source: str | None = None,
    sources: tuple[str, ...] | None = NEWS_SOURCES,
) -> list[dict]:
    """Enriched rows that carry at least one asset — the input to the signal layer.

    Defaults to the news sources (`NEWS_SOURCES`) so numeric data feeds don't
    dilute sentiment. Pass an explicit `source=` for a single source (bypasses
    the allowlist, e.g. for display/query), or `sources=None` for every source.
    """
    sql = (
        "SELECT uri, source, url, author_handle, indexed_at, text, sentiment_score, "
        "catalyst, assets, likes, reposts, event, severity FROM posts "
        "WHERE sentiment_model IS NOT NULL AND assets IS NOT NULL AND assets != '[]'"
    )
    params: dict[str, object] = {}
    if source:                              # explicit single source overrides the allowlist
        sql += " AND source = :source"
        params["source"] = source
    elif sources:                           # default: restrict to the news sources
        names = ", ".join(f":src{i}" for i in range(len(sources)))
        sql += f" AND source IN ({names})"
        params.update({f"src{i}": s for i, s in enumerate(sources)})
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_events(conn: sqlite3.Connection, *, sources: tuple[str, ...] | None = NEWS_SOURCES) -> list[dict]:
    """News-source posts carrying a concrete `event` — input to the catalyst.events
    feed. Unlike `fetch_enriched`, there is NO asset requirement: market-wide macro
    or geopolitical events (a Fed decision, a missile strike) move the whole market
    but carry no ticker, and those are exactly what this feed wants to surface.
    """
    sql = (
        "SELECT uri, source, url, author_handle, indexed_at, text, sentiment_score, "
        "catalyst, assets, event, severity FROM posts WHERE event IS NOT NULL"
    )
    params: dict[str, object] = {}
    if sources:
        names = ", ".join(f":src{i}" for i in range(len(sources)))
        sql += f" AND source IN ({names})"
        params.update({f"src{i}": s for i, s in enumerate(sources)})
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def to_dataframe(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    limit: int | None = None,
):
    """Load stored posts into a pandas DataFrame (newest-first), all columns.

    Requires the optional `[ml]` extra (pandas). pandas is imported lazily so
    the core package has no hard dependency on it.
    """
    try:
        import pandas as pd
    except ModuleNotFoundError as err:  # pragma: no cover - depends on install extra
        raise RuntimeError(
            "to_dataframe requires pandas — install the extra: pip install 'catalyst[ml]'"
        ) from err

    sql = "SELECT * FROM posts"
    params: dict[str, object] = {}
    if source:
        sql += " WHERE source = :source"
        params["source"] = source
    sql += " ORDER BY indexed_at DESC"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return pd.read_sql_query(sql, conn, params=params)


def save_score_snapshot(conn: sqlite3.Connection, row: dict) -> int:
    """Insert one per-asset score snapshot; returns its id.

    The (ts, asset) unique index makes a re-run of the same cycle a no-op, so a
    crashed/repeated `--once` run never records twice. The id is read back by
    key instead of lastrowid (which the Postgres proxy doesn't support).
    """
    row.setdefault("catalyst_text", None)  # tolerate callers predating the column
    with conn:
        conn.execute(
            "INSERT INTO score_snapshots (cycle, ts, asset, sentiment, strength, score, "
            "direction, mentions, velocity, catalysts, sources, latest_at, action, "
            "confidence, horizon, layers, price_at_score, catalyst_text, created_at) "
            "VALUES (:cycle, :ts, :asset, :sentiment, :strength, :score, :direction, "
            ":mentions, :velocity, :catalysts, :sources, :latest_at, :action, "
            ":confidence, :horizon, :layers, :price_at_score, :catalyst_text, :created_at) "
            "ON CONFLICT (ts, asset) DO NOTHING",
            row,
        )
    got = conn.execute(
        "SELECT id FROM score_snapshots WHERE ts = :ts AND asset = :asset",
        {"ts": row["ts"], "asset": row["asset"]},
    ).fetchone()
    return got[0]


def save_pending_outcomes(
    conn: sqlite3.Connection, snapshot_id: int, asset: str, scored_at: str,
    horizons_hours: Iterable[float],
) -> int:
    """Pre-create one pending outcome row per horizon for a snapshot.

    Resolution later is just `status='pending' AND due_at <= now` — idempotent
    via the (snapshot_id, horizon_hours) unique index."""
    base = datetime.fromisoformat(scored_at)
    n = 0
    with conn:
        for h in horizons_hours:
            conn.execute(
                "INSERT INTO score_outcomes (snapshot_id, asset, scored_at, "
                "horizon_hours, due_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (snapshot_id, horizon_hours) DO NOTHING",
                (snapshot_id, asset, scored_at, float(h),
                 (base + timedelta(hours=float(h))).isoformat()),
            )
            n += 1
    return n


def fetch_due_outcomes(
    conn: sqlite3.Connection, *, now_iso: str, limit: int = 500
) -> list[dict]:
    """Pending outcomes whose horizon has elapsed, oldest-due first."""
    rows = conn.execute(
        "SELECT o.id, o.snapshot_id, o.asset, o.scored_at, o.horizon_hours, o.due_at, "
        "o.attempts, o.entry_px, s.price_at_score "
        "FROM score_outcomes o JOIN score_snapshots s ON s.id = o.snapshot_id "
        "WHERE o.status = 'pending' AND o.due_at <= :now "
        "ORDER BY o.due_at LIMIT :limit",
        {"now": now_iso, "limit": limit},
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_outcome(
    conn: sqlite3.Connection, outcome_id: int, *, entry_px: float, exit_px: float,
    ret: float, btc_ret: float | None, resolved_at: str,
) -> int:
    """Mark one outcome resolved with its realized prices/returns.

    The `status='pending'` guard makes concurrent hosted double-runs harmless."""
    with conn:
        cur = conn.execute(
            "UPDATE score_outcomes SET status='resolved', entry_px=?, exit_px=?, "
            "ret=?, btc_ret=?, resolved_at=? WHERE id=? AND status='pending'",
            (entry_px, exit_px, ret, btc_ret, resolved_at, outcome_id),
        )
    return cur.rowcount


def bump_outcome_attempt(
    conn: sqlite3.Connection, outcome_id: int, *, give_up: bool = False
) -> None:
    """Count a failed resolution attempt; `give_up` retires the row as no_price."""
    with conn:
        if give_up:
            conn.execute(
                "UPDATE score_outcomes SET attempts = attempts + 1, status='no_price' "
                "WHERE id=? AND status='pending'", (outcome_id,),
            )
        else:
            conn.execute(
                "UPDATE score_outcomes SET attempts = attempts + 1 "
                "WHERE id=? AND status='pending'", (outcome_id,),
            )


def fetch_outcomes(
    conn: sqlite3.Connection, *, asset: str | None = None,
    horizon_hours: float | None = None, status: str | None = None, limit: int = 100,
) -> list[dict]:
    """Snapshot⋈outcome rows (features next to labels), newest-scored first."""
    sql = (
        "SELECT o.id, o.asset, o.scored_at, o.horizon_hours, o.due_at, o.status, "
        "o.attempts, o.resolved_at, o.entry_px, o.exit_px, o.ret, o.btc_ret, "
        "s.cycle, s.sentiment, s.strength, s.score, s.direction, s.mentions, "
        "s.velocity, s.catalysts, s.action, s.confidence, s.horizon, s.layers, "
        "s.price_at_score "
        "FROM score_outcomes o JOIN score_snapshots s ON s.id = o.snapshot_id"
    )
    clauses: list[str] = []
    params: dict[str, object] = {"limit": limit}
    if asset:
        clauses.append("o.asset = :asset")
        params["asset"] = asset
    if horizon_hours is not None:
        clauses.append("o.horizon_hours = :h")
        params["h"] = float(horizon_hours)
    if status:
        clauses.append("o.status = :status")
        params["status"] = status
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY o.scored_at DESC, o.horizon_hours LIMIT :limit"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_latest_score(
    conn: sqlite3.Connection, asset: str, *, since_iso: str
) -> float | None:
    """The most recent recorded signal score for `asset` since `since_iso`."""
    row = conn.execute(
        "SELECT score FROM score_snapshots WHERE asset = :a AND ts >= :s "
        "ORDER BY ts DESC LIMIT 1",
        {"a": asset, "s": since_iso},
    ).fetchone()
    return row[0] if row else None


def save_market_move(conn: sqlite3.Connection, move: dict) -> int:
    """Record one significant market move (with its catalyst attribution)."""
    with conn:
        conn.execute(
            "INSERT INTO market_moves (asset, detected_at, window_hours, start_px, "
            "end_px, ret, catalysts, evidence, signal_score, action, explained) "
            "VALUES (:asset, :detected_at, :window_hours, :start_px, :end_px, :ret, "
            ":catalysts, :evidence, :signal_score, :action, :explained)",
            move,
        )
    return 1


def fetch_recent_moves(
    conn: sqlite3.Connection, *, asset: str | None = None,
    within_hours: float | None = None, limit: int = 50,
    now: datetime | None = None,
) -> list[dict]:
    """Recorded market moves, newest first — also feeds the detection cooldown."""
    sql = (
        "SELECT id, asset, detected_at, window_hours, start_px, end_px, ret, "
        "catalysts, evidence, signal_score, action, explained FROM market_moves"
    )
    clauses: list[str] = []
    params: dict[str, object] = {"limit": limit}
    if asset:
        clauses.append("asset = :asset")
        params["asset"] = asset
    if within_hours is not None:
        now = now or datetime.now(timezone.utc)
        clauses.append("detected_at >= :cutoff")
        params["cutoff"] = (now - timedelta(hours=within_hours)).isoformat()
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY detected_at DESC LIMIT :limit"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_posts(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    source: str | None = None,
) -> list[dict]:
    """Read posts back, newest-first by indexed_at, optionally filtered by source."""
    sql = (
        "SELECT uri, source, url, text, author_handle, indexed_at, "
        "likes, reposts, replies, quotes, "
        "sentiment_score, sentiment_label, catalyst FROM posts "
    )
    params: dict[str, object] = {"limit": limit}
    if source:
        sql += "WHERE source = :source "
        params["source"] = source
    sql += "ORDER BY indexed_at DESC LIMIT :limit"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
