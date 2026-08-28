"""Fail-closed adapter around RQAlpha's public stock-order API."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable

from .contracts import (
    ENGINE_VERSION, DataState, MarketState, OrderInstruction, PositionState,
    PreflightResult, PriceType, Side,
)


def rqalpha_order_book_id(symbol: str) -> str:
    if symbol.startswith(("5", "6", "9")):
        return f"{symbol}.XSHG"
    if symbol.startswith(("0", "1", "2", "3")):
        return f"{symbol}.XSHE"
    if symbol.startswith(("4", "8")):
        return f"{symbol}.BJSE"
    raise ValueError(f"unsupported A-share symbol prefix: {symbol}")


class RQAlphaAdapter:
    """Validates orders before passing them to RQAlpha; never provides a fallback engine."""

    def assert_framework_version(self) -> str:
        try:
            installed = version("rqalpha")
        except PackageNotFoundError as error:
            raise RuntimeError("pinned RQAlpha is unavailable; simulation must stop") from error
        if installed != ENGINE_VERSION:
            raise RuntimeError(f"RQAlpha version mismatch: expected {ENGINE_VERSION}, got {installed}")
        return installed

    def preflight(
        self,
        instruction: OrderInstruction,
        market: MarketState,
        position: PositionState | None,
    ) -> PreflightResult:
        def reject(code: str, reason: str) -> PreflightResult:
            return PreflightResult(instruction.instruction_id, False, code, reason)

        if market.symbol != instruction.symbol or market.business_date != instruction.business_date:
            return reject("market_scope_mismatch", "market state is not aligned to the instruction")
        if market.data_release_id != instruction.data_release_id:
            return reject("data_lineage_mismatch", "order and market state use different data releases")
        if market.data_state is not DataState.FRESH:
            return reject(f"market_data_{market.data_state.value}", "fresh market data is required")
        required = (market.last_price, market.previous_close, market.upper_limit, market.lower_limit, market.suspended)
        if any(value is None for value in required):
            return reject("market_data_incomplete", "required tradeability fields are missing")
        if market.suspended:
            return reject("suspended", "suspended securities cannot trade")
        if instruction.valid_until < market.business_date:
            return reject("instruction_expired", "instruction validity window has ended")
        if instruction.side is Side.BUY:
            if instruction.quantity % 100:
                return reject("board_lot", "A-share buy quantity must be a multiple of 100")
            if market.one_price_limit_up is None:
                return reject("limit_state_missing", "one-price limit-up state is unknown")
            if market.one_price_limit_up:
                return reject("one_price_limit_up", "one-price limit-up security is not buyable")
        else:
            if market.one_price_limit_down is None:
                return reject("limit_state_missing", "one-price limit-down state is unknown")
            if market.one_price_limit_down:
                return reject("one_price_limit_down", "one-price limit-down security is not sellable")
            if position is None or instruction.quantity > position.total_shares:
                return reject("insufficient_position", "sell quantity exceeds the position")
            if instruction.quantity > position.sellable_shares:
                return reject("t_plus_one", "sell quantity exceeds T+1 sellable shares")
        if instruction.price_type is PriceType.LIMIT:
            assert instruction.limit_price is not None
            assert market.upper_limit is not None and market.lower_limit is not None
            if not market.lower_limit <= instruction.limit_price <= market.upper_limit:
                return reject("limit_price_out_of_range", "limit price is outside the daily price band")
        signed_quantity = instruction.quantity if instruction.side is Side.BUY else -instruction.quantity
        return PreflightResult(
            instruction.instruction_id, True, "accepted", "validated for RQAlpha submission",
            rqalpha_order_book_id(instruction.symbol), signed_quantity,
        )

    def submit(
        self,
        instruction: OrderInstruction,
        market: MarketState,
        position: PositionState | None,
        order_function: Callable[..., Any] | None = None,
    ) -> Any:
        result = self.preflight(instruction, market, position)
        if not result.accepted:
            raise RuntimeError(f"order rejected [{result.reason_code}]: {result.reason}")
        self.assert_framework_version()
        if order_function is None:
            from rqalpha.api import order_shares as order_function
        kwargs: dict[str, Any] = {}
        if instruction.price_type is PriceType.LIMIT:
            kwargs["price"] = float(instruction.limit_price)  # type: ignore[arg-type]
        return order_function(result.rqalpha_order_book_id, result.signed_quantity, **kwargs)

    def run_strategy(
        self,
        *,
        config: dict[str, Any],
        init: Callable[..., Any],
        handle_bar: Callable[..., Any],
        before_trading: Callable[..., Any] | None = None,
        after_trading: Callable[..., Any] | None = None,
        run_function: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Run only through RQAlpha's documented ``run_func`` extension point."""
        base = config.get("base")
        if not isinstance(base, dict) or str(base.get("run_type", "")).lower() not in {"b", "backtest"}:
            raise ValueError("M3.1 acceptance permits RQAlpha backtest mode only")
        accounts = base.get("accounts")
        if not isinstance(accounts, dict) or float(accounts.get("stock", 0)) <= 0:
            raise ValueError("M3.1 RQAlpha config requires a positive simulation-only stock account")
        self.assert_framework_version()
        if run_function is None:
            from rqalpha import run_func as run_function
        callbacks: dict[str, Any] = {"config": config, "init": init, "handle_bar": handle_bar}
        if before_trading is not None:
            callbacks["before_trading"] = before_trading
        if after_trading is not None:
            callbacks["after_trading"] = after_trading
        result = run_function(**callbacks)
        if not isinstance(result, dict):
            raise RuntimeError("RQAlpha run_func returned an invalid result")
        return result
