CREATE TABLE IF NOT EXISTS public.stock_market_regime (
  id BIGSERIAL PRIMARY KEY,
  regime_date DATE UNIQUE NOT NULL,
  regime TEXT NOT NULL,
  csi300_close NUMERIC,
  market_turnover_yi NUMERIC,
  limit_up_count INTEGER,
  limit_down_count INTEGER,
  break_rate_pct NUMERIC,
  advance_decline_ratio NUMERIC,
  position_cap_pct NUMERIC,
  details JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.stock_market_regime ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.stock_market_regime TO authenticated;

DROP POLICY IF EXISTS "admin manage market regime" ON public.stock_market_regime;
CREATE POLICY "admin manage market regime"
ON public.stock_market_regime FOR ALL
TO authenticated
USING ((SELECT public.is_admin()))
WITH CHECK ((SELECT public.is_admin()));
