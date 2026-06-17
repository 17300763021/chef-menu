-- Backtest credibility v1.1: benchmark comparison and equity curve.

alter table public.stock_backtest_runs
  add column if not exists benchmark_name text not null default 'pick_equal_weight',
  add column if not exists benchmark_return_rate numeric not null default 0,
  add column if not exists excess_return_rate numeric not null default 0;

create table if not exists public.stock_backtest_equity_curve (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.stock_backtest_runs(id) on delete cascade,
  curve_date date not null,
  equity_value numeric not null default 1000000,
  daily_return_rate numeric not null default 0,
  drawdown_rate numeric not null default 0,
  benchmark_value numeric not null default 1000000,
  benchmark_return_rate numeric not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists stock_backtest_equity_curve_run_date_idx
  on public.stock_backtest_equity_curve(run_id, curve_date);

alter table public.stock_backtest_equity_curve enable row level security;

grant select, insert, update, delete on public.stock_backtest_equity_curve to authenticated;

drop policy if exists "admin manage stock backtest equity curve" on public.stock_backtest_equity_curve;
create policy "admin manage stock backtest equity curve"
on public.stock_backtest_equity_curve for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
