# arb-scanner

Cross-venue arbitrage **scanner** between [Kalshi](https://kalshi.com) and
[Polymarket](https://polymarket.com). Discovery/alert-only by default: it finds
rule-equivalent market pairs, prices both legs on depth-adjusted, fee-complete,
slippage-stressed terms, and alerts when an opportunity clears every risk control.
Execution paths exist in the codebase but are **hard-disabled** behind a config flag
plus a runtime geoblock check, and live order routing is intentionally unimplemented.

> This project claims **measurement, not profitability.** See LIMITATIONS below.

## Setup

```bash
git clone <repo> && cd kalshi-polymarket-arbitrage
uv sync                      # Python 3.12+, https://docs.astral.sh/uv/
cp .env.example .env         # fill in what you need; everything is optional for dry-run
uv run arb-scanner dry-run   # one verbose pass against live public endpoints
```

### Environment variables (all prefixed `ARB_`, see `.env.example`)

| Variable | Purpose |
|---|---|
| `ARB_MODE` | `discovery-only` (default) or `execution-enabled` (gated further by geoblock) |
| `ARB_DATABASE_URL` | SQLite default; any async SQLAlchemy URL (Postgres) works |
| `ARB_KALSHI_API_KEY_ID` / `ARB_KALSHI_PRIVATE_KEY_PATH` | RSA-PSS API key; **not needed for discovery** (market data is public) |
| `ARB_POLYMARKET_*` | L1/L2 credentials; only relevant to the disabled execution path |
| `ARB_DISCORD_WEBHOOK_URL`, `ARB_TELEGRAM_*`, `ARB_SMTP_*` | alert channels; alerts print to console when unset |

Secrets come only from env and are `SecretStr`-masked in logs. Never commit `.env`.

## Commands

```bash
uv run arb-scanner scan          # continuous discovery loop (default 60s interval)
uv run arb-scanner dry-run       # one pass, verbose, console only
uv run arb-scanner replay        # replay persisted snapshots through the fill simulator
uv run arb-scanner report --out reports/report.html   # plotly metrics report

uv run pytest                    # tests (live network tests: uv run pytest -m live)
uv run mypy arb_scanner          # strict type checking
uv run ruff check .              # lint
```

Example dry-run output: `docs/examples/dry-run-example.log` (worked scenario showing
the full trail: matched pair → depth → fee components → net edge → alert/rejection)
and `docs/examples/dry-run-live.log` (a real pass).

## Docker / VPS deployment

```bash
docker compose up -d --build     # scanner + persistent SQLite volume
docker compose logs -f scanner
```

Notes for VPS use:
- The scan loop needs unrestricted egress to `api.elections.kalshi.com`,
  `gamma-api.polymarket.com`, `clob.polymarket.com` (some corporate/institutional
  networks block these — see `docs/VERIFICATION.md` §4).
- Snapshot persistence is the primary source of backtest history; run `scan`
  continuously if you intend to backtest later.
- Kill switch: touch a file and point `risk.KillSwitch(flag_file=...)` at it, or use
  the engaged-by-default behavior in your own wiring, to stop alerts without
  stopping the process.

## Architecture

See `CLAUDE.md` for the module map and `SPEC.md` for the full build specification.
Every fee constant and venue behavior this code relies on is recorded with source
URL, retrieval date, and live-verification result in **`docs/VERIFICATION.md`** —
including nine explicitly flagged discrepancies between documentation sources.

## Real-money deployment checklist

Validated live (2026-06-11, see `docs/VERIFICATION.md`):
- [x] Kalshi public market data + orderbook (unauthenticated, fixed-point format)
- [x] Kalshi per-series fee override feed (`/series/fee_changes`)
- [x] Polymarket Gamma/CLOB public reads, books, prices-history
- [x] Polymarket orderbook-history endpoint (undocumented; do not depend on it)
- [x] Geoblock endpoint semantics (US fully blocked for order placement)
- [x] Fee formulas vs. official worked examples (Kalshi fill-exact: 3/3 doc examples
      reproduced; Polymarket: official SDK test vectors reproduced)

Simulated / NOT validated live — verify before risking money:
- [ ] Re-fetch the Kalshi fee schedule (`kalshi.com/fee-schedule` returned 429 at
      verification time; coefficients corroborated from help center only)
- [ ] Kalshi authenticated trading (order placement, fills, fee debits)
- [ ] Polymarket order placement (geoblocked from the US; execution unimplemented)
- [ ] Bridge quotes under real size (only the doc example was used)
- [ ] Polymarket taker-rebate program details (SPEC date unconfirmed in changelog)
- [ ] Withdrawal hold durations per funding method (modeled as config, not verified)
- [ ] Fill rates, latency, and adverse selection — backtest with YOUR recorded
      snapshots; reference scenario 2 goes negative at ~1¢ adverse execution

## LIMITATIONS

- **No profitability claim.** The scanner measures *apparent* edges on resting
  depth. Scenario 2 in `tests/unit/test_economics.py` shows a $30 gross edge
  reduced to $6.43 net by fees and erased by 1¢ of adverse execution.
- **Cross-venue legging risk is not eliminated**: legs are not atomic; the fill
  simulator models latency and partial fills, but real adverse selection between
  legs can exceed modeled slippage.
- **Rule equivalence is conservative but not exhaustive**: hard mismatches
  (determination time, resolution source, void policy) reject pairs, and UMA
  dispute windows are flagged — yet identical-looking markets can still resolve
  differently. `manual_review` status exists precisely because text cannot prove
  equivalence.
- **US jurisdiction**: Polymarket blocks US order placement; this tool ships
  discovery-only and fails closed. Operating it for execution from a blocked
  jurisdiction would violate venue terms; nothing here circumvents geoblocking.
- **Capital costs dominate**: cross-venue locks (no collateral netting),
  withdrawal holds, and bridge costs are modeled from config/live quotes, but the
  annualized-return figures are only as good as your `hold_days` estimates.
- **Fee drift**: venue fees changed repeatedly during 2026; the override table and
  per-market fee resolution mitigate this, but always re-run Phase-0 verification
  before relying on current numbers.
