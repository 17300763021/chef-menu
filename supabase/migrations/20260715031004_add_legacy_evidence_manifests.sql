-- M0.2: append-only forensic evidence manifests for the frozen legacy ledgers.

create table if not exists public.legacy_evidence_manifests (
  id uuid primary key default gen_random_uuid(),
  evidence_id text not null unique,
  source_commit text not null,
  exported_at timestamptz not null,
  archive_sha256 text not null unique
    check (archive_sha256 ~ '^[0-9a-f]{64}$'),
  archive_size_bytes bigint not null check (archive_size_bytes > 0),
  storage_bucket text not null,
  storage_path text not null unique,
  table_counts jsonb not null,
  table_hashes jsonb not null,
  file_hashes jsonb not null,
  workflow_runs jsonb not null default '[]'::jsonb,
  manifest jsonb not null,
  verification_status text not null default 'verified'
    check (verification_status = 'verified'),
  created_at timestamptz not null default now()
);

alter table public.legacy_evidence_manifests enable row level security;
revoke all on table public.legacy_evidence_manifests from anon, authenticated;

create or replace function private.reject_legacy_evidence_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'legacy forensic evidence is append-only; % is forbidden', tg_op
    using errcode = '55000';
end;
$$;

revoke all on function private.reject_legacy_evidence_mutation() from public, anon, authenticated;

drop trigger if exists reject_legacy_evidence_mutation
  on public.legacy_evidence_manifests;
create trigger reject_legacy_evidence_mutation
before update or delete or truncate on public.legacy_evidence_manifests
for each statement execute function private.reject_legacy_evidence_mutation();

create or replace function public.legacy_evidence_schema_snapshot()
returns jsonb
language sql
stable
security invoker
set search_path = ''
as $$
  with target_tables(table_name) as (
    values
      ('stock_positions'),
      ('stock_trade_history'),
      ('stock_auto_trade_orders'),
      ('stock_portfolio_snapshots'),
      ('stock_model_positions'),
      ('stock_model_orders'),
      ('stock_model_trade_history'),
      ('stock_model_portfolio_snapshots')
  )
  select jsonb_build_object(
    'columns', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'table', c.table_name,
          'ordinal', c.ordinal_position,
          'column', c.column_name,
          'data_type', c.data_type,
          'nullable', c.is_nullable,
          'default', c.column_default
        ) order by c.table_name, c.ordinal_position
      )
      from information_schema.columns c
      join target_tables t on t.table_name = c.table_name
      where c.table_schema = 'public'
    ), '[]'::jsonb),
    'constraints', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'table', rel.relname,
          'name', con.conname,
          'type', con.contype,
          'definition', pg_catalog.pg_get_constraintdef(con.oid, true)
        ) order by rel.relname, con.conname
      )
      from pg_catalog.pg_constraint con
      join pg_catalog.pg_class rel on rel.oid = con.conrelid
      join pg_catalog.pg_namespace nsp on nsp.oid = rel.relnamespace
      join target_tables t on t.table_name = rel.relname
      where nsp.nspname = 'public'
    ), '[]'::jsonb),
    'triggers', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'table', rel.relname,
          'name', trg.tgname,
          'definition', pg_catalog.pg_get_triggerdef(trg.oid, true)
        ) order by rel.relname, trg.tgname
      )
      from pg_catalog.pg_trigger trg
      join pg_catalog.pg_class rel on rel.oid = trg.tgrelid
      join pg_catalog.pg_namespace nsp on nsp.oid = rel.relnamespace
      join target_tables t on t.table_name = rel.relname
      where nsp.nspname = 'public' and not trg.tgisinternal
    ), '[]'::jsonb)
  );
$$;

revoke all on function public.legacy_evidence_schema_snapshot() from public, anon, authenticated;
grant execute on function public.legacy_evidence_schema_snapshot() to service_role;
