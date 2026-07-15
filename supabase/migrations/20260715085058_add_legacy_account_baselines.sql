-- M0.4: immutable reconstructed historical account baselines.

create table if not exists public.legacy_baseline_runs (
  id uuid primary key default gen_random_uuid(),
  baseline_run_id text not null unique,
  evidence_id text not null references public.legacy_evidence_manifests(evidence_id),
  classification_run_id text not null references public.legacy_reconciliation_runs(classification_run_id),
  rule_set_version text not null,
  source_archive_sha256 text not null check (source_archive_sha256 ~ '^[0-9a-f]{64}$'),
  classification_sha256 text not null check (classification_sha256 ~ '^[0-9a-f]{64}$'),
  baseline_sha256 text not null check (baseline_sha256 ~ '^[0-9a-f]{64}$'),
  classifier_commit text not null,
  rebuilt_at timestamptz not null,
  account_count integer not null check (account_count >= 0),
  entry_count integer not null check (entry_count >= 0),
  position_count integer not null check (position_count >= 0),
  report jsonb not null,
  status text not null default 'frozen' check (status = 'frozen'),
  created_at timestamptz not null default now(),
  unique(evidence_id, classification_run_id, rule_set_version)
);

create table if not exists public.legacy_account_baselines (
  id uuid primary key default gen_random_uuid(),
  baseline_run_id text not null references public.legacy_baseline_runs(baseline_run_id),
  account_key text not null,
  account_label text not null,
  initial_capital numeric not null,
  cash numeric not null,
  holding_market_value numeric not null,
  realized_pnl numeric not null,
  floating_pnl numeric not null,
  total_pnl numeric not null,
  total_assets numeric not null,
  total_return_rate numeric not null,
  recorded_fee_total numeric not null,
  recorded_slippage_total numeric not null,
  filled_order_count integer not null,
  open_position_count integer not null,
  original_metrics jsonb not null,
  reconciliation jsonb not null,
  result_sha256 text not null check (result_sha256 ~ '^[0-9a-f]{64}$'),
  status text not null default 'frozen' check (status = 'frozen'),
  created_at timestamptz not null default now(),
  unique(baseline_run_id, account_key)
);

create table if not exists public.legacy_account_baseline_entries (
  id uuid primary key default gen_random_uuid(),
  baseline_run_id text not null references public.legacy_baseline_runs(baseline_run_id),
  account_key text not null,
  sequence_no integer not null check (sequence_no > 0),
  source_order_id text not null,
  fill_evidence_kind text not null check (fill_evidence_kind = 'filled_order_surrogate'),
  event_time timestamptz not null,
  code text not null,
  name text not null default '',
  side text not null check (side in ('buy', 'sell')),
  price numeric not null,
  shares integer not null,
  gross_amount numeric not null,
  fee_amount numeric not null,
  slippage_amount numeric not null,
  reconstructed_cash_before numeric not null,
  reconstructed_cash_after numeric not null,
  recorded_cash_before numeric not null,
  recorded_cash_after numeric not null,
  cash_before_difference numeric not null,
  cash_after_difference numeric not null,
  position_shares_before integer not null,
  position_shares_after integer not null,
  reconstructed_realized_pnl numeric not null,
  recorded_realized_pnl numeric not null,
  result_sha256 text not null check (result_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  unique(baseline_run_id, account_key, sequence_no),
  unique(baseline_run_id, source_order_id)
);

create table if not exists public.legacy_account_baseline_positions (
  id uuid primary key default gen_random_uuid(),
  baseline_run_id text not null references public.legacy_baseline_runs(baseline_run_id),
  account_key text not null,
  code text not null,
  name text not null default '',
  shares integer not null check (shares > 0),
  total_book_cost numeric not null,
  average_book_cost numeric not null,
  reference_price numeric not null,
  market_value numeric not null,
  floating_pnl numeric not null,
  source_position_ids jsonb not null,
  result_sha256 text not null check (result_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  unique(baseline_run_id, account_key, code)
);

create index if not exists legacy_account_baseline_entries_run_idx
  on public.legacy_account_baseline_entries(baseline_run_id, account_key, sequence_no);
create index if not exists legacy_account_baseline_positions_run_idx
  on public.legacy_account_baseline_positions(baseline_run_id, account_key);

alter table public.legacy_baseline_runs enable row level security;
alter table public.legacy_account_baselines enable row level security;
alter table public.legacy_account_baseline_entries enable row level security;
alter table public.legacy_account_baseline_positions enable row level security;
revoke all on table public.legacy_baseline_runs from anon, authenticated;
revoke all on table public.legacy_account_baselines from anon, authenticated;
revoke all on table public.legacy_account_baseline_entries from anon, authenticated;
revoke all on table public.legacy_account_baseline_positions from anon, authenticated;
grant select, insert on table public.legacy_baseline_runs to service_role;
grant select, insert on table public.legacy_account_baselines to service_role;
grant select, insert on table public.legacy_account_baseline_entries to service_role;
grant select, insert on table public.legacy_account_baseline_positions to service_role;

drop trigger if exists reject_legacy_baseline_run_mutation on public.legacy_baseline_runs;
create trigger reject_legacy_baseline_run_mutation
before update or delete or truncate on public.legacy_baseline_runs
for each statement execute function private.reject_legacy_evidence_mutation();
drop trigger if exists reject_legacy_account_baseline_mutation on public.legacy_account_baselines;
create trigger reject_legacy_account_baseline_mutation
before update or delete or truncate on public.legacy_account_baselines
for each statement execute function private.reject_legacy_evidence_mutation();
drop trigger if exists reject_legacy_baseline_entry_mutation on public.legacy_account_baseline_entries;
create trigger reject_legacy_baseline_entry_mutation
before update or delete or truncate on public.legacy_account_baseline_entries
for each statement execute function private.reject_legacy_evidence_mutation();
drop trigger if exists reject_legacy_baseline_position_mutation on public.legacy_account_baseline_positions;
create trigger reject_legacy_baseline_position_mutation
before update or delete or truncate on public.legacy_account_baseline_positions
for each statement execute function private.reject_legacy_evidence_mutation();

create or replace function public.publish_legacy_account_baseline(
  run_payload jsonb,
  account_payload jsonb,
  entry_payload jsonb,
  position_payload jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  existing_run public.legacy_baseline_runs%rowtype;
  inserted_accounts integer;
  inserted_entries integer;
  inserted_positions integer;
begin
  if jsonb_typeof(account_payload) <> 'array'
    or jsonb_typeof(entry_payload) <> 'array'
    or jsonb_typeof(position_payload) <> 'array' then
    raise exception 'baseline child payloads must be JSON arrays' using errcode = '22023';
  end if;

  select * into existing_run
  from public.legacy_baseline_runs
  where evidence_id = run_payload->>'evidence_id'
    and classification_run_id = run_payload->>'classification_run_id'
    and rule_set_version = run_payload->>'rule_set_version';

  if found then
    if existing_run.baseline_sha256 <> run_payload->>'baseline_sha256'
      or existing_run.account_count <> jsonb_array_length(account_payload)
      or existing_run.entry_count <> jsonb_array_length(entry_payload)
      or existing_run.position_count <> jsonb_array_length(position_payload) then
      raise exception 'deterministic baseline conflict for evidence %', run_payload->>'evidence_id'
        using errcode = '23505';
    end if;
    return jsonb_build_object(
      'baseline_run_id', existing_run.baseline_run_id,
      'baseline_sha256', existing_run.baseline_sha256,
      'account_count', existing_run.account_count,
      'entry_count', existing_run.entry_count,
      'position_count', existing_run.position_count,
      'idempotent_replay', true
    );
  end if;

  if (run_payload->>'account_count')::integer <> jsonb_array_length(account_payload)
    or (run_payload->>'entry_count')::integer <> jsonb_array_length(entry_payload)
    or (run_payload->>'position_count')::integer <> jsonb_array_length(position_payload) then
    raise exception 'baseline payload count mismatch' using errcode = '22023';
  end if;

  insert into public.legacy_baseline_runs (
    baseline_run_id, evidence_id, classification_run_id, rule_set_version,
    source_archive_sha256, classification_sha256, baseline_sha256,
    classifier_commit, rebuilt_at, account_count, entry_count, position_count, report, status
  ) values (
    run_payload->>'baseline_run_id', run_payload->>'evidence_id',
    run_payload->>'classification_run_id', run_payload->>'rule_set_version',
    run_payload->>'source_archive_sha256', run_payload->>'classification_sha256',
    run_payload->>'baseline_sha256', run_payload->>'classifier_commit',
    (run_payload->>'rebuilt_at')::timestamptz, (run_payload->>'account_count')::integer,
    (run_payload->>'entry_count')::integer, (run_payload->>'position_count')::integer,
    run_payload->'report', 'frozen'
  );

  insert into public.legacy_account_baselines (
    baseline_run_id, account_key, account_label, initial_capital, cash,
    holding_market_value, realized_pnl, floating_pnl, total_pnl, total_assets,
    total_return_rate, recorded_fee_total, recorded_slippage_total,
    filled_order_count, open_position_count, original_metrics, reconciliation,
    result_sha256, status
  ) select
    item.baseline_run_id, item.account_key, item.account_label, item.initial_capital,
    item.cash, item.holding_market_value, item.realized_pnl, item.floating_pnl,
    item.total_pnl, item.total_assets, item.total_return_rate,
    item.recorded_fee_total, item.recorded_slippage_total, item.filled_order_count,
    item.open_position_count, item.original_metrics, item.reconciliation,
    item.result_sha256, 'frozen'
  from jsonb_to_recordset(account_payload) as item(
    baseline_run_id text, account_key text, account_label text, initial_capital numeric,
    cash numeric, holding_market_value numeric, realized_pnl numeric, floating_pnl numeric,
    total_pnl numeric, total_assets numeric, total_return_rate numeric,
    recorded_fee_total numeric, recorded_slippage_total numeric, filled_order_count integer,
    open_position_count integer, original_metrics jsonb, reconciliation jsonb,
    result_sha256 text
  ) where item.baseline_run_id = run_payload->>'baseline_run_id';
  get diagnostics inserted_accounts = row_count;

  insert into public.legacy_account_baseline_entries (
    baseline_run_id, account_key, sequence_no, source_order_id, fill_evidence_kind,
    event_time, code, name, side, price, shares, gross_amount, fee_amount,
    slippage_amount, reconstructed_cash_before, reconstructed_cash_after,
    recorded_cash_before, recorded_cash_after, cash_before_difference,
    cash_after_difference, position_shares_before, position_shares_after,
    reconstructed_realized_pnl, recorded_realized_pnl, result_sha256
  ) select
    item.baseline_run_id, item.account_key, item.sequence_no, item.source_order_id,
    item.fill_evidence_kind, item.event_time, item.code, item.name, item.side,
    item.price, item.shares, item.gross_amount, item.fee_amount, item.slippage_amount,
    item.reconstructed_cash_before, item.reconstructed_cash_after,
    item.recorded_cash_before, item.recorded_cash_after, item.cash_before_difference,
    item.cash_after_difference, item.position_shares_before, item.position_shares_after,
    item.reconstructed_realized_pnl, item.recorded_realized_pnl, item.result_sha256
  from jsonb_to_recordset(entry_payload) as item(
    baseline_run_id text, account_key text, sequence_no integer, source_order_id text,
    fill_evidence_kind text, event_time timestamptz, code text, name text, side text,
    price numeric, shares integer, gross_amount numeric, fee_amount numeric,
    slippage_amount numeric, reconstructed_cash_before numeric,
    reconstructed_cash_after numeric, recorded_cash_before numeric,
    recorded_cash_after numeric, cash_before_difference numeric,
    cash_after_difference numeric, position_shares_before integer,
    position_shares_after integer, reconstructed_realized_pnl numeric,
    recorded_realized_pnl numeric, result_sha256 text
  ) where item.baseline_run_id = run_payload->>'baseline_run_id';
  get diagnostics inserted_entries = row_count;

  insert into public.legacy_account_baseline_positions (
    baseline_run_id, account_key, code, name, shares, total_book_cost,
    average_book_cost, reference_price, market_value, floating_pnl,
    source_position_ids, result_sha256
  ) select
    item.baseline_run_id, item.account_key, item.code, item.name, item.shares,
    item.total_book_cost, item.average_book_cost, item.reference_price,
    item.market_value, item.floating_pnl, item.source_position_ids, item.result_sha256
  from jsonb_to_recordset(position_payload) as item(
    baseline_run_id text, account_key text, code text, name text, shares integer,
    total_book_cost numeric, average_book_cost numeric, reference_price numeric,
    market_value numeric, floating_pnl numeric, source_position_ids jsonb,
    result_sha256 text
  ) where item.baseline_run_id = run_payload->>'baseline_run_id';
  get diagnostics inserted_positions = row_count;

  if inserted_accounts <> jsonb_array_length(account_payload)
    or inserted_entries <> jsonb_array_length(entry_payload)
    or inserted_positions <> jsonb_array_length(position_payload) then
    raise exception 'baseline child identity mismatch' using errcode = '22023';
  end if;

  return jsonb_build_object(
    'baseline_run_id', run_payload->>'baseline_run_id',
    'baseline_sha256', run_payload->>'baseline_sha256',
    'account_count', inserted_accounts,
    'entry_count', inserted_entries,
    'position_count', inserted_positions,
    'idempotent_replay', false
  );
end;
$$;

revoke all on function public.publish_legacy_account_baseline(jsonb, jsonb, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.publish_legacy_account_baseline(jsonb, jsonb, jsonb, jsonb)
  to service_role;
