-- Link strategy suggestions to paper execution outcomes.
-- This remains simulation-only metadata; it does not create broker orders.

alter table public.stock_signal_events
  add column if not exists execution_status text not null default 'not_executed',
  add column if not exists execution_order_id uuid,
  add column if not exists execution_reason text not null default '',
  add column if not exists execution_handled_at timestamptz;

alter table public.stock_auto_trade_orders
  add column if not exists source_signal_id uuid,
  add column if not exists failure_reason text not null default '';

create index if not exists stock_signal_events_execution_status_idx
on public.stock_signal_events(execution_status, signal_time desc);

create index if not exists stock_auto_trade_orders_source_signal_id_idx
on public.stock_auto_trade_orders(source_signal_id);
