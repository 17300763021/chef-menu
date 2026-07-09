alter table public.stock_scan_results
  add column if not exists factor_trend numeric default 0,
  add column if not exists factor_momentum numeric default 0,
  add column if not exists factor_volume numeric default 0,
  add column if not exists factor_flow numeric default 0,
  add column if not exists factor_quality numeric default 0,
  add column if not exists sector_rank integer default 0;

alter table public.stock_strong_picks
  add column if not exists factor_trend numeric default 0,
  add column if not exists factor_momentum numeric default 0,
  add column if not exists factor_volume numeric default 0,
  add column if not exists factor_flow numeric default 0,
  add column if not exists factor_quality numeric default 0,
  add column if not exists sector_rank integer default 0;
