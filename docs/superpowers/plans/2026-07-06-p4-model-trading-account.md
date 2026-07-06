# P4 Model Trading Account Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a model-driven virtual trading account for P4 that generates predictions, decisions, simulated orders, account snapshots, and dashboard visibility.

**Architecture:** Add model prediction and decision tables, then build deterministic model research scripts on top of `stock_daily_history`. The model execution script writes only virtual account data tagged with `strategy_account = model_qlib_lgbm_v1` and reuses the existing simulated trading constraints.

**Tech Stack:** Python standard library, pandas where available through existing stock engine dependencies, Supabase REST, PostgreSQL migrations, React/TypeScript/Vitest.

---

### Task 1: Database Schema

**Files:**
- Create: `supabase/migrations/20260706_add_stock_model_trading.sql`

- [ ] Add `stock_model_predictions` for daily model score/rank records.
- [ ] Add `stock_model_decisions` for model action and risk-gate audit records.
- [ ] Add strategy/model metadata columns to existing virtual order, position, trade, and snapshot tables.
- [ ] Add indexes by prediction date, strategy account, and model version.

### Task 2: Model Prediction Script

**Files:**
- Create: `scripts/model_prediction_engine.py`
- Create: `scripts/test_model_prediction_engine.py`

- [ ] Write deterministic tests for feature-window boundaries, ranking reproducibility, and Supabase payload shape.
- [ ] Implement cached history loading from `stock_daily_history`.
- [ ] Implement baseline Qlib-compatible features and scoring.
- [ ] Insert predictions with model metadata and train/validation/test windows.

### Task 3: Model Simulation Engine

**Files:**
- Create: `scripts/model_trade_engine.py`
- Create: `scripts/test_model_trade_engine.py`

- [ ] Write tests for buy, sell, hold, blocked low confidence, blocked lot size, and T+1 sell protection.
- [ ] Implement isolated model account calculations by `strategy_account`.
- [ ] Convert predictions into audited model decisions.
- [ ] Write virtual orders, positions, trade history, and model account snapshots.

### Task 4: Frontend Repository Mapping

**Files:**
- Modify: `src/features/stocks/types.ts`
- Modify: `src/features/stocks/repository.ts`
- Modify: `src/features/stocks/repository.test.ts`

- [ ] Add model prediction and decision TypeScript types.
- [ ] Map Supabase rows for predictions, decisions, strategy account fields, and model metadata.
- [ ] Add repository methods for model predictions and model decisions.
- [ ] Add tests for model row mapping and model account isolation fields.

### Task 5: Dashboard Model Account View

**Files:**
- Modify: `src/features/stocks/StockDashboard.tsx`
- Modify: `src/features/stocks/stocks.css`

- [ ] Add a primary model account tab.
- [ ] Display latest model account snapshot, predictions, decisions, and model orders.
- [ ] Keep model wording simulation-only.
- [ ] Avoid changing unrelated dashboard behavior.

### Task 6: Verification And Online Closure

**Files:**
- Modify: `AGENTS.md`

- [ ] Run Python test group including new model tests.
- [ ] Run `npm test`.
- [ ] Run `npm run build`.
- [ ] Apply Supabase migration online.
- [ ] Run prediction and model trade scripts against Supabase.
- [ ] Verify online prediction, decision, order, snapshot, and account isolation rows.
- [ ] Update P4 roadmap status and completion note.
