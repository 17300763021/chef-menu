# Stock Account V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-million-yuan virtual account view, professional position sizing rules, account-level profit/loss visibility, and first-step chart visuals.

**Architecture:** Account totals are derived from open positions and closed trade history in the frontend. The strategy remains record-only: it never places broker orders, and signal actions still require user confirmation.

**Tech Stack:** TypeScript, React, Vitest, CSS chart primitives.

---

**Delivery Note:** For this project, after completing and verifying website changes, Codex should commit and push the update instead of asking the user to push manually.

### Task 1: Account Model

**Files:**
- Create: `src/features/stocks/account.ts`
- Create: `src/features/stocks/account.test.ts`

- [x] Add default 1,000,000 capital config.
- [x] Calculate cash, total assets, current floating P/L, realized P/L, total P/L, total return rate, and per-stock allocation.
- [x] Add professional sizing rules: 6-stock limit, 8%-10% first position, 15% single-stock cap, 1% single-trade risk, 25% cash reserve.

### Task 2: Account Overview UI

**Files:**
- Modify: `src/features/stocks/StockDashboard.tsx`
- Modify: `src/features/stocks/stocks.css`

- [x] Add default Account Overview tab.
- [x] Add top-level account cards for initial capital, cash, holding market value, floating P/L, realized P/L, and total return.
- [x] Add holding allocation and P/L contribution charts.
- [x] Add allocation table and account-ratio column in current holdings.

### Task 3: Signal Sizing and Details

**Files:**
- Modify: `src/features/stocks/StockDashboard.tsx`

- [x] Use account sizing rules as the default share suggestion when confirming a buy signal.
- [x] Block new signal buys when sizing rules produce zero allowed shares.
- [x] Add key-price visual bars in the stock drawer for holdings and signal events.

### Task 4: Verification

**Files:**
- `package.json`

- [x] Run focused account tests.
- [x] Run full test suite.
- [x] Run production build.
- [x] Run lint.
- [x] Verify the account overview renders locally in browser.
