---
title: 'M2 Daily Calendar Target-Boundary Repair'
type: 'bugfix'
created: '2026-09-01'
status: 'done'
review_loop_iteration: 0
baseline_commit: '779cb7199d0130b8e0ade31baf4a64c322d73eca'
context:
  - '{project-root}/AGENTS.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The scheduled M2 daily increment compares both providers' complete calendars through the observation date before it determines the next missing session. A harmless difference after the backlog target therefore blocks all 800 symbols before capture, even when both providers agree on every session required for the target.

**Approach:** Keep the accepted M2.2 full-history calendar gate unchanged, but give the daily path a target-bounded gate. Discover the next unprocessed session without hiding provider omissions, then require exact dual-source agreement from the historical start through that target before acquisition.

## Boundaries & Constraints

**Always:** Preserve sequential lineage and the 16:30 Asia/Shanghai readiness cutoff; require the target in both calendars; compare every session through the target; use both providers' session union to expose rather than skip a missing session; retain target-bounded calendar hashes and diagnostic evidence for differences after the target; remain cloud-only, non-authoritative, idempotent, and `simulation_orders_allowed=false`.

**Ask First:** Any database/schema or manifest-version change, workflow schedule change, new provider, weakening of historical M2.2 admission, correction/deletion of accepted data, or change that could incur cloud cost.

**Never:** Take the intersection as the candidate calendar; silently skip a provider-missing session; accept a target present in only one source; guess a trading day; change shared `evaluate_calendars`; touch existing untracked files; enable simulated or real orders.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Future-only primary difference | Both sources agree through the backlog target; primary has one later session | Select and process the next sequential target | Emit later difference as diagnostic only |
| Future-only secondary difference | Both sources agree through the backlog target; secondary has one later session | Select and process the next sequential target | Emit later difference as diagnostic only |
| Historical omission | Either source lacks a session on or before the candidate target | Do not acquire symbols or advance lineage | Fail closed with the mismatched date |
| Target absent from one source | Candidate session exists in only one source | Preserve the missing session as the next candidate | Fail closed; never skip to a later common date |
| Not ready or stale | Target is before 16:30 readiness or beyond either source's closed horizon | Do not process the target | Return an explicit readiness/freshness error |
| Replay/weekend | No new mutually confirmed session, or exact accepted target requested | Keep the same target/hash or return immutable replay/no-op | No duplicate result or capture |

</frozen-after-approval>

## Code Map

- `scripts/market_data/daily_incremental.py:152-270` -- owns readiness, previous-session selection, target-scoped hashes, and deterministic plan validation; add/reuse a daily-only bounded calendar validator here.
- `scripts/market_data/daily_incremental_runner.py:797-874` -- currently rejects complete-calendar drift before loading lineage; determine both closed horizons, discover the first candidate from the union, and defer admission to the bounded validator.
- `scripts/market_data/pit_quality_gates.py:14-24` -- accepted strict M2.2 full-history gate; read-only and unchanged.
- `scripts/market_data/historical_bars.py:251-264` -- historical calendar loader used by M2.2/M2.3; continue returning full-calendar diagnostics without weakening shared acceptance.
- `scripts/test_market_data_daily_incremental.py:175-230` -- deterministic readiness and plan tests; cover both future-only directions, historical mismatch, missing target, cutoff, and stable hashes.
- `scripts/test_tidb_daily_store.py:950-1030` -- runner target/replay orchestration fixtures; prove union-based discovery cannot skip a missing session and no acquisition starts on rejection.
- Read-only production evidence: GitHub Actions run `33418452159` loaded 2,103 primary versus 2,102 secondary sessions and failed before data capture; the prior accepted lineage must remain unchanged.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/market_data/daily_incremental.py` -- add target-bounded dual-calendar validation and use it in plan construction while preserving full historical strictness.
- [x] `scripts/market_data/daily_incremental_runner.py` -- select the next session from the bounded dual-source horizon and expose post-target drift diagnostically.
- [x] `scripts/test_market_data_daily_incremental.py` and `scripts/test_tidb_daily_store.py` -- cover every matrix row and verify no symbol acquisition on a bounded-gate failure.
- [ ] `AGENTS.md` -- after production-facing acceptance only, record status, evidence, and remaining M2 work.

**Acceptance Criteria:**
- Given providers agree through the next missing session but differ only afterward, when the daily runner executes, then it advances exactly that one session and retains deterministic dataset identity.
- Given either provider disagrees on or before the next missing session, when the daily runner executes, then it fails before acquisition and reports the relevant date.
- Given a source omits the earliest pending session but contains a later one, when target discovery runs, then the omitted session remains the candidate and cannot be skipped.
- Given the strict historical suites run, when this repair is present, then their full-calendar acceptance behavior remains unchanged.
- Given local verification passes, when cloud acceptance is authorized with a fresh quota attestation, then the 2026-08-12 backlog reuses valid checkpoints and publishes only after all 800 symbols reconcile.

## Spec Change Log

## Design Notes

The daily boundary is not a weaker source contract. The runner uses the union to discover obligations, while admission uses equality through the candidate target. Consequently, a future row published early cannot block an old backlog day, but a missing historical row cannot disappear through intersection filtering. Existing target-scoped calendar hashes remain suitable for repeat-safe checkpoint identity.

## Verification

**Commands:**
- `E:\software\anass\python.exe -m unittest scripts.test_market_data_daily_incremental scripts.test_tidb_daily_store scripts.test_market_calendar scripts.test_historical_market_data` -- all daily and strict historical calendar regressions pass.
- `E:\software\anass\python.exe -m py_compile scripts/market_data/daily_incremental.py scripts/market_data/daily_incremental_runner.py scripts/test_market_data_daily_incremental.py scripts/test_tidb_daily_store.py` -- compilation succeeds.
- `git diff --check` -- no whitespace errors.

**Manual checks (if no CLI):**
- Inspect the cloud log to confirm the selected target, post-target diagnostic dates, checkpoint reuse counts, 800/800 final reconciliation, immutable lineage, `authoritative=false`, and `simulation_orders_allowed=false`.

**Local evidence (2026-09-01):**
- `C:\Users\middol\AppData\Local\Python\bin\python.exe -m unittest scripts.test_market_data_daily_incremental scripts.test_tidb_daily_store scripts.test_market_calendar scripts.test_historical_market_data` -- 102 tests passed, including every I/O matrix row and the unchanged strict historical calendar suite.
- The Python compilation command passed with the same dependency-complete interpreter; `git diff --check` passed.
- The originally named `E:\software\anass\python.exe` interpreter passed the 74 daily/TiDB/calendar tests but lacks installed `akshare` distribution metadata. This environment issue was resolved for verification by using the repository's dependency-complete local interpreter; no test expectation or production code was bypassed.

## Suggested Review Order

**Daily target selection**

- The runner now finds the earliest pending obligation from both providers without silently skipping a missing session.
  [`daily_incremental_runner.py:838`](../../scripts/market_data/daily_incremental_runner.py#L838)

- Both providers must have closed and agreed through the selected target before any acquisition begins.
  [`daily_incremental_runner.py:853`](../../scripts/market_data/daily_incremental_runner.py#L853)

**Boundary validation**

- Target-bounded equality preserves strict historical evidence while allowing harmless later provider drift.
  [`daily_incremental.py:201`](../../scripts/market_data/daily_incremental.py#L201)

- The deterministic plan uses the earlier source horizon and target-scoped hashes for repeat-safe identity.
  [`daily_incremental.py:303`](../../scripts/market_data/daily_incremental.py#L303)

**Regression coverage**

- Future-only drift, historical omission, single-source target, and readiness boundaries are asserted directly.
  [`test_market_data_daily_incremental.py:210`](../../scripts/test_market_data_daily_incremental.py#L210)

- Union discovery is proven to reject a provider-missing target before corporate-action acquisition.
  [`test_tidb_daily_store.py:959`](../../scripts/test_tidb_daily_store.py#L959)
