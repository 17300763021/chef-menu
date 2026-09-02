---
title: 'Platform Operational Closeout'
type: 'bugfix'
created: '2026-09-02'
status: 'in-review'
review_loop_iteration: 1
baseline_commit: 'cabb753cdeeb6d20235b2b4095e146d909151f76'
context:
  - '{project-root}/AGENTS.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** M2 and M3 have product acceptance evidence, but expected operational blocks still create noisy red workflows, historical live-source checks run on unrelated pushes, M2 backlog publication is not yet closed, M3 is not integrated into `main`, and M4 is blocked by a release-inventory query that does not match the deployed research schema.

**Approach:** Close the existing operational and integration debt without adding product scope: preserve every fail-closed data gate, turn expected quota blocks into audited outcomes, keep live historical acceptance explicit, let the isolated M4 task repair only its schema-contract query, finish bounded M2 catch-up, then integrate the accepted M3/M4 chain cleanly.

## Boundaries & Constraints

**Always:** Preserve simulation-only labels, immutable evidence, source-independence gates, quota stops, atomic publication, explicit release IDs and hashes, and all user-owned untracked files. Keep RQAlpha and Qlib framework ownership unchanged. Treat each accepted M2 daily session as research-only with `simulation_orders_allowed=false`. Finish with the Superpowers closeout disciplines available in this repository: systematic root-cause separation, regression-first verification, verification before completion claims, and a clean branch/merge handoff.

**Ask First:** Any new table or migration, quality-threshold reduction, paid quota use, new data source, alteration of an accepted dataset, force-push, destructive Git operation, or change beyond the files listed below.

**Never:** Make blocked or missing data look valid, suppress genuine data-quality evidence, allow a quota-blocked job to perform work, modify framework core code, activate an account, create orders, or expand M4 strategy/factor scope.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Scheduled runtime allowed | Fresh quotas below thresholds | Heartbeat/recovery completes and report is uploaded | Unexpected infrastructure or RPC defects remain failures |
| Scheduled runtime blocked | Missing/stale quota or hard stop | No protected work runs; structured blocked reason is reported; workflow is not a false infrastructure failure | Preserve fail-closed state and audit evidence |
| Historical code push | M2 acquisition code changes | Deterministic tests run without calling live providers | Test failures remain red |
| Explicit historical acceptance | Manual dispatch | Live sample runs and enforces independent verification | Overlapping/missing sources remain rejected |
| M4 inventory | Existing deployed research schema | Exact accepted component identities are exported without writes | Missing/ambiguous rows or count/hash mismatch fails closed |
| M2 catch-up | Ordered missing sessions and fresh quota below 80% | Bounded batches publish once per session through the latest eligible date | Stop on data failure, stale quota, or 80% threshold |

</frozen-after-approval>

## Code Map

- `scripts/cloud_runtime.py:132-292` -- strict claim/RPC contracts, scheduled quota gating, recovery handling, and structured failure reporting.
- `scripts/test_cloud_runtime.py` -- allowed/blocked/malformed RPC, recovery, CLI report, and workflow contract fixtures.
- `.github/workflows/cloud-runtime.yml:32-77` -- scheduled runtime execution and report artifact publication.
- `.github/workflows/market-data-history-acceptance.yml:31-85` -- push trigger, deterministic tests, and currently automatic live sample.
- `scripts/test_market_data_m2_workflows.py` -- YAML contract tests; add separation between push validation and manual live acquisition.
- `.github/workflows/market-data-daily-incremental.yml` -- read-only during code repair; existing bounded catch-up, quota gate, and atomic publication path.
- `.github/workflows/m4-release-inventory-once.yml` -- owned by the isolated M4 task; remove the nonexistent aggregate column and reconcile failures from stable counts/manifest without changing schema.
- `scripts/market_data/tidb_fundamental_store.py:16-39` -- read-only deployed-table contract proving `failed_symbol_count` is not a physical column.
- `AGENTS.md` -- update only after production-facing evidence, with M2 maintenance outcome, M3 integration truth, M4 status, and remaining limitations.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/cloud_runtime.py`, `scripts/test_cloud_runtime.py`, `.github/workflows/cloud-runtime.yml` -- represent expected quota denial as a structured no-work outcome while retaining real failures and artifacts; strict RPC contracts prevent malformed responses from being treated as success.
- [x] `.github/workflows/market-data-history-acceptance.yml`, `scripts/test_market_data_m2_workflows.py` -- keep deterministic push coverage and require explicit dispatch for live acquisition; the workflow's deterministic job runs its own trigger-isolation test.
- [ ] M2 cloud workflow -- monitor the current bounded run and continue only accepted missing sessions while fresh quota remains below 80%.
- [ ] Isolated M4 task -- validate and repair the exact inventory query, then run inventory, full-release binding, idempotency, and final Qlib acceptance.
- [ ] Git integration -- land the operational repair, accepted M3 lineage, and completed-or-clearly-blocked M4 state without force-push or touching untracked files.
- [ ] Final closeout review -- apply the repository's Superpowers-style debugging, verification-before-completion, and branch-finishing checks; record unresolved external/data blocks instead of hiding them.
- [ ] `AGENTS.md` -- record only verified online evidence and the accurate next roadmap item.

**Acceptance Criteria:**
- Given an expected quota denial, when the scheduled foundation heartbeat runs, then no protected work occurs, the exact reason is stored in its report, and the workflow does not masquerade as a software crash.
- Given an M2 code push, when CI runs, then deterministic contracts execute and no live historical acquisition starts automatically.
- Given an explicit historical acceptance, when sources overlap, then the quality gate still rejects the evidence.
- Given each eligible missing session, when catch-up completes, then the 800-symbol scope reconciles, retry is idempotent, and no authoritative or simulated-order permission is created.
- Given the deployed fundamental schema, when M4 inventory runs, then all six components resolve exactly once and the 1,403-symbol accepted research release reconciles without database writes.
- Given accepted M3 evidence, when integration completes, then M3 remains `Completed`, regressions pass, and M4—not M2/M3 feature work—is the only active product milestone until its gate closes.

## Spec Change Log

- 2026-09-02: Completed the local operational repair. Expected quota denials now return audited `blocked` outcomes with exact reasons and no protected work, unexpected runtime defects still return a failing process status with a report artifact path, and historical live acquisition is manual-dispatch-only while push coverage remains deterministic. Remote M2 publication, isolated M4 repair/acceptance, branch integration, final online closeout, and `AGENTS.md` remain incomplete because this implementation handoff prohibited remote operations and production claims without evidence.

## Design Notes

Expected business-policy blocks and unexpected software failures are separate states. A quota denial is successful enforcement only when it performs no protected work and emits durable evidence; connection errors, malformed RPC responses, or reconciliation defects remain failing runs. Historical source independence is never weakened—only the timing of live acceptance moves from incidental push execution to deliberate dispatch.

## Verification

**Commands:**
- `python -m scripts.test_cloud_runtime` -- expected: blocked and allowed runtime paths pass.
- `python -m scripts.test_market_data_m2_workflows` -- expected: workflow YAML parses, actions remain pinned, push is deterministic-only, and manual live acceptance remains available.
- `python -m scripts.test_historical_quality_gates` -- expected: source-overlap evidence is still rejected.
- `python -m scripts.test_market_data_daily_incremental` -- expected: daily target-boundary, idempotency, and fail-closed regressions pass.
- `git diff --check` -- expected: no whitespace errors.
- Cloud acceptance -- expected: structured quota block, bounded M2 publication, M4 exact inventory, release binding, idempotent replay, and Qlib evidence match this specification.

**Local evidence (2026-09-02):**

- `python -m scripts.test_cloud_runtime` -- 19 tests passed, covering allowed scheduling, expected missing/hard-stop quota blocks, recovery retry preservation, strict RPC response validation, zero protected work, CLI failure reports, and unexpected RPC failure propagation.
- `python -m scripts.test_market_data_m2_workflows` -- 5 tests passed; parsed workflow contracts prove push runs deterministic tests only, the guard test is in the push job, and live historical sample acquisition requires explicit dispatch.
- `python -m scripts.test_historical_quality_gates` -- 5 tests passed, retaining fail-closed source-overlap and historical quality behavior.
- `python -m scripts.test_market_data_daily_incremental` -- 33 tests passed in an isolated Python 3.11 environment with pinned market-data dependencies plus Windows timezone data.
- `python -m py_compile scripts/cloud_runtime.py scripts/test_cloud_runtime.py scripts/test_market_data_m2_workflows.py` and `git diff --check` passed.

**Review disposition (2026-09-02):** Three independent review lenses were deduplicated. The implementation patched strict boolean/status validation, avoided claiming recovery work when the scheduled quota gate is blocked, verified terminal RPC responses, preserved artifact upload on runtime failure, and added push-trigger isolation coverage. No new table, migration, source, threshold, or paid capability was introduced.

**Incomplete production evidence:**

- M2 bounded catch-up was not monitored or dispatched; no fresh quota attestation or 800-symbol session reconciliation was produced locally.
- The isolated M4 branch still needs its deployed-schema inventory query repaired and all inventory, release-binding, idempotency, and Qlib acceptance evidence rerun.
- M3/M4 branch integration was not performed, so the current branch does not yet establish integrated milestone status.
- `AGENTS.md` was not updated because production-facing closeout evidence is incomplete.
