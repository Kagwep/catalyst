import pytest

from catalyst.models import Author, Metrics, Post
from catalyst.store import open_store, save_posts, to_dataframe

# Skip the whole module if the optional [ml] extra (pandas) isn't installed.
pd = pytest.importorskip("pandas")


def _post(uri, source, indexed_at, likes=0):
    return Post(
        source=source,
        uri=uri,
        url="https://x/" + uri,
        text="post " + uri,
        created_at="2026-06-15T09:00:00Z",
        indexed_at=indexed_at,
        author=Author(handle="h_" + uri),
        metrics=Metrics(likes=likes),
        raw={"uri": uri},
    )


def test_to_dataframe_newest_first_all_columns(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        save_posts(
            conn,
            [
                _post("p1", "bluesky", "2026-06-15T10:01:00Z", likes=3),
                _post("p2", "bluesky", "2026-06-15T10:05:00Z"),
                _post("r1", "rss", "2026-06-15T10:03:00Z"),
            ],
        )
        df = to_dataframe(conn)
        assert list(df["uri"]) == ["p2", "r1", "p1"]  # newest indexed_at first
        # full row shape, not the trimmed query columns
        for col in ("text", "created_at", "author_handle", "likes", "raw", "fetched_at"):
            assert col in df.columns
        assert int(df.loc[df["uri"] == "p1", "likes"].iloc[0]) == 3
    finally:
        conn.close()


def test_to_dataframe_source_filter_and_limit(tmp_path):
    conn = open_store(str(tmp_path / "t.db"))
    try:
        save_posts(
            conn,
            [
                _post("p1", "bluesky", "2026-06-15T10:01:00Z"),
                _post("r1", "rss", "2026-06-15T10:03:00Z"),
                _post("r2", "rss", "2026-06-15T10:04:00Z"),
            ],
        )
        assert set(to_dataframe(conn, source="rss")["uri"]) == {"r1", "r2"}
        assert len(to_dataframe(conn, limit=1)) == 1
    finally:
        conn.close()


def test_parquet_round_trip(tmp_path):
    pytest.importorskip("pyarrow")
    conn = open_store(str(tmp_path / "t.db"))
    try:
        save_posts(conn, [_post("p1", "rss", "2026-06-15T10:01:00Z")])
        df = to_dataframe(conn)
    finally:
        conn.close()

    out = tmp_path / "posts.parquet"
    df.to_parquet(out, index=False)
    back = pd.read_parquet(out)
    assert list(back["uri"]) == ["p1"]
