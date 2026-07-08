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
