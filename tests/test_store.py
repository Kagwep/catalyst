from catalyst.models import Author, Metrics, Post
from catalyst.store import last_cycle_number, open_store, query_posts, save_cycle_health, save_posts


def test_last_cycle_number_persists_sequence(tmp_path):
    """The cycle counter must continue across restarts / --once runs (a hosted
    cron poller), not reset to 1 each invocation."""
    from types import SimpleNamespace

    conn = open_store(str(tmp_path / "t.db"))
    try:
        assert last_cycle_number(conn) == 0        # empty DB
        for n in (1, 2, 3):
            save_cycle_health(conn, SimpleNamespace(
                cycle=n, started_at=f"2026-07-06T0{n}:00:00Z", duration_ms=1.0,
                fetched=0, inserted=0, enriched=0, llm_calls=0, actions=0,
                notable=0, error=None, per_source={}, summary=""))
        assert last_cycle_number(conn) == 3        # a fresh run would resume at 4
    finally:
        conn.close()


def post(uri, handle, likes, indexed_at, source="bluesky"):
    return Post(
        source=source,
        uri=uri,
        cid="c_" + uri,
        url="https://bsky.app/x/" + uri,
        text="post " + uri,
        created_at="2026-06-15T09:00:00Z",
        indexed_at=indexed_at,
        author=Author(did="did:" + handle, handle=handle, display_name=handle.upper()),
        metrics=Metrics(likes=likes),
        raw={"uri": uri},
    )


def test_save_inserts_new_rows_and_reports_counts(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        r = save_posts(
            conn,
            [
                post("p1", "nyt", 10, "2026-06-15T10:01:00Z"),
                post("p2", "bbc", 5, "2026-06-15T10:02:00Z"),
            ],
        )
        assert r == {"inserted": 2, "updated": 0, "total": 2}
    finally:
        conn.close()


def test_upsert_updates_metrics_no_duplicate(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        save_posts(conn, [post("p1", "nyt", 10, "2026-06-15T10:01:00Z")])
        r = save_posts(
            conn,
            [
                post("p1", "nyt", 42, "2026-06-15T10:01:00Z"),  # same uri, more likes
                post("p3", "ap", 7, "2026-06-15T10:03:00Z"),
            ],
        )
        assert r == {"inserted": 1, "updated": 1, "total": 2}

        rows = query_posts(conn, limit=99)
        assert len(rows) == 2
        assert next(x for x in rows if x["uri"] == "p1")["likes"] == 42
    finally:
        conn.close()


def test_query_newest_first_and_source_filter(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        save_posts(
            conn,
            [
                post("p1", "nyt", 1, "2026-06-15T10:01:00Z"),
                post("p2", "bbc", 1, "2026-06-15T10:05:00Z"),
                post("r1", "feed", 0, "2026-06-15T10:03:00Z", source="rss"),
            ],
        )
        all_rows = query_posts(conn, limit=10)
        assert [x["uri"] for x in all_rows] == ["p2", "r1", "p1"]

        only_rss = query_posts(conn, source="rss")
        assert [x["uri"] for x in only_rss] == ["r1"]
    finally:
        conn.close()


def test_save_skips_records_without_uri(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        valid = post("p1", "nyt", 1, "2026-06-15T10:01:00Z")
        # A Post requires uri, so simulate a "no uri" record with a stub object.
        bad = type("Stub", (), {"uri": None})()
        r = save_posts(conn, [valid, bad])
        assert r == {"inserted": 1, "updated": 0, "total": 1}
    finally:
        conn.close()
