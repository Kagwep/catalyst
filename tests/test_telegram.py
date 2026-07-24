from datetime import datetime, timedelta, timezone

import catalyst.telegram as tg

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


class FakeMsg:
    def __init__(self, id, message, date):
        self.id = id
        self.message = message
        self.date = date


class FakeClient:
    """Stands in for a Telethon client: newest-first iter_messages."""

    def __init__(self, messages):
        self._messages = messages

    def iter_messages(self, channel, limit=None):
        return iter(self._messages[:limit] if limit else self._messages)


def test_message_post_builds_from_message():
    msg = FakeMsg(42, "  BREAKING: Foo lists Bar  ", NOW)
    p = tg.message_post("@BWEnews", msg, now=NOW)
    assert p is not None
    assert p.uri == "telegram:BWEnews:42"
    assert p.url == "https://t.me/BWEnews/42"
    assert p.source == "telegram"
    assert p.text == "BREAKING: Foo lists Bar"  # stripped
    assert p.created_at == NOW.isoformat()
    assert p.raw == {"kind": "telegram", "channel": "BWEnews", "message_id": 42}


def test_message_post_skips_empty_media_only():
    assert tg.message_post("chan", FakeMsg(1, "", NOW), now=NOW) is None
    assert tg.message_post("chan", FakeMsg(2, None, NOW), now=NOW) is None


def test_fetch_channel_stops_at_since_cutoff():
    msgs = [
        FakeMsg(3, "newest", NOW),
        FakeMsg(2, "recent", NOW - timedelta(hours=3)),
        FakeMsg(1, "too old", NOW - timedelta(hours=20)),  # newest-first → break here
    ]
    posts = tg.fetch_channel(FakeClient(msgs), "chan", since_hours=6, now=NOW)
    assert [p.raw["message_id"] for p in posts] == [3, 2]


def test_fetch_channels_no_creds_is_noop(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION", raising=False)
    assert tg.fetch_channels(["BWEnews"], now=NOW) == []
    assert "unset" in capsys.readouterr().err


def test_fetch_channels_empty_list_is_silent_noop(capsys):
    assert tg.fetch_channels([], now=NOW) == []
    assert capsys.readouterr().err == ""
