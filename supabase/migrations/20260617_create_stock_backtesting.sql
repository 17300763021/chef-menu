-- Lightweight backtesting tables for the stock assistant.
-- These are research and virtual simulation records only; no broker integration.

create table if not exists public.stock_backtest_runs (
  id uuid primary key default gen_random_uuid(),
  run_time timestamptz not null default now(),
  strategy_name text not null default 'strong_pick_v1',
  start_date date not null,
  end_date date not null,
  initial_cash numeric not null default 1000000,
  final_value numeric not null default 1000000,
  total_return_rate numeric not null default 0,
  max_drawdown_rate numeric not null default 0,
  win_rate numeric not null default 0,
  profit_loss_ratio numeric not null default 0,
  trade_count integer not null default 0,
  avg_holding_days numeric not null default 0,
  missed_runner_count integer not null default 0,
  note text not null default ''
);

create table if not exists public.stock_backtest_trades (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.stock_backtest_runs(id) on delete cascade,
  code text not null,
  name text not null,
  entry_date date not null,
  exit_date date not null,
  entry_price numeric not null default 0,
  exit_price numeric not null default 0,
  shares integer not null default 0,
  pnl_amount numeric not null default 0,
  pnl_rate numeric not null default 0,
  holding_days integer not null default 0,
  exit_reason text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists public.stock_missed_runners (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.stock_backtest_runs(id) on delete cascade,
  pick_date date not null,
  code text not null,
  name text not null,
  pick_price numeric not null default 0,
  max_price numeric not null default 0,
  max_return_rate numeric not null default 0,
  days_to_high integer not null default 0,
  reason text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists stock_backtest_runs_time_idx
  on public.stock_backtest_runs(run_time desc);
create index if not exists stock_backtest_trades_run_idx
  on public.stock_backtest_trades(run_id, entry_date desc);
create index if not exists stock_missed_runners_run_idx
  on public.stock_missed_runners(run_id, max_return_rate desc);

alter table public.stock_backtest_runs enable row level security;
alter table public.stock_backtest_trades enable row level security;
alter table public.stock_missed_runners enable row level security;

grant select, insert, update, delete on public.stock_backtest_runs,
  public.stock_backtest_trades, public.stock_missed_runners to authenticated;

drop policy if exists "admin manage stock backtest runs" on public.stock_backtest_runs;
create policy "admin manage stock backtest runs"
on public.stock_backtest_runs for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock backtest trades" on public.stock_backtest_trades;
create policy "admin manage stock backtest trades"
on public.stock_backtest_trades for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock missed runners" on public.stock_missed_runners;
create policy "admin manage stock missed runners"
on public.stock_missed_runners for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
