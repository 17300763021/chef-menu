-- Online-first daily stock history used by research, backtests and simulation.

create table if not exists public.stock_daily_history (
  code text not null,
  trade_date date not null,
  adjustment text not null default 'qfq',
  open numeric not null,
  close numeric not null,
  high numeric not null,
  low numeric not null,
  volume numeric not null default 0,
  amount numeric,
  change_rate numeric,
  source text not null default '',
  updated_at timestamptz not null default now(),
  primary key (code, trade_date, adjustment)
);

create index if not exists stock_daily_history_date_idx
  on public.stock_daily_history(trade_date desc);
create index if not exists stock_daily_history_code_date_idx
  on public.stock_daily_history(code, trade_date desc);

alter table public.stock_daily_history enable row level security;

grant select, insert, update, delete on public.stock_daily_history to authenticated;

drop policy if exists "admin manage stock daily history" on public.stock_daily_history;
create policy "admin manage stock daily history"
on public.stock_daily_history for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

