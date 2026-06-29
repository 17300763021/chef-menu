# P1 Automatic Sell Rule MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the minimum P1 automatic sell-rule loop so paper trading can stage profit-taking, protect strong limit-up holdings, clear trailing-stop breaks, and explain every automatic sell decision.

**Architecture:** Keep the change inside the current paper-trading path. Add small state fields to open positions, compute sell decisions in `paper_trade_engine.py`, and keep the existing UI fed by order/history/signal records.

**Tech Stack:** Python unittest, Supabase SQL migrations, existing React/Supabase UI.

---

## File Structure

- Modify: `scripts/test_paper_trade_execution_status.py`
  Add deterministic fixtures for stop loss, 1R, 2R, strong limit-up skip/update, and trailing stop clear.

- Modify: `scripts/paper_trade_engine.py`
  Add sell-stage constants, trailing-stop helpers, and staged sell decision logic.

- Create: `supabase/migrations/20260629_add_stock_sell_stage_fields.sql`
  Add `sell_stage`, `trailing_stop_price`, and `last_profit_taking_price` to `stock_positions`.

- Modify after verification: `AGENTS.md`
  Mark P1 completed or in progress with a dated note.

## Task 1: Sell Rule Fixtures

- [ ] Add tests that call `sell_reason` with synthetic decisions and positions.
- [ ] Verify tests fail before implementation.
- [ ] Expected cases: stop loss clears all shares, 1R sells half, 2R sells remaining normal stock, strong limit-up sells 0 with trailing-stop update reason, trailing stop clears all shares.

## Task 2: Minimal Sell State

- [ ] Add Supabase migration for sell-stage fields.
- [ ] Add helper accessors that default missing fields to safe values.
- [ ] Keep old rows compatible.

## Task 3: Sell Decision Logic

- [ ] Stop loss wins first and clears all shares.
- [ ] Trailing stop break clears all shares.
- [ ] Strong limit-up returns no sell shares and a Chinese reason to raise/update trailing stop.
- [ ] 1R sells 50% only when stage is `none`.
- [ ] 2R sells remaining shares when stage is `sold_1r` or later.

## Task 4: Execution State Writes

- [ ] Update `sell_position` to write next `sell_stage`, `last_profit_taking_price`, and `trailing_stop_price`.
- [ ] Ensure skipped strong limit-up writes signal execution as `blocked` with Chinese explanation, not silent skip.
- [ ] Keep every real sell writing order, trade history, and linked signal status.

## Task 5: Verification

- [ ] Run Python sell-rule tests.
- [ ] Run `npm test`.
- [ ] Run `npm run build`.
- [ ] Update `AGENTS.md` after checks pass.
