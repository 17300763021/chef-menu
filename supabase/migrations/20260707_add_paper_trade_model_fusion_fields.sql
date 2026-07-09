alter table public.stock_auto_trade_orders
  add column if not exists model_score numeric default 0,
  add column if not exists model_rank integer default 0,
  add column if not exists multi_factor_score numeric default 0;

create index if not exists stock_auto_trade_orders_model_rank_idx
  on public.stock_auto_trade_orders(model_rank);
