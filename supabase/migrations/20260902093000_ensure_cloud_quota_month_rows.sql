-- Keep the internal quota ledger available when a new calendar month starts.
-- This is idempotent and preserves any existing usage/attestation rows.
create or replace function private.ensure_cloud_quota_period(p_period_start date)
returns void
language sql
security invoker
set search_path = public, pg_temp
as $$
  insert into public.cloud_quota_states
    (provider, period_start, used_units, free_limit_units, reported_percent, hard_stop, source, metadata)
  values
    ('github_actions_internal', p_period_start, 0, 10000, 0, false, 'internal_hard_budget', '{"unit":"unique_cloud_runs","repository_visibility":"public"}'::jsonb),
    ('supabase_internal', p_period_start, 0, 100000, 0, false, 'internal_hard_budget', '{"unit":"runtime_operations"}'::jsonb)
  on conflict (provider, period_start) do nothing;
$$;

grant execute on function private.ensure_cloud_quota_period(date) to service_role;

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

  perform private.ensure_cloud_quota_period(date_trunc('month', v_business_date)::date);
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
  perform private.ensure_cloud_quota_period(date_trunc('month', now())::date);
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
