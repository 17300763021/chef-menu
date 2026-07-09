from datetime import date
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_trade_engine as engine
from paper_trade_engine import (
    account_drawdown_pct,
    adaptive_position_size,
    buy_position,
    consecutive_loss_count,
    decision_signal_strength,
    enrich_decisions_with_model_predictions,
    insert_snapshot,
    record_skipped_sell_decision,
    run,
    sell_decision,
    sell_position,
)


class FakeSupabaseClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object | None, str | None]] = []

    def request(self, method: str, path: str, body=None, prefer: str | None = None):
        self.requests.append((method, path, body, prefer))
        if method == "GET" and path.startswith("stock_signal_events?"):
            return [{"id": "signal-1"}]
        if method == "POST" and path == "stock_auto_trade_orders":
            return [{"id": "order-1"}]
        return []

    def insert(self, table: str, rows: list[dict]) -> int:
        self.requests.append(("INSERT", table, rows, None))
        return len(rows)


class PaperTradeExecutionStatusTest(unittest.TestCase):
    def patch_engine(self, **replacements):
        original_values = {name: getattr(engine, name, None) for name in replacements}
        missing = {name for name in replacements if not hasattr(engine, name)}
        for name, value in replacements.items():
            setattr(engine, name, value)

        def restore():
            for name, value in original_values.items():
                if name in missing:
                    delattr(engine, name)
                else:
                    setattr(engine, name, value)

        return restore

    def test_adaptive_position_size_applies_regime_caps(self) -> None:
        strong_bull = adaptive_position_size(
            base_amount=100000,
            signal_strength=1.0,
            market_regime="强牛市",
            account_drawdown_pct=0,
            consecutive_losses=0,
            stock_volatility_pct=40,
        )
        defense = adaptive_position_size(
            base_amount=100000,
            signal_strength=1.0,
            market_regime="防御",
            account_drawdown_pct=0,
            consecutive_losses=0,
            stock_volatility_pct=40,
        )

        self.assertEqual(strong_bull, 80000)
        self.assertEqual(defense, 25000)

    def test_adaptive_position_size_shrinks_after_five_consecutive_losses_to_floor(self) -> None:
        result = adaptive_position_size(
            base_amount=100000,
            signal_strength=1.0,
            market_regime="强牛市",
            account_drawdown_pct=0,
            consecutive_losses=5,
            stock_volatility_pct=40,
        )

        self.assertEqual(result, 25000)

    def test_adaptive_position_size_combines_drawdown_and_high_volatility(self) -> None:
        result = adaptive_position_size(
            base_amount=100000,
            signal_strength=0.8,
            market_regime="弱牛市",
            account_drawdown_pct=6,
            consecutive_losses=0,
            stock_volatility_pct=65,
        )

        self.assertEqual(result, 29400)

    def test_account_drawdown_uses_peak_snapshot_total_assets(self) -> None:
        snapshots = [
            {"total_assets": 1030000},
            {"total_assets": 1200000},
            {"total_assets": 1100000},
        ]

        self.assertAlmostEqual(account_drawdown_pct(snapshots, current_total_assets=1080000), 10.0)

    def test_consecutive_loss_count_stops_at_latest_profitable_trade(self) -> None:
        trades = [
            {"sell_date": "2026-07-01", "pnl_amount": -100},
            {"sell_date": "2026-07-02", "pnl_amount": 50},
            {"sell_date": "2026-07-03", "pnl_amount": -20},
            {"sell_date": "2026-07-04", "pnl_amount": -30},
        ]

        self.assertEqual(consecutive_loss_count(trades), 2)

    def test_decision_signal_strength_uses_multi_factor_score(self) -> None:
        self.assertEqual(decision_signal_strength({"multi_factor_score": 72}), 0.72)
        self.assertEqual(decision_signal_strength({"score": 60}), 0.6)
        self.assertEqual(decision_signal_strength({}), 1.0)

    def test_buy_position_uses_adaptive_size_inputs(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "Ping An",
            "current_price": 10,
            "suggest_buy_price": 10,
            "stop_loss": 9,
            "can_buy": True,
            "multi_factor_score": 100,
        }

        result = buy_position(
            client,
            decision,
            [],
            [],
            market_regime="防御",
            account_drawdown=0,
            consecutive_losses=0,
            stock_volatility_pct=40,
        )

        self.assertIsNotNone(result)
        order_payload = [
            request[2][0]
            for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0]
        self.assertLessEqual(order_payload["amount"], 25000)

    def test_sector_filter_blocks_bottom_sector_buy_candidate(self) -> None:
        calls: list[str] = []
        client = FakeSupabaseClient()

        def fake_buy_position(*args, **kwargs):
            calls.append("buy")
            return {"code": "000001", "shares": 100}

        restore = self.patch_engine(
            get_client=lambda: client,
            classify_market_regime=lambda active_client: {"regime": "强牛市", "position_cap_pct": 80},
            latest_live_decisions=lambda active_client: [{
                "code": "000001",
                "can_buy": True,
                "current_price": 10,
                "shenwan_industry_l1": "弱行业",
                "factor_scores": {"momentum": 45},
            }],
            latest_model_predictions=lambda active_client: {},
            open_positions=lambda active_client: [],
            trade_history=lambda active_client: [],
            portfolio_snapshots=lambda active_client: [],
            today_orders=lambda active_client: [],
            sector_momentum_ranking=lambda active_client, lookback_days=20: {"弱行业": {"rank": 18}},
            buy_position=fake_buy_position,
            insert_snapshot=lambda active_client, positions, trades, trade_count, regime="震荡市": None,
        )
        try:
            result = run(dry_run=False)
        finally:
            restore()

        self.assertEqual(calls, [])
        self.assertEqual(result["orders"], 0)
        signal_reasons = [
            request[2]["execution_reason"]
            for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_signal_events?")
        ]
        self.assertIn("板块排名靠后", "".join(signal_reasons))

    def test_sector_filter_allows_top_sector_buy_candidate(self) -> None:
        calls: list[str] = []

        def fake_buy_position(*args, **kwargs):
            calls.append("buy")
            return {"code": "000001", "shares": 100}

        restore = self.patch_engine(
            get_client=lambda: FakeSupabaseClient(),
            classify_market_regime=lambda client: {"regime": "强牛市", "position_cap_pct": 80},
            latest_live_decisions=lambda client: [{
                "code": "000001",
                "can_buy": True,
                "current_price": 10,
                "shenwan_industry_l1": "强行业",
                "factor_scores": {"momentum": 40},
            }],
            latest_model_predictions=lambda client: {},
            open_positions=lambda client: [],
            trade_history=lambda client: [],
            portfolio_snapshots=lambda client: [],
            today_orders=lambda client: [],
            sector_momentum_ranking=lambda client, lookback_days=20: {"强行业": {"rank": 3}},
            buy_position=fake_buy_position,
            insert_snapshot=lambda client, positions, trades, trade_count, regime="震荡市": None,
        )
        try:
            result = run(dry_run=False)
        finally:
            restore()

        self.assertEqual(calls, ["buy"])
        self.assertEqual(result["orders"], 1)

    def test_sector_rank_failure_degrades_without_blocking_buy(self) -> None:
        calls: list[str] = []

        def fake_sector_ranking(client, lookback_days=20):
            raise RuntimeError("sector unavailable")

        def fake_buy_position(*args, **kwargs):
            calls.append("buy")
            return {"code": "000001", "shares": 100}

        restore = self.patch_engine(
            get_client=lambda: FakeSupabaseClient(),
            classify_market_regime=lambda client: {"regime": "强牛市", "position_cap_pct": 80},
            latest_live_decisions=lambda client: [{"code": "000001", "can_buy": True, "current_price": 10}],
            latest_model_predictions=lambda client: {},
            open_positions=lambda client: [],
            trade_history=lambda client: [],
            portfolio_snapshots=lambda client: [],
            today_orders=lambda client: [],
            sector_momentum_ranking=fake_sector_ranking,
            buy_position=fake_buy_position,
            insert_snapshot=lambda client, positions, trades, trade_count, regime="震荡市": None,
        )
        try:
            result = run(dry_run=False)
        finally:
            restore()

        self.assertEqual(calls, ["buy"])
        self.assertEqual(result["orders"], 1)

    def test_weak_sector_open_position_gets_observation_patch(self) -> None:
        client = FakeSupabaseClient()
        positions = [{
            "id": "pos-1",
            "code": "000001",
            "shares": 100,
            "current_price": 10,
            "cost_price": 10,
            "shenwan_industry_l1": "弱行业",
        }]

        restore = self.patch_engine(
            get_client=lambda: client,
            classify_market_regime=lambda active_client: {"regime": "强牛市", "position_cap_pct": 80},
            latest_live_decisions=lambda active_client: [{"code": "000001", "current_price": 10}],
            latest_model_predictions=lambda active_client: {},
            open_positions=lambda active_client: positions,
            trade_history=lambda active_client: [],
            portfolio_snapshots=lambda active_client: [],
            today_orders=lambda active_client: [],
            sector_momentum_ranking=lambda active_client, lookback_days=20: {"弱行业": {"rank": 21}},
            insert_snapshot=lambda active_client, positions, trades, trade_count, regime="震荡市": None,
        )
        try:
            run(dry_run=False)
        finally:
            restore()

        position_patches = [
            request[2]
            for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_positions?")
        ]
        self.assertTrue(any("减仓观察" in str(payload.get("current_suggestion")) for payload in position_patches))

    def test_bear_regime_blocks_new_buys_in_run(self) -> None:
        calls: list[str] = []

        def fake_buy_position(*args, **kwargs):
            calls.append("buy")
            return {"code": "000001", "shares": 100}

        restore = self.patch_engine(
            get_client=lambda: FakeSupabaseClient(),
            classify_market_regime=lambda client: {"regime": "熊市", "position_cap_pct": 20},
            latest_live_decisions=lambda client: [{"code": "000001", "can_buy": True, "current_price": 10}],
            latest_model_predictions=lambda client: {},
            open_positions=lambda client: [],
            trade_history=lambda client: [],
            portfolio_snapshots=lambda client: [],
            today_orders=lambda client: [],
            sector_momentum_ranking=lambda client, lookback_days=20: {},
            buy_position=fake_buy_position,
            insert_snapshot=lambda client, positions, trades, trade_count, regime="震荡市": None,
        )
        try:
            result = run(dry_run=False)
        finally:
            restore()

        self.assertEqual(calls, [])
        self.assertEqual(result["orders"], 0)

    def test_strong_bull_regime_raises_run_holding_cap_to_eight(self) -> None:
        calls: list[str] = []
        positions = [
            {"code": f"00000{index}", "shares": 100, "current_price": 10, "cost_price": 10}
            for index in range(1, 7)
        ]

        def fake_buy_position(client, decision, current_positions, trades, max_holdings=6, **kwargs):
            calls.append(f"buy:{max_holdings}:{len(current_positions)}")
            return {"code": "000007", "shares": 100, "current_price": 10, "cost_price": 10}

        restore = self.patch_engine(
            get_client=lambda: FakeSupabaseClient(),
            classify_market_regime=lambda client: {"regime": "强牛市", "position_cap_pct": 80},
            latest_live_decisions=lambda client: [{"code": "000007", "can_buy": True, "current_price": 10}],
            latest_model_predictions=lambda client: {},
            open_positions=lambda client: positions,
            trade_history=lambda client: [],
            portfolio_snapshots=lambda client: [],
            today_orders=lambda client: [],
            sector_momentum_ranking=lambda client, lookback_days=20: {},
            buy_position=fake_buy_position,
            insert_snapshot=lambda client, positions, trades, trade_count, regime="震荡市": None,
        )
        try:
            result = run(dry_run=False)
        finally:
            restore()

        self.assertEqual(calls, ["buy:8:6"])
        self.assertEqual(result["orders"], 1)

    def test_insert_snapshot_records_regime_in_note(self) -> None:
        client = FakeSupabaseClient()

        insert_snapshot(client, [], [], 0, regime="熊市")

        snapshot_payload = [
            request[2][0]
            for request in client.requests
            if request[0] == "INSERT" and request[1] == "stock_portfolio_snapshots"
        ][0]
        self.assertEqual(snapshot_payload["note"], "[熊市] auto paper trading snapshot")

    def test_invalid_buy_price_marks_signal_failed_instead_of_silent_skip(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "平安银行",
            "decision_date": "2026-06-25",
            "current_price": 0,
            "suggest_buy_price": 0,
            "can_buy": True,
            "final_action": "可以买小仓",
        }

        result = buy_position(client, decision, [], [])

        self.assertIsNone(result)
        patches = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_signal_events?")
        ]
        self.assertEqual(len(patches), 1)
        payload = patches[0][2]
        self.assertEqual(payload["execution_status"], "failed")
        self.assertIn("价格无效", payload["execution_reason"])

    def test_model_prediction_fields_are_written_to_order_payload(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "平安银行",
            "current_price": 11,
            "suggest_sell_price": 11,
            "model_score": 0.72,
            "model_rank": 8,
            "multi_factor_score": 66,
        }
        position = {"id": "pos-1", "code": "000001", "shares": 1000, "cost_price": 10, "buy_date": "2026-01-01"}

        sell_position(client, decision, position, [position], [], "test sell", 500)

        order_payload = [
            request[2][0]
            for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0]
        self.assertEqual(order_payload["model_score"], 0.72)
        self.assertEqual(order_payload["model_rank"], 8)
        self.assertEqual(order_payload["multi_factor_score"], 66)

    def test_latest_model_predictions_are_merged_with_multi_factor_score(self) -> None:
        predictions = {
            "000001": {"score": 0.8, "rank": 5},
            "000002": {"score": 0.2, "rank": 80},
        }
        decisions = [
            {"code": "000001", "score": 70},
            {"code": "000002", "multi_factor_score": 40},
        ]

        enriched = enrich_decisions_with_model_predictions(decisions, predictions)

        self.assertEqual(enriched[0]["model_score"], 0.8)
        self.assertEqual(enriched[0]["model_rank"], 5)
        self.assertEqual(enriched[0]["multi_factor_score"], 70)
        self.assertGreater(enriched[0]["combined_score"], enriched[1]["combined_score"])

    def test_stop_loss_sell_decision_clears_all_shares(self) -> None:
        result = sell_decision(
            {"current_price": 9.8, "stop_loss": 10, "target_price_1": 11},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none"},
        )

        self.assertEqual(result.reason, "触发止损")
        self.assertEqual(result.shares, 1000)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_first_r_sell_decision_sells_half_lot(self) -> None:
        result = sell_decision(
            {"current_price": 11, "stop_loss": 9, "target_price_1": 11},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none"},
        )

        self.assertEqual(result.reason, "触发第一止盈位")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "sold_1r")
        self.assertEqual(result.last_profit_taking_price, 11)

    def test_second_r_sell_decision_sells_remaining_normal_stock(self) -> None:
        result = sell_decision(
            {"current_price": 12, "stop_loss": 9, "target_price_1": 11, "change_rate": 4},
            {"shares": 500, "cost_price": 10, "sell_stage": "sold_1r"},
        )

        self.assertEqual(result.reason, "触发第二止盈位")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_strong_limit_up_updates_trailing_stop_without_selling(self) -> None:
        result = sell_decision(
            {"current_price": 12, "stop_loss": 9, "target_price_1": 11, "change_rate": 10.01},
            {"shares": 500, "cost_price": 10, "sell_stage": "sold_1r", "trailing_stop_price": 10.5},
        )

        self.assertEqual(result.reason, "强势涨停，暂不机械止盈，抬高移动止损")
        self.assertEqual(result.shares, 0)
        self.assertEqual(result.execution_status, "blocked")
        self.assertEqual(result.next_sell_stage, "trailing_stop")
        self.assertGreater(result.trailing_stop_price, 10.5)

    def test_high_profit_normal_stock_forces_protection_sell(self) -> None:
        result = sell_decision(
            {"current_price": 12.6, "stop_loss": 9, "target_price_1": 0, "change_rate": 4},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none"},
        )

        self.assertEqual(result.reason, "浮盈超过25%，普通持仓强制减仓保护")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "sold_1r")
        self.assertEqual(result.last_profit_taking_price, 12.6)

    def test_profit_near_pressure_reduces_position(self) -> None:
        result = sell_decision(
            {"current_price": 11.2, "stop_loss": 9, "target_price_1": 0, "change_rate": 3, "final_action": "临近压力，先减仓保护"},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none"},
        )

        self.assertEqual(result.reason, "浮盈超过10%且临近压力，减仓保护")
        self.assertEqual(result.shares, 300)
        self.assertEqual(result.next_sell_stage, "sold_1r")

    def test_heavy_volume_stagnation_clears_high_profit_position(self) -> None:
        result = sell_decision(
            {"current_price": 11.8, "stop_loss": 9, "target_price_1": 0, "change_rate": 1, "risk": "放量滞涨，上影线较长"},
            {"shares": 1000, "cost_price": 10, "sell_stage": "sold_1r"},
        )

        self.assertEqual(result.reason, "浮盈超过15%且放量滞涨，清仓保护利润")
        self.assertEqual(result.shares, 1000)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_high_profit_strong_limit_up_records_protection_without_selling(self) -> None:
        result = sell_decision(
            {"current_price": 12.6, "stop_loss": 9, "target_price_1": 0, "change_rate": 10.01},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none", "trailing_stop_price": 10.5},
        )

        self.assertEqual(result.reason, "浮盈超过25%且强势涨停，暂不卖出，抬高移动止损保护利润")
        self.assertEqual(result.shares, 0)
        self.assertEqual(result.execution_status, "blocked")
        self.assertEqual(result.next_sell_stage, "trailing_stop")
        self.assertGreater(result.trailing_stop_price, 10.5)

    def test_consecutive_limit_up_tracks_board_strength_without_selling(self) -> None:
        result = sell_decision(
            {
                "current_price": 13.31,
                "stop_loss": 9,
                "target_price_1": 11,
                "change_rate": 10.02,
                "final_action": "连续涨停，封单强，继续跟踪",
                "limit_up_days": 2,
            },
            {"shares": 1000, "cost_price": 10, "sell_stage": "sold_1r", "trailing_stop_price": 11.2},
        )

        self.assertEqual(result.reason, "连续涨停且封板强，暂不卖出，继续抬高移动止损")
        self.assertEqual(result.shares, 0)
        self.assertEqual(result.execution_status, "blocked")
        self.assertEqual(result.next_sell_stage, "trailing_stop")
        self.assertGreater(result.trailing_stop_price, 11.2)

    def test_heavy_volume_board_break_reduces_position(self) -> None:
        result = sell_decision(
            {
                "current_price": 12.4,
                "stop_loss": 9,
                "target_price_1": 11,
                "change_rate": 6.2,
                "risk": "放量炸板，封板松动",
            },
            {"shares": 1000, "cost_price": 10, "sell_stage": "trailing_stop", "trailing_stop_price": 11},
        )

        self.assertEqual(result.reason, "放量炸板，减仓保护利润")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "sold_2r")
        self.assertEqual(result.last_profit_taking_price, 12.4)

    def test_failed_reseal_after_board_break_clears_position(self) -> None:
        result = sell_decision(
            {
                "current_price": 11.6,
                "stop_loss": 9,
                "target_price_1": 11,
                "change_rate": 2.1,
                "sell_reason": "炸板后回封失败，资金承接转弱",
            },
            {"shares": 1000, "cost_price": 10, "sell_stage": "trailing_stop", "trailing_stop_price": 11},
        )

        self.assertEqual(result.reason, "炸板后回封失败，清仓保护利润")
        self.assertEqual(result.shares, 1000)
        self.assertEqual(result.next_sell_stage, "closed")
        self.assertEqual(result.last_profit_taking_price, 11.6)

    def test_trailing_stop_break_clears_remaining_shares(self) -> None:
        result = sell_decision(
            {"current_price": 10.9, "stop_loss": 9, "target_price_1": 11, "change_rate": -3},
            {"shares": 500, "cost_price": 10, "sell_stage": "trailing_stop", "trailing_stop_price": 11},
        )

        self.assertEqual(result.reason, "跌破移动止损")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_partial_sell_writes_next_sell_stage_to_position(self) -> None:
        client = FakeSupabaseClient()
        decision = {"code": "000001", "name": "平安银行", "current_price": 11, "target_price_1": 11}
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "平安银行",
            "shares": 1000,
            "cost_price": 10,
            "buy_date": "2026-06-25",
            "sell_stage": "none",
        }
        decision_result = sell_decision(decision, position)

        sell_position(client, decision, position, [position], [], decision_result.reason, decision_result.shares, decision_result)

        position_patches = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_positions?")
        ]
        self.assertTrue(position_patches)
        payload = position_patches[-1][2]
        self.assertEqual(payload["shares"], 500)
        self.assertEqual(payload["sell_stage"], "sold_1r")
        self.assertEqual(payload["last_profit_taking_price"], 11)

    def test_strong_limit_up_skip_records_blocked_signal_and_trailing_stop(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "平安银行",
            "current_price": 12,
            "target_price_1": 11,
            "change_rate": 10.01,
        }
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "平安银行",
            "shares": 500,
            "cost_price": 10,
            "sell_stage": "sold_1r",
            "trailing_stop_price": 10.5,
        }
        decision_result = sell_decision(decision, position)

        record_skipped_sell_decision(client, decision, position, decision_result)

        position_patch = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_positions?")
        ][0][2]
        signal_patch = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_signal_events?")
        ][0][2]
        self.assertEqual(position_patch["sell_stage"], "trailing_stop")
        self.assertGreater(position_patch["trailing_stop_price"], 10.5)
        self.assertEqual(signal_patch["execution_status"], "blocked")
        self.assertIn("强势涨停", signal_patch["execution_reason"])

    def test_high_profit_protection_sell_is_visible_in_order_history(self) -> None:
        client = FakeSupabaseClient()
        decision = {"code": "000001", "name": "平安银行", "current_price": 12.6, "target_price_1": 0}
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "平安银行",
            "shares": 1000,
            "cost_price": 10,
            "buy_date": "2026-06-25",
            "sell_stage": "none",
        }
        decision_result = sell_decision(decision, position)

        sell_position(client, decision, position, [position], [], decision_result.reason, decision_result.shares, decision_result)

        order_posts = [
            request for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ]
        self.assertEqual(len(order_posts), 1)
        order_payload = order_posts[0][2][0]
        self.assertEqual(order_payload["side"], "sell")
        self.assertEqual(order_payload["reason"], "浮盈超过25%，普通持仓强制减仓保护")
        self.assertEqual(order_payload["shares"], 500)

    def test_same_day_sell_is_blocked_and_recorded_as_order(self) -> None:
        client = FakeSupabaseClient()
        decision = {"code": "000001", "name": "Ping An", "current_price": 11}
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "Ping An",
            "shares": 1000,
            "cost_price": 10,
            "buy_date": date.today().isoformat(),
            "sell_stage": "none",
        }

        result = sell_position(client, decision, position, [position], [], "test sell", 500)

        self.assertEqual(result, position)
        order_payload = [
            request for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0][2][0]
        self.assertEqual(order_payload["status"], "blocked")
        self.assertEqual(order_payload["shares"], 0)
        self.assertIn("T+1", order_payload["failure_reason"])

    def test_limit_down_sell_is_blocked_and_recorded_as_order(self) -> None:
        client = FakeSupabaseClient()
        decision = {"code": "000001", "name": "Ping An", "current_price": 9, "change_rate": -10.01}
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "Ping An",
            "shares": 1000,
            "cost_price": 10,
            "buy_date": "2026-06-25",
            "sell_stage": "none",
        }

        sell_position(client, decision, position, [position], [], "test sell", 500)

        order_payload = [
            request for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0][2][0]
        self.assertEqual(order_payload["status"], "blocked")
        self.assertIn("limit-down", order_payload["failure_reason"])

    def test_suspended_stock_buy_is_blocked_and_recorded_as_order(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "Ping An",
            "current_price": 10,
            "suggest_buy_price": 10,
            "can_buy": True,
            "status": "suspended",
        }

        result = buy_position(client, decision, [], [])

        self.assertIsNone(result)
        order_payload = [
            request for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0][2][0]
        self.assertEqual(order_payload["status"], "blocked")
        self.assertIn("suspended", order_payload["failure_reason"])

    def test_limit_up_buy_is_blocked_and_recorded_as_order(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "Ping An",
            "current_price": 10,
            "suggest_buy_price": 10,
            "can_buy": True,
            "change_rate": 10.01,
        }

        result = buy_position(client, decision, [], [])

        self.assertIsNone(result)
        order_payload = [
            request for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0][2][0]
        self.assertEqual(order_payload["status"], "blocked")
        self.assertIn("limit-up", order_payload["failure_reason"])

    def test_sell_fees_and_slippage_reduce_cash_and_realized_pnl(self) -> None:
        client = FakeSupabaseClient()
        decision = {"code": "000001", "name": "Ping An", "current_price": 11}
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "Ping An",
            "shares": 1000,
            "cost_price": 10,
            "buy_date": "2026-06-25",
            "sell_stage": "none",
        }

        sell_position(client, decision, position, [position], [], "test sell", 500)

        order_payload = [
            request for request in client.requests
            if request[0] == "POST" and request[1] == "stock_auto_trade_orders"
        ][0][2][0]
        self.assertLess(order_payload["price"], 11)
        self.assertGreater(order_payload["fee_amount"], 0)
        self.assertGreater(order_payload["slippage_amount"], 0)
        self.assertLess(order_payload["realized_pnl"], 500)
        self.assertLess(order_payload["cash_after"], order_payload["cash_before"] + 5500)


if __name__ == "__main__":
    unittest.main()
