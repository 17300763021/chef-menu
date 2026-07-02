-- Track simulation trading costs for auditability.
-- These fields remain paper-trading metadata only; they do not represent broker confirmations.

alter table public.stock_auto_trade_orders
  add column if not exists fee_amount numeric not null default 0,
  add column if not exists slippage_amount numeric not null default 0;
