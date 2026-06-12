# Verification Log

Every fee, limit, endpoint, and auth fact this project relies on, with its source URL,
retrieval date, and how it was verified. Per the project prime directives: official docs
are ground truth, live calls beat assumptions, and documented conflicts go behind config
flags — never guesses.

**Retrieval date for everything below: 2026-06-11** (live calls made from a VPN egress;
see "Environment caveats" at the bottom). Raw live responses are committed under
`tests/fixtures/live_2026-06-11/`.

---

## 1. Kalshi

### 1.1 Trading fees (general schedule)

| Fact | Value | Source | Verified how |
|---|---|---|---|
| Taker fee | `ceil_to_cent(0.07 × C × P × (1−P))` | https://kalshi.com/fee-schedule, https://help.kalshi.com/en/articles/13823805-fees | Doc (search-indexed copy; direct fetch was HTTP 429 at retrieval time — re-fetch before real-money use) |
| Maker fee (where applicable) | `ceil_to_cent(0.0175 × C × P × (1−P))`; charged only on series with maker fees; no fee on canceled resting orders | same | Doc |
| Fee charged on | taker fees on immediately-matched orders only | same | Doc |

### 1.2 Fill-exact precision model

Source: https://docs.kalshi.com/getting_started/fee_rounding (fetched directly, 200).

- Trade fee from the fee model is rounded **up to the nearest $0.0001** (centicent).
- Balance precision targets: **non-direct members $0.01**, **direct members $0.0001**.
- Per fill: `balance_change = revenue − trade_fee`, floored to target precision;
  `rounding_fee = balance_change − floor(balance_change)`.
- **Net fee = trade fee + rounding fee − rebate (always ≥ $0.00).**
- A **fee accumulator** tracks cumulative rounding overpayment across all fills of an
  order; when it exceeds $0.01 a whole-cent rebate is issued and the accumulator is
  reduced by $0.01. The accumulator carries over if an order takes then rests as maker.
- Total fee converges to what a single equivalent fill would cost.

### 1.3 Per-series fee overrides (the versioned override table source)

- Endpoint: `GET /trade-api/v2/series/fee_changes?show_historical=true` — **works
  unauthenticated, live-verified HTTP 200** (`tests/fixtures/live_2026-06-11/kalshi_fee_changes.json`).
- Doc: https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes
- Schema: `{id, series_ticker, fee_type ∈ {quadratic, quadratic_with_maker_fees, flat},
  fee_multiplier (double), scheduled_ts}`.
- Live examples observed: `KXHYPEPERP` → quadratic, multiplier 0 (fee-free);
  `KXWCGAME` → quadratic_with_maker_fees, multiplier 1.
- Interpretation: `fee_multiplier` scales the general schedule (e.g. multiplier 0.5 on
  an index series halves the 0.07 coefficient). ⚠️ The exact semantics of `flat` fee_type
  are not documented on the fetched page — **coded behind the override table; `flat`
  entries are surfaced to manual review rather than priced.**

### 1.4 Orderbook access and format

- **`GET /trade-api/v2/markets/{ticker}/orderbook` works UNAUTHENTICATED in production**
  — live-verified HTTP 200 (the API reference shows auth headers; quick-start behavior
  is what holds). Fixture: `kalshi_orderbook.json`.
- ⚠️ **Fixed-point migration**: responses now use `orderbook_fp: {yes_dollars, no_dollars}`
  with string dollar prices, and market objects use `*_dollars` / `*_fp` fields
  (`yes_bid_dollars`, `volume_24h_fp`, …). Source: https://docs.kalshi.com/getting_started/fixed_point_migration.
  Clients must parse the fixed-point format; legacy integer-cent fields are not present.
- Kalshi exposes only YES-bid and NO-bid ladders; asks are synthesized
  (YES ask at X ≡ NO bid at 1−X). Multivariate (`MVE*`) series returned empty REST books
  even with high volume — combo markets don't quote in the standard book. **Excluded from
  scanning in Phase 3.**
- `GET /markets` documents cursor pagination and `mve_filter=exclude`:
  https://docs.kalshi.com/api-reference/market/get-markets. The client now follows every
  cursor, sends the exclusion filter, and rejects repeated cursors or excessive page
  counts. Integration tests cover both pagination and loop protection. This addresses a
  prior live run where the first 100 unfiltered results were all MVE markets.

### 1.5 Rate limits

Source: https://docs.kalshi.com/getting_started/rate_limits (fetched directly).

- Token-bucket per tier; **most requests cost 10 tokens** (`GET /account/endpoint_costs`
  for non-default costs).
- Basic tier: **200 read tokens/s, 100 write tokens/s** → ≈ **20 reads/s and 10 writes/s**
  effective. ⚠️ SPEC.md's "Basic 200r/100w per second" is the token budget, not requests.
- Read buckets hold 1 s of budget (no accumulation); write buckets hold 2 s from
  Advanced tier up (burst), Basic write bucket holds only 1 s.
- 429 body `{"error": "too many requests"}` with **no Retry-After / X-RateLimit headers**
  → client-side exponential backoff with jitter is mandatory (as SPEC assumed).
- Tiers: Basic (default), Advanced (300/300), Premier (1k/1k), Paragon (2k/2k),
  Prime (4k/4k); upgrades by request or 30-day volume share.

### 1.6 Auth and WebSocket

Source: official starter repo (primary source, fetched raw):
https://github.com/Kalshi/kalshi-starter-code-python (`clients.py`).

- REST base: `https://api.elections.kalshi.com/trade-api/v2`; demo `https://demo-api.kalshi.co`.
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2`.
- Signing: RSA-PSS over `timestamp_ms + METHOD + path`, SHA-256, salt length =
  `padding.PSS.DIGEST_LENGTH`, base64 signature; headers `KALSHI-ACCESS-KEY`,
  `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`.

### 1.7 Funding costs

Sources: https://help.kalshi.com/en/articles/13823798-bank-deposits,
https://help.kalshi.com/en/articles/13823795-card-deposits,
https://help.kalshi.com/en/articles/13823803-bank-withdrawals,
https://help.kalshi.com/en/articles/13823791-transfers-faq (search-indexed copies).

- ACH/bank deposits: free. Wire deposits: free; wire-deposited funds withdrawable
  immediately (no hold).
- Debit card deposits: **up to 2% processing fee** (configurable in our model).
- Temporary withdrawal holds on deposited funds vary by method → modeled as
  capital-lock time, not dollar cost (per SPEC).
- Crypto processor fees: not found on the indexed pages. The scanner leaves processor
  cost unknown, and therefore rejects by default, until an operator configures it.

---

## 2. Polymarket

### 2.1 Trading fees

Source: https://docs.polymarket.com/trading/fees (fetched directly, 200).

- Formula (docs): `fee = C × feeRate × p × (1 − p)`; **takers only, makers never pay**;
  fees computed **at match time by the protocol** (no fee info in orders since V2).
- Generalized form (official SDK `py-clob-client-v2/py_clob_client_v2/fees.py`, fetched raw):
  `platform_fee_rate = rate × (p × (1 − p)) ** exponent`; fee = shares × that.
  The docs formula is the `exponent = 1` case. **Phase 1 implements (rate, exponent).**
- Current category taker rates (docs, verbatim): Crypto **0.07**, Sports **0.03**,
  Finance/Politics/Mentions/Tech **0.04**, Economics/Culture/Weather/Other **0.05**,
  Geopolitics **0** (fee-free, no rebates). ✅ Matches SPEC.md exactly.
- Rounding: **5 decimal places; minimum fee 0.00001 USDC**; smaller rounds to zero.
- Rollout history (https://docs.polymarket.com/changelog): 2026-01-05 15-min crypto;
  2026-02-18 NCAAB/Serie A; 2026-03-06 all crypto; 2026-03-30 all categories except
  geopolitics. **CLOB V2 live 2026-04-28: `feeRateBps` removed from order struct.**
- ⚠️ Third-party articles (Medium, PredictionHunt, etc.) describe different rates
  ("Crypto 1.80%, Sports 0.75%"). The official docs page contradicts them; official wins.
  Those articles appear to quote effective fee at p=0.5 (0.07 × 0.25 = 1.75% ≈ "1.80%").

### 2.2 Per-market fee resolution

- SDK: `get_clob_market_info(condition_id)` returns fee fields; WS `new_market` event
  carries `fee_schedule: {exponent, rate, taker_only, rebate_rate}` —
  **this is the runtime per-market source** (per SPEC directive).
- A live Gamma market observed during the independent audit exposed a `0.05` fee schedule.
  The scanner now parses Gamma `feeSchedule`, snake-case WS metadata, and compact CLOB
  `fd` metadata, then threads the resulting rate/exponent into candidate economics. A
  fixture test proves a `0.05` schedule is charged rather than treated as zero.
- `GET https://clob.polymarket.com/fee-rate?token_id=…` (doc:
  https://docs.polymarket.com/api-reference/market-data/get-fee-rate) returns
  `{"base_fee": <bps>}`. ⚠️ **Live-observed `base_fee = 1000` uniformly across Crypto,
  Politics, Economics, Culture markets** — 1000 bps ≠ any category rate, so this is a
  protocol cap/base, NOT the effective taker rate. **Discrepancy recorded; fee resolution
  uses market-info `fee_schedule` first, category defaults as flagged fallback, and
  never `/fee-rate.base_fee` alone.**
- Market metadata `maker_base_fee` / `taker_base_fee` (observed 1000) have the same
  caveat.

### 2.3 Rebates

- Maker rebates: daily, 25% of collected taker fees for most categories, 20% crypto
  (docs/trading/fees + changelog).
- Taker rebate program: SPEC cites "live 2026-05-28". ⚠️ **Not found in the changelog
  excerpt fetched** — modeled as optional, user-specific, off-by-default offsets
  (excluded from headline edge) regardless, per SPEC.

### 2.4 Bridge quotes

Source: https://docs.polymarket.com/api-reference/bridge/get-a-quote (fetched directly).

- `POST https://bridge.polymarket.com/quote`, JSON body: `fromAmountBaseUnit,
  fromChainId, fromTokenAddress, recipientAddress, toChainId, toTokenAddress`.
- Response `estFeeBreakdown`: `gasUsd, appFeePercent, appFeeUsd, fillCostPercent,
  fillCostUsd, maxSlippage, swapImpact(+Usd), totalImpact(+Usd), minReceived` plus
  `estCheckoutTimeMs, estInputUsd, estOutputUsd, estToTokenBaseUnit, quoteId`.
- Bridge API rate limit: **50 req / 10 s** (most restrictive of all).
- `BridgeQuote` parses this schema, but the scan loop does not yet request live bridge
  quotes. Bridge cost is an explicit operator setting and is unknown by default; unknown
  required costs reject candidates unless `ARB_ALLOW_UNKNOWN_COSTS=true` is set.

### 2.5 Geoblocking / jurisdiction

Source: https://docs.polymarket.com/api-reference/geoblock (fetched directly).

- `GET https://polymarket.com/api/geoblock` → `{blocked: bool, ip, country, region}`.
- **US is fully blocked for order placement** (33 fully blocked countries; Poland,
  Singapore, Thailand, Taiwan close-only; Ontario and occupied-Ukraine regions blocked).
- Confirms the SPEC design: `discovery-only` shipped default; execution requires config
  flag AND a passing runtime geoblock check; hard-disable on `blocked: true`.

### 2.6 Rate limits

Source: https://docs.polymarket.com/api-reference/rate-limits (fetched directly).

- Global: 15,000 req/10 s. CLOB general **9,000 req/10 s**; `/book` **1,500 req/10 s**;
  `/books` 500/10 s; `/prices` 500/10 s. Gamma general 4,000/10 s (`/markets` 300/10 s,
  `/events` 500/10 s). Data-API 1,000/10 s. Bridge 50/10 s.
- Enforcement: Cloudflare **sliding windows; excess requests are throttled/queued**
  rather than instantly rejected. ✅ Matches SPEC's "sliding-window awareness".
- Trading endpoints have dual burst/sustained limits (e.g. `POST /order` 5,000/10 s
  burst, 120,000/10 min sustained).

### 2.7 Orderbook history (backtesting source)

- **`GET https://clob.polymarket.com/orderbook-history?asset_id=<token_id>&startTs=<epoch>`
  EXISTS and works — live-verified HTTP 200.** Errors observed: missing `startTs` →
  `{"error":"startTs is required"}`; `token_id`/bare `market` param rejected
  (`asset_id` is the working param).
- Response: `{count, data: [{market, asset_id, timestamp(ms string), hash, bids, asks,
  min_order_size, tick_size, neg_risk, last_trade_price}]}` — full L2 snapshots.
- Observed depth: **count = 1,265,449 snapshots** for one sample asset, oldest observed
  timestamp ≈ 2025-11-26. Fixture: `poly_orderbook_history.json` (truncated sample).
- ⚠️ **Endpoint is absent from the docs sitemap and changelog → undocumented/semi-official.**
  Treated as available but unstable: Phase 6 uses it where present, but prospective
  snapshot persistence from live scanning remains the primary historical source (per SPEC).
  Pagination/retention limits unprobed — verify before relying on bulk export.

### 2.8 Market WebSocket

Source: https://docs.polymarket.com/market-data/websocket/market-channel (fetched directly).

- `wss://ws-subscriptions-clob.polymarket.com/ws/market`, public, no auth.
- Subscribe: `{"assets_ids": [...], "type": "market", "custom_feature_enabled": true}`.
- Events: `book` (L2 snapshot), `price_change`, `tick_size_change` (tick boundaries at
  >0.96 / <0.04), `last_trade_price`; with custom features: `best_bid_ask`, `new_market`
  (includes `fee_schedule`), `market_resolved` (winning_asset_id/outcome).
- ✅ Matches SPEC's channel list; `tick_size_change` and `new_market` are additions worth
  consuming.

---

## 3. Cross-cutting discrepancies and flags

1. **`/fee-rate.base_fee = 1000` uniform** vs category schedule → fee resolution order:
   market `fee_schedule` (rate, exponent, taker_only) → category default (flagged
   "default, unverified for this market") → never base_fee alone. (§2.2)
2. **Kalshi rate limits are token-cost based** (≈20 r/s, 10 w/s on Basic), not raw
   request counts as SPEC's shorthand suggested. Token-bucket client sized accordingly. (§1.5)
3. **Kalshi fixed-point migration**: all parsing uses `*_dollars`/`*_fp` string fields. (§1.4)
4. **Kalshi `flat` fee_type semantics undocumented** → such series go to manual review. (§1.3)
5. **Polymarket taker-rebate launch date (SPEC: 2026-05-28) unconfirmed** in fetched
   changelog → rebates off-by-default regardless. (§2.3)
6. **`orderbook-history` undocumented** → used opportunistically, never as the sole
   backtest source. (§2.7)
7. **Kalshi MVE (multivariate) series** show empty REST orderbooks → excluded from
   matching/scanning. (§1.4)
8. **kalshi.com/fee-schedule returned 429** at retrieval time; formulas corroborated via
   help center + search-indexed copies → re-fetch and re-verify coefficients before any
   real-money deployment.
9. Third-party fee articles contradict official Polymarket docs; official docs win. (§2.1)

## 4. Environment caveats

- The development machine's default network sits behind a Zscaler filter (NYC DOE) that
  **blocks both venues' API and doc hosts outright** (HTTP 403 block pages) and MITMs TLS.
  All live verification above was performed over a user-enabled VPN egress on 2026-06-11.
- Live smoke tests (`pytest -m live`) therefore require the VPN (or an unrestricted host,
  e.g. the deployment VPS). CI/local default test runs use committed fixtures only.
- Fixtures: `tests/fixtures/live_2026-06-11/` (large responses truncated; marked with
  `_truncated_from`).

## 5. Scanner implementation verification

The following labels describe the current code, not the target specification.

| Area | Status | Evidence / limitation |
|---|---|---|
| Kalshi discovery | Implemented, fixture-tested | Cursor pagination, `mve_filter=exclude`, repeated-cursor failure. |
| Polymarket discovery | Implemented, fixture-tested | Bounded Gamma `/markets/keyset` pagination, deduplication, repeated-page/cursor protection, and normalized metadata. Default limit is 500 markets over at most 5 pages. |
| Order books | Implemented, public-read verified | Kalshi REST capture time is used because the REST payload has no server snapshot time. |
| Rule equivalence | Conservative partial | Typed election/control/nominee, margin, sports, financial, and weather contracts plus event date, threshold, source, time, void/fair-value/DNP, cancellation, and dispute terms. Conflicts reject; missing critical facts become `manual_review`. Full natural-language rule interpretation is not implemented. |
| Polymarket fees | Implemented, fixture-tested | Per-market metadata wins. Category fallback/unknown rejects unless explicitly allowed. |
| Kalshi fees | Partial | General taker schedule is used. Per-series override code is not wired into scanning. |
| Non-trading costs | Configured/unknown | Bridge, withdrawal, gas, processor, and conversion are explicit fields but not live-fetched. Unknowns reject by default. |
| Slippage | Simulated | Configurable model; default fixed 0.5 cents/share. Not a realized fill measurement. |
| Risk facts | Implemented with caveats | Hold time comes from venue end timestamps; quote age from snapshots; exposure is scan-session only. |
| Persistence | Implemented, bounded | Raw low-similarity persistence is off by default. Manual-review and accepted rows remain; rejected rows are capped per scan. Retention removes old pairs/books/evaluations. |
| Replay | Partial | Re-evaluates complete paired snapshots. No realized P&L or later two-leg fill sequence is claimed. |
| Alerts | Implemented/mocked in tests | Dry-run external sinks are disabled by default; transport tests use local HTTP fixtures. |
| Execution | Disabled / unimplemented | No order placement code. Router raises after eligibility checks and is not called by the CLI. |

Gamma pagination follows the official keyset contract documented at
https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination.
The endpoint documents `limit` up to 100, `after_cursor`, and `next_cursor`; scanner
limits remain operator-configurable and finite.

The 2026-06-11 public dry-run fetched 500 unique Gamma markets over 5 pages instead
of the prior 100-market single page. After typed market, date, threshold, party, state,
and ticker diagnostics, it produced 18,615 raw-title candidates, 2,482 structured
candidates, 14 manual-review candidates, 18,601 rejections, and 0 accepted candidates.
The prior parser left 230 candidates in manual review; the stricter diagnostics now
reject additional market-type, date, threshold, and outcome conflicts. Manual-review
entries remain diagnostic and explicitly labeled NOT TRADE SAFE. See
`docs/examples/dry-run-live.log`.

Validation after the structured parsing changes: `312 passed, 1 deselected`,
`ruff check .` clean, `mypy arb_scanner` clean, `mypy .` clean, and `git diff --check`
clean. The deselected test is the opt-in live public-read smoke test.

## 6. Report exports, verification checklist, and source identifiers (2026-06-11)

Diagnostic reports now render the same sorted rows as `text`, `csv`, or `json`
(`--format`, `--output`), plus a human-readable `--verification-packet`. Every
non-accepted exported row carries the literal label `NOT TRADE SAFE`, and the
JSON/packet outputs carry an explicit disclaimer that no row is a trade
recommendation or an arbitrage/profitability claim. Exports contain only
persisted venue market metadata; settings and credentials are never serialized.

Checklist fields (`needs_determination_time`, `needs_resolution_source`,
`needs_void_policy`, `needs_threshold_confirmation`,
`needs_event_date_confirmation`, `needs_market_type_confirmation`,
`needs_fee_confirmation`, `needs_liquidity_confirmation`) are derived
diagnostics, not acceptance rules: a field missing on either venue, named in
the unresolved rule fields, or named in a structured conflict flags it;
`needs_fee_confirmation` flags any fee source other than per-market venue
metadata; `needs_liquidity_confirmation` flags rows whose order-book economics
were never computed. Clearing the checklist does not accept a pair and
acceptance thresholds were not changed.

Public URL derivation policy:

- Polymarket: Gamma event metadata exposes `slug` values that map to the
  public page `https://polymarket.com/event/<event-slug>`; the export derives
  that URL only when an event slug was captured. The market-level `slug` is
  exported as an identifier without a URL.
- Kalshi: no public market URL is reliably derivable from the market ticker
  alone (public pages are organized by series/event slugs that the trade API
  market payload does not document as URL components), so Kalshi rows export
  the market ticker and `event_ticker` identifiers without a URL. Verified
  against current code/docs on 2026-06-11; revisit if Kalshi documents a
  canonical ticker-addressed URL.

Rows persisted before this change lack the new identifiers (`event_ticker`,
`slug`, `event_slug`); exports render those as null/empty rather than guessing.

Storage growth: raw conflict-free low-similarity candidates are not persisted
by default; structured rejections are capped per scan; every persisted scan
pass prunes rows older than `ARB_STORAGE_RETENTION_DAYS`; and
`arb-scanner report --cleanup-retention` prunes on demand, printing per-table
removal counts.

## 7. GOVPARTY settlement-basis divergence (verified 2026-06-11)

Manual source verification of the top manual-review pair (GOVPARTYSC-26-R vs
Polymarket condition `0x1b7fa10e…`, the same-party 2026 South Carolina
governor markets). Sources fetched 2026-06-11:

- https://api.elections.kalshi.com/trade-api/v2/markets/GOVPARTYSC-26-R
- https://api.elections.kalshi.com/trade-api/v2/events/GOVPARTYSC-26
- https://api.elections.kalshi.com/trade-api/v2/series/GOVPARTYSC
- https://kalshi-public-docs.s3.amazonaws.com/contract_terms/GOVPARTY.pdf
- https://gamma-api.polymarket.com/events?slug=south-carolina-governor-winner-2026
- https://gamma-api.polymarket.com/markets?slug=will-the-republicans-win-the-south-carolina-governor-race-in-2026

Findings:

- **Kalshi (GOVPARTY family)** pays on the party of the person **sworn
  in/inaugurated**: market rules — *"If a representative of the Republican
  party is inaugurated as the governor … pursuant to the 2026 election"*;
  contract terms — payout encompasses *"the person sworn in to the
  governorship"*, a governor-elect vacancy passes to *"the party of the person
  first sworn in as their temporary, or permanent, replacement"*, and a
  candidate's party is locked to **election day** if they switch before
  seating. Source Agency is the state; accelerated early resolution on 4-of-8
  media calls (NYT, AP, DDHQ, CNN, Fox, NBC, CBS, ABC); expiration runs to the
  swearing-in (capped one year after the vote).
- **Polymarket** pays on the **election winner**: *"resolve according to the
  winner of the 2026 … gubernatorial election"*, where party means **the
  nominee** and independents are excluded *"regardless of any affiliation"*.
  It resolves once AP, Fox News, and NBC all call the race for the same
  candidate, else on official certification, executed through the UMA oracle
  (bond $2,500, default dispute window, negRisk event). The structured
  `resolutionSource` field is null — the sources exist only in the description
  text.

Conclusion: **not equivalent for arbitrage**. The two bases settle differently
in documented scenarios (governor-elect dies/resigns/replaced before
inauguration; a party-registered candidate running as an independent wins;
recount flips after UMA finalization). Neither venue documents a void/50-50
policy for a canceled election.

Encoded as `settlement_basis_conflict` in
`arb_scanner/app/markets/rule_equivalence.py`: a hard failure (rejection) only
when the Kalshi rules text shows sworn-in/inaugurated/member-of-party language
**and** the Polymarket rules text shows winner/nominee/race-call language,
each side showing exactly one basis. Texts showing both bases or neither stay
with the existing conservative checks (manual_review on missing facts). This
check rejects; it never accepts, and acceptance thresholds are unchanged.

## 8. 2,000-market dry-run: office-level and basket-scope conflicts (2026-06-11)

A wider discovery window (`ARB_POLYMARKET_MAX_MARKETS=2000`,
`ARB_POLYMARKET_MAX_PAGES=20`, alerts off) produced:

- Kalshi discovered=65225 scannable=65223
- Polymarket discovered=2000 scannable=2000
- raw_title=30080, structured=2313
- manual_review=5, accepted=0, rejected=30075

The 5 manual-review rows were inspected against live rules text and exposed
two false-positive families, both **not equivalent**:

1. **State legislative chamber control vs U.S. Senate race.** Kalshi
   `KXSTATELEG-NCSEN26-R` — *"If the Republican party wins the North Carolina
   State Senate in 2026 … Winning is defined as holding more seats than any
   other party"* — paired with Polymarket *"the winner of the 2026 midterm
   North Carolina U.S. Senate election, inclusive of any run-offs"*. Different
   offices and different elections despite near-identical titles.
2. **Multi-state sweep basket vs single race.** Kalshi
   `KXDEMCOREFOURSENATESWEEP-26NOV03` — *"If Democrats win the 2026 Senate
   elections in ALL of the following states: Georgia, Michigan, North
   Carolina, AND Maine"* — paired with the single-state Polymarket North
   Carolina Senate market. An all-of-N contract is never equivalent to one of
   its legs.

Encoded in `rule_equivalence.py` as two narrow **rejection-only** rules (they
can never accept a pair; acceptance thresholds are unchanged):

- `office_level_conflict`: fires only when one side's rules text classifies
  unambiguously as state-legislature (State Senate/House/Assembly/legislature,
  general assembly, "holding more seats") and the other unambiguously as
  federal Senate (U.S./US/United States/federal Senate). Text matching both
  patterns, or a bare "Senate race" with no level evidence, classifies as
  ambiguous and falls through to the existing conservative checks
  (manual_review on missing facts).
- `basket_scope_conflict`: fires only when one side is a confident basket —
  at least two distinct full state names plus all-of/sweep/win-all wording or
  a comma/"and" chain of state names — and the other side references exactly
  one state. Same-basket pairs, candidate-name lists (only state names count
  toward the chain), and zero-state texts never fire.

Both surface as named buckets in the scan rejection histogram.

## 9. 5,000-market dry-run: four contract-shape conflict families (2026-06-11)

A wider window (`ARB_POLYMARKET_MAX_MARKETS=5000`, `ARB_POLYMARKET_MAX_PAGES=50`,
alerts off) produced:

- Kalshi discovered=65183 scannable=65181
- Polymarket discovered=5000 scannable=4945
- raw_title=38270, structured=1321
- manual_review=17, accepted=0, rejected=38253

The 17 manual-review rows exposed four false-positive families, all **not
equivalent**:

1. **World Cup continent complement vs specific continent winner.** Kalshi
   `KXWCNOEURSA-26-Y` — *"any country not in Europe or South America wins"* —
   vs Polymarket *"Will South America win the 2026 FIFA World Cup?"*. A
   not-X-or-Y complement contract is the opposite side of, not the same bet
   as, one of its excluded continents.
2. **World Cup knockout-stage regional team count vs continent winner.**
   Kalshi `KXWCREGIONKO-26SA-1…6` — *"at least/exactly N teams from South
   America reach the knockout stage"* — vs the same Polymarket continent-
   winner market. Counting group-stage survivors is not the tournament
   winner.
3. **Crypto price threshold vs best-month performance.** Kalshi
   `KXBTCMAX100-26-SEP/JUNE` — *"Bitcoin above $100000 by <date>"* — vs
   Polymarket *"Will <month> be the best month for Bitcoin in 2026?"*
   (relative monthly percentage change).
4. **Stock fixed-date close threshold vs intramonth high.** Kalshi
   `KXINXDIRY-26DEC31H1600-T8200` — *"index value on Dec 31, 2026 at 4pm
   EST"* — vs Polymarket *"hit $8,200 (HIGH) in December"* (*"at any point …
   any 1-minute candle"*). A touch/high contract pays in strictly more worlds
   than a fixed-time close contract.

Encoded in `rule_equivalence.py` as four narrow **rejection-only** detectors
(they can never accept a pair; acceptance thresholds are unchanged), each
reading title + rules text and requiring one clear, opposing classification
per side — anything ambiguous falls through to the existing conservative
checks:

- `continent_scope_conflict`: needs World Cup context on both sides, a
  complement ("other than / not in / outside X or Y") on exactly one side,
  and exactly one named continent inside the exclusion set in the other
  side's **title**. Only the title identifies the specific side's continent:
  the live Gamma description names other continents as examples ("if France
  wins, the market will resolve to Europe"). A complement phrase inside a
  resolves-to-No sentence is the inverse statement of a single-continent
  market and is never read as an exclusion. Same-continent winner pairs
  (e.g. `KXWCCONTINENT-26-SA` vs "South America wins") never fire and stay
  manual_review.
- `sports_stage_vs_winner_conflict`: knockout-stage + team-count language on
  exactly one side, tournament-winner language (and no stage language) on the
  other.
- `crypto_performance_vs_price_threshold_conflict`: crypto asset on both
  sides; best-month/highest-percentage-change/monthly-candle language on
  exactly one side, an explicit price threshold on the other. A month name
  alone is never performance evidence.
- `stock_close_vs_intramonth_high_conflict`: index context on both sides;
  intramonth-high language (hit-HIGH / at any point / 1-minute candle) takes
  classification priority, so *"market close on the final day"* inside an
  any-point sentence still classifies as a high market. Kalshi fixed-date
  close vs Polymarket *"closes over X on the final trading day"* never fires
  and stays manual_review (sources/void still unverified).

Parser improvements in the same pass: `sports_stage_count` and
`crypto_monthly_performance` market types, so these shapes also surface as
structured market-type evidence. All four conflicts have named rejection
histogram buckets, ranked above the generic threshold/market-type buckets.

## 10. World Cup South America pair: cancellation-policy divergence (2026-06-11)

Manual verification of KXWCCONTINENT-26-SA vs Polymarket condition
`0x0ed2e5e9…` ("Will South America (CONMEBOL) win the 2026 FIFA World Cup?",
negRisk continent event). Sources fetched 2026-06-11:

- https://api.elections.kalshi.com/trade-api/v2/markets/KXWCCONTINENT-26-SA
- https://api.elections.kalshi.com/trade-api/v2/series/KXWCCONTINENT
- https://kalshi-public-docs.s3.amazonaws.com/contract_terms/SOCCER.pdf
- https://gamma-api.polymarket.com/markets?condition_ids=0x0ed2e5e9…
- https://worldpopulationreview.com/country-rankings/list-of-countries-by-continent
  (Polymarket's cited definitive continent source)

**Normal-case match:** Kalshi defines continent by FIFA qualification pathway
(explicit country table in rules_secondary); Polymarket by World Population
Review geography. Checked against the live WPR data, the two classifications
coincide on **every 2026 qualified team** for the South America leg (WPR puts
Curaçao and Aruba in North America; Suriname did not qualify). Türkiye
diverges (Kalshi UEFA/Europe vs WPR Asia) — a live warning for the Europe and
Asia legs of this family, but not for South America.

**Edge-case mismatch (the verdict driver):** the cancellation policies are
documented and structurally opposite. Kalshi's ACHIEVEMENTS contract terms
settle a cancelled event at **fair value** — *"'Yes' holders receive the last
traded price prior to cancellation"*, else an Outcome Review Committee
determination, else a *"$1/[number of eligible participants]"* split.
Polymarket resolves to **"Other"** — *"If the 2026 FIFA World Cup is
cancelled, postponed after December 31, 2026, or there is otherwise no winner
declared within that timeframe, this market will resolve to 'Other'"* — so
the South America leg pays a hard No. A two-leg hedge does not net $1 in the
cancellation state. Verdict: **NOT EQUIVALENT for arbitrage purposes.** This
is not a profit or arbitrage claim; the pair remains NOT TRADE SAFE.

Encoded in `rule_equivalence.py` as a two-layer diagnostic (rejection-only;
nothing here can accept a pair):

- `cancellation_policy_terms` / `cancellation_policy_basis` extract named
  terms (`fair_value`, `committee_review`, `split_or_1_over_n`,
  `resolves_to_other`, `hard_no_on_other`, `cancellation`,
  `postponement_deadline`) and classify rules text as
  `fair_value_settlement` or `resolves_to_other`; text showing both families
  or neither is ambiguous and classifies as None.
- `void_policy_conflict` **hard-rejects** only when BOTH sides' rules text
  proves a basis and the bases differ.
- One-sided extraction adds a `void_policy_mismatch` **warning** and a
  `void_policy_basis` missing field instead — the pair stays manual_review.
  This is the live KXWCCONTINENT shape: Kalshi's fair-value handling lives in
  the series-level contract-terms PDF, which the scanner does not fetch, so
  only the Polymarket basis is provable from market metadata.

The bases are persisted in metadata excerpts and exported
(`kalshi_cancellation_policy_basis`, `polymarket_cancellation_policy_basis`,
appended after the checklist columns to keep CSV headers append-only) and
appear in the rule-evidence summary used by text reports and verification
packets.

## 11. S&P 500 final-day close pair: source/finalization divergence (2026-06-11)

Manual verification of KXINXDIRY-26DEC31H1600-T8000 vs Polymarket condition
`0x8b13efb0…` ("Will S&P 500 (SPX) close over $8,000 on the final trading day
of December 2026?"). Sources fetched 2026-06-11:

- https://api.elections.kalshi.com/trade-api/v2/markets/KXINXDIRY-26DEC31H1600-T8000
- https://api.elections.kalshi.com/trade-api/v2/series/KXINXDIRY
- https://kalshi-public-docs.s3.amazonaws.com/contract_terms/INXDIR.pdf
- https://gamma-api.polymarket.com/markets?condition_ids=0x8b13efb0…

**Verdict: probably the same event, but NOT TRADE SAFE.** This is not an
arbitrage or profit claim. The boundary matches exactly (Kalshi
`floor_strike=8000.0001` greater-or-equal ≡ Polymarket "higher than $8,000";
exactly 8000.00 pays No on both) and the date matches in the expected
calendar (Dec 31, 2026 is a Thursday and the final scheduled NYSE trading day
of December). The blocker is source/finalization mechanics: Kalshi's
underlying is the **4:00 PM ET index snapshot** documented by Kalshi itself
("The Source Agency is Kalshi"; settlement source "For example, Google
Finance"; *"Revisions to the Underlying made after Expiration will not be
accounted for"*; no-data fallback to the most recent prior value), while
Polymarket resolves on the **official closing price** per Yahoo Finance
Historical Prices through UMA, with a last-valid-trade fallback. A correction
or auction finalization landing after Kalshi's ~4:01 PM expiration can
diverge near the strike.

Encoded as a **diagnostic-only** mismatch (never a rejection, never an
acceptance): `source_finalization_terms` / `source_finalization_basis`
classify rules text as `fixed_time_snapshot` (index-value-at-time, revisions
ignored, Kalshi source agency, no-data fallback) or `official_close`
(official closing price, historical/Yahoo close, last valid trade). When both
sides prove a basis and they differ, validate_rules adds a
`source_finalization_mismatch` warning and a `source_finalization_basis`
blocking field, keeping the pair manual_review. Same-basis or ambiguous pairs
get no warning; wording-only differences never classify.

## 12. Democratic Senate sweep pair: candidate-set conflict (2026-06-11)

Manual verification of KXDEMPROGRESSIVESENATESWEEP-26NOV03 vs Polymarket
condition `0x21ac6c0f…` ("Will Democratic Senate incumbents win all their
nominating elections in the 2026 cycle?"). Sources fetched 2026-06-11:

- https://api.elections.kalshi.com/trade-api/v2/markets/KXDEMPROGRESSIVESENATESWEEP-26NOV03
- https://gamma-api.polymarket.com/markets?condition_ids=0x21ac6c0f…

**Verdict: NOT EQUIVALENT.** Kalshi requires a **fixed named slate** to sweep
their primaries — *"Juliana Stratton in Illinois, Graham Platner in Maine,
Mallory McMorrow OR Abdul El-Sayed in Michigan, Peggy Flanagan in Minnesota,
and Ed Markey in Massachusetts"* — mostly challengers/non-incumbents.
Polymarket tracks the **incumbent cohort**: all Democratic Senate incumbents'
nominating elections (party, top-two/jungle, and special primaries, March 1 –
September 30, 2026), with membership conditioned on registration
(*"Incumbents who do not officially register as candidates for reelection
will not be considered"*) and withdrawal counting as a loss. A fixed slate
can never equal a registration-dependent cohort, and a slate member's primary
loss (e.g. Stratton) flips Kalshi to No without touching Polymarket.

Encoded as `candidate_set_conflict`, a hard rejection that fires only when
both sides are all-of sweep markets and either (a) both enumerate named
slates that differ (OR-alternatives like "McMorrow OR El-Sayed" are parsed as
one interchangeable group), or (b) one side enumerates a named slate of at
least two groups while the other defines its set as the incumbent cohort
without naming candidates. One-sided extraction without cohort evidence
produces a `candidate_set_mismatch` warning (manual_review) instead.

## 13. Live-regression gate for detectors

The 2026-06-11 audit found a detector with green unit tests that never fired
live. Tests alone are not sufficient evidence that a detector works: every
detector whose target was observed in a live scan must also be asserted
against a saved dry-run log. Workflow:

```bash
ARB_DRY_RUN_SEND_ALERTS=false uv run arb-scanner dry-run … | tee /tmp/dryrun.log
uv run arb-scanner check-log --file /tmp/dryrun.log \
  --expect accepted=0 \
  --expect manual_review<=10 \
  --expect continent_scope_conflict>=1 \
  --expect source_finalization_mismatch>=1
```

`check-log` reads only the local file (no network, no execution capability).
Expectation names resolve against the candidate-funnel counters, then the
rejection-histogram buckets (a bucket absent from a present histogram counts
as 0, so dead detectors fail loudly), then fall back to counting occurrences
in the log — which is how diagnostic warnings like
`source_finalization_mismatch` and `void_policy_mismatch` are checked, since
they never appear in the rejection histogram. Exit code 0 only when every
expectation passes.

## 14. 10,000-market dry-run: prefilter recall collapse and player-prop family (2026-06-11)

First 10k window (`ARB_POLYMARKET_MAX_MARKETS=10000`,
`ARB_POLYMARKET_MAX_PAGES=100`, alerts off):

- Kalshi discovered=62905 scannable=62903; Polymarket 10000/9942 over 100 pages
- raw_title=51514, structured=388, manual_review=133, accepted=0, rejected=51381

Two findings:

1. **Prefilter recall collapse.** Structured candidates fell from ~1,330 (5k
   window) to 388 (10k) and previously-firing conflict buckets
   (`settlement_basis_conflict`, `continent_scope_conflict`,
   `candidate_set_conflict`, …) vanished from the histogram. Cause: the
   token-breadth caps in `_candidate_positions` were absolute
   (`min(50, n/20)`, `min(10, n/100)`), so growing the corpus pushed
   previously-rare tokens over the cap and silently dropped known pairs.
   Fixed by scaling both caps proportionally above 5,000 markets; behavior at
   smaller windows is unchanged. This is a recall fix only — every recovered
   pair still flows through the unchanged conservative rule gates. Guarded by
   a regression test that plants a known pair in growing corpora.
2. **Player-proposition false-positive family** (~100+ of the 133
   manual-review rows). Same athlete, different proposition: Kalshi
   "Will Kayvon Thibodeaux win the Defensive Player of the Year?" vs
   Polymarket "Will Kayvon Thibodeaux be traded?"; Kalshi "lead Pro Baseball
   in wins" vs Polymarket "strike out the most batters"; Kalshi "record
   1000+ receiving yards" vs Polymarket "be traded". Shared name tokens push
   similarity over the review threshold but the payout events are unrelated.

Encoded as `player_prop_scope_conflict`, a hard rejection over a
`player_prop_kind` classifier (award_winner, transaction, stat_threshold,
`stat_leader:<stat>` with venue phrasings normalized per statistic — "lead
Pro Baseball in strikeouts" ≡ "strike out the most batters"). It fires only
when both sides classify to exactly one kind and the kinds differ. Same-stat
pairs (the potentially equivalent family, e.g. Yamamoto strikeout-leader on
both venues) and ambiguous/unclassified text always fall through to the
ordinary conservative checks. Rejection-only; acceptance logic unchanged.

## 15. Outcome-entity matching for categorical markets (2026-06-11)

Verified against the KXDCMAYORD family, the dominant residue of the 10k
window (104 of 153 manual-review rows). Sources fetched 2026-06-11:

- https://api.elections.kalshi.com/trade-api/v2/events/KXDCMAYORD-26?with_nested_markets=true
- the persisted Polymarket D.C.-mayor candidate markets (slugs
  `will-<candidate>-win-the-2026-democratic-dc-mayoral-primary`)

Finding: every Kalshi contract in the event shares one generic title ("Who
will win the 2026 D.C. Democratic Mayoral Primary?") while the per-contract
candidate lives in `custom_strike` (`{'Candidate/Party': 'Muriel Bowser'}`),
`yes_sub_title`, and the rules text. Polymarket's candidate is in each
market's question. Title-only matching therefore paired all 13 Kalshi
contracts with all 8 Polymarket candidate markets at ~0.95 confidence — 104
pairs, only ~13 of them candidate-aligned.

Encoded as an outcome-entity layer in `markets/discovery.py`:

- `kalshi_outcome_entity`: explicit per-contract fields only —
  `custom_strike` values outrank `yes_sub_title`; the generic title is never
  used. Non-name-like values (numeric strikes, UUID party ids, bare Yes/No)
  extract nothing.
- `poly_outcome_entity`: `groupItemTitle` outranks a conservative
  "Will <Name> win/be/become" question pattern; single-token subjects (e.g.
  "Republicans") extract nothing.
- `normalize_entity_name`: lowercase, strip punctuation, drop generational
  suffixes, keep initials.
- `outcome_entities_conflict`: provably-different-only — subset names
  ("brianne nadeau" vs "brianne k nadeau") are the same person; differing
  surnames or differing full first names conflict; initial-only or
  single-token differences are ambiguous and never conflict.

Wiring: both entities proven and conflicting → structured conflict
("outcome entity A != B") → REJECTED through the existing decide path,
bucketed as `outcome_entity_conflict`. Exactly one side extracted →
"outcome_entity unverified on one venue" warning + missing field; pair stays
manual_review. Neither side → behavior unchanged. Matching entities only
prevent false rejection — they never accept. Entity tokens
(custom_strike/yes_sub_title) are also added to the Kalshi searchable text in
candidate prefiltering, a recall-only change so entity-aligned pairs can
form; every formed pair still passes unchanged conservative validation.
Entities are persisted in matched_fields, exported
(`kalshi_outcome_entity`/`polymarket_outcome_entity`, appended to keep CSV
headers append-only), and shown in text reports, packets, and the
evidence-confidence summary.

Live effect (10k window, 2026-06-11): manual_review 153 → 61;
KXDCMAYORD 104 → 12, with all 12 survivors entity-aligned;
`outcome_entity_conflict=256` in the rejection histogram; accepted=0.

## 16. Central-bank decision direction/magnitude (2026-06-11)

Verified against the KXCBDECISIONMEXICO family (18 manual-review rows in the
10k window). Kalshi titles carry direction and magnitude ("Will the Bank of
Mexico Cut 25bps at the June … meeting?"); Polymarket titles carry direction
only ("Will the Bank of Mexico announce an increase at the June meeting?"),
and the Polymarket rules text enumerates every outcome ("Increase if raised,
Decrease if lowered, No Change otherwise"), so classification reads the
title line first — otherwise the live family would classify as ambiguous.

Encoded in `rule_equivalence.py`:

- `central_bank_decision`: requires rate/monetary-policy context; classifies
  direction (cut / hike / hold) and magnitude (`<n>bps`, `<n>bps_or_more`,
  or `any`); multiple or zero directions in scope → None (ambiguous never
  fires anything).
- `central_bank_direction_conflict` (hard rejection): both sides classified
  and directions differ — cut vs increase, hold vs move.
- `central_bank_magnitude_mismatch` (diagnostic, manual_review): same
  direction, different magnitude scope (Cut 25bps vs any decrease) — the
  contracts overlap but are not equivalent (a 50bps cut pays Polymarket
  "decrease" Yes and Kalshi "Cut 25bps" No).

Rejection/diagnostic-only; acceptance logic unchanged. No arbitrage or
profitability claim is implied for any same-direction pair.
