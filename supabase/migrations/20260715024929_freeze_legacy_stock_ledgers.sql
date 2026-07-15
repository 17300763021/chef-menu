-- Freeze legacy simulation ledgers while preserving all historical rows for audit.
-- The service role bypasses RLS, so a database trigger is the final write barrier.

create schema if not exists private;

create or replace function private.reject_legacy_stock_ledger_write()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception using
    errcode = '55000',
    message = format(
      'legacy stock ledger is frozen: %I.%I rejects %s',
      tg_table_schema,
      tg_table_name,
      tg_op
    );
end;
$$;

revoke all on function private.reject_legacy_stock_ledger_write() from public;
revoke all on function private.reject_legacy_stock_ledger_write() from anon;
revoke all on function private.reject_legacy_stock_ledger_write() from authenticated;

do $$
declare
  table_name text;
  frozen_tables constant text[] := array[
    'stock_positions',
    'stock_trade_history',
    'stock_auto_trade_orders',
    'stock_portfolio_snapshots',
    'stock_model_positions',
    'stock_model_orders',
    'stock_model_trade_history',
    'stock_model_portfolio_snapshots'
  ];
begin
  foreach table_name in array frozen_tables loop
    if to_regclass(format('public.%I', table_name)) is not null then
      execute format('drop trigger if exists freeze_legacy_stock_ledger on public.%I', table_name);
      execute format(
        'create trigger freeze_legacy_stock_ledger '
        'before insert or update or delete or truncate on public.%I '
        'for each statement execute function private.reject_legacy_stock_ledger_write()',
        table_name
      );
    end if;
  end loop;
end;
$$;
