-- Paper trading tables for the stock assistant.
-- These store virtual orders and account snapshots only; no broker integration.

create table if not exists public.stock_auto_trade_orders (
  id uuid primary key default gen_random_uuid(),
  order_time timestamptz not null default now(),
  order_date date not null default current_date,
  code text not null,
  name text not null,
  side text not null,
  reason text not null default '',
  price numeric not null default 0,
  shares integer not null default 0,
  amount numeric not null default 0,
  cash_before numeric not null default 0,
  cash_after numeric not null default 0,
  position_shares_before integer not null default 0,
  position_shares_after integer not null default 0,
  realized_pnl numeric not null default 0,
  status text not null default 'filled',
  source_decision_date date,
  source_update_time text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists public.stock_portfolio_snapshots (
  id uuid primary key default gen_random_uuid(),
  snapshot_time timestamptz not null default now(),
  snapshot_date date not null default current_date,
  cash numeric not null default 0,
  holding_market_value numeric not null default 0,
  total_assets numeric not null default 0,
  realized_pnl numeric not null default 0,
  floating_pnl numeric not null default 0,
  total_pnl numeric not null default 0,
  total_return_rate numeric not null default 0,
  position_count integer not null default 0,
  trade_count integer not null default 0,
  note text not null default ''
);

create index if not exists stock_auto_trade_orders_order_time_idx
  on public.stock_auto_trade_orders(order_time desc);
create index if not exists stock_auto_trade_orders_code_idx
  on public.stock_auto_trade_orders(code);
create index if not exists stock_portfolio_snapshots_time_idx
  on public.stock_portfolio_snapshots(snapshot_time desc);

alter table public.stock_auto_trade_orders enable row level security;
alter table public.stock_portfolio_snapshots enable row level security;

grant select, insert, update, delete on public.stock_auto_trade_orders,
  public.stock_portfolio_snapshots to authenticated;

drop policy if exists "admin manage stock auto trade orders" on public.stock_auto_trade_orders;
create policy "admin manage stock auto trade orders"
on public.stock_auto_trade_orders for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin manage stock portfolio snapshots" on public.stock_portfolio_snapshots;
create policy "admin manage stock portfolio snapshots"
on public.stock_portfolio_snapshots for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
