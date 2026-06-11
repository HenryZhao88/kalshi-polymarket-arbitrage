# arb-scanner

Kalshi-Polymarket arbitrage discovery prototype. It reads public market data,
matches candidate markets, evaluates both buy/buy directions against book depth,
applies fees and configured costs, persists evidence, and can emit alerts.

This project does not place orders and makes no profitability claim. Live order
routing ends in `NotImplementedError`; discovery-only is the shipped default.

## Current status

Implemented and tested:

- Paginated Kalshi discovery with `mve_filter=exclude` and repeated-cursor protection.
- Bounded Gamma keyset pagination plus public Kalshi/CLOB order-book reads.
- Normalized Polymarket question, condition/token IDs, outcomes, dates, rules text,
  category, status, liquidity/volume, and fee metadata when Gamma supplies them.
- Structured market-type parsing for election, nomination/control, margin/vote-share,
  appointments, sports, crypto/index thresholds, and weather threshold contracts.
- Conservative Kalshi ticker inference for obvious date/year, family, state, office,
  and strike fields. Ticker evidence is diagnostic/conflict-only and cannot accept a pair.
- Candidate-funnel diagnostics and bounded persistence of rejected and manual-review pairs.
- Kalshi binary-book normalization and depth-adjusted VWAP.
- Per-market Polymarket `feeSchedule` parsing from Gamma or CLOB market metadata.
- Explicit economics fields for Kalshi fee, Polymarket fee, bridge, withdrawal, gas,
  processor, conversion, slippage, and unknown-cost buffer.
- Fail-closed handling for unknown fee metadata, costs, hold time, quote age, and rules.
- Requested versus executable size and minimum-fill enforcement.
- Scan-session exposure tracking and a file kill switch.
- SQLite/Postgres persistence of candidate pairs, books, evaluations, rejection reasons,
  assumptions, and alert payloads.
- Paired-snapshot economics replay. It does not claim realized P&L or historical fills.
- Discord, Telegram, and email sinks. Dry-run is console-only unless explicitly enabled.

Incomplete or not live-wired:

- Market matching is heuristic. Missing void, determination, source, or resolution text
  produces `manual_review`, not an accepted pair.
- Text/rules parsing is pattern-based, not a complete legal interpretation of either
  venue's contract. Ticker inference is never sufficient acceptance evidence.
- Kalshi per-series fee overrides are implemented in isolation but not applied by the
  scanner. Scans currently record the general conservative fee schedule.
- Bridge quotes and withdrawal, gas, processor, and conversion costs are not fetched by
  the scan loop. They must be configured or candidates are rejected by default.
- Exposure survives a continuous scan process but is not restored after process restart.
- Gamma pagination is bounded by configured page and market limits. It does not
  guarantee that every active Polymarket market is fetched in one pass.
- Replay re-evaluates paired snapshots; it does not model later two-leg fills or realized
  outcomes because the scanner does not yet record a time series for each opportunity.
- No live order placement exists.

## Setup

Run commands from the repository root:

```bash
uv sync
cp .env.example .env
uv run pytest
uv run ruff check .
uv run mypy .
```

One safe public-data pass:

```bash
uv run arb-scanner dry-run
```

By default this persists scan evidence to `arb_scanner.db`, sends no external alerts,
and rejects candidates whose required costs are unset. To inspect discovery while costs
remain unknown, use this testing-only override:

```bash
ARB_ALLOW_UNKNOWN_COSTS=true uv run arb-scanner dry-run
```

That override does not make the economics complete or suitable for trading.

## Configuration

All variables use the `ARB_` prefix. See `.env.example` for the complete list.

| Variable | Default | Meaning |
|---|---:|---|
| `ARB_MODE` | `discovery-only` | Execution is still impossible even if changed. |
| `ARB_DATABASE_URL` | SQLite | Async SQLAlchemy database URL. |
| `ARB_PERSIST_SCANS` | `true` | Persist pairs, snapshots, and evaluations. |
| `ARB_PERSIST_RAW_CANDIDATES` | `false` | Persist conflict-free low-similarity raw pairs. |
| `ARB_STORAGE_RETENTION_DAYS` | `30` | Remove older persisted pairs, books, and evaluations. |
| `ARB_STORAGE_MAX_CANDIDATES_PER_SCAN` | `5000` | Cap persisted rejected candidates; manual/accepted rows are retained. |
| `ARB_POLYMARKET_MAX_MARKETS` | `500` | Maximum unique Gamma markets per scan. |
| `ARB_POLYMARKET_PAGE_SIZE` | `100` | Gamma keyset rows per request; maximum 100. |
| `ARB_POLYMARKET_MAX_PAGES` | `5` | Maximum Gamma pages per scan. |
| `ARB_KILL_SWITCH_FILE` | `.arb-scanner.kill` | Existing file blocks alerts. |
| `ARB_DRY_RUN_SEND_ALERTS` | `false` | Permit configured external sinks during dry-run. |
| `ARB_ALLOW_UNKNOWN_FEES` | `false` | Allow unverified category/unknown fee metadata. |
| `ARB_ALLOW_UNKNOWN_COSTS` | `false` | Allow unset non-trading cost components. |
| `ARB_ALLOW_UNKNOWN_HOLD_TIME` | `false` | Allow missing end/determination timestamps. |
| `ARB_ALLOW_UNKNOWN_QUOTE_AGE` | `false` | Allow books without snapshot timestamps. |
| `ARB_*_COST_DOLLARS` | unset | Operator-supplied per-opportunity cost assumption. |
| `ARB_UNKNOWN_COST_BUFFER_DOLLARS` | `0` | Additional conservative dollar buffer. |

Credentials and webhook values are `SecretStr` settings and are not included in alert
transport errors. Public discovery does not require trading credentials.

## Commands

```bash
uv run arb-scanner scan --interval 60
uv run arb-scanner dry-run
uv run arb-scanner dry-run --show-manual-review 20 --manual-review-sort similarity
uv run arb-scanner dry-run --show-manual-review 20 --manual-review-sort missing_fields
uv run arb-scanner report --latest
uv run arb-scanner report --manual-review --limit 20 --sort missing_fields
uv run arb-scanner report --rejections --limit 20
uv run arb-scanner replay
uv run arb-scanner report --out reports/report.html
```

Every dry-run prints discovered/scannable venue counts and the raw-title,
structured, manual-review, accepted, and rejected candidate stages, including a
rejection-reason histogram. Diagnostic reports read the persisted candidate rows.
Manual-review sorting supports `similarity`, `hypothetical_edge`, `missing_fields`,
`category`, and `event_date`. Hypothetical-edge sorting places uncomputed rows last;
the scanner does not fetch books merely to rank an unsafe pair.

Only `accepted` means the implemented rule checks found affirmative equivalence.
`manual_review` means required facts are missing and is **NOT TRADE SAFE**. `rejected`
means known facts conflict or similarity is insufficient. A displayed hypothetical
edge is diagnostic only; it is not arbitrage, executable economics, or a profit claim.
An `accepted=0` result is valid fail-closed behavior when no candidate has enough
affirmative rule evidence. More matches are not preferable to unsafe matches.

`replay` and `report` require complete paired snapshots produced by a persisted scan.
Legacy isolated order-book rows fail with an actionable message.

## Safety

- `discovery-only` is the default.
- There is no order-placement implementation.
- The router checks mode and Polymarket eligibility, then deliberately raises
  `NotImplementedError`.
- The CLI never invokes the router.
- Manual-review and rejected candidates never reach order-book economics or routing.
- Creating `.arb-scanner.kill` blocks alerts in both scan and dry-run paths.
- US Polymarket order placement is geoblocked; the project does not bypass that control.

## Docker

```bash
docker compose up -d --build
docker compose logs -f scanner
```

Docker runs the same discovery loop with persistent SQLite storage. Configure all
required cost assumptions explicitly before interpreting an evaluation as complete.

See `SPEC.md` for the original target specification and `docs/VERIFICATION.md` for
source/API verification and current implementation caveats.
