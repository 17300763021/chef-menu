---
title: 'M2.4 Verified Pure-Cash Dividend Factor Fallback'
type: 'bugfix'
created: '2026-08-28'
status: 'in-progress'
review_loop_iteration: 0
baseline_commit: '52039c8ad0488ea70cb28580ffb799b36f989155'
context:
  - '{project-root}/AGENTS.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The 2026-08-07 daily increment is blocked at 799/800 because `689009` has a real pure-cash dividend but the AKShare/Sina factor endpoint returns no QFQ/HFQ rows. The earlier software defect that discarded its raw bar is fixed; the remaining gap is independently evidenced corporate-action continuity.

**Approach:** Reuse the existing Eastmoney point-in-time corporate-action inventory and Tencent structured cash-dividend evidence to admit only an exactly reconciled pure-cash action when the primary factor endpoint is empty. Record the event as RQAlpha-deferred adjustment lineage, carry forward accepted price factors, and preserve fail-closed behavior for every unsupported or conflicting action.

## Boundaries & Constraints

**Always:** Require an in-universe Eastmoney candidate for the exact target session; require positive, matching cash-per-ten values from Eastmoney and Tencent; require exact previous-session registration and target-session ex-rights dates; require zero/no bonus and conversion ratios; retain source hashes and structured evidence; publish atomically only after all 800 checkpoints succeed; keep `authoritative=false` and `simulation_orders_allowed=false`; preserve idempotent checkpoint reuse.

**Ask First:** Any database migration, manifest/schema-version change, new external provider, support for bonus shares/transfers/rights issues, modification of framework core code, deletion or correction of accepted datasets, or activation of scheduled production ingestion.

**Never:** Hard-code `689009`, its dividend, or any date; infer missing corporate actions from price movement alone; fabricate vendor factors; exclude an active security as “delisted”; weaken the 800/800 gate; modify existing untracked files; authorize orders or imply investment profitability.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Verified pure cash | Eastmoney candidate, missing Sina factors, Tencent dates and cash amount match | Persist cash lineage and deferred event; carry predecessor QFQ/HFQ factors; checkpoint succeeds | Vendor absolute-factor mismatch remains non-critical diagnostic |
| Amount/date conflict | Eastmoney and Tencent disagree | No adjusted bar; raw bar and tradeability evidence remain; checkpoint blocked | Record explicit mismatch error |
| Unsupported action | Bonus, transfer, rights, nonpositive or missing cash | No fallback acceptance | Block without guessing |
| Non-candidate factor gap | No Eastmoney candidate | Preserve existing Tencent no-adjustment continuity path | Block if continuity cannot be proved |
| Replay | Matching successful checkpoint already exists | Reuse deterministically and publish once | No duplicate business result |

</frozen-after-approval>

## Code Map

- `scripts/market_data/daily_incremental_runner.py:287-531` -- `_target_events`, `_factor_reference_closes`, and `capture_symbol`; add the bounded candidate fallback and pass the already-fetched inventory record into capture without changing dataset identity.
- `scripts/market_data/sources/eastmoney_corporate_action_source.py:55-86` -- read-only canonical inventory contract containing cash, bonus, conversion, dates, and plan evidence; reuse rather than add a provider.
- `scripts/market_data/sources/tencent_history_source.py:196-274` -- existing strict pure-cash parser validates exact row, action text, dates, cash amount, positivity, and evidence hash; no change expected.
- `scripts/market_data/daily_adjustments.py:89-175,182-354` -- existing exact-reference path deliberately carries predecessor factors with `rqalpha_deferred_cash_action` when vendor absolute factors are unavailable/incomparable.
- `scripts/test_tidb_daily_store.py:1099-1304` -- capture orchestration fixtures for candidates, Sina factor gaps, Tencent actions, and blocked evidence.
- `scripts/test_market_data_daily_incremental.py:566-661` -- deterministic deferred-factor arithmetic and non-critical vendor-comparability gate.
- Read-only evidence: commit `52039c8ad0488ea70cb28580ffb799b36f989155`; cloud run `33051759373`; TiDB checkpoint for `689009` currently has primary/tradeability evidence but no adjusted row.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/market_data/daily_incremental_runner.py` -- validate Eastmoney/Tencent pure-cash agreement and produce an explicitly deferred adjustment marker only after strict evidence succeeds.
- [x] `scripts/test_tidb_daily_store.py` -- cover matching cash fallback, amount/date disagreement, unsupported action, unchanged non-candidate fallback, evidence retention, and repeat-safe behavior.
- [ ] Cloud acceptance -- push the reviewed commit, rerun only 2026-08-07, then query TiDB before starting later sessions.

**Acceptance Criteria:**
- Given the verified `689009` 2026-08-07 cash dividend, when Sina factors are absent, then the checkpoint succeeds from two-source structured evidence without fabricating QFQ/HFQ factors.
- Given conflicting, incomplete, or non-cash action evidence, when capture runs, then the symbol and day remain blocked with raw evidence preserved.
- Given all 800 checkpoints succeed, when finalization runs, then exactly one immutable daily run is published with 800 primary, adjusted, and tradeability rows, 40 verification rows, `authoritative=0`, and `simulation_orders_allowed=0`.
- Given the same target is rerun, when the accepted dataset exists, then no duplicate business result or mutation is created.

## Spec Change Log

## Design Notes

The fallback is a corporate-action marker, not a substitute vendor factor. It carries the accepted predecessor factors and lets the existing exact cash-reference path label the adjusted row `rqalpha_deferred_cash_action`; RQAlpha remains responsible for applying the cash action later. Eastmoney inventory discovery plus Tencent structured details form the two-source admission contract.

## Verification

**Commands:**
- `E:\software\anass\python.exe -m unittest scripts.test_tidb_daily_store scripts.test_market_data_daily_incremental scripts.test_market_data_daily_catchup scripts.test_market_data_daily_quota` -- all daily orchestration, arithmetic, persistence, catch-up, and quota tests pass.
- `E:\software\anass\python.exe -m py_compile scripts/market_data/daily_incremental_runner.py scripts/test_tidb_daily_store.py` -- compilation succeeds.
- `git diff --check` -- no whitespace errors.

**Manual checks:**
- Inspect run logs and TiDB counts for 2026-08-07; a green workflow alone is insufficient evidence of acceptance.

**Local implementation evidence (2026-08-28):**
- 77 daily orchestration, adjustment, catch-up, persistence, and quota tests passed.
- Python compilation and `git diff --check` passed.
- Cloud acceptance remains intentionally unchecked because the BMad implementation step forbids push and remote operations.
