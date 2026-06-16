create table if not exists public.stock_job_requests (
  id uuid primary key default gen_random_uuid(),
  job_type text not null,
  status text not null default 'pending',
  requested_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  error_message text not null default ''
);

create index if not exists stock_job_requests_status_idx
on public.stock_job_requests(status, requested_at);

alter table public.stock_job_requests enable row level security;

grant select, insert, update, delete on public.stock_job_requests to authenticated;
grant usage, select on all sequences in schema public to authenticated;

drop policy if exists "admin manage stock job requests" on public.stock_job_requests;
create policy "admin manage stock job requests"
on public.stock_job_requests for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
