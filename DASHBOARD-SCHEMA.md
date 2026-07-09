# Output schema

Field reference for consumers of the two Catalyst services. Two schemas:
`catalyst.signals` (depth) and `catalyst.events` (breadth). Any field not marked
**required** may be absent from a given delivery.

---

## `catalyst.signals`

### Delivery — narration fields

Present only when narration is enabled. Each is a plain-language restatement of
values already in the payload; never trading advice.

| Field | Type | Required | When present |
| --- | --- | --- | --- |
| `summary` | string | no | narration on |
| `catalyst_notes` | object (string → string) | no | narration on + signal has catalysts |
| `layer_notes` | object (string → string) | no | narration on + signal has layers |

- `summary` — one-to-two sentence read of the top signal.
  _Example:_ `"BTC is a low-conviction neutral watch — ETF and regulation chatter across 10 mentions, but sentiment is flat and conviction is low."`
- `catalyst_notes` — one short gloss per catalyst tag on the signal.
  _Keys (fixed set):_ `listing, hack, etf, mainnet, regulation, partnership, liquidation, macro`
  _Example:_ `{ "etf": "spot-ETF flow/approval news", "regulation": "policy/legal developments" }`
- `layer_notes` — one short phrase per modifier layer that moved the signal.
  _Keys (fixed set):_ `macro, flow, supply, market, derivs, trend`
  _Example:_ `{ "flow": "ETF inflows nudged bullish", "macro": "risk-on regime" }`

### Requirements — input

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `window` | string | no | Lookback for catalyst history. Amount + unit: `6h`, `3d`, `1w`; bare number = hours (`48`). Range 1h–1w (clamped). Blank = `24h` |

---

## `catalyst.events`

### Delivery

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `schema` | string | **yes** | Fixed `"catalyst.events"` |
| `version` | string | **yes** | Schema version, `"2.0"` |
| `generated_at` | string | **yes** | ISO-8601 UTC timestamp the feed was produced |
| `disclaimer` | string | **yes** | Proposals-only / not-financial-advice notice |
| `count` | integer | **yes** | Number of events in this delivery (0 allowed) |
| `events` | array of strings | **yes** | One line per event: `ASSET \| catalyst \| what happened \| direction \| severity \| age`. Never empty (sentinel line when nothing matched) |
| `lead` | object | no | The single most market-moving event, structured (see sub-fields). Omitted when `count` is 0 |
| `assets` | array of strings | no | Distinct assets referenced across the events |
| `catalysts` | array of strings | no | Distinct catalyst types present (etf, hack, macro, …) |
| `window_hours` | number | no | The resolved lookback window used |
| `requirements` | object | no | Echo of the buyer's filters (present only when filters were sent) |

**`lead`** sub-fields:

| Sub-field | Type | Description |
| --- | --- | --- |
| `asset` | string | Ticker, or `MARKET` for market-wide |
| `catalyst` | string | Catalyst category |
| `event` | string | One-line "what happened" |
| `direction` | string | bullish / bearish / neutral |
| `severity` | string | high / medium / low |
| `sentiment` | number | −1..+1 market-directional score |
| `source` | string | Origin (rss, bluesky, github) |
| `url` | string | Link to the source post (may be null) |
| `at` | string | ISO-8601 timestamp of the event |

### Requirements — input

All optional; an order with no filters returns the top events. Multi-value
fields are comma-separated strings.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `assets` | string | no | Comma-separated tickers, e.g. `BTC,ETH`. Blank = all assets |
| `catalysts` | string | no | Comma-separated catalyst types, e.g. `etf,hack,regulation`. Blank = all |
| `min_severity` | string | no | Lowest severity to include: `high` / `medium` / `low`. Blank = `medium` |
| `direction` | string | no | Filter to `bullish` / `bearish` / `neutral`. Blank = all |
| `window` | string | no | Lookback: `6h` / `3d` / `1w` (bare number = hours). 1h–1w, clamped. Blank = `24h` |
| `limit` | string | no | Max events to return, e.g. `15`. Blank = `20` |
