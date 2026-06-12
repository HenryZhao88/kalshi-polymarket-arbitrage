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
Manual-review sorting supports `similarity`, `confidence`, `hypothetical_edge`,
`missing_fields`, `category`, `event_date`, `market_type`, and `fee_confidence`.
Sorting is stable, tolerates missing values, and applies identically to every
output format. Hypothetical-edge sorting places uncomputed rows last; the
scanner does not fetch books merely to rank an unsafe pair.

### Report formats and exports

Diagnostic reports render as `text` (default), `csv`, or `json`, to stdout or to
a file via `--output`:

```bash
uv run arb-scanner report --manual-review --limit 50 --sort missing_fields --format csv --output manual_review.csv
uv run arb-scanner report --manual-review --limit 50 --sort missing_fields --format json --output manual_review.json
uv run arb-scanner report --rejections --limit 50 --sort similarity --format csv --output rejections.csv
```

CSV headers are stable and append-only. JSON is structured (lists stay lists,
checklist booleans stay booleans) with a top-level NOT TRADE SAFE label and
disclaimer. Exports contain only persisted venue market metadata — never
credentials or settings. Each record carries venue identifiers (Kalshi ticker
and event ticker; Polymarket condition id, token ids, slug, and the public
`polymarket.com/event/<event-slug>` URL when an event slug was captured — no
public Kalshi URL is derivable from a ticker, so Kalshi rows export identifiers
only) plus a diagnostic verification checklist (`needs_determination_time`,
`needs_resolution_source`, `needs_void_policy`, `needs_threshold_confirmation`,
`needs_event_date_confirmation`, `needs_market_type_confirmation`,
`needs_fee_confirmation`, `needs_liquidity_confirmation`). Checklist fields are
research to-dos for a human, **not** acceptance rules — a fully cleared
checklist does not accept a pair or make it tradeable.

### Verification packet

```bash
uv run arb-scanner report --manual-review --limit 10 --sort missing_fields --verification-packet
uv run arb-scanner report --verification-packet --limit 10   # implies --manual-review
```

The packet is a human-readable research worksheet: every row is labeled NOT
TRADE SAFE and lists why the pair matched, why it was not accepted, the exact
unresolved fields blocking acceptance, venue identifiers/URLs, rule-text
excerpts, and an unchecked verification checklist. It is for manual research
only and is never a trade recommendation.

### Live-regression gate

Unit tests cannot prove a detector fires on real venue data. After any
detector change, run a dry-run, save the log, and assert expectations:

```bash
uv run arb-scanner check-log --file /tmp/dryrun.log --expect accepted=0 --expect "continent_scope_conflict>=1"
```

Names resolve against funnel counters, then histogram buckets (absent buckets
count as 0), then raw occurrences in the log (for diagnostic warnings such as
`source_finalization_mismatch`). Nonzero exit on any failed expectation. The
command reads only the local file — no network. See `docs/VERIFICATION.md`
§13.

Manual-review rows in reports and verification packets start with a
`blocking summary` (primary blocker, diagnostic mismatches, unresolved
fields, evidence confidence, next human action); the same fields are exported
to CSV/JSON as `primary_blocker`, `diagnostic_reasons`, `unresolved_fields`,
`next_human_action`, and `evidence_confidence_summary`.

### Storage growth and retention

Persisting every raw candidate would grow the database quickly, so raw
conflict-free low-similarity candidates are not persisted by default
(`ARB_PERSIST_RAW_CANDIDATES=false`), structured rejections are capped per scan
(`ARB_STORAGE_MAX_CANDIDATES_PER_SCAN`), and every scan pass prunes rows older
than `ARB_STORAGE_RETENTION_DAYS`. Manual-review and accepted/rejected summary
rows persist within those bounds. To prune on demand:

```bash
uv run arb-scanner report --cleanup-retention
```

Only `accepted` means the implemented rule checks found affirmative equivalence.
`manual_review` means required facts are missing and is **NOT TRADE SAFE**: the
pair is plausibly the same event, but determination time, resolution source,
void policy, or other rule facts are unverified, so it is a research lead — not
a profitable arbitrage claim. `rejected` means known facts conflict or
similarity is insufficient. A displayed hypothetical edge is diagnostic only;
it is not arbitrage, executable economics, or a profit claim. An `accepted=0`
result is valid fail-closed behavior when no candidate has enough affirmative
rule evidence — it is the correct and safe outcome, not a defect. More matches
are not preferable to unsafe matches.

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

See `SPEC.md` for the original target specification, `docs/VERIFICATION.md` for
source/API verification and current implementation caveats, and `docs/examples/`
for real dry-run, CSV-export, and verification-packet output samples.
