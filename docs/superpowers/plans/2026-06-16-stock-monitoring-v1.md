# Stock Monitoring V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first online version of automatic watch monitoring, signal events, one-click position actions, T-action recording, and historical strong-pick review.

**Architecture:** Keep Python as the strategy producer and Supabase as the event store. The React page reads signal/history tables and performs user-confirmed actions; no broker account or automatic order execution is added.

**Tech Stack:** TypeScript, React, Vitest, Python CSV sync script, Supabase migrations, GitHub Actions.

---

**Delivery Note:** For this project, after completing and verifying website changes, Codex should commit and push the update instead of asking the user to push manually.

### Task 1: Signal Event Persistence

**Files:**
- Create: `supabase/migrations/20260616_create_stock_signal_events.sql`
- Modify: `scripts/sync_stock_data.py`
- Test manually by importing `signal_event_row` in Python.

- [x] Add `stock_signal_events` migration with admin RLS.
- [x] Map live decision CSV rows to signal events.
- [x] Insert signal events during sync without deleting history.

### Task 2: Online Holding Watchlist

**Files:**
- Modify: `scripts/run_stock_tasks.py`

- [x] Fetch open Supabase positions before live decision.
- [x] Write a temporary holdings CSV for the Python live-decision script.
- [x] Include that holdings CSV in `a_stock_live_decision_v8.py`.

### Task 3: Frontend Signal Center

**Files:**
- Modify: `src/features/stocks/types.ts`
- Modify: `src/features/stocks/repository.ts`
- Modify: `src/features/stocks/repository.test.ts`
- Modify: `src/features/stocks/StockDashboard.tsx`
- Modify: `src/features/stocks/stocks.css`

- [x] Add signal and historical strong-pick types.
- [x] Add repository methods for signal events, event updates, one-click buy/sell/T, and historical picks.
- [x] Add Vitest coverage for new repository behavior.
- [x] Add Signal Center and Historical Picks tabs.

### Task 4: Verification

**Files:**
- `package.json`
- `scripts/sync_stock_data.py`

- [x] Run focused tests for stocks repository.
- [x] Run full test suite.
- [x] Run production build.
- [x] Report SQL/user steps needed after deployment.
