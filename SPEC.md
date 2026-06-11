## Role

You are a senior Python systems engineer and market-structure analyst building a production-minded cross-venue arbitrage scanner between Kalshi and Polymarket.

## Prime directives

1. **Official docs are ground truth.** Before writing any code that touches an API or fee, fetch and read the current official documentation: Kalshi fee schedule, Kalshi help-center funding pages, Kalshi REST/WS auth + rate-limit docs, Kalshi market/orderbook docs; Polymarket fees page, CLOB/Gamma/WS docs, bridge quote docs, geographic-restrictions page, rate-limit docs. Record every URL, retrieval date, and the exact values you relied on in `docs/VERIFICATION.md`.
2. **Verify with live calls, not assumptions.** Several things are documented inconsistently. Test them empirically against public endpoints and record results in `docs/VERIFICATION.md`:
   - Whether `GET /markets/{ticker}/orderbook` on Kalshi works unauthenticated in production (quick-start says yes, API reference shows auth headers).
   - Whether Polymarket exposes a usable orderbook-history endpoint (exact path, params, retention, schema) — if not, state clearly that book-level backtesting requires prospectively persisted snapshots.
   - Actual Polymarket per-market fee metadata via `getClobMarketInfo(conditionID)` / `feesEnabled`, not category defaults alone.
3. **If docs conflict, do not guess.** Document the discrepancy and code behind a config flag or adapter abstraction.
4. **Never overstate profitability.** Every opportunity must be evaluated on depth-adjusted, fee-complete, slippage-stressed net edge — never top-of-book quotes.
5. **Jurisdiction-aware by design.** Polymarket's main API blocks U.S. order placement. Ship `discovery-only` mode as the default; `execution-enabled` mode must be gated behind an explicit config flag AND a runtime geoblock/eligibility check before any Polymarket order path. Hard-disable execution if blocked.

## Project setup (Phase 0)

- Initialize the repo: `pyproject.toml` (Python 3.12+), `uv` or `pip` workflow, `pytest` + `pytest-asyncio`, `ruff`, `mypy --strict`-clean code, pre-commit config.
- Create `CLAUDE.md` documenting: module map, coding conventions, how to run tests, how to run the dry-run CLI, and the rule "fee functions are pure, tested, and cite their source in the docstring."
- Create `.env.example` with every secret/credential placeholder. Never hardcode keys; mask secrets in logs.
- Complete the doc-fetching and live-call verification described above. **Do not proceed to Phase 1 until `docs/VERIFICATION.md` exists.**

## Architecture

Async-first. Clean separation of concerns with dependency-injected venue adapters:

```
arb_scanner/
  app/
    config.py            # pydantic-settings; all thresholds/modes configurable
    types.py             # shared models (Money, Side, Quote, BookLevel, Opportunity, ...)
    clients/             # kalshi_rest, kalshi_ws, polymarket_gamma, polymarket_clob,
                         # polymarket_ws, polymarket_bridge, geoblock
    fees/                # kalshi.py, polymarket.py, bridge.py, slippage.py, profit.py
    markets/             # discovery.py, parsers.py, matching.py, rule_equivalence.py
    books/               # kalshi_book.py, polymarket_book.py, depth.py, snapshots.py
    execution/           # simulator.py, router.py, orders.py (disabled by default)
    risk/                # controls.py, exposure.py, kill switch
    storage/             # SQLAlchemy, SQLite default, Postgres-swappable
    alerts/              # discord.py, telegram.py, email.py
    backtest/            # datasets.py, fills.py, replay.py, metrics.py
    main.py              # CLI: scan / dry-run / replay / report
  tests/{unit,integration,fixtures}
  docs/VERIFICATION.md
```

Libraries: `aiohttp`, `websockets`, `pydantic`, `SQLAlchemy`, `rapidfuzz` (difflib fallback), `tenacity`, `pandas`/`numpy` for backtest analytics, `plotly` or `matplotlib` for reports. Prefer official SDKs/REST/WS over third-party wrappers; do not use ccxt.

## Phase 1 — Fee engine (build and test FIRST, in isolation)

Pure functions, exhaustive unit tests, docstrings containing formula + source URL + worked example with current official rates.

Kalshi:
- General taker fee: `ceil_to_cent(0.07 × C × P × (1 − P))`, charged only on immediately-matched orders.
- Maker fee where applicable: `ceil_to_cent(0.0175 × C × P × (1 − P))`; no fee on canceled resting orders.
- Support per-product fee-schedule overrides (e.g., reduced S&P 500 / Nasdaq-100 schedules) via a versioned override table loaded at runtime.
- Implement BOTH precision models: (a) coarse cents-based schedule model, (b) fill-exact model per API docs — round trade fee up to nearest $0.0001, then apply rounding fee/rebate to align balance precision ($0.01 for non-direct members), with the cumulative rounding accumulator. Test both against worked examples.
- Funding costs: ACH/bank free; debit deposit up to 2% processing; crypto processor fees configurable; withdrawal holds modeled as capital-lock time, not dollar cost.

Polymarket:
- Taker-only fee: `C × feeRate × p × (1 − p)`, makers pay zero. Fee rate resolved per market at runtime (current category defaults: Crypto 0.07, Sports 0.03, Finance/Politics/Mentions/Tech 0.04, Economics/Culture/Weather/Other 0.05, Geopolitics 0). Round to 5 decimals, minimum fee 0.00001.
- Rebates (maker daily rebates; taker rebate program live 2026-05-28) modeled as OPTIONAL, user-specific, non-baseline offsets — off by default, never counted in the headline edge.
- Bridge/deposit costs: query the live bridge quote endpoint (`gasUsd`, `appFeePercent`, `appFeeUsd`, `fillCostPercent`, `fillCostUsd`, `maxSlippage`, `swapImpact`, `totalImpact`). Never hardcode bridge costs.
- Gas: configurable by wallet mode — gasless relayer / deposit-wallet (POLY_1271, default for new API users) vs. EOA (pays gas on approvals, splits, merges, execution).

Acceptance criteria: 100% of fee functions unit-tested, including rounding edge cases (P near 0/1, size 1, large sizes), both Kalshi precision models, and minimum-fee floors. `pytest` green before Phase 2.

## Phase 2 — Venue clients

Kalshi:
- REST market discovery + orderbook; WS book updates (RSA-PSS signing: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`).
- Correct binary-book normalization: Kalshi exposes only YES and NO bid ladders; synthesize asks from complementary bids (YES bid at X ≡ NO ask at 1 − X). Unit-test this transformation thoroughly — it is the most common implementation bug.
- Token-bucket rate-limit client (separate read/write buckets; Basic 200r/100w per second; no Retry-After on 429, so client-side backoff with jitter is mandatory).

Polymarket:
- Gamma for discovery; CLOB for books/prices (public reads); public market WS (level-2 book, `price_change`, `best_bid_ask`, `last_trade_price`, `market_resolved`).
- L1 (EIP-712 private key) + L2 (HMAC API creds) auth scaffolding for the execution path, behind the eligibility gate.
- Respect documented limits (≈9,000 req/10s general, /book 1,500/10s) with sliding-window awareness.

Shared: tenacity retries with exponential backoff + jitter, circuit breaker per venue, structured logging with correlation IDs, explicit handling of 400/401/404/425/429/5xx, deterministic client order IDs where supported.

Acceptance: integration tests with mocked HTTP/WS fixtures pass; a live smoke test against public read endpoints succeeds and its output is committed to `tests/fixtures/`.

## Phase 3 — Market matching and rule equivalence

Staged pipeline:
1. Normalize titles (lowercase, strip noise, normalize dates/teams/tickers/strikes/names).
2. Parse structured features (event time, determination time, strike/line, entity, category).
3. Similarity: exact structured match → RapidFuzz score → token overlap.
4. **Rule-equivalence validation (mandatory):** resolution source, end date vs. determination time, void/DNP/50-50/fair-value/dispute handling, UMA challenge windows on Polymarket, sports early-start behavior (Polymarket auto-cancels sports limit orders at game start but may miss early starts; Kalshi may keep trading past close while awaiting official confirmation).
5. Confidence score; statuses: accepted / rejected / manual_review. Anything below threshold never reaches the economics engine.

Output per pair: kalshi ticker, polymarket condition_id + token_ids, confidence, matched fields, differing fields, status. Persist all of it.

Acceptance: unit tests covering true matches, near-miss traps (same title, different determination time; same game, different void rules), and sports edge cases.

## Phase 4 — Economics engine

For both directions (Kalshi YES + Poly NO; Kalshi NO + Poly YES):
- Aggregate fillable depth per level; compute VWAP for candidate size on each leg; flag partial-fill risk.
- Net P&L:
```
gross = size × (1 − leg1_price_vwap − leg2_price_vwap)
net   = gross − kalshi_fee − polymarket_fee − bridge − withdrawal
        − processor − conversion − gas − expected_slippage − latency_miss
```
- Capital efficiency:
```
locked = size×p1 + size×p2 + fee_buffer
simple_return = net / locked
annualized = simple_return × (365 / hold_days)   # cross-venue lock; no collateral netting
```
- Compute break-even slippage and break-even extra fees per opportunity.
- Slippage models, configurable: fixed ¢/share, % of quoted edge, depth-derived impact.
- Alert ONLY on depth-adjusted net edge passing all risk controls: max exposure (total/venue/trade), min net $ and ROI and annualized ROI, min match confidence, min fill probability, max time-to-resolution, max quote age, category/geo allowlists, kill switch, dry-run mode.
- Persist every evaluated opportunity with full book snapshot, fee breakdown by component, assumptions, and (when known) realized outcome.

Sanity check against these reference scenarios (recompute with your verified rates; flag any disagreement):
- Kalshi YES 0.90 / Poly NO 0.03 (4% cat), 100 sh → gross $7.00, net ≈ $6.25.
- Kalshi YES 0.61 / Poly NO 0.36 (3% sports), 1,000 sh → gross $30.00, net ≈ $6.43; goes negative at ~1¢ total adverse execution.
- Kalshi YES 0.52 / Poly NO 0.46 (4% cat), 10,000 sh → net ≈ −$74 before slippage.

## Phase 5 — Alerts, persistence, CLI

- Discord, Telegram, email alert adapters with a common payload: pair, confidence, depth summary, fee breakdown, net edge, annualized return, break-evens, snapshot ID.
- CLI: `scan` (live discovery loop), `dry-run` (one pass, verbose console output), `replay`, `report`.
- Dockerfile + docker-compose; README covering setup, env vars, keys, tests, VPS deployment, limitations, compliance notes.

## Phase 6 — Backtest and simulation

- Snapshot persistence from live scanning (this is the primary historical book source unless Phase 0 verified an official archive).
- Fill simulator: executes against stored depth, models partial fills, stale quotes, latency (ms), configurable slippage, fee drift via versioned fee metadata.
- Metrics: hit rate, avg gross vs. net edge, realized vs. estimated slippage, capital utilization, time-weighted returns, rejection-reason histogram. Charts for net-profit sensitivity to fees/slippage/liquidity.

## Final deliverables checklist

- All tests green (`pytest`, `mypy`, `ruff`) — run them yourself and show output.
- `docs/VERIFICATION.md` with every doc URL, date, verified value, and discrepancy found.
- Example dry-run log: matched pair → depth summary → fee components → net edge → alert or rejection reason.
- Real-money deployment checklist clearly labeling what was validated live vs. simulated.
- Execution adapters present but disabled; discovery/alert-only is the shipped default.
- An honest LIMITATIONS section. Do not claim profitability; claim measurement.