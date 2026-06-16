-- Tables for the personal stock strategy assistant.
-- These tables are intentionally admin-only because positions and trade
-- records are personal financial data.

create table if not exists public.stock_scan_results (
  id uuid primary key default gen_random_uuid(),
  scan_date date not null,
  code text not null,
  name text not null,
  score numeric not null default 0,
  prev_close numeric not null default 0,
  signal text not null default '',
  action text not null default '',
  support_level numeric not null default 0,
  resistance_level numeric not null default 0,
  stop_loss numeric not null default 0,
  reason text not null default '',
  risk text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists public.stock_strong_picks (
  id uuid primary key default gen_random_uuid(),
  scan_date date not null,
  code text not null,
  name text not null,
  strategy_level text not null default '',
  review_status text not null default '',
  score numeric not null default 0,
  prev_close numeric not null default 0,
  signal text not null default '',
  action text not null default '',
  support_level numeric not null default 0,
  resistance_level numeric not null default 0,
  stop_loss numeric not null default 0,
  reason text not null default '',
  risk text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists public.stock_live_decisions (
  id uuid primary key default gen_random_uuid(),
  decision_date date not null,
  update_time text not null default '',
  code text not null,
  name text not null,
  operation_type text not null default '',
  current_price numeric not null default 0,
  change_rate numeric not null default 0,
  can_buy boolean not null default false,
  suggest_buy_price numeric,
  suggest_sell_price numeric,
  stop_loss numeric not null default 0,
  target_price_1 numeric,
  final_action text not null default '',
  no_buy_reason text not null default '',
  sell_reason text not null default '',
  status text not null default '不买/无动作',
  updated_at timestamptz not null default now()
);

create table if not exists public.stock_positions (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  name text not null,
  cost_price numeric not null,
  shares integer not null,
  current_price numeric not null,
  market_value numeric not null default 0,
  floating_pnl numeric not null default 0,
  pnl_rate numeric not null default 0,
  buy_date date not null,
  holding_days integer not null default 0,
  current_suggestion text not null default '',
  buy_memo text not null default '',
  status text not null default 'open',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.stock_trade_history (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  name text not null,
  buy_date date not null,
  sell_date date not null,
  cost_price numeric not null,
  sell_price numeric not null,
  shares integer not null,
  pnl_amount numeric not null default 0,
  pnl_rate numeric not null default 0,
  buy_memo text not null default '',
  sell_memo text not null default '',
  is_cleared boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.stock_job_runs (
  id uuid primary key default gen_random_uuid(),
  job_type text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default '运行中',
  imported_count integer not null default 0,
  error_message text not null default ''
);

create index if not exists stock_scan_results_scan_date_idx on public.stock_scan_results(scan_date desc);
create index if not exists stock_strong_picks_scan_date_idx on public.stock_strong_picks(scan_date desc);
create index if not exists stock_live_decisions_updated_at_idx on public.stock_live_decisions(updated_at desc);
create index if not exists stock_positions_status_idx on public.stock_positions(status);
create index if not exists stock_trade_history_sell_date_idx on public.stock_trade_history(sell_date desc);
create index if not exists stock_job_runs_started_at_idx on public.stock_job_runs(started_at desc);
create unique index if not exists stock_positions_code_status_key on public.stock_positions(code, status);

alter table public.stock_scan_results enable row level security;
alter table public.stock_strong_picks enable row level security;
alter table public.stock_live_decisions enable row level security;
alter table public.stock_positions enable row level security;
alter table public.stock_trade_history enable row level security;
alter table public.stock_job_runs enable row level security;

grant select, insert, update, delete on public.stock_scan_results,
  public.stock_strong_picks, public.stock_live_decisions, public.stock_positions,
  public.stock_trade_history, public.stock_job_runs to authenticated;

grant usage, select on all sequences in schema public to authenticated;

drop policy if exists "admin manage stock scan results" on public.stock_scan_results;
create policy "admin manage stock scan results"
on public.stock_scan_results for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock strong picks" on public.stock_strong_picks;
create policy "admin manage stock strong picks"
on public.stock_strong_picks for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock live decisions" on public.stock_live_decisions;
create policy "admin manage stock live decisions"
on public.stock_live_decisions for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock positions" on public.stock_positions;
create policy "admin manage stock positions"
on public.stock_positions for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock trade history" on public.stock_trade_history;
create policy "admin manage stock trade history"
on public.stock_trade_history for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock job runs" on public.stock_job_runs;
create policy "admin manage stock job runs"
on public.stock_job_runs for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
