# Croo Dashboard schema — fields to register

Copy-paste field names + helper text for the Croo service builder. Two groups:
new **delivery** (output) fields for the grounded narration, and one new
**requirements** (input) field for the history window.

> **Required-field rule (learned the hard way):** the backend treats an empty
> `{}` / `[]` / null as *missing*. So any field that can be empty or absent in
> some deliveries MUST be marked **optional**, or the order fails
> `INVALID_DELIVERABLE` and SLA-expires. Every field below is **optional**.

---

## A. Delivery schema — grounded narration (3 new fields)

These appear only when narration is on (`ANTHROPIC_API_KEY` set). They are pure
presentation — a restatement of the numbers already in the payload. All optional.

### `summary`  ·  type: **string**  ·  optional
> Plain-language, one-to-two sentence read of the top signal — what it is and
> why — grounded entirely in the computed score, sentiment, mentions, catalysts,
> and layer biases. Non-prescriptive; never trading advice. Absent when narration
> is off.

_Example:_ `"BTC is a low-conviction neutral watch — ETF and regulation chatter across 10 mentions, but sentiment is flat and conviction is low."`

### `catalyst_notes`  ·  type: **object** (string → string)  ·  optional
> A short gloss for each catalyst tag present on the signal. Keys are the catalyst
> categories the signal actually carries; values are a few words explaining what
> that category means. Omitted when the signal has no catalysts.

_Key domain (fixed set):_ `listing, hack, etf, mainnet, regulation, partnership, liquidation, macro`
_Example:_ `{ "etf": "spot-ETF flow/approval news", "regulation": "policy/legal developments" }`

### `layer_notes`  ·  type: **object** (string → string)  ·  optional
> A short phrase for each modifier layer that moved the signal, explaining how it
> pushed (using that layer's label/bias/effect). Omitted for neutral watches,
> which carry no layers.

_Key domain (fixed set):_ `macro, flow, supply, market, derivs, trend`
_Example:_ `{ "flow": "ETF inflows nudged bullish", "macro": "risk-on regime" }`

> **Register both as a generic / free-form object** (dynamic string keys, no
> declared sub-fields) — the builder allows this, same as the existing `layers`
> field. No need to enumerate the keys. (The key domains above are listed only so
> you know what values can appear.) To be confirmed by a live order test.

---

## B. Requirements schema — history window (1 new field)

### `window`  ·  type: **string**  ·  optional
> How far back to read catalyst history when building the signal. Accepts an
> amount + unit: hours (`6h`), days (`3d`), or a week (`1w`); a bare number means
> hours (`48`). Range is 1 hour to 1 week — values outside are clamped. Leave
> blank for the default 24-hour window.

_Accepted:_ `6h` · `48` · `3d` · `1w`
_Default when blank:_ `24h`  ·  _Max:_ `168h` (one week)

**Register as a plain string** (same as `assets`) — the v2 requirements builder
can't do typed numbers cleanly, and the code parses the string. The provider also
accepts the keys `lookback`, `window_hours`, and `window_days` as aliases, but
buyers only need the one `window` field.

**Why a separate field, not `horizon`:** `horizon` filters the *output* signal's
time-horizon (intraday / swing); `window` sets how much *input* history feeds the
signal. Different axes — keep them distinct so buyers aren't confused.

---

## Quick reference

| Field            | Schema        | Type   | Required | When present |
| ---------------- | ------------- | ------ | -------- | ------------ |
| `summary`        | delivery      | string | no       | narration on |
| `catalyst_notes` | delivery      | object | no       | narration on + has catalysts |
| `layer_notes`    | delivery      | object | no       | narration on + is an alert (has layers) |
| `window`         | requirements  | string | no       | buyer chooses a lookback |

---

# Service: `catalyst.events` (the "events" feed)

Second service — breadth product (market-wide catalyst events), distinct from the
depth `catalyst.signals` service. No LLM at serve time; reads the stored `event` /
`severity` fields written at enrich time. Delivery uses **array-of-strings** for
the feed (supported) — never array-of-objects.

## C. Delivery schema — `catalyst.events`

| Field          | Type             | Required | Description |
| -------------- | ---------------- | -------- | ----------- |
| `schema`       | string           | **yes**  | Fixed `"catalyst.events"` |
| `version`      | string           | **yes**  | Schema version, `"2.0"` |
| `generated_at` | string           | **yes**  | ISO-8601 UTC timestamp the feed was produced |
| `disclaimer`   | string           | **yes**  | Proposals-only / not-financial-advice notice |
| `count`        | integer          | **yes**  | Number of events in this delivery (0 allowed) |
| `events`       | array of strings | **yes**  | The feed, one line per event: `ASSET \| catalyst \| what happened \| direction \| severity \| age`. **Never empty** — a single sentinel line when nothing matched |
| `lead`         | object           | no       | The single most market-moving event, structured (see sub-fields). Omitted when `count` is 0 |
| `assets`       | array of strings | no       | Distinct assets referenced across the events |
| `catalysts`    | array of strings | no       | Distinct catalyst types present (etf, hack, macro, …) |
| `window_hours` | number           | no       | The resolved lookback window used |
| `requirements` | object           | no       | Echo of the buyer's filters (present only when filters were sent) |

**`lead`** — register as a generic/free-form object (like `layers`). Sub-fields:

| Sub-field   | Type   | Description |
| ----------- | ------ | ----------- |
| `asset`     | string | Ticker, or `MACRO` for market-wide |
| `catalyst`  | string | Catalyst category |
| `event`     | string | One-line "what happened" |
| `direction` | string | bullish / bearish / neutral |
| `severity`  | string | high / medium / low |
| `sentiment` | number | −1..+1 market-directional score |
| `source`    | string | Origin (rss, bluesky, github) |
| `url`       | string | Link to the source post (may be null) |
| `at`        | string | ISO-8601 timestamp of the event |

> All non-`yes` fields are **optional** — the backend treats empty `[]` / `{}` /
> null as *missing* and expires the order. `events` stays required but is
> guaranteed non-empty (sentinel line on the empty case).

## D. Requirements schema — `catalyst.events` (all optional strings)

The order must work with **no** filters (returns the top events). All fields are
plain strings — the v2 requirements builder can't register typed numbers or
string arrays, so multi-value fields are comma-separated.

| Field          | Type   | Required | Description |
| -------------- | ------ | -------- | ----------- |
| `assets`       | string | no       | Comma-separated tickers to include, e.g. `BTC,ETH`. Blank = all assets |
| `catalysts`    | string | no       | Comma-separated catalyst types, e.g. `etf,hack,regulation`. Blank = all |
| `min_severity` | string | no       | Lowest severity to include: `high` / `medium` / `low`. Blank = `medium` (market-movers only) |
| `direction`    | string | no       | Filter to `bullish` / `bearish` / `neutral`. Blank = all |
| `window`       | string | no       | Lookback: `6h` / `3d` / `1w` (bare number = hours). 1h–1w, clamped. Blank = `24h` |
| `limit`        | string | no       | Max events to return, e.g. `15`. Blank = default (20) |
