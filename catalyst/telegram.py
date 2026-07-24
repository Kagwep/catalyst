"""Telegram adapter — read public announcement channels via MTProto (Telethon).

Why MTProto and not the Bot API: a *bot* can only read channels where the owner
made it an admin, so it can never watch Binance/BWE/a project's channel. A
*user* client (MTProto) joins and reads any public channel exactly like the app.
That is what we need, and it doubles as the fast-news aggregator consumer —
BWE News, Tree of Alpha, and Whale Alert all mirror their feeds to public
Telegram channels, so subscribing to those channels here gives you the
aggregator's lead-time for free without running a separate websocket process.

Credentials (all env, adapter no-ops when unset — same pattern as ``bluesky``):

  - ``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH`` — free from my.telegram.org.
  - ``TELEGRAM_SESSION`` — a Telethon StringSession. Generate it once locally
    (``python -m catalyst.telegram login``) and paste the printed string into
    the hosted env; the poller then logs in non-interactively.

Operational note: MTProto runs as a *user account*. Automated user accounts are
technically against Telegram's ToS, and joining many channels quickly trips
``FloodWait``. Treat the account as disposable infra: use a dedicated number,
join channels gradually and by hand, keep this adapter strictly read-only.

Learning path: ``source="telegram"`` is in ``store.NEWS_SOURCES``, so channel
posts are LLM-enriched, feed the signal layer, and land on score→outcome.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from .models import Author, Post

DEFAULT_SINCE_HOURS = 6
DEFAULT_MAX = 30


def _credentials() -> tuple[int, str, str] | None:
    """(api_id, api_hash, session) when all three are set, else None."""
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    session = os.environ.get("TELEGRAM_SESSION", "").strip()
    if api_id and api_hash and session:
        return int(api_id), api_hash, session
    return None


def _channel_name(channel: str) -> str:
    """Bare @-less username for URL building; leaves t.me/-links/ids alone."""
    return channel.lstrip("@").strip()


def message_post(channel: str, msg, *, now: datetime | None = None) -> Post | None:
    """Build a Post from a Telethon message; None for empty (media-only) messages.

    Kept pure/injectable so tests don't need Telethon or the network — ``msg`` is
    any object exposing ``id``, ``message`` (text), and ``date`` (aware datetime).
    """
    text = (getattr(msg, "message", None) or "").strip()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    name = _channel_name(channel)
    date = getattr(msg, "date", None)
    created = date.isoformat() if isinstance(date, datetime) else now.isoformat()
    return Post(
        source="telegram",
        uri=f"telegram:{name}:{msg.id}",
        url=f"https://t.me/{name}/{msg.id}",
        text=text,
        created_at=created, indexed_at=now.isoformat(),
        author=Author(handle=name, display_name=f"Telegram: {name}"),
        raw={"kind": "telegram", "channel": name, "message_id": msg.id},
    )


def fetch_channel(client, channel: str, *, max: int = DEFAULT_MAX,
                  since_hours: float | None = DEFAULT_SINCE_HOURS,
                  now: datetime | None = None) -> list[Post]:
    """Recent posts from one channel via an open Telethon client (fresh-filtered)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=since_hours) if since_hours else None
    out: list[Post] = []
    for msg in client.iter_messages(channel, limit=max):
        date = getattr(msg, "date", None)
        if cutoff and isinstance(date, datetime) and date < cutoff:
            break  # iter_messages is newest-first; older ones follow
        post = message_post(channel, msg, now=now)
        if post:
            out.append(post)
    return out


def fetch_channels(channels: list[str], *, max: int = DEFAULT_MAX,
                   since_hours: float | None = DEFAULT_SINCE_HOURS,
                   now: datetime | None = None) -> list[Post]:
    """Read the configured channels. No-op (with a note) when creds are unset."""
    creds = _credentials()
    if not creds or not channels:
        if channels and not creds:
            print("telegram: TELEGRAM_API_ID/HASH/SESSION unset — skipping", file=sys.stderr)
        return []

    try:
        from telethon.sync import TelegramClient  # noqa: PLC0415 — optional dep
        from telethon.sessions import StringSession  # noqa: PLC0415
    except ImportError:
        print("telegram: telethon not installed (pip install 'catalyst[telegram]')",
              file=sys.stderr)
        return []

    api_id, api_hash, session = creds
    now = now or datetime.now(timezone.utc)
    out: list[Post] = []
    with TelegramClient(StringSession(session), api_id, api_hash) as client:
        for ch in channels:
            try:
                out.extend(fetch_channel(client, ch, max=max, since_hours=since_hours, now=now))
            except Exception as err:  # noqa: BLE001 — one bad channel shouldn't sink the rest
                print(f"telegram channel {ch} skipped: {err}", file=sys.stderr)
    return out


def login() -> None:
    """Interactive one-time login: prints a StringSession for TELEGRAM_SESSION.

    Run locally: ``TELEGRAM_API_ID=... TELEGRAM_API_HASH=... python -m catalyst.telegram login``.
    """
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nTELEGRAM_SESSION=" + client.session.save())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print("usage: python -m catalyst.telegram login", file=sys.stderr)
