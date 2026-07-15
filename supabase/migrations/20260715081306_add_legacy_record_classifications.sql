-- M0.3: deterministic, reversible classifications for frozen legacy evidence.

create table if not exists public.legacy_reconciliation_runs (
  id uuid primary key default gen_random_uuid(),
  classification_run_id text not null unique,
  evidence_id text not null references public.legacy_evidence_manifests(evidence_id),
  rule_set_version text not null,
  source_archive_sha256 text not null check (source_archive_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_source_commit text not null,
  classifier_commit text not null,
  classified_at timestamptz not null,
  classification_sha256 text not null check (classification_sha256 ~ '^[0-9a-f]{64}$'),
  record_count integer not null check (record_count >= 0),
  category_counts jsonb not null,
  disposition_counts jsonb not null,
  excluded_count integer not null check (excluded_count >= 0),
  excluded_pnl numeric not null default 0,
  report jsonb not null,
  created_at timestamptz not null default now(),
  unique(evidence_id, rule_set_version)
);

create table if not exists public.legacy_record_classifications (
  id uuid primary key default gen_random_uuid(),
  classification_run_id text not null
    references public.legacy_reconciliation_runs(classification_run_id),
  evidence_id text not null,
  rule_set_version text not null,
  source_table text not null,
  source_record_id text not null,
  source_record_sha256 text not null check (source_record_sha256 ~ '^[0-9a-f]{64}$'),
  classification_code text not null,
  disposition text not null check (
    disposition in (
      'authoritative_candidate',
      'audit_only',
      'derived_projection',
      'excluded_polluted',
      'reference_only',
      'review_required'
    )
  ),
  rule_id text not null,
  reason text not null,
  evidence jsonb not null,
  record_result_sha256 text not null check (record_result_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  unique(evidence_id, rule_set_version, source_table, source_record_id)
);

create index if not exists legacy_record_classifications_run_idx
  on public.legacy_record_classifications(classification_run_id);
create index if not exists legacy_record_classifications_disposition_idx
  on public.legacy_record_classifications(disposition, source_table);

alter table public.legacy_reconciliation_runs enable row level security;
alter table public.legacy_record_classifications enable row level security;
revoke all on table public.legacy_reconciliation_runs from anon, authenticated;
revoke all on table public.legacy_record_classifications from anon, authenticated;
grant select, insert on table public.legacy_reconciliation_runs to service_role;
grant select, insert on table public.legacy_record_classifications to service_role;

drop trigger if exists reject_legacy_reconciliation_run_mutation
  on public.legacy_reconciliation_runs;
create trigger reject_legacy_reconciliation_run_mutation
before update or delete or truncate on public.legacy_reconciliation_runs
for each statement execute function private.reject_legacy_evidence_mutation();

drop trigger if exists reject_legacy_record_classification_mutation
  on public.legacy_record_classifications;
create trigger reject_legacy_record_classification_mutation
before update or delete or truncate on public.legacy_record_classifications
for each statement execute function private.reject_legacy_evidence_mutation();

create or replace function public.publish_legacy_record_classification(
  run_payload jsonb,
  classification_payload jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  existing_run public.legacy_reconciliation_runs%rowtype;
  inserted_count integer;
begin
  if jsonb_typeof(classification_payload) <> 'array' then
    raise exception 'classification_payload must be a JSON array' using errcode = '22023';
  end if;

  select * into existing_run
  from public.legacy_reconciliation_runs
  where evidence_id = run_payload->>'evidence_id'
    and rule_set_version = run_payload->>'rule_set_version';

  if found then
    if existing_run.classification_sha256 <> run_payload->>'classification_sha256'
      or existing_run.record_count <> jsonb_array_length(classification_payload) then
      raise exception 'deterministic reconciliation conflict for evidence % and rules %',
        run_payload->>'evidence_id', run_payload->>'rule_set_version'
        using errcode = '23505';
    end if;
    return jsonb_build_object(
      'classification_run_id', existing_run.classification_run_id,
      'classification_sha256', existing_run.classification_sha256,
      'record_count', existing_run.record_count,
      'idempotent_replay', true
    );
  end if;

  if (run_payload->>'record_count')::integer <> jsonb_array_length(classification_payload) then
    raise exception 'record_count does not match classification payload length' using errcode = '22023';
  end if;

  insert into public.legacy_reconciliation_runs (
    classification_run_id, evidence_id, rule_set_version, source_archive_sha256,
    evidence_source_commit, classifier_commit, classified_at, classification_sha256,
    record_count, category_counts, disposition_counts, excluded_count, excluded_pnl, report
  ) values (
    run_payload->>'classification_run_id',
    run_payload->>'evidence_id',
    run_payload->>'rule_set_version',
    run_payload->>'source_archive_sha256',
    run_payload->>'evidence_source_commit',
    run_payload->>'classifier_commit',
    (run_payload->>'classified_at')::timestamptz,
    run_payload->>'classification_sha256',
    (run_payload->>'record_count')::integer,
    run_payload->'category_counts',
    run_payload->'disposition_counts',
    (run_payload->>'excluded_count')::integer,
    (run_payload->>'excluded_pnl')::numeric,
    run_payload->'report'
  );

  insert into public.legacy_record_classifications (
    classification_run_id, evidence_id, rule_set_version, source_table,
    source_record_id, source_record_sha256, classification_code, disposition,
    rule_id, reason, evidence, record_result_sha256
  )
  select
    item.classification_run_id,
    item.evidence_id,
    item.rule_set_version,
    item.source_table,
    item.source_record_id,
    item.source_record_sha256,
    item.classification_code,
    item.disposition,
    item.rule_id,
    item.reason,
    item.evidence,
    item.record_result_sha256
  from jsonb_to_recordset(classification_payload) as item(
    classification_run_id text,
    evidence_id text,
    rule_set_version text,
    source_table text,
    source_record_id text,
    source_record_sha256 text,
    classification_code text,
    disposition text,
    rule_id text,
    reason text,
    evidence jsonb,
    record_result_sha256 text
  )
  where item.classification_run_id = run_payload->>'classification_run_id'
    and item.evidence_id = run_payload->>'evidence_id'
    and item.rule_set_version = run_payload->>'rule_set_version';

  get diagnostics inserted_count = row_count;
  if inserted_count <> jsonb_array_length(classification_payload) then
    raise exception 'classification identity mismatch: inserted %, expected %',
      inserted_count, jsonb_array_length(classification_payload)
      using errcode = '22023';
  end if;

  return jsonb_build_object(
    'classification_run_id', run_payload->>'classification_run_id',
    'classification_sha256', run_payload->>'classification_sha256',
    'record_count', inserted_count,
    'idempotent_replay', false
  );
end;
$$;

revoke all on function public.publish_legacy_record_classification(jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.publish_legacy_record_classification(jsonb, jsonb)
  to service_role;
