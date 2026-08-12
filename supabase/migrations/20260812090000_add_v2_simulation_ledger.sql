-- M3.1 disabled acceptance ledger. It cannot activate a daily account or place real orders.
create extension if not exists pgcrypto with schema extensions;

create table public.v2_simulation_runs (
  run_id text primary key,
  idempotency_key text not null unique,
  environment text not null references public.cloud_runtime_accounts(environment),
  business_date date not null,
  data_release_id text not null,
  strategy_version text not null,
  source_commit text not null,
  engine_name text not null check (engine_name = 'rqalpha'),
  engine_version text not null check (engine_version = '6.2.1'),
  predecessor_run_id text references public.v2_simulation_runs(run_id),
  ledger_schema_version text not null check (ledger_schema_version = 'v2-simulation-ledger-v1'),
  simulation_only boolean not null default true check (simulation_only),
  activation_state text not null check (activation_state = 'disabled_acceptance'),
  authoritative_account_write boolean not null default false check (not authoritative_account_write),
  initial_capital numeric(24,4) not null check (initial_capital > 0),
  opening_cash numeric(24,4) not null,
  opening_realized_pnl numeric(24,4) not null,
  cash numeric(24,4) not null,
  market_value numeric(24,4) not null,
  total_equity numeric(24,4) not null,
  realized_pnl numeric(24,4) not null,
  floating_pnl numeric(24,4) not null,
  total_fees numeric(24,4) not null check (total_fees >= 0),
  reconciliation jsonb not null,
  manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_json jsonb not null,
  published_at timestamptz not null default now(),
  check (environment in ('development', 'shadow')),
  check ((reconciliation->>'accepted')::boolean),
  unique (environment, business_date, strategy_version, data_release_id, source_commit)
);

create table public.v2_simulation_opening_positions (
  run_id text not null references public.v2_simulation_runs(run_id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  total_shares integer not null check (total_shares >= 0),
  sellable_shares integer not null check (sellable_shares between 0 and total_shares),
  average_cost numeric(18,4) not null check (average_cost >= 0),
  primary key (run_id, symbol)
);

create table public.v2_simulation_decisions (
  run_id text not null references public.v2_simulation_runs(run_id),
  instruction_id text not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  side text not null check (side in ('buy', 'sell')),
  quantity integer not null check (quantity > 0),
  price_type text not null check (price_type in ('market', 'limit')),
  limit_price numeric(18,4),
  business_date date not null,
  valid_until date not null,
  strategy_version text not null,
  data_release_id text not null,
  primary key (run_id, instruction_id),
  check ((price_type = 'market' and limit_price is null) or (price_type = 'limit' and limit_price > 0)),
  check (valid_until >= business_date)
);

create table public.v2_simulation_orders (
  run_id text not null references public.v2_simulation_runs(run_id),
  order_id text not null,
  instruction_id text not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  side text not null check (side in ('buy', 'sell')),
  requested_quantity integer not null check (requested_quantity > 0),
  filled_quantity integer not null check (filled_quantity between 0 and requested_quantity),
  price_type text not null check (price_type in ('market', 'limit')),
  limit_price numeric(18,4),
  status text not null check (status in ('created', 'rejected', 'filled', 'partially_filled', 'cancelled')),
  reject_reason text not null default '',
  primary key (run_id, order_id),
  foreign key (run_id, instruction_id) references public.v2_simulation_decisions(run_id, instruction_id),
  check ((price_type = 'market' and limit_price is null) or (price_type = 'limit' and limit_price > 0)),
  check ((status = 'rejected' and reject_reason <> '' and filled_quantity = 0) or status <> 'rejected')
);

create table public.v2_simulation_fills (
  run_id text not null references public.v2_simulation_runs(run_id),
  fill_id text not null,
  order_id text not null,
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  side text not null check (side in ('buy', 'sell')),
  quantity integer not null check (quantity > 0),
  price numeric(18,4) not null check (price > 0),
  commission numeric(18,4) not null check (commission >= 0),
  tax numeric(18,4) not null check (tax >= 0),
  slippage numeric(18,4) not null check (slippage >= 0),
  realized_pnl numeric(24,4) not null,
  primary key (run_id, fill_id),
  foreign key (run_id, order_id) references public.v2_simulation_orders(run_id, order_id)
);

create table public.v2_simulation_cash_entries (
  run_id text not null references public.v2_simulation_runs(run_id),
  entry_id text not null,
  fill_id text not null,
  sequence_no integer not null check (sequence_no > 0),
  amount numeric(24,4) not null,
  balance_after numeric(24,4) not null,
  primary key (run_id, entry_id),
  unique (run_id, sequence_no),
  unique (run_id, fill_id),
  foreign key (run_id, fill_id) references public.v2_simulation_fills(run_id, fill_id)
);

create table public.v2_simulation_positions (
  run_id text not null references public.v2_simulation_runs(run_id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  total_shares integer not null check (total_shares >= 0),
  sellable_shares integer not null check (sellable_shares between 0 and total_shares),
  average_cost numeric(18,4) not null check (average_cost >= 0),
  mark_price numeric(18,4) not null check (mark_price >= 0),
  market_value numeric(24,4) not null,
  floating_pnl numeric(24,4) not null,
  data_state text not null check (data_state in ('fresh', 'missing', 'stale', 'inconsistent')),
  primary key (run_id, symbol)
);

create table public.v2_simulation_position_evaluations (
  run_id text not null references public.v2_simulation_runs(run_id),
  symbol text not null check (symbol ~ '^[0-9]{6}$'),
  data_state text not null check (data_state in ('fresh', 'missing', 'stale', 'inconsistent')),
  evaluated boolean not null,
  blocked_reason text not null default '',
  primary key (run_id, symbol),
  foreign key (run_id, symbol) references public.v2_simulation_positions(run_id, symbol),
  check ((data_state = 'fresh') or blocked_reason <> '')
);

create index v2_simulation_runs_business_idx
  on public.v2_simulation_runs(environment, business_date desc);
create index v2_simulation_orders_instruction_idx
  on public.v2_simulation_orders(run_id, instruction_id);
create index v2_simulation_fills_order_idx
  on public.v2_simulation_fills(run_id, order_id);

alter table public.v2_simulation_runs enable row level security;
alter table public.v2_simulation_opening_positions enable row level security;
alter table public.v2_simulation_decisions enable row level security;
alter table public.v2_simulation_orders enable row level security;
alter table public.v2_simulation_fills enable row level security;
alter table public.v2_simulation_cash_entries enable row level security;
alter table public.v2_simulation_positions enable row level security;
alter table public.v2_simulation_position_evaluations enable row level security;

revoke all on table public.v2_simulation_runs from public, anon, authenticated;
revoke all on table public.v2_simulation_opening_positions from public, anon, authenticated;
revoke all on table public.v2_simulation_decisions from public, anon, authenticated;
revoke all on table public.v2_simulation_orders from public, anon, authenticated;
revoke all on table public.v2_simulation_fills from public, anon, authenticated;
revoke all on table public.v2_simulation_cash_entries from public, anon, authenticated;
revoke all on table public.v2_simulation_positions from public, anon, authenticated;
revoke all on table public.v2_simulation_position_evaluations from public, anon, authenticated;

grant select on table public.v2_simulation_runs to service_role;
grant select on table public.v2_simulation_opening_positions to service_role;
grant select on table public.v2_simulation_decisions to service_role;
grant select on table public.v2_simulation_orders to service_role;
grant select on table public.v2_simulation_fills to service_role;
grant select on table public.v2_simulation_cash_entries to service_role;
grant select on table public.v2_simulation_positions to service_role;
grant select on table public.v2_simulation_position_evaluations to service_role;

create or replace function public.publish_v2_simulation_run(
  p_manifest jsonb,
  p_opening_positions jsonb,
  p_instructions jsonb,
  p_orders jsonb,
  p_fills jsonb,
  p_cash_entries jsonb,
  p_positions jsonb,
  p_evaluations jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_run_id text := p_manifest->>'run_id';
  v_idempotency_key text := p_manifest->>'idempotency_key';
  v_manifest_sha256 text := p_manifest->>'manifest_sha256';
  v_payload_sha256 text := encode(extensions.digest(
    convert_to(jsonb_build_array(
      p_opening_positions, p_instructions, p_orders, p_fills,
      p_cash_entries, p_positions, p_evaluations
    )::text, 'UTF8'), 'sha256'
  ), 'hex');
  v_existing public.v2_simulation_runs%rowtype;
  v_inserted integer;
  v_difference record;
begin
  if coalesce(v_run_id, '') = '' or coalesce(v_idempotency_key, '') = '' then
    raise exception 'simulation run identity is incomplete';
  end if;
  if p_manifest->>'ledger_schema_version' <> 'v2-simulation-ledger-v1'
     or p_manifest->>'engine_name' <> 'rqalpha'
     or p_manifest->>'engine_version' <> '6.2.1'
     or coalesce((p_manifest->>'simulation_only')::boolean, false) is not true
     or p_manifest->>'activation_state' <> 'disabled_acceptance'
     or coalesce((p_manifest->>'authoritative_account_write')::boolean, true) is not false
     or coalesce((p_manifest#>>'{reconciliation,accepted}')::boolean, false) is not true then
    raise exception 'simulation boundary, engine identity or reconciliation is invalid';
  end if;
  if not ((p_manifest#>'{reconciliation,differences}') ?&
      array['cash','market_value','equity','floating_pnl','fees','realized_pnl','total_pnl'])
     or (select count(*) from jsonb_object_keys(p_manifest#>'{reconciliation,differences}')) <> 7 then
    raise exception 'simulation reconciliation inventory is incomplete';
  end if;
  for v_difference in select value from jsonb_each_text(p_manifest#>'{reconciliation,differences}') loop
    if v_difference.value::numeric <> 0 then
      raise exception 'non-zero reconciliation difference cannot publish';
    end if;
  end loop;
  if jsonb_array_length(p_opening_positions) <> (p_manifest#>>'{counts,opening_positions}')::integer
     or jsonb_array_length(p_instructions) <> (p_manifest#>>'{counts,instructions}')::integer
     or jsonb_array_length(p_orders) <> (p_manifest#>>'{counts,orders}')::integer
     or jsonb_array_length(p_fills) <> (p_manifest#>>'{counts,fills}')::integer
     or jsonb_array_length(p_cash_entries) <> (p_manifest#>>'{counts,cash_entries}')::integer
     or jsonb_array_length(p_positions) <> (p_manifest#>>'{counts,positions}')::integer
     or jsonb_array_length(p_evaluations) <> (p_manifest#>>'{counts,evaluations}')::integer then
    raise exception 'simulation child count differs from manifest';
  end if;

  insert into public.v2_simulation_runs (
    run_id, idempotency_key, environment, business_date, data_release_id, strategy_version,
    source_commit, engine_name, engine_version, predecessor_run_id, ledger_schema_version, simulation_only,
    activation_state, authoritative_account_write, initial_capital, opening_cash, opening_realized_pnl,
    cash, market_value,
    total_equity, realized_pnl, floating_pnl, total_fees, reconciliation,
    manifest_sha256, payload_sha256, manifest_json
  ) values (
    v_run_id, v_idempotency_key, p_manifest->>'environment', (p_manifest->>'business_date')::date,
    p_manifest->>'data_release_id', p_manifest->>'strategy_version', p_manifest->>'source_commit',
    p_manifest->>'engine_name', p_manifest->>'engine_version', nullif(p_manifest->>'predecessor_run_id', ''),
    p_manifest->>'ledger_schema_version',
    (p_manifest->>'simulation_only')::boolean, p_manifest->>'activation_state',
    (p_manifest->>'authoritative_account_write')::boolean,
    (p_manifest#>>'{snapshot,initial_capital}')::numeric, (p_manifest#>>'{snapshot,opening_cash}')::numeric,
    (p_manifest#>>'{snapshot,opening_realized_pnl}')::numeric,
    (p_manifest#>>'{snapshot,cash}')::numeric,
    (p_manifest#>>'{snapshot,market_value}')::numeric, (p_manifest#>>'{snapshot,total_equity}')::numeric,
    (p_manifest#>>'{snapshot,realized_pnl}')::numeric, (p_manifest#>>'{snapshot,floating_pnl}')::numeric,
    (p_manifest#>>'{snapshot,total_fees}')::numeric, p_manifest->'reconciliation',
    v_manifest_sha256, v_payload_sha256, p_manifest
  ) on conflict (idempotency_key) do nothing;
  get diagnostics v_inserted = row_count;
  if v_inserted = 0 then
    select * into strict v_existing
    from public.v2_simulation_runs where idempotency_key = v_idempotency_key;
    if v_existing.run_id <> v_run_id or v_existing.manifest_sha256 <> v_manifest_sha256
       or v_existing.payload_sha256 <> v_payload_sha256 then
      raise exception 'idempotency key already belongs to different simulation content';
    end if;
    return jsonb_build_object('run_id', v_existing.run_id, 'idempotent_replay', true, 'published', true);
  end if;

  insert into public.v2_simulation_opening_positions
  select v_run_id, item.symbol, item.total_shares, item.sellable_shares, item.average_cost
  from jsonb_to_recordset(p_opening_positions) as item(
    symbol text, total_shares integer, sellable_shares integer, average_cost numeric
  );

  insert into public.v2_simulation_decisions
  select v_run_id, item.instruction_id, item.symbol, item.side, item.quantity,
         item.price_type, item.limit_price, item.business_date, item.valid_until,
         item.strategy_version, item.data_release_id
  from jsonb_to_recordset(p_instructions) as item(
    instruction_id text, symbol text, side text, quantity integer, price_type text,
    limit_price numeric, business_date date, valid_until date, strategy_version text, data_release_id text
  );

  insert into public.v2_simulation_orders
  select v_run_id, item.order_id, item.instruction_id, item.symbol, item.side,
         item.requested_quantity, item.filled_quantity, item.price_type, item.limit_price,
         item.status, coalesce(item.reject_reason, '')
  from jsonb_to_recordset(p_orders) as item(
    order_id text, instruction_id text, symbol text, side text, requested_quantity integer,
    filled_quantity integer, price_type text, limit_price numeric, status text, reject_reason text
  );

  insert into public.v2_simulation_fills
  select v_run_id, item.fill_id, item.order_id, item.symbol, item.side, item.quantity,
         item.price, item.commission, item.tax, item.slippage, item.realized_pnl
  from jsonb_to_recordset(p_fills) as item(
    fill_id text, order_id text, symbol text, side text, quantity integer, price numeric,
    commission numeric, tax numeric, slippage numeric, realized_pnl numeric
  );

  insert into public.v2_simulation_cash_entries
  select v_run_id, item.entry_id, item.fill_id, item.sequence_no, item.amount, item.balance_after
  from jsonb_to_recordset(p_cash_entries) as item(
    entry_id text, fill_id text, sequence_no integer, amount numeric, balance_after numeric
  );

  insert into public.v2_simulation_positions
  select v_run_id, item.symbol, item.total_shares, item.sellable_shares, item.average_cost,
         item.mark_price, item.market_value, item.floating_pnl, item.data_state
  from jsonb_to_recordset(p_positions) as item(
    symbol text, total_shares integer, sellable_shares integer, average_cost numeric,
    mark_price numeric, market_value numeric, floating_pnl numeric, data_state text
  );

  insert into public.v2_simulation_position_evaluations
  select v_run_id, item.symbol, item.data_state, item.evaluated, coalesce(item.blocked_reason, '')
  from jsonb_to_recordset(p_evaluations) as item(
    symbol text, data_state text, evaluated boolean, blocked_reason text
  );

  if exists (
    select 1 from public.v2_simulation_decisions d
    join public.v2_simulation_runs r on r.run_id = d.run_id
    where d.run_id = v_run_id and (
      d.business_date <> r.business_date or d.strategy_version <> r.strategy_version
      or d.data_release_id <> r.data_release_id
    )
  ) then
    raise exception 'instruction lineage differs from its simulation run';
  end if;
  if exists (
    select 1 from public.v2_simulation_orders o
    join public.v2_simulation_decisions d
      on d.run_id = o.run_id and d.instruction_id = o.instruction_id
    where o.run_id = v_run_id and (
      o.symbol <> d.symbol or o.side <> d.side or o.requested_quantity <> d.quantity
      or o.price_type <> d.price_type or o.limit_price is distinct from d.limit_price
    )
  ) or exists (
    select 1 from public.v2_simulation_decisions d
    left join public.v2_simulation_orders o
      on o.run_id = d.run_id and o.instruction_id = d.instruction_id
    where d.run_id = v_run_id and o.order_id is null
  ) then
    raise exception 'order differs from its structured instruction';
  end if;
  if exists (
    select 1 from public.v2_simulation_fills f
    join public.v2_simulation_orders o on o.run_id = f.run_id and o.order_id = f.order_id
    where f.run_id = v_run_id and (f.symbol <> o.symbol or f.side <> o.side)
  ) or exists (
    select 1 from public.v2_simulation_orders o
    left join (
      select run_id, order_id, coalesce(sum(quantity), 0) as filled_quantity
      from public.v2_simulation_fills where run_id = v_run_id group by run_id, order_id
    ) f on f.run_id = o.run_id and f.order_id = o.order_id
    where o.run_id = v_run_id and o.filled_quantity <> coalesce(f.filled_quantity, 0)
  ) then
    raise exception 'fill identity or order quantity reconciliation failed';
  end if;
  if exists (
    select 1 from public.v2_simulation_cash_entries c
    join public.v2_simulation_fills f on f.run_id = c.run_id and f.fill_id = c.fill_id
    where c.run_id = v_run_id and c.amount <> case when f.side = 'buy'
      then -(f.price * f.quantity + f.commission + f.tax)
      else f.price * f.quantity - f.commission - f.tax end
  ) or exists (
    select 1 from public.v2_simulation_fills f
    left join public.v2_simulation_cash_entries c
      on c.run_id = f.run_id and c.fill_id = f.fill_id
    where f.run_id = v_run_id and c.entry_id is null
  ) or exists (
    select 1 from (
      select c.entry_id, c.balance_after,
        r.opening_cash + sum(c.amount) over (order by c.sequence_no) as expected_balance,
        row_number() over (order by c.sequence_no) as expected_sequence
      from public.v2_simulation_cash_entries c
      join public.v2_simulation_runs r on r.run_id = c.run_id
      where c.run_id = v_run_id
    ) x where x.balance_after <> x.expected_balance or x.expected_sequence <> (
      select sequence_no from public.v2_simulation_cash_entries where run_id = v_run_id and entry_id = x.entry_id
    )
  ) then
    raise exception 'cash ledger does not reconcile to fills or running balance';
  end if;
  if exists (
    select 1 from (
      select symbols.symbol,
        coalesce(op.total_shares, 0)
          + coalesce(sum(case when f.side = 'buy' then f.quantity else -f.quantity end), 0) as expected_shares,
        coalesce(cp.total_shares, 0) as actual_shares
      from (
        select symbol from public.v2_simulation_opening_positions where run_id = v_run_id
        union select symbol from public.v2_simulation_fills where run_id = v_run_id
        union select symbol from public.v2_simulation_positions where run_id = v_run_id
      ) symbols
      left join public.v2_simulation_opening_positions op on op.run_id = v_run_id and op.symbol = symbols.symbol
      left join public.v2_simulation_fills f on f.run_id = v_run_id and f.symbol = symbols.symbol
      left join public.v2_simulation_positions cp on cp.run_id = v_run_id and cp.symbol = symbols.symbol
      group by symbols.symbol, op.total_shares, cp.total_shares
    ) x where x.expected_shares <> x.actual_shares
  ) then
    raise exception 'closing position quantities do not reconcile to opening positions and fills';
  end if;
  if exists (
    select 1 from public.v2_simulation_positions p where p.run_id = v_run_id and (
      p.market_value <> p.total_shares * p.mark_price
      or p.floating_pnl <> p.total_shares * (p.mark_price - p.average_cost)
    )
  ) or exists (
    select 1 from public.v2_simulation_fills f where f.run_id = v_run_id
      and f.side = 'buy' and f.realized_pnl <> 0
  ) then
    raise exception 'position valuation or realized PnL contract is invalid';
  end if;
  if exists (
    select 1 from public.v2_simulation_runs r where r.run_id = v_run_id and (
      r.cash <> r.opening_cash + coalesce((select sum(amount) from public.v2_simulation_cash_entries where run_id=v_run_id), 0)
      or r.market_value <> coalesce((select sum(market_value) from public.v2_simulation_positions where run_id=v_run_id), 0)
      or r.total_equity <> r.cash + r.market_value
      or r.realized_pnl <> r.opening_realized_pnl + coalesce((select sum(realized_pnl) from public.v2_simulation_fills where run_id=v_run_id), 0)
      or r.floating_pnl <> coalesce((select sum(floating_pnl) from public.v2_simulation_positions where run_id=v_run_id), 0)
      or r.total_fees <> coalesce((select sum(commission + tax) from public.v2_simulation_fills where run_id=v_run_id), 0)
      or r.total_equity <> r.initial_capital + r.realized_pnl + r.floating_pnl
    )
  ) then
    raise exception 'server-side account reconciliation failed';
  end if;

  if (select count(*) from public.v2_simulation_positions where run_id = v_run_id and total_shares > 0)
     <> (select count(*) from public.v2_simulation_position_evaluations where run_id = v_run_id and evaluated)
     or exists (
       select 1 from public.v2_simulation_positions p
       left join public.v2_simulation_position_evaluations e
         on e.run_id = p.run_id and e.symbol = p.symbol and e.evaluated
       where p.run_id = v_run_id and p.total_shares > 0 and e.symbol is null
     ) then
    raise exception 'open-position evaluation coverage is incomplete';
  end if;
  return jsonb_build_object('run_id', v_run_id, 'idempotent_replay', false, 'published', true);
end;
$$;

create or replace function private.reject_v2_simulation_ledger_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog
as $$
begin
  raise exception 'V2 simulation ledger is append-only';
end;
$$;

create trigger v2_simulation_runs_append_only before update or delete or truncate on public.v2_simulation_runs
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_opening_positions_append_only before update or delete or truncate on public.v2_simulation_opening_positions
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_decisions_append_only before update or delete or truncate on public.v2_simulation_decisions
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_orders_append_only before update or delete or truncate on public.v2_simulation_orders
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_fills_append_only before update or delete or truncate on public.v2_simulation_fills
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_cash_entries_append_only before update or delete or truncate on public.v2_simulation_cash_entries
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_positions_append_only before update or delete or truncate on public.v2_simulation_positions
for each statement execute function private.reject_v2_simulation_ledger_mutation();
create trigger v2_simulation_position_evaluations_append_only before update or delete or truncate on public.v2_simulation_position_evaluations
for each statement execute function private.reject_v2_simulation_ledger_mutation();

revoke all on function public.publish_v2_simulation_run(jsonb, jsonb, jsonb, jsonb, jsonb, jsonb, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.publish_v2_simulation_run(jsonb, jsonb, jsonb, jsonb, jsonb, jsonb, jsonb, jsonb)
  to service_role;
revoke all on function private.reject_v2_simulation_ledger_mutation() from public, anon, authenticated;
grant usage on schema private to service_role;
