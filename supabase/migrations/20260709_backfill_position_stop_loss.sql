update public.stock_positions
set entry_stop_loss = round(cost_price * 0.94, 3)
where status = 'open'
  and coalesce(entry_stop_loss, 0) <= 0
  and cost_price > 0;
