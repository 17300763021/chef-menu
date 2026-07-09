alter table public.stock_positions
  add column if not exists entry_stop_loss numeric default 0;
