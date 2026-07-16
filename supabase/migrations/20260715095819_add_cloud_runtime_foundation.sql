create extension if not exists pg_cron with schema pg_catalog;

create schema if not exists private;

create table public.cloud_runtime_accounts (
  environment text primary key,
  display_name text not null,
  account_role text not null check (account_role in ('development', 'shadow', 'main_simulation')),
  simulation_only boolean not null default true check (simulation_only),
  enabled boolean not null default false,
  authoritative_engine text not null default 'rqalpha',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (environment = account_role)
);

create table public.cloud_job_definitions (
  job_type text primary key,
  description text not null,
  criticality text not null check (criticality in ('nonessential', 'critical')),
  enabled boolean not null default false,
  monitored_environment text not null references public.cloud_runtime_accounts(environment),
  max_staleness interval not null check (max_staleness > interval '0'),
  recovery_mode text not null default 'queue' check (recovery_mode in ('none', 'queue')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.cloud_quota_states (
  provider text not null,
  period_start date not null,
  used_units bigint not null default 0 check (used_units >= 0),
  free_limit_units bigint not null check (free_limit_units > 0),
  reported_percent numeric(6,2) not null default 0 check (reported_percent between 0 and 100),
  hard_stop boolean not null default false,
  source text not null,
  observed_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  primary key (provider, period_start)
);

create table public.cloud_job_runs (
  run_id uuid primary key default gen_random_uuid(),
  idempotency_key text not null unique,
  environment text not null references public.cloud_runtime_accounts(environment),
  business_date date not null,
  job_type text not null references public.cloud_job_definitions(job_type),
  run_slot text not null,
  source_commit text not null,
  status text not null check (status in ('claimed', 'running', 'succeeded', 'failed', 'blocked')),
  started_at timestamptz not null default now(),
  heartbeat_at timestamptz not null default now(),
  finished_at timestamptz,
  result_published boolean not null default false,
  quota_decision text not null check (quota_decision in ('normal', 'degraded_80', 'critical_only_90', 'blocked_100', 'blocked_missing_quota')),
  error_message text not null default '',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (environment, business_date, job_type, run_slot, source_commit)
);

create table public.cloud_job_events (
  event_id bigint generated always as identity primary key,
  run_id uuid not null references public.cloud_job_runs(run_id),
  event_type text not null check (event_type in ('claimed', 'heartbeat', 'succeeded', 'failed', 'blocked', 'recovery_claimed', 'recovery_completed')),
  occurred_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb
);

create table public.cloud_recovery_requests (
  recovery_id uuid primary key default gen_random_uuid(),
  environment text not null references public.cloud_runtime_accounts(environment),
  job_type text not null references public.cloud_job_definitions(job_type),
  business_date date not null,
  status text not null default 'pending' check (status in ('pending', 'claimed', 'completed', 'failed')),
  detected_at timestamptz not null default now(),
  claimed_at timestamptz,
  completed_at timestamptz,
  claimed_by text not null default '',
  source_run_id uuid references public.cloud_job_runs(run_id),
  reason text not null,
  error_message text not null default '',
  metadata jsonb not null default '{}'::jsonb,
  unique (environment, job_type, business_date)
);

create index cloud_job_runs_heartbeat_idx
  on public.cloud_job_runs(environment, job_type, heartbeat_at desc);
create index cloud_job_runs_business_idx
  on public.cloud_job_runs(business_date desc, environment, job_type);
create index cloud_job_runs_job_type_idx
  on public.cloud_job_runs(job_type);
create index cloud_job_definitions_environment_idx
  on public.cloud_job_definitions(monitored_environment);
create index cloud_job_events_run_idx
  on public.cloud_job_events(run_id, occurred_at);
create index cloud_recovery_requests_status_idx
  on public.cloud_recovery_requests(status, detected_at);
create index cloud_recovery_requests_job_type_idx
  on public.cloud_recovery_requests(job_type);
create index cloud_recovery_requests_source_run_idx
  on public.cloud_recovery_requests(source_run_id) where source_run_id is not null;

alter table public.cloud_runtime_accounts enable row level security;
alter table public.cloud_job_definitions enable row level security;
alter table public.cloud_quota_states enable row level security;
alter table public.cloud_job_runs enable row level security;
alter table public.cloud_job_events enable row level security;
alter table public.cloud_recovery_requests enable row level security;

revoke all on table public.cloud_runtime_accounts from public, anon, authenticated;
revoke all on table public.cloud_job_definitions from public, anon, authenticated;
revoke all on table public.cloud_quota_states from public, anon, authenticated;
revoke all on table public.cloud_job_runs from public, anon, authenticated;
revoke all on table public.cloud_job_events from public, anon, authenticated;
revoke all on table public.cloud_recovery_requests from public, anon, authenticated;

grant select on table public.cloud_runtime_accounts to service_role;
grant select on table public.cloud_job_definitions to service_role;
grant select, insert, update on table public.cloud_quota_states to service_role;
grant select, insert, update on table public.cloud_job_runs to service_role;
grant select, insert on table public.cloud_job_events to service_role;
grant select, insert, update on table public.cloud_recovery_requests to service_role;
grant usage, select on sequence public.cloud_job_events_event_id_seq to service_role;

insert into public.cloud_runtime_accounts
  (environment, display_name, account_role, enabled)
values
  ('development', '开发模拟账户', 'development', true),
  ('shadow', '云端影子模拟账户', 'shadow', true),
  ('main_simulation', '主模拟账户（待后续验收启用）', 'main_simulation', false)
on conflict (environment) do update
set display_name = excluded.display_name,
    account_role = excluded.account_role,
    simulation_only = true,
    enabled = excluded.enabled,
    updated_at = now();

insert into public.cloud_job_definitions
  (job_type, description, criticality, enabled, monitored_environment, max_staleness, recovery_mode)
values
  ('foundation_heartbeat', 'M1 云端基础心跳与恢复队列处理', 'critical', true, 'shadow', interval '75 minutes', 'queue'),
  ('foundation_acceptance_nonessential', 'M1 配额降级验收专用任务', 'nonessential', true, 'development', interval '8 days', 'none'),
  ('daily_simulation', '未来 RQAlpha 每日主模拟任务', 'critical', false, 'main_simulation', interval '26 hours', 'queue'),
  ('account_reconciliation', '未来权威账户每日对账', 'critical', false, 'main_simulation', interval '26 hours', 'queue'),
  ('research_training', '未来 Qlib 非必要训练', 'nonessential', false, 'shadow', interval '8 days', 'none'),
  ('historical_backfill', '未来历史数据回补', 'nonessential', false, 'development', interval '8 days', 'none')
on conflict (job_type) do update
set description = excluded.description,
    criticality = excluded.criticality,
    enabled = excluded.enabled,
    monitored_environment = excluded.monitored_environment,
    max_staleness = excluded.max_staleness,
    recovery_mode = excluded.recovery_mode,
    updated_at = now();

insert into public.cloud_quota_states
  (provider, period_start, used_units, free_limit_units, reported_percent, hard_stop, source, metadata)
values
  ('github_actions_internal', date_trunc('month', now())::date, 0, 10000, 0, false, 'internal_hard_budget', '{"unit":"unique_cloud_runs","repository_visibility":"public"}'::jsonb),
  ('supabase_internal', date_trunc('month', now())::date, 0, 100000, 0, false, 'internal_hard_budget', '{"unit":"runtime_operations"}'::jsonb)
on conflict (provider, period_start) do nothing;

create or replace function private.cloud_effective_quota_percent(p_row public.cloud_quota_states)
returns numeric
language sql
immutable
as $$
  select greatest(
    p_row.reported_percent,
    round((p_row.used_units::numeric / p_row.free_limit_units::numeric) * 100, 2)
  );
$$;

create or replace function public.claim_cloud_job(p_payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_environment text := p_payload->>'environment';
  v_business_date date := (p_payload->>'business_date')::date;
  v_job_type text := p_payload->>'job_type';
  v_run_slot text := p_payload->>'run_slot';
  v_source_commit text := p_payload->>'source_commit';
  v_idempotency_key text := p_payload->>'idempotency_key';
  v_metadata jsonb := coalesce(p_payload->'metadata', '{}'::jsonb);
  v_account_enabled boolean;
  v_definition public.cloud_job_definitions%rowtype;
  v_quota_count integer;
  v_quota_percent numeric;
  v_hard_stop boolean;
  v_decision text;
  v_allowed boolean := true;
  v_run public.cloud_job_runs%rowtype;
  v_inserted boolean := false;
begin
  if nullif(v_environment, '') is null
     or v_business_date is null
     or nullif(v_job_type, '') is null
     or nullif(v_run_slot, '') is null
     or nullif(v_source_commit, '') is null
     or nullif(v_idempotency_key, '') is null then
    raise exception 'cloud job claim payload is incomplete';
  end if;

  select enabled into v_account_enabled
  from public.cloud_runtime_accounts where environment = v_environment;
  if not found or not v_account_enabled then
    raise exception 'cloud runtime account % is missing or disabled', v_environment;
  end if;

  select * into v_definition
  from public.cloud_job_definitions where job_type = v_job_type;
  if not found or not v_definition.enabled then
    raise exception 'cloud job definition % is missing or disabled', v_job_type;
  end if;

  select count(*), coalesce(max(private.cloud_effective_quota_percent(q)), 0), coalesce(bool_or(q.hard_stop), false)
  into v_quota_count, v_quota_percent, v_hard_stop
  from public.cloud_quota_states q
  where q.period_start = date_trunc('month', v_business_date)::date
    and q.provider in ('github_actions_internal', 'supabase_internal');

  if v_quota_count <> 2 then
    v_allowed := false;
    v_decision := 'blocked_missing_quota';
  elsif v_hard_stop or v_quota_percent >= 100 then
    v_allowed := false;
    v_decision := 'blocked_100';
  elsif v_quota_percent >= 90 then
    v_decision := 'critical_only_90';
    v_allowed := v_definition.criticality = 'critical';
  elsif v_quota_percent >= 80 then
    v_decision := 'degraded_80';
    v_allowed := v_definition.criticality = 'critical';
  else
    v_decision := 'normal';
  end if;

  insert into public.cloud_job_runs (
    idempotency_key, environment, business_date, job_type, run_slot, source_commit,
    status, result_published, quota_decision, metadata, error_message
  ) values (
    v_idempotency_key, v_environment, v_business_date, v_job_type, v_run_slot, v_source_commit,
    case when v_allowed then 'claimed' else 'blocked' end,
    false, v_decision, v_metadata,
    case when v_allowed then '' else 'quota gate rejected this job' end
  )
  on conflict (idempotency_key) do nothing
  returning * into v_run;

  if found then
    v_inserted := true;
    insert into public.cloud_job_events(run_id, event_type, payload)
    values (v_run.run_id, case when v_allowed then 'claimed' else 'blocked' end,
      jsonb_build_object('quota_decision', v_decision, 'quota_percent', v_quota_percent));

    if v_allowed then
      update public.cloud_quota_states
      set used_units = used_units + 1, observed_at = now()
      where period_start = date_trunc('month', v_business_date)::date
        and provider in ('github_actions_internal', 'supabase_internal');
    end if;
  else
    select * into strict v_run
    from public.cloud_job_runs where idempotency_key = v_idempotency_key;
  end if;

  return jsonb_build_object(
    'run_id', v_run.run_id,
    'idempotency_key', v_run.idempotency_key,
    'status', v_run.status,
    'quota_decision', v_run.quota_decision,
    'allowed', v_run.status <> 'blocked',
    'idempotent_replay', not v_inserted
  );
end;
$$;

create or replace function public.heartbeat_cloud_job(p_run_id uuid, p_metadata jsonb default '{}'::jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_run public.cloud_job_runs%rowtype;
begin
  update public.cloud_job_runs
  set status = case when status = 'claimed' then 'running' else status end,
      heartbeat_at = now(),
      metadata = metadata || coalesce(p_metadata, '{}'::jsonb)
  where run_id = p_run_id and status in ('claimed', 'running')
  returning * into v_run;
  if not found then
    raise exception 'cloud job % is not heartbeat-eligible', p_run_id;
  end if;
  insert into public.cloud_job_events(run_id, event_type, payload)
  values (p_run_id, 'heartbeat', coalesce(p_metadata, '{}'::jsonb));
  return jsonb_build_object('run_id', v_run.run_id, 'status', v_run.status, 'heartbeat_at', v_run.heartbeat_at);
end;
$$;

create or replace function public.finish_cloud_job(
  p_run_id uuid,
  p_status text,
  p_result_published boolean default false,
  p_error_message text default '',
  p_metadata jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_run public.cloud_job_runs%rowtype;
begin
  if p_status not in ('succeeded', 'failed') then
    raise exception 'invalid cloud job terminal status %', p_status;
  end if;
  if p_status = 'failed' and p_result_published then
    raise exception 'failed cloud job cannot publish a result';
  end if;
  update public.cloud_job_runs
  set status = p_status,
      heartbeat_at = now(),
      finished_at = now(),
      result_published = p_result_published,
      error_message = coalesce(p_error_message, ''),
      metadata = metadata || coalesce(p_metadata, '{}'::jsonb)
  where run_id = p_run_id and status in ('claimed', 'running')
  returning * into v_run;
  if not found then
    select * into v_run from public.cloud_job_runs where run_id = p_run_id;
    if not found or v_run.status <> p_status
       or v_run.result_published is distinct from p_result_published then
      raise exception 'cloud job % cannot transition to %', p_run_id, p_status;
    end if;
    return jsonb_build_object('run_id', v_run.run_id, 'status', v_run.status, 'idempotent_replay', true);
  end if;
  insert into public.cloud_job_events(run_id, event_type, payload)
  values (p_run_id, p_status, jsonb_build_object('result_published', p_result_published, 'error_message', coalesce(p_error_message, '')) || coalesce(p_metadata, '{}'::jsonb));
  return jsonb_build_object('run_id', v_run.run_id, 'status', v_run.status, 'result_published', v_run.result_published, 'idempotent_replay', false);
end;
$$;

create or replace function private.monitor_cloud_job_health(p_reference_time timestamptz default now())
returns integer
language plpgsql
security invoker
set search_path = public, private, pg_temp
as $$
declare
  v_definition record;
  v_latest public.cloud_job_runs%rowtype;
  v_has_latest boolean;
  v_count integer := 0;
  v_business_date date := timezone('Asia/Shanghai', p_reference_time)::date;
begin
  if extract(isodow from timezone('Asia/Shanghai', p_reference_time)) > 5 then
    return 0;
  end if;
  for v_definition in
    select * from public.cloud_job_definitions
    where enabled and recovery_mode = 'queue'
  loop
    select * into v_latest
    from public.cloud_job_runs
    where environment = v_definition.monitored_environment
      and job_type = v_definition.job_type
      and status in ('claimed', 'running', 'succeeded')
    order by heartbeat_at desc
    limit 1;
    v_has_latest := found;

    if not v_has_latest or v_latest.heartbeat_at < p_reference_time - v_definition.max_staleness then
      insert into public.cloud_recovery_requests(
        environment, job_type, business_date, source_run_id, reason, metadata
      ) values (
        v_definition.monitored_environment,
        v_definition.job_type,
        v_business_date,
        case when v_has_latest then v_latest.run_id else null end,
        case when v_has_latest then 'stale heartbeat' else 'missing heartbeat' end,
        jsonb_build_object('detected_by', 'supabase_cron', 'max_staleness_seconds', extract(epoch from v_definition.max_staleness)::integer)
      )
      on conflict (environment, job_type, business_date) do nothing;
      if found then v_count := v_count + 1; end if;
    end if;
  end loop;
  return v_count;
end;
$$;

create or replace function public.monitor_cloud_job_health(p_reference_time timestamptz default now())
returns jsonb
language sql
security invoker
set search_path = public, private, pg_temp
as $$
  select jsonb_build_object('created_count', private.monitor_cloud_job_health(p_reference_time));
$$;

create or replace function public.claim_cloud_recovery(p_claimed_by text)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_recovery public.cloud_recovery_requests%rowtype;
begin
  if nullif(p_claimed_by, '') is null then
    raise exception 'claimed_by is required';
  end if;
  select * into v_recovery
  from public.cloud_recovery_requests
  where status = 'pending'
  order by detected_at
  for update skip locked
  limit 1;
  if not found then
    return jsonb_build_object('found', false);
  end if;
  update public.cloud_recovery_requests
  set status = 'claimed', claimed_at = now(), claimed_by = p_claimed_by
  where recovery_id = v_recovery.recovery_id
  returning * into v_recovery;
  return jsonb_build_object(
    'found', true,
    'recovery_id', v_recovery.recovery_id,
    'environment', v_recovery.environment,
    'job_type', v_recovery.job_type,
    'business_date', v_recovery.business_date,
    'reason', v_recovery.reason
  );
end;
$$;

create or replace function public.complete_cloud_recovery(
  p_recovery_id uuid,
  p_status text,
  p_source_run_id uuid default null,
  p_error_message text default ''
)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_recovery public.cloud_recovery_requests%rowtype;
begin
  if p_status not in ('completed', 'failed') then
    raise exception 'invalid recovery terminal status %', p_status;
  end if;
  update public.cloud_recovery_requests
  set status = p_status,
      completed_at = now(),
      source_run_id = coalesce(p_source_run_id, source_run_id),
      error_message = coalesce(p_error_message, '')
  where recovery_id = p_recovery_id and status = 'claimed'
  returning * into v_recovery;
  if not found then
    raise exception 'recovery request % cannot transition to %', p_recovery_id, p_status;
  end if;
  if v_recovery.source_run_id is not null then
    insert into public.cloud_job_events(run_id, event_type, payload)
    values (v_recovery.source_run_id, 'recovery_completed', jsonb_build_object('recovery_id', v_recovery.recovery_id, 'status', p_status));
  end if;
  return jsonb_build_object('recovery_id', v_recovery.recovery_id, 'status', v_recovery.status);
end;
$$;

create or replace function public.set_cloud_quota_for_acceptance(
  p_provider text,
  p_reported_percent numeric,
  p_hard_stop boolean default false
)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_row public.cloud_quota_states%rowtype;
begin
  if p_provider not in ('github_actions_internal', 'supabase_internal') then
    raise exception 'unsupported quota provider %', p_provider;
  end if;
  if p_reported_percent < 0 or p_reported_percent > 100 then
    raise exception 'quota percentage must be between 0 and 100';
  end if;
  update public.cloud_quota_states
  set reported_percent = p_reported_percent,
      hard_stop = p_hard_stop,
      observed_at = now(),
      source = 'acceptance_probe'
  where provider = p_provider and period_start = date_trunc('month', now())::date
  returning * into v_row;
  if not found then
    raise exception 'current quota row is missing for %', p_provider;
  end if;
  return jsonb_build_object('provider', v_row.provider, 'reported_percent', v_row.reported_percent, 'hard_stop', v_row.hard_stop);
end;
$$;

create or replace function private.reject_cloud_job_event_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog
as $$
begin
  raise exception 'cloud job events are append-only';
end;
$$;

create trigger cloud_job_events_append_only
before update or delete or truncate on public.cloud_job_events
for each statement execute function private.reject_cloud_job_event_mutation();

revoke all on function public.claim_cloud_job(jsonb) from public, anon, authenticated;
revoke all on function public.heartbeat_cloud_job(uuid, jsonb) from public, anon, authenticated;
revoke all on function public.finish_cloud_job(uuid, text, boolean, text, jsonb) from public, anon, authenticated;
revoke all on function public.claim_cloud_recovery(text) from public, anon, authenticated;
revoke all on function public.complete_cloud_recovery(uuid, text, uuid, text) from public, anon, authenticated;
revoke all on function public.set_cloud_quota_for_acceptance(text, numeric, boolean) from public, anon, authenticated;
revoke all on function public.monitor_cloud_job_health(timestamptz) from public, anon, authenticated;
revoke all on function private.cloud_effective_quota_percent(public.cloud_quota_states) from public, anon, authenticated;
revoke all on function private.monitor_cloud_job_health(timestamptz) from public, anon, authenticated;
revoke all on function private.reject_cloud_job_event_mutation() from public, anon, authenticated;

grant execute on function public.claim_cloud_job(jsonb) to service_role;
grant execute on function public.heartbeat_cloud_job(uuid, jsonb) to service_role;
grant execute on function public.finish_cloud_job(uuid, text, boolean, text, jsonb) to service_role;
grant execute on function public.claim_cloud_recovery(text) to service_role;
grant execute on function public.complete_cloud_recovery(uuid, text, uuid, text) to service_role;
grant execute on function public.set_cloud_quota_for_acceptance(text, numeric, boolean) to service_role;
grant execute on function public.monitor_cloud_job_health(timestamptz) to service_role;
grant usage on schema private to service_role;
grant execute on function private.cloud_effective_quota_percent(public.cloud_quota_states) to service_role;
grant execute on function private.monitor_cloud_job_health(timestamptz) to service_role;

do $$
declare
  v_job_id bigint;
begin
  select jobid into v_job_id from cron.job where jobname = 'cloud-runtime-health-monitor';
  if v_job_id is not null then
    perform cron.unschedule(v_job_id);
  end if;
  perform cron.schedule(
    'cloud-runtime-health-monitor',
    '*/10 * * * *',
    'select private.monitor_cloud_job_health();'
  );
end;
$$;
