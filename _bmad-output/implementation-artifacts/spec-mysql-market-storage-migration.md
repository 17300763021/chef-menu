---
title: 'Migrate Market-Data Persistence from TiDB to MySQL'
type: 'refactor'
created: '2026-09-08'
status: 'done'
baseline_commit: '8a0a8cb77592da76e28d21e4dca271e2e52401d1'
review_loop_iteration: 0
context:
  - 'AGENTS.md'
  - 'docs/luna-server-database-install.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Market-data scripts and seven GitHub Actions workflows still select TiDB through `TIDB_*` configuration although the reconciled 34-table market warehouse is now MySQL. This leaves future writes, resumability, and release reads pointed at the retired storage target.

**Approach:** Replace the runtime storage adapter identity and environment contract with MySQL equivalents, preserve all existing schema, dataset, transaction, idempotency, checkpoint, and research-only behavior, then rewire each persistence workflow to the existing `MYSQL_*` Actions Secrets and an equivalent MySQL capacity gate.

## Boundaries & Constraints

**Always:** Require `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, and `MYSQL_SSL_MODE`; require TLS; preserve table names, primary keys, dataset IDs, manifest hashes, atomic publication, retry bounds, `authoritative=false`, and `simulation_orders_allowed=false`; preserve disabled-by-default scheduled daily ingestion and manual-only acceptance writes; fail closed for missing or stale capacity attestations.

**Ask First:** Start a GitHub Actions run, collect market data, write a new checkpoint/run, change the server's public network exposure or TLS policy, delete TiDB data or credentials, or alter/re-import the 34 migrated MySQL tables.

**Never:** Introduce a TiDB fallback or read `TIDB_*` at runtime; weaken TLS; remove capacity/cost safeguards merely because TiDB Request Units no longer apply; modify strategy, simulation-account, order, fill, or ledger behavior.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Valid MySQL execution | All six `MYSQL_*` values, TLS required, existing accepted dataset | Reuse the existing MySQL tables and preserve idempotent publication/replay semantics | Roll back failed transactions; do not create simulation authority |
| Missing or insecure configuration | A missing MySQL value or a non-required TLS mode | No connection or write is attempted | Raise a configuration error before acquisition/publication |
| Capacity attestation unsafe | MySQL storage percentage at or above a gate, invalid, or stale | Nonessential work stops; only the existing critical daily policy may pass below 100% | Return a clear fail-closed error; never substitute a guessed value |
| Historical resume | A partial MySQL checkpoint exists | Resume only missing/failed symbols using the same deterministic dataset ID | Reject mismatched manifest/hash evidence |

</frozen-after-approval>

## Code Map

- `scripts/market_data/tidb_checkpoint_store.py` -- MySQL-compatible PyMySQL connection, M2.3 schema, checkpoint and manifest publication; replace `TiDBConfig`/`TIDB_*` runtime contract and module identity without changing SQL semantics.
- `scripts/market_data/tidb_daily_store.py`, `tidb_flow_store.py`, `tidb_fundamental_store.py`, `tidb_index_store.py`, `tidb_industry_store.py` -- dependent transactional stores with idempotent publication and retry invariants.
- `scripts/market_data/publish_historical_to_tidb.py`, `historical_bars.py`, `daily_incremental_runner.py`, `daily_catchup_runner.py`, `flow_runner.py`, `fundamental_runner.py`, `index_runner.py`, `industry_runner.py`, `m2_release_gate.py` -- runtime imports, checkpoint CLI names, event text, and storage configuration consumers.
- `scripts/market_data/daily_quota_guard.py`, `industry_quota_guard.py` -- replace TiDB RU/storage terminology with an auditable MySQL-capacity contract; retain 80/90/100 fail-closed behavior and freshness checks.
- `.github/workflows/market-data-{daily-incremental,flow-admission,fundamental-acceptance,history-acceptance,index-acceptance,industry-acceptance,m2-release-acceptance}.yml` -- all seven persistence paths; bind only existing `MYSQL_*` Secrets, rename TiDB checkpoint switches/text, and replace TiDB quota inputs/variables with MySQL storage attestations.
- `scripts/test_tidb_checkpoint_store.py`, `test_tidb_daily_store.py`, `test_tidb_fundamental_store.py`, `test_tidb_industry_store.py`, `test_historical_market_data.py`, `test_market_data_daily_{incremental,catchup,quota}.py`, `test_market_data_{fundamentals,index_bars,verified_flow,industry,m2_workflows,industry_quota}.py` -- storage semantics, runner imports, capacity limits, and exact workflow wiring assertions to migrate with the implementation.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/market_data/*` -- establish one MySQL-named storage adapter family and migrate every production import, CLI option, event name, and configuration lookup to it; preserve existing data and publication semantics.
- [x] `scripts/market_data/{daily_quota_guard.py,industry_quota_guard.py}` and their tests -- implement fresh, manually attested MySQL storage-capacity gates instead of TiDB RU gates, without relaxing stop thresholds.
- [x] `.github/workflows/market-data-*.yml` -- replace all seven persistence workflows' TiDB settings with `MYSQL_*` Secrets and MySQL capacity inputs/variables; retain pinning, concurrency, timeout, manual-write gates, and artifact retention.
- [x] `scripts/test_*.py` -- rename/update affected storage and workflow tests; add coverage proving `TIDB_*` is rejected and missing or insecure MySQL configuration fails closed.
- [x] `docs/luna-server-mysql-install-report.md` -- correct the deployment note to the verified public `13306` TLS route and describe the pending, separately approved cloud acceptance; keep secrets out of documentation.

**Acceptance Criteria:**
- Given valid local `MYSQL_*` TLS settings, when each store's deterministic tests run, then the MySQL adapter preserves published-manifest hashes, idempotent replay, resume behavior, and research-only flags.
- Given each of the seven workflow files, when parsed and inspected, then no persistence job binds a `TIDB_*` Secret or invokes a TiDB-named storage entry point, while manual/scheduled safety gates remain intact.
- Given missing, stale, invalid, or >=80% MySQL capacity attestations, when a nonessential workflow is evaluated, then it fails before acquisition or storage writes; at 100% even critical daily work fails.
- Given the completed source change, when the local test suite and YAML parsing run, then they pass without contacting TiDB or starting a cloud workflow.

## Design Notes

The current SQL is already MySQL 8.4 compatible (`PyMySQL`, `%s` parameters, transactions, `ON DUPLICATE KEY UPDATE`, UTF-8). This migration changes the storage destination contract, not accounting or evidence semantics. MySQL has no TiDB Request Unit API, so the cloud workflows must retain a fresh, manually supplied disk-capacity attestation rather than invent provider telemetry.

## Verification

**Commands:**
- `python -m scripts.test_mysql_checkpoint_store` -- expected: checkpoint, hash, resume, TLS, and missing-configuration cases pass.
- `python -m scripts.test_mysql_daily_store` -- expected: daily retry, revision, and atomic publish cases pass.
- `python -m scripts.test_mysql_fundamental_store` -- expected: checkpoint and aggregate publication cases pass.
- `python -m scripts.test_mysql_industry_store` -- expected: resumable industry publication cases pass.
- `python -m scripts.test_historical_market_data` -- expected: deterministic historical capture/resume behavior passes.
- `python -m scripts.test_market_data_m2_workflows` -- expected: all workflow YAML parses and retains safety constraints.
- `python -m scripts.test_market_data_daily_quota` -- expected: MySQL-capacity thresholds and stale-attestation failures pass.
- `python -m scripts.test_market_data_industry_quota` -- expected: manual nonessential capacity gate fails closed.

## Suggested Review Order

**Storage contract and evidence integrity**

- Start here: one strict MySQL/TLS contract now governs every market-data store.
  [`mysql_checkpoint_store.py:82`](../../scripts/market_data/mysql_checkpoint_store.py#L82)

- Accepted historical datasets replay without mutation and reject hash conflicts.
  [`mysql_checkpoint_store.py:1013`](../../scripts/market_data/mysql_checkpoint_store.py#L1013)

- Late symbol checkpoints cannot rewrite an accepted historical dataset.
  [`mysql_checkpoint_store.py:1096`](../../scripts/market_data/mysql_checkpoint_store.py#L1096)

- Daily publication retains transactional checkpoint and immutable-run semantics.
  [`mysql_daily_store.py:1674`](../../scripts/market_data/mysql_daily_store.py#L1674)

- Capital-flow publication preserves research-only boundaries and manifest replay.
  [`mysql_flow_store.py:74`](../../scripts/market_data/mysql_flow_store.py#L74)

- Index publication remains atomic, accepted-only, and non-authoritative.
  [`mysql_index_store.py:67`](../../scripts/market_data/mysql_index_store.py#L67)

**Runtime and cost safety**

- Daily acquisition now imports only the MySQL storage adapter.
  [`daily_incremental_runner.py:58`](../../scripts/market_data/daily_incremental_runner.py#L58)

- Manual capacity attestations fail closed before nonessential writes.
  [`industry_quota_guard.py:26`](../../scripts/market_data/industry_quota_guard.py#L26)

- Historical capture and resume both depend on the capacity gate.
  [`market-data-history-acceptance.yml:91`](../../.github/workflows/market-data-history-acceptance.yml#L91)

- Final M2 release publication is also capacity-gated before database access.
  [`market-data-m2-release-acceptance.yml:45`](../../.github/workflows/market-data-m2-release-acceptance.yml#L45)

**Verification and deployment evidence**

- Workflow tests enforce MySQL-only wiring and capacity protection across all writers.
  [`test_market_data_m2_workflows.py:52`](../../scripts/test_market_data_m2_workflows.py#L52)

- Historical tests prove accepted-run and late-checkpoint immutability.
  [`test_mysql_checkpoint_store.py:495`](../../scripts/test_mysql_checkpoint_store.py#L495)

- Migration evidence records counts, hashes, TLS route, and research-only flags.
  [`luna-server-mysql-install-report.md:36`](../../docs/luna-server-mysql-install-report.md#L36)
