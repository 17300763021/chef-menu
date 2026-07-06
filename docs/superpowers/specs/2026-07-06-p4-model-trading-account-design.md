# P4 Model Trading Account Design

## Goal

Build P4 as a model-driven simulation trading account. The model can create virtual buy, hold, reduce, sell, and blocked decisions, but it must not connect to a broker or place real-money orders.

## Product Boundary

The platform remains simulation-only. Qlib/LightGBM is treated as a research and decision layer, not an investment authority. Every model decision must be auditable by date, model version, input window, score, risk gate, and resulting virtual order state.

## Architecture

1. Model data layer reads the existing `stock_daily_history` cache and builds deterministic features from data available on or before the prediction date.
2. Prediction layer stores daily model scores, ranks, predicted returns, confidence, and train/validation/test windows in `stock_model_predictions`.
3. Decision layer converts predictions into virtual trading intents in `stock_model_decisions`.
4. Execution layer reuses A-share simulation constraints: T+1, 100-share board lots, suspension/limit-up/limit-down blocking, fees, slippage, max position sizing, and cash reserve.
5. Account layer isolates model assets with `strategy_account = model_qlib_lgbm_v1`, so rule-based simulation and model simulation can be compared without contaminating each other.
6. UI layer shows model predictions, model decisions, model orders, and model account snapshots with clear simulation-only language.

## Data Flow

```text
stock_daily_history
  -> deterministic feature builder
  -> stock_model_predictions
  -> model decision engine
  -> stock_model_decisions
  -> stock_auto_trade_orders / stock_positions / stock_trade_history / stock_portfolio_snapshots
  -> dashboard model account view
```

## First Model

The first version is `qlib_lgbm_baseline_v1`. It uses Qlib-compatible feature concepts and a deterministic LightGBM-style scoring baseline. If full Qlib/LightGBM dependencies are unavailable in the runtime, the script still produces reproducible baseline predictions from local cached features. This keeps P4 testable and deployable while leaving the research area ready for real Qlib training.

## Risk Gates

The model decision engine must block or reduce trading when:

- the prediction confidence is below the configured threshold
- the stock is already at or near limit-up for buys
- the stock is at or near limit-down for sells
- the virtual account lacks enough cash for at least one board lot
- max holding count or max single-position allocation would be exceeded
- existing position was bought today and a sell would violate T+1
- model rank deteriorates beyond the sell threshold
- the account drawdown or consecutive loss controls are triggered

## Acceptance Checks

- Re-running predictions for the same date and version produces the same ranks.
- Prediction feature windows do not read future dates after the prediction date.
- Predictions insert into Supabase with model name, version, feature set, score, rank, predicted return, and confidence.
- Model decisions insert into Supabase with action, reason, risk gate status, and linked prediction id.
- Model virtual orders are tagged with `strategy_account = model_qlib_lgbm_v1`.
- Model positions and snapshots do not mix with the existing rule simulation account.
- UI can compare model predictions, decisions, orders, and account snapshots.
- All local Python tests, frontend tests, and production build pass before completion.
