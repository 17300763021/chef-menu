-- Rebuild the administrator check and write policies.
-- Safe to run more than once in Supabase SQL Editor.

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.admin_users
    where user_id = (select auth.uid())
  );
$$;

revoke all on function public.is_admin() from public;
grant execute on function public.is_admin() to anon, authenticated;

alter table public.admin_users enable row level security;
alter table public.chefs enable row level security;
alter table public.recipes enable row level security;
alter table public.daily_menus enable row level security;
alter table public.daily_menu_items enable row level security;
alter table public.cooking_records enable row level security;
alter table public.record_photos enable row level security;
alter table public.daily_quotes enable row level security;
alter table public.holidays enable row level security;

grant select on public.chefs, public.recipes, public.daily_menus,
  public.daily_menu_items, public.cooking_records, public.record_photos,
  public.daily_quotes, public.holidays to anon, authenticated;

grant insert, update, delete on public.chefs, public.recipes,
  public.daily_menus, public.daily_menu_items, public.cooking_records,
  public.record_photos, public.daily_quotes, public.holidays to authenticated;

grant usage, select on all sequences in schema public to authenticated;

drop policy if exists "admin can see own membership" on public.admin_users;
create policy "admin can see own membership"
on public.admin_users for select
to authenticated
using (user_id = (select auth.uid()));

drop policy if exists "admin write chefs" on public.chefs;
create policy "admin write chefs"
on public.chefs for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write recipes" on public.recipes;
create policy "admin write recipes"
on public.recipes for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write menus" on public.daily_menus;
create policy "admin write menus"
on public.daily_menus for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write menu items" on public.daily_menu_items;
create policy "admin write menu items"
on public.daily_menu_items for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write records" on public.cooking_records;
create policy "admin write records"
on public.cooking_records for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write photos" on public.record_photos;
create policy "admin write photos"
on public.record_photos for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write quotes" on public.daily_quotes;
create policy "admin write quotes"
on public.daily_quotes for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "admin write holidays" on public.holidays;
create policy "admin write holidays"
on public.holidays for all
to authenticated
using ((select public.is_admin()))
with check ((select public.is_admin()));

drop policy if exists "public view cooking assets" on storage.objects;
create policy "public view cooking assets"
on storage.objects for select
to anon, authenticated
using (bucket_id in ('chef-avatars', 'recipe-images', 'cooking-records'));

drop policy if exists "admin upload cooking assets" on storage.objects;
create policy "admin upload cooking assets"
on storage.objects for insert
to authenticated
with check (
  bucket_id in ('chef-avatars', 'recipe-images', 'cooking-records')
  and (select public.is_admin())
);

drop policy if exists "admin update cooking assets" on storage.objects;
create policy "admin update cooking assets"
on storage.objects for update
to authenticated
using (
  bucket_id in ('chef-avatars', 'recipe-images', 'cooking-records')
  and (select public.is_admin())
)
with check (
  bucket_id in ('chef-avatars', 'recipe-images', 'cooking-records')
  and (select public.is_admin())
);

drop policy if exists "admin delete cooking assets" on storage.objects;
create policy "admin delete cooking assets"
on storage.objects for delete
to authenticated
using (
  bucket_id in ('chef-avatars', 'recipe-images', 'cooking-records')
  and (select public.is_admin())
);

-- SQL Editor does not run as the browser's signed-in user, so auth.uid() is
-- empty there. Use this report to confirm that the intended account is bound
-- to public.admin_users instead.
select
  users.id,
  users.email,
  admins.user_id is not null as is_admin
from auth.users as users
left join public.admin_users as admins on admins.user_id = users.id
order by users.created_at desc;
