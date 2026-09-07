---
title: 'M2 Skip Isolated Source Failures Without Blocking the Daily Package'
type: 'bugfix'
created: '2026-09-07'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'c7e1e23ac358ae8ada5ad7fe2da28434d056df7d'
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-m2-daily-partial-acceptance.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The cloud daily-ingestion run can finish all symbol shards but remain blocked when a single verification source fails after retries. The runner currently classifies that stock-level source outage as `blocked`, while the approved partial-acceptance policy only permits explicitly failed symbols to be excluded. This prevents a usable session from advancing and does not match the product rule that an isolated returned error is recorded and skipped.

**Approach:** Classify only the final stock-level verification-source failure as `failed`, preserve the complete error in the existing checkpoint and failure-audit rows, and let the existing >=98% explicit-exclusion gate publish `accepted_with_exclusions`. Keep structural data-integrity failures as `blocked`.

## Boundaries & Constraints

**Always:** Do not fabricate bars, factors, prices, or verification rows. Preserve source independence, point-in-time scope, atomic publication, failure auditability, `simulation_orders_allowed=false`, and reuse of successful checkpoints on rerun. Keep missing predecessor state, invalid adjustment continuity, unknown status, calendar/scope errors, and other dataset-level quality failures fail-closed.

**Ask First:** Any change to the 98% threshold, a new source, schema migration, account/order permission, accepted evidence mutation, or behavior outside stock-level failure classification and its tests.

**Never:** Do not hide a failure, convert structural integrity failures to exclusions, create simulated orders, or modify existing untracked files.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|----------------------------|----------------|
| Verification source outage | Primary/adjusted evidence exists; independent verification fails after bounded retries | Persist `failed` checkpoint and failure row; exclude symbol; publish `accepted_with_exclusions` when coverage and other gates pass | No guessed verification data; `simulation_orders_allowed=false` |
| Structural continuity failure | Missing predecessor or invalid adjustment continuity | Persist `blocked` checkpoint; daily package remains blocked | Keep fail-closed behavior and error audit |
| Rerun after partial result | Same dataset id with successful checkpoints already stored | Reuse successful symbols and retry only failed symbols; idempotent final result | Accepted evidence remains immutable |

</frozen-after-approval>

## Code Map

- `scripts/market_data/daily_incremental_runner.py:659-700,1090-1160` -- maps final verification-source errors to checkpoint status and writes retry/failure evidence.
- `scripts/market_data/daily_incremental.py:470-540` -- validates explicit checkpoint inventory, coverage, blocked symbols, and derives `accepted_with_exclusions`.
- `scripts/market_data/tidb_daily_store.py:790-895` -- persists mutable symbol checkpoints and the existing failure-audit row without schema changes.
- `scripts/test_tidb_daily_store.py:1730-1775` -- current capture-symbol tests distinguish structural blocking from verification outage.
- `scripts/test_market_data_daily_incremental.py:495-565` -- pure partial-acceptance and unknown-checkpoint gate fixtures.
- `scripts/test_market_data_daily_catchup.py:28-75` -- catch-up progression behavior for blocked versus partial results.
- `scripts/market_data/daily_catchup_runner.py:85-120` -- finalization advances only after an accepted daily result.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/market_data/daily_incremental_runner.py` -- return `failed` for final independent-verification source outage while retaining `blocked` for structural continuity/integrity errors -- align runtime behavior with the approved partial-acceptance policy.
- [x] `scripts/test_tidb_daily_store.py`, `scripts/test_market_data_daily_incremental.py`, `scripts/test_market_data_daily_catchup.py` -- update and add focused regression fixtures for verification outage exclusion, structural blocking, and catch-up advancement -- prevent recurrence of the production-path classification gap.
- [x] `AGENTS.md` -- record the dated implementation and local/cloud verification evidence after acceptance -- keep roadmap status auditable.

**Acceptance Criteria:**
- Given one stock's independent verification sources fail after bounded retries, when the symbol checkpoint is persisted, then its status is `failed`, its error is present in the failure-audit table, and no fabricated verification row exists.
- Given >=98% stock coverage and no unresolved structural gate, when the daily aggregate finalizes, then it is `accepted_with_exclusions`, lists the failed symbol, advances catch-up, and keeps `simulation_orders_allowed=false`.
- Given missing predecessor state or invalid adjustment continuity, when the symbol is finalized, then it remains `blocked` and the daily package does not publish.
- Given a rerun of the same dataset, when successful checkpoints already exist, then they are reused and the final status/exclusion list is deterministic.

## Verification

**Commands:**
- `uv run --no-cache --with-requirements scripts/market_data/requirements.lock.txt python -m unittest scripts.test_tidb_daily_store scripts.test_market_data_daily_incremental scripts.test_market_data_daily_catchup` -- expected: all focused tests pass.
- `python -m py_compile scripts/market_data/daily_incremental_runner.py` -- expected: success.
- Cloud workflow dispatch for the same missing session -- expected: Success, `accepted_with_exclusions`, explicit failed-symbol record, no simulated orders.

## Suggested Review Order

**Verification failure classification**

- Only explicit provider exhaustion becomes an exclusion; malformed responses remain fail-closed.
  [`daily_incremental_runner.py:672`](../../scripts/market_data/daily_incremental_runner.py#L672)

- Provider adapters preserve unavailable versus integrity outcomes for the runner.
  [`akshare_history_source.py:37`](../../scripts/market_data/sources/akshare_history_source.py#L37)

**Aggregate audit and publication**

- Failed checkpoints retain valid primary evidence while remaining excluded from verification coverage.
  [`tidb_daily_store.py:906`](../../scripts/market_data/tidb_daily_store.py#L906)

- Aggregate publication requires a matching failure-audit row before accepting exclusions.
  [`tidb_daily_store.py:1787`](../../scripts/market_data/tidb_daily_store.py#L1787)

**Regression coverage**

- Structural verification errors and fallback corruption are asserted to remain blocked.
  [`test_tidb_daily_store.py:1816`](../../scripts/test_tidb_daily_store.py#L1816)

- Catch-up advancement after an isolated verification outage is covered in the cloud deterministic suite.
  [`test_market_data_daily_catchup.py:74`](../../scripts/test_market_data_daily_catchup.py#L74)

- The workflow executes the catch-up regression module alongside daily ingestion tests.
  [`market-data-daily-incremental.yml:89`](../../.github/workflows/market-data-daily-incremental.yml#L89)
