-- P5 capital-flow data foundation for the simulation-only quant platform.

create table if not exists public.stock_capital_flow (
  id bigserial primary key,
  code text not null,
  name text not null default '',
  flow_date date not null,
  north_bound_net_inflow numeric default 0,
  north_bound_holding_pct numeric default 0,
  north_bound_holding_change numeric default 0,
  big_order_net_inflow numeric default 0,
  big_order_buy_ratio numeric default 0,
  main_net_inflow numeric default 0,
  main_net_inflow_ratio numeric default 0,
  margin_balance_change numeric default 0,
  created_at timestamptz default now(),
  unique (code, flow_date)
);

create index if not exists stock_capital_flow_flow_date_idx
  on public.stock_capital_flow(flow_date desc);

create index if not exists stock_capital_flow_code_date_idx
  on public.stock_capital_flow(code, flow_date desc);

alter table public.stock_capital_flow enable row level security;

grant select, insert, update, delete on public.stock_capital_flow to authenticated;
grant usage, select on sequence public.stock_capital_flow_id_seq to authenticated;

drop policy if exists "admin manage stock capital flow" on public.stock_capital_flow;
create policy "admin manage stock capital flow"
on public.stock_capital_flow for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
