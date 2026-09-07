---
title: 'M2 Daily Partial Acceptance With Explicit Exclusions'
type: 'bugfix'
created: '2026-09-07'
status: 'done'
review_loop_iteration: 0
baseline_commit: '2fe9773874e2045b5645f696c0e12cf3c64a673d'
context:
  - '{project-root}/AGENTS.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A rare, isolated symbol acquisition failure currently blocks an otherwise usable daily research package and prevents sequential catch-up from advancing, even when the active-universe coverage remains at least 98%.

**Approach:** Admit a daily package with an explicit `accepted_with_exclusions` status when the failed symbols are recorded, the coverage gate remains at least 98%, and every other critical quality gate passes. Keep the missing symbols out of authoritative evidence, preserve their failure records, and retain simulation-only boundaries.

## Boundaries & Constraints

**Always:** Preserve source independence, point-in-time scope, atomic publication, immutable accepted runs, explicit failure audit rows, no fabricated prices or factors, and `simulation_orders_allowed=false`. A package may contain fewer active symbols only when its explicit coverage gate is at least 98%.

**Ask First:** Any change to the 98% threshold, any new data source, any account/order permission, any deletion or mutation of accepted evidence, or any change outside daily quality gates, daily aggregate publication, catch-up progression, and their tests.

**Never:** Treat an unresolved or uncheckpointed symbol as an exclusion, hide a failure, accept below 98% coverage, weaken critical price/adjustment/tradeability gates, or permit simulated orders from a degraded research package.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Isolated failure | One or more explicit failed symbol checkpoints; coverage >=98%; other critical gates pass | Publish immutable `accepted_with_exclusions`, record excluded symbols, and advance catch-up | Keep failure rows and `simulation_orders_allowed=false` |
| Coverage breach | Explicit failures reduce required coverage below 98% | Keep the dataset blocked and stop sequential catch-up | Do not publish an accepted aggregate |
| Unknown gap | A symbol has no successful or explicit failed/blocked checkpoint | Keep the dataset blocked | Never infer an exclusion from missing rows |
| Replay | Same accepted or degraded dataset is requested again | Return idempotent replay without new rows | Reject conflicting accepted content |

</frozen-after-approval>

## Code Map

- `scripts/market_data/daily_quality_gates.py` -- daily tradeability coverage gate and critical quality classification.
- `scripts/market_data/daily_incremental.py` -- deterministic manifest fields and `accepted_with_exclusions` status.
- `scripts/market_data/daily_incremental_runner.py` -- finalization of explicit failed checkpoints and catch-up progression.
- `scripts/market_data/tidb_daily_store.py` -- TiDB aggregate status schema, checkpoint inventory validation, and idempotent publication.
- `scripts/market_data/m2_release_gate.py` -- carry degraded daily status and explicit exclusions into the research release disclosure.
- `scripts/market_data/daily_catchup_runner.py` -- sequential stop/advance behavior based on the aggregate result.
- `scripts/test_market_data_daily_incremental.py` -- coverage and manifest fixtures.
- `scripts/test_market_data_daily_catchup.py` -- blocked versus degraded catch-up behavior.
- `scripts/test_tidb_daily_store.py` -- schema and aggregate publication fixtures.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/market_data/daily_quality_gates.py` -- make the explicit tradeability coverage threshold 98% while keeping all other critical gates fail-closed -- allow only the approved partial case.
- [x] `scripts/market_data/daily_incremental.py` and `scripts/market_data/daily_incremental_runner.py` -- derive explicit exclusions, emit `accepted_with_exclusions`, and allow only checkpointed failures to finalize -- preserve auditability and unknown-gap blocking.
- [x] `scripts/market_data/tidb_daily_store.py` -- persist aggregate acceptance status and validate partial checkpoint inventory atomically and idempotently -- prevent incomplete or conflicting publication.
- [x] `scripts/market_data/m2_release_gate.py` -- preserve degraded-status disclosure for downstream research releases -- never present partial data as complete.
- [x] `scripts/market_data/daily_catchup_runner.py` -- advance after an accepted degraded session but stop on a blocked session -- keep sequential lineage safe.
- [x] `scripts/test_market_data_daily_incremental.py`, `scripts/test_market_data_daily_catchup.py`, and `scripts/test_tidb_daily_store.py` -- cover the matrix and regression paths -- make the behavior executable.

**Acceptance Criteria:**
- Given 1 missing symbol out of 800 with an explicit failure checkpoint, when finalization runs, then the dataset is accepted with `acceptance_status=accepted_with_exclusions`, the exclusion is visible, and no simulated order permission is created.
- Given coverage below 98% or an unknown/uncheckpointed gap, when finalization runs, then the dataset remains blocked and catch-up does not advance.
- Given a repeated accepted or degraded dataset, when publication is retried, then the existing immutable result is returned as an idempotent replay without duplicate rows.

## Verification

**Commands:**
- `python -m unittest scripts.test_market_data_daily_incremental scripts.test_market_data_daily_catchup scripts.test_tidb_daily_store` -- expected: all tests pass.
- `python -m unittest scripts.test_market_data_m2_release` -- expected: release disclosure preserves daily acceptance status.
- `python -m py_compile scripts/market_data/daily_quality_gates.py scripts/market_data/daily_incremental.py scripts/market_data/daily_incremental_runner.py scripts/market_data/tidb_daily_store.py scripts/market_data/daily_catchup_runner.py` -- expected: success.
- `git diff --check` -- expected: no whitespace errors.

## Suggested Review Order

**Acceptance decision and finalization**

- Confirm the single-session policy and explicit exclusion state at the evidence boundary.
  [`daily_incremental.py:395`](../../scripts/market_data/daily_incremental.py#L395)

- Confirm finalization reuses only persisted failed checkpoints and blocks unknown gaps.
  [`daily_incremental_runner.py:772`](../../scripts/market_data/daily_incremental_runner.py#L772)

- Confirm the 98% critical coverage gate remains fail-closed for active research data.
  [`daily_quality_gates.py:166`](../../scripts/market_data/daily_quality_gates.py#L166)

**Atomic publication and disclosure**

- Confirm TiDB schema and checkpoint validation preserve degraded status and audit errors.
  [`tidb_daily_store.py:330`](../../scripts/market_data/tidb_daily_store.py#L330)

- Confirm partial publication validates scope, coverage, checkpoint dates, and idempotent replay.
  [`tidb_daily_store.py:1674`](../../scripts/market_data/tidb_daily_store.py#L1674)

- Confirm downstream M2 release output discloses exclusions instead of implying complete coverage.
  [`m2_release_gate.py:48`](../../scripts/market_data/m2_release_gate.py#L48)

**Verification**

- Confirm the approved partial, unknown-gap, and catch-up progression fixtures.
  [`test_market_data_daily_incremental.py:501`](../../scripts/test_market_data_daily_incremental.py#L501)

- Confirm the TiDB aggregate partial/replay and failure-audit fixtures.
  [`test_tidb_daily_store.py:261`](../../scripts/test_tidb_daily_store.py#L261)
