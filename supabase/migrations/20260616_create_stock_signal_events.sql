create table if not exists public.stock_signal_events (
  id uuid primary key default gen_random_uuid(),
  signal_time timestamptz not null default now(),
  signal_date date not null default current_date,
  code text not null,
  name text not null,
  signal_key text not null,
  source_type text not null default '',
  signal_type text not null default '观察',
  status text not null default '新信号',
  trigger_price numeric not null default 0,
  current_price numeric not null default 0,
  change_rate numeric not null default 0,
  buy_price_text text not null default '',
  sell_price_text text not null default '',
  stop_loss numeric not null default 0,
  target_price_1 numeric,
  final_action text not null default '',
  reason text not null default '',
  risk text not null default '',
  raw_payload jsonb not null default '{}'::jsonb,
  handled_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists stock_signal_events_time_idx
on public.stock_signal_events(signal_time desc);

create index if not exists stock_signal_events_status_idx
on public.stock_signal_events(status, signal_time desc);

create index if not exists stock_signal_events_code_idx
on public.stock_signal_events(code, signal_time desc);

create unique index if not exists stock_signal_events_signal_key_key
on public.stock_signal_events(signal_key);

alter table public.stock_signal_events enable row level security;

grant select, insert, update, delete on public.stock_signal_events to authenticated;
grant usage, select on all sequences in schema public to authenticated;

drop policy if exists "admin manage stock signal events" on public.stock_signal_events;
create policy "admin manage stock signal events"
on public.stock_signal_events for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
