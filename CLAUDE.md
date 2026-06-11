# arb-scanner

Cross-venue arbitrage scanner between Kalshi and Polymarket. Discovery/alert-only by
default; execution paths exist but are hard-disabled behind config + runtime geoblock checks.

## Module map

```
arb_scanner/app/
  config.py    pydantic-settings: modes, thresholds, slippage model, risk limits
  types.py     shared frozen models: Money, Side, Quote, BookLevel, OrderBook,
               FeeBreakdown, MatchedPair, Opportunity
  clients/     venue I/O: kalshi_rest, kalshi_ws, polymarket_gamma, polymarket_clob,
               polymarket_ws, polymarket_bridge, geoblock; base.py has retry/
               circuit-breaker/rate-limit primitives
  fees/        PURE fee math: kalshi, polymarket, bridge, overrides, slippage, profit
  markets/     discovery, parsers, matching, rule_equivalence
  books/       kalshi_book (binary normalization), polymarket_book, depth, snapshots
  execution/   simulator, router, orders — DISABLED BY DEFAULT
  risk/        controls, exposure, kill_switch
  storage/     async SQLAlchemy; SQLite default, Postgres via DATABASE_URL
  alerts/      discord, telegram, email; one common payload
  backtest/    datasets, fills, replay, metrics
  main.py      CLI: scan / dry-run / replay / report
```

## Conventions

- Async-first; venue adapters are dependency-injected, never imported deep in business logic.
- Domain models are frozen pydantic models; money is `Money` (integer micro-dollars), never float.
- **Fee functions are pure, tested, and cite their source URL in the docstring** (formula +
  retrieval date + worked example). No fee constant appears outside `fees/` and config.
- Secrets come only from env; logging masks them. Never hardcode keys.
- Anything contradicted between official docs goes behind a config flag, recorded in
  `docs/VERIFICATION.md` — do not guess.

## Commands

- Tests: `uv run pytest` (live network tests are opt-in: `uv run pytest -m live`)
- Types: `uv run mypy arb_scanner`
- Lint: `uv run ruff check .` / format: `uv run ruff format .`
- Dry-run CLI: `uv run arb-scanner dry-run` (one pass, verbose console output)
- Scan loop: `uv run arb-scanner scan`

All three checks (pytest, mypy, ruff) must be green before a phase is considered done.
