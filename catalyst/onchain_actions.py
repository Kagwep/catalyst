"""On-chain *actions* — governance/technical events on watched contracts.

The last planned-but-unbuilt source (`README.md`): read real on-chain activity —
**proxy upgrades**, **timelocked governance actions**, and **treasury moves** —
straight from Ethereum event logs and normalise them into the shared `Post`
shape so they ride enrich→signal like every other source (a proxy `Upgraded`
event becomes a `catalyst="upgrade"` post attributed to the protocol's token).

Kept deliberately separate from `onchain.py` (which is the *supply* tier —
unlocks + staking, a bias modifier). This module emits **discrete event posts**,
not a bias: an upgrade/timelock/treasury action is a dated catalyst the planner
ranks, mirroring how DefiLlama hacks/listings work.

Design, matching the rest of the codebase:
  - **Keyless.** Reads a public Ethereum JSON-RPC (`eth_getLogs`) — no API key.
    Any endpoint works; the default is a free public node.
  - **Event topics are derived, not hard-coded.** A small pure-Python Keccak-256
    turns a human-readable event signature ("Upgraded(address)") into its
    `topic0`, so adding a new event kind is one line (a signature string). The
    keccak is validated in tests against the two universally-known topics
    (ERC-20 `Transfer`, EIP-1967 `Upgraded`).
  - **Address → asset** comes from the watch list (each entry names its ticker),
    so a matched log is attributed to the right token.
  - **Fail-soft & USD-gated.** One bad address can't sink the batch; treasury
    transfers below `min_value_usd` (priced via the free DefiLlama coins API) are
    dropped as noise.

Watch-list entry shape (config `onchain_actions.watch[]`):
    {"address": "0x…", "asset": "AAVE", "kinds": ["upgrade", "timelock"]}
    {"address": "0x…token…", "asset": "ARB", "kinds": ["treasury"],
     "from": "0x…treasury…", "gecko_id": "arbitrum", "decimals": 18}
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .flows import HttpCache, _HEADERS
from .models import Author, Post
from .onchain import _prices  # DefiLlama coins price join (free), reused for treasury USD

DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
DEFAULT_LOOKBACK_BLOCKS = 300  # ~1h at 12s blocks — a few poll cycles of overlap
# Keyless public nodes cap eth_getLogs by block range (publicnode ≈100). We scan
# in windows this size and aggregate, so any lookback works on any free node.
DEFAULT_CHUNK_BLOCKS = 100
DEFAULT_KINDS = ("upgrade", "timelock")


# ---- Keccak-256 (pure Python, no deps) --------------------------------------
# Ethereum uses original Keccak (0x01 padding), not NIST SHA3 (0x06). Validated
# in tests against Transfer/Upgraded topic0 — if those match, all derived topics
# are correct.

_MASK = (1 << 64) - 1
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [  # rho offsets, indexed [x][y]
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rotl(v: int, n: int) -> int:
    n &= 63
    return ((v << n) | (v >> (64 - n))) & _MASK


def _keccak_f(a: list[int]) -> None:
    for rnd in range(24):
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x + 5 * y] ^= d[x]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(a[x + 5 * y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x + 5 * y] = b[x + 5 * y] ^ (~b[(x + 1) % 5 + 5 * y] & b[(x + 2) % 5 + 5 * y])
        a[0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    rate = 136  # bytes (1088-bit rate for Keccak-256)
    msg = bytearray(data)
    msg.append(0x01)
    while len(msg) % rate != 0:
        msg.append(0x00)
    msg[-1] ^= 0x80
    a = [0] * 25
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            a[i] ^= int.from_bytes(msg[off + 8 * i:off + 8 * i + 8], "little")
        _keccak_f(a)
    out = b"".join(lane.to_bytes(8, "little") for lane in a)
    return out[:32]


def event_topic(signature: str) -> str:
    """topic0 (0x-hex, 66 chars) for a canonical event signature."""
    return "0x" + keccak256(signature.encode()).hex()


# ---- Event registry: signature → (kind, verb for the post text) -------------
# `verb` is chosen so the lexicon (enrich.py) classifies the post to the same
# catalyst label and gives it a sensible sentiment.

_EVENTS: list[tuple[str, str, str]] = [
    ("Upgraded(address)", "upgrade", "upgraded"),
    ("CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)", "timelock", "timelocked"),
    ("CallExecuted(bytes32,uint256,address,uint256,bytes)", "timelock", "timelocked"),
    ("Transfer(address,address,uint256)", "treasury", "treasury"),
]
_TOPIC_TO_EVENT = {event_topic(sig): (kind, verb) for sig, kind, verb in _EVENTS}
_UPGRADE_TOPIC = event_topic("Upgraded(address)")
_TIMELOCK_TOPICS = [event_topic(_EVENTS[1][0]), event_topic(_EVENTS[2][0])]
_TRANSFER_TOPIC = event_topic("Transfer(address,address,uint256)")

_KIND_TOPICS = {
    "upgrade": [_UPGRADE_TOPIC],
    "timelock": _TIMELOCK_TOPICS,
    "treasury": [_TRANSFER_TOPIC],
}


# ---- Watch list -------------------------------------------------------------

@dataclass
class Watch:
    address: str
    asset: str
    kinds: tuple[str, ...]
    from_address: str | None = None
    gecko_id: str | None = None
    decimals: int = 18


def _load_watch(watch) -> list[Watch]:
    out: list[Watch] = []
    for w in watch or []:
        addr = (w.get("address") or "").lower()
        if not addr:
            continue
        kinds = tuple(k.lower() for k in (w.get("kinds") or DEFAULT_KINDS) if k.lower() in _KIND_TOPICS)
        if not kinds:
            continue
        out.append(Watch(
            address=addr,
            asset=(w.get("asset") or "").upper(),
            kinds=kinds,
            from_address=(w.get("from") or "").lower() or None,
            gecko_id=w.get("gecko_id"),
            decimals=int(w.get("decimals", 18)),
        ))
    return out


def _addr_topic(address: str) -> str:
    """A 20-byte address as a 32-byte log topic (left-zero-padded)."""
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


# ---- Log → Post -------------------------------------------------------------

def _short(addr: str) -> str:
    a = addr.lower()
    return a if len(a) <= 12 else f"{a[:8]}…{a[-4:]}"


def _amount(log: dict, decimals: int) -> float:
    data = (log.get("data") or "0x")
    if data in ("", "0x"):
        return 0.0
    return int(data, 16) / (10 ** decimals)


def _action_post(w: Watch, log: dict, kind: str, verb: str, now: datetime,
                 *, usd: float | None = None, tokens: float | None = None) -> Post:
    tx = (log.get("transactionHash") or "?")
    idx = log.get("logIndex") or "0"
    sym = w.asset
    dollar = f"${sym} " if sym else ""
    where = _short(w.address)
    if kind == "upgrade":
        text = f"[ON-CHAIN] {dollar}proxy contract {verb} — new implementation deployed ({where})"
    elif kind == "timelock":
        text = f"[ON-CHAIN] {dollar}governance action {verb} — execution scheduled on-chain ({where})"
    else:  # treasury
        mag = f" ${usd / 1e6:.1f}M" if usd else ""
        amt = f"{tokens:,.0f} tokens" if tokens else "funds"
        text = f"[ON-CHAIN] {dollar}treasury moved {amt}{mag} — treasury transfer ({where})"
    return Post(
        source="onchain_actions",
        uri=f"onchain_actions:{kind}:{tx}:{idx}",
        url=f"https://etherscan.io/tx/{tx}" if tx != "?" else None,
        text=text,
        created_at=now.isoformat(),
        indexed_at=now.isoformat(),
        author=Author(handle="onchain", display_name=f"{sym or 'on-chain'} {kind}"),
        raw={"kind": kind, "asset": sym, "address": w.address, "tx": tx,
             "usd": usd, "tokens": tokens},
    )


def logs_to_posts(logs, w: Watch, *, now: datetime | None = None,
                  min_value_usd: float = 0.0, price_usd: float | None = None) -> list[Post]:
    """Classify raw `eth_getLogs` logs for one watched contract into event posts.

    Treasury (Transfer) logs are USD-gated: below `min_value_usd`, or unpriceable
    when a gate is set, they're dropped as noise.
    """
    now = now or datetime.now(timezone.utc)
    out: list[Post] = []
    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        ev = _TOPIC_TO_EVENT.get(str(topics[0]).lower())
        if ev is None:
            continue
        kind, verb = ev
        if kind not in w.kinds:
            continue
        if kind == "treasury":
            if w.from_address and (len(topics) < 2 or not str(topics[1]).lower().endswith(w.from_address.removeprefix("0x"))):
                continue
            tokens = _amount(log, w.decimals)
            usd = tokens * price_usd if price_usd else None
            if min_value_usd > 0 and (usd is None or usd < min_value_usd):
                continue
            out.append(_action_post(w, log, kind, verb, now, usd=usd, tokens=tokens))
        else:
            out.append(_action_post(w, log, kind, verb, now))
    return out


# ---- JSON-RPC (eth_getLogs) -------------------------------------------------

def _rpc(method: str, params, url: str, *, timeout: float = 60.0):
    headers = dict(_HEADERS)
    headers["Content-Type"] = "application/json"
    resp = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      headers=headers, timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"rpc {method} failed: {resp.status_code} {resp.reason_phrase}")
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"rpc {method} error: {body['error']}")
    return body.get("result")


def latest_block(url: str = DEFAULT_RPC) -> int:
    return int(_rpc("eth_blockNumber", [], url), 16)


def fetch_logs(address: str, topic0, from_block: int, to_block: int, url: str,
               *, topic1: str | None = None) -> list[dict]:
    """`eth_getLogs` for one address over a block range. topic0 may be a list (OR)."""
    topics: list = [topic0]
    if topic1 is not None:
        topics.append(topic1)
    return _rpc("eth_getLogs", [{
        "address": address,
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "topics": topics,
    }], url) or []


def fetch_logs_chunked(address: str, topic0, from_block: int, to_block: int, url: str,
                       *, topic1: str | None = None,
                       chunk_blocks: int = DEFAULT_CHUNK_BLOCKS, pause: float = 0.15) -> list[dict]:
    """`fetch_logs` split into ≤`chunk_blocks` windows (keyless nodes cap the range).

    A `pause` between windows keeps a free node from rate-limiting a burst of
    requests. A window that fails (a node's range/result cap, a transient 403) is
    logged and skipped rather than sinking the whole scan.
    """
    logs: list[dict] = []
    start = from_block
    first = True
    while start <= to_block:
        end = min(start + chunk_blocks - 1, to_block)
        if not first and pause:
            time.sleep(pause)
        first = False
        try:
            logs.extend(fetch_logs(address, topic0, start, end, url, topic1=topic1))
        except Exception as err:  # noqa: BLE001 — one window shouldn't sink the scan
            print(f"onchain_actions {address} blocks {start}-{end} skipped: {err}", file=sys.stderr)
        start = end + 1
    return logs


# ---- Orchestration ----------------------------------------------------------

def fetch_onchain_actions(
    watch,
    *,
    rpc_url: str = DEFAULT_RPC,
    lookback_blocks: int = DEFAULT_LOOKBACK_BLOCKS,
    min_value_usd: float = 0.0,
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
    now: datetime | None = None,
    cache: HttpCache | None | str = ".cache/onchain_http.json",
    head_block: int | None = None,
) -> list[Post]:
    """Governance/technical events on watched contracts as `onchain_actions` posts."""
    now = now or datetime.now(timezone.utc)
    entries = _load_watch(watch)
    if not entries:
        return []
    if isinstance(cache, str):
        cache = HttpCache(cache)

    head = head_block if head_block is not None else latest_block(rpc_url)
    from_block = max(0, head - lookback_blocks)

    # Price treasury tokens once (batched, free DefiLlama coins API).
    gecko_ids = {e.gecko_id for e in entries if "treasury" in e.kinds and e.gecko_id}
    price_by_gecko = _prices(gecko_ids, cache) if gecko_ids else {}

    posts: list[Post] = []
    for w in entries:
        gov_topics = [t for k in w.kinds if k != "treasury" for t in _KIND_TOPICS[k]]
        try:
            if gov_topics:
                logs = fetch_logs_chunked(w.address, gov_topics, from_block, head, rpc_url,
                                          chunk_blocks=chunk_blocks)
                posts.extend(logs_to_posts(logs, w, now=now))
            if "treasury" in w.kinds:
                topic1 = _addr_topic(w.from_address) if w.from_address else None
                logs = fetch_logs_chunked(w.address, _TRANSFER_TOPIC, from_block, head, rpc_url,
                                          topic1=topic1, chunk_blocks=chunk_blocks)
                price = (price_by_gecko.get(w.gecko_id) or {}).get("price") if w.gecko_id else None
                posts.extend(logs_to_posts(logs, w, now=now, min_value_usd=min_value_usd, price_usd=price))
        except Exception as err:  # noqa: BLE001 — one bad address shouldn't sink the batch
            print(f"onchain_actions {w.address} skipped: {err}", file=sys.stderr)
    return posts
