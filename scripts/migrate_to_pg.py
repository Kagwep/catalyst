"""One-time migration: copy a local SQLite catalyst.db into Postgres (Supabase).

Usage (PowerShell):
    $env:DATABASE_URL = "postgresql://...supabase..."
    uv run python scripts/migrate_to_pg.py [catalyst.db]

Idempotent: posts upsert on their URI and the other tables skip on conflict, so
re-running won't duplicate. Identity `id` columns are dropped on insert and
reassigned by Postgres (the ids aren't referenced across tables).
"""

from __future__ import annotations

import sqlite3
import sys

from catalyst import pg

# Order doesn't matter — there are no cross-table foreign keys.
TABLES = ["posts", "actions", "bias_snapshots", "alerts", "monitor_fires", "cycle_health"]


def main() -> int:
    url = pg.database_url()
    if not url:
        print("Set DATABASE_URL to your Supabase Postgres DSN first.", file=sys.stderr)
        return 2

    src_path = sys.argv[1] if len(sys.argv) > 1 else "catalyst.db"
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    dst = pg.open_pg(url)  # connects + ensures the schema exists
    try:
        for table in TABLES:
            cols = [r["name"] for r in src.execute(f"PRAGMA table_info({table})")]
            if not cols:
                print(f"{table}: not in source, skipped")
                continue
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"{table}: 0 rows")
                continue

            insert_cols = [c for c in cols if c != "id"]  # let Postgres assign id
            collist = ", ".join(insert_cols)
            placeholders = ", ".join(["?"] * len(insert_cols))
            conflict = "ON CONFLICT (uri) DO NOTHING" if table == "posts" else "ON CONFLICT DO NOTHING"
            sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) {conflict}"
            data = [tuple(r[c] for c in insert_cols) for r in rows]

            with dst:
                dst.executemany(sql, data)
            total = dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"{table}: sent {len(data)} rows -> {total} now in Postgres")
    finally:
        dst.close()
        src.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
