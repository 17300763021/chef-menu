-- P4 model-driven simulation account.
-- These tables are virtual/simulation only and do not connect to any broker.

create table if not exists public.stock_model_predictions (
  id uuid primary key default gen_random_uuid(),
  prediction_date date not null,
  code text not null,
  name text not null default '',
  model_name text not null,
  model_version text not null,
  feature_set text not null,
  score numeric not null default 0,
  rank integer not null default 0,
  predicted_return numeric not null default 0,
  confidence numeric not null default 0,
  close_price numeric not null default 0,
  feature_window_start date,
  feature_window_end date,
  train_start_date date,
  train_end_date date,
  validation_start_date date,
  validation_end_date date,
  test_start_date date,
  test_end_date date,
  feature_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (prediction_date, code, model_name, model_version, feature_set)
);

create table if not exists public.stock_model_decisions (
  id uuid primary key default gen_random_uuid(),
  decision_time timestamptz not null default now(),
  decision_date date not null default current_date,
  prediction_id uuid references public.stock_model_predictions(id) on delete set null,
  strategy_account text not null,
  code text not null,
  name text not null default '',
  model_name text not null,
  model_version text not null,
  action text not null,
  reason text not null default '',
  risk_gate_status text not null default 'passed',
  risk_gate_reason text not null default '',
  target_weight numeric not null default 0,
  planned_shares integer not null default 0,
  status text not null default 'new',
  linked_order_id uuid,
  created_at timestamptz not null default now()
);

create table if not exists public.stock_model_positions (
  id uuid primary key default gen_random_uuid(),
  strategy_account text not null,
  code text not null,
  name text not null default '',
  cost_price numeric not null default 0,
  shares integer not null default 0,
  current_price numeric not null default 0,
  market_value numeric not null default 0,
  floating_pnl numeric not null default 0,
  pnl_rate numeric not null default 0,
  buy_date date not null default current_date,
  current_suggestion text not null default '',
  status text not null default 'open',
  model_name text not null default '',
  model_version text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (strategy_account, code, status)
);

create table if not exists public.stock_model_orders (
  id uuid primary key default gen_random_uuid(),
  order_time timestamptz not null default now(),
  order_date date not null default current_date,
  strategy_account text not null,
  decision_id uuid references public.stock_model_decisions(id) on delete set null,
  prediction_id uuid references public.stock_model_predictions(id) on delete set null,
  code text not null,
  name text not null default '',
  side text not null,
  reason text not null default '',
  price numeric not null default 0,
  shares integer not null default 0,
  amount numeric not null default 0,
  fee_amount numeric not null default 0,
  slippage_amount numeric not null default 0,
  cash_before numeric not null default 0,
  cash_after numeric not null default 0,
  position_shares_before integer not null default 0,
  position_shares_after integer not null default 0,
  realized_pnl numeric not null default 0,
  status text not null default 'filled',
  failure_reason text not null default '',
  model_name text not null default '',
  model_version text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists public.stock_model_trade_history (
  id uuid primary key default gen_random_uuid(),
  strategy_account text not null,
  code text not null,
  name text not null default '',
  buy_date date not null,
  sell_date date not null,
  cost_price numeric not null default 0,
  sell_price numeric not null default 0,
  shares integer not null default 0,
  pnl_amount numeric not null default 0,
  pnl_rate numeric not null default 0,
  fee_amount numeric not null default 0,
  slippage_amount numeric not null default 0,
  buy_memo text not null default '',
  sell_memo text not null default '',
  is_cleared boolean not null default true,
  model_name text not null default '',
  model_version text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists public.stock_model_portfolio_snapshots (
  id uuid primary key default gen_random_uuid(),
  snapshot_time timestamptz not null default now(),
  snapshot_date date not null default current_date,
  strategy_account text not null,
  cash numeric not null default 0,
  holding_market_value numeric not null default 0,
  total_assets numeric not null default 0,
  realized_pnl numeric not null default 0,
  floating_pnl numeric not null default 0,
  total_pnl numeric not null default 0,
  total_return_rate numeric not null default 0,
  max_drawdown_rate numeric not null default 0,
  consecutive_losses integer not null default 0,
  position_count integer not null default 0,
  trade_count integer not null default 0,
  model_name text not null default '',
  model_version text not null default '',
  note text not null default ''
);

create index if not exists stock_model_predictions_date_rank_idx
  on public.stock_model_predictions(prediction_date desc, rank asc);
create index if not exists stock_model_predictions_version_idx
  on public.stock_model_predictions(model_name, model_version, prediction_date desc);
create index if not exists stock_model_decisions_date_idx
  on public.stock_model_decisions(decision_date desc, strategy_account);
create index if not exists stock_model_positions_account_idx
  on public.stock_model_positions(strategy_account, status);
create index if not exists stock_model_orders_account_time_idx
  on public.stock_model_orders(strategy_account, order_time desc);
create index if not exists stock_model_snapshots_account_time_idx
  on public.stock_model_portfolio_snapshots(strategy_account, snapshot_time desc);

alter table public.stock_model_predictions enable row level security;
alter table public.stock_model_decisions enable row level security;
alter table public.stock_model_positions enable row level security;
alter table public.stock_model_orders enable row level security;
alter table public.stock_model_trade_history enable row level security;
alter table public.stock_model_portfolio_snapshots enable row level security;

grant select, insert, update, delete on public.stock_model_predictions,
  public.stock_model_decisions, public.stock_model_positions,
  public.stock_model_orders, public.stock_model_trade_history,
  public.stock_model_portfolio_snapshots to authenticated;

drop policy if exists "admin manage stock model predictions" on public.stock_model_predictions;
create policy "admin manage stock model predictions"
on public.stock_model_predictions for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock model decisions" on public.stock_model_decisions;
create policy "admin manage stock model decisions"
on public.stock_model_decisions for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock model positions" on public.stock_model_positions;
create policy "admin manage stock model positions"
on public.stock_model_positions for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock model orders" on public.stock_model_orders;
create policy "admin manage stock model orders"
on public.stock_model_orders for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock model trade history" on public.stock_model_trade_history;
create policy "admin manage stock model trade history"
on public.stock_model_trade_history for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock model portfolio snapshots" on public.stock_model_portfolio_snapshots;
create policy "admin manage stock model portfolio snapshots"
on public.stock_model_portfolio_snapshots for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
