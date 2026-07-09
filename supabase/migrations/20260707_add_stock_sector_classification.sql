-- P5 sector-classification data foundation for the simulation-only quant platform.

create table if not exists public.stock_sector_mapping (
  code text primary key,
  name text not null default '',
  shenwan_industry_l1 text not null default '',
  shenwan_industry_l2 text not null default '',
  concept_tags text[] default '{}',
  updated_at timestamptz default now()
);

create index if not exists stock_sector_mapping_l1_idx
  on public.stock_sector_mapping(shenwan_industry_l1);

create index if not exists stock_sector_mapping_l2_idx
  on public.stock_sector_mapping(shenwan_industry_l2);

alter table public.stock_sector_mapping enable row level security;

grant select, insert, update, delete on public.stock_sector_mapping to authenticated;

drop policy if exists "admin manage stock sector mapping" on public.stock_sector_mapping;
create policy "admin manage stock sector mapping"
on public.stock_sector_mapping for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));
