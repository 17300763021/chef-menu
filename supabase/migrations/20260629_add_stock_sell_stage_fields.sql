-- Track automatic sell lifecycle for simulation-only paper positions.

alter table public.stock_positions
  add column if not exists sell_stage text not null default 'none',
  add column if not exists trailing_stop_price numeric,
  add column if not exists last_profit_taking_price numeric;

create index if not exists stock_positions_sell_stage_idx
on public.stock_positions(status, sell_stage);
