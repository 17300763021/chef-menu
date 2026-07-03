-- P3 professional backtest audit fields.
-- Simulation/research only: these fields improve explainability and do not enable broker execution.

alter table public.stock_backtest_runs
  add column if not exists benchmark_csi300_return_rate numeric not null default 0,
  add column if not exists benchmark_csi500_return_rate numeric not null default 0,
  add column if not exists equity_reconciled boolean not null default false,
  add column if not exists sample_split_summary jsonb not null default '{}'::jsonb,
  add column if not exists parameter_sensitivity_summary jsonb not null default '[]'::jsonb;
