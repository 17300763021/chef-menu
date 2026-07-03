-- Professional backtest metrics for simulation research.
-- These fields make risk, cost, and repeatability review auditable.

alter table public.stock_backtest_runs
  add column if not exists annual_return_rate numeric not null default 0,
  add column if not exists sharpe_ratio numeric not null default 0,
  add column if not exists calmar_ratio numeric not null default 0,
  add column if not exists turnover_rate numeric not null default 0,
  add column if not exists consecutive_losses integer not null default 0,
  add column if not exists largest_single_loss numeric not null default 0;

alter table public.stock_backtest_trades
  add column if not exists fee_amount numeric not null default 0,
  add column if not exists slippage_amount numeric not null default 0;
