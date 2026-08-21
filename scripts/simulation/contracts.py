"""Typed, deterministic contracts for the V2 simulation ledger.

Human-readable recommendation text is intentionally absent from executable order
instructions.  The strategy layer must supply validated structured values.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable


LEDGER_SCHEMA_VERSION = "v2-simulation-ledger-v1"
ENGINE_NAME = "rqalpha"
ENGINE_VERSION = "6.2.1"
_SYMBOL = re.compile(r"^\d{6}$")
_MONEY_QUANTUM = Decimal("0.0001")


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PriceType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class DataState(str, Enum):
    FRESH = "fresh"
    MISSING = "missing"
    STALE = "stale"
    INCONSISTENT = "inconsistent"


class OrderStatus(str, Enum):
    CREATED = "created"
    REJECTED = "rejected"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"


def decimal_value(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a numeric structured field") from error
    if not number.is_finite() or (positive and number <= 0):
        requirement = "positive and finite" if positive else "finite"
        raise ValueError(f"{field} must be {requirement}")
    return number.quantize(_MONEY_QUANTUM)


def canonicalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return canonicalize(asdict(value))
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json(value)).hexdigest()}"


def simulation_identity(
    environment: str,
    business_date: date,
    data_release_id: str,
    strategy_version: str,
    source_commit: str,
    predecessor_run_id: str | None = None,
) -> tuple[str, str]:
    """Return the stable business identity used by every retry of one run."""
    key = stable_id("simulation-key", {
        "environment": environment, "business_date": business_date,
        "data_release_id": data_release_id, "strategy_version": strategy_version,
        "source_commit": source_commit, "engine_name": ENGINE_NAME, "engine_version": ENGINE_VERSION,
        "predecessor_run_id": predecessor_run_id,
    })
    return stable_id("simulation-run", {"idempotency_key": key}), key


@dataclass(frozen=True)
class OrderInstruction:
    instruction_id: str
    symbol: str
    side: Side
    quantity: int
    price_type: PriceType
    limit_price: Decimal | None
    business_date: date
    valid_until: date
    strategy_version: str
    data_release_id: str

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "OrderInstruction":
        required = {
            "instruction_id", "symbol", "side", "quantity", "price_type",
            "limit_price", "business_date", "valid_until", "strategy_version", "data_release_id",
        }
        missing = sorted(required - row.keys())
        if missing:
            raise ValueError(f"order instruction missing structured fields: {missing}")
        symbol = str(row["symbol"])
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol must be a six-digit A-share code")
        if isinstance(row["quantity"], bool):
            raise ValueError("quantity must be a positive integer")
        try:
            quantity = int(row["quantity"])
        except (TypeError, ValueError) as error:
            raise ValueError("quantity must be a positive integer") from error
        if quantity <= 0 or str(quantity) != str(row["quantity"]):
            raise ValueError("quantity must be a positive integer")
        side = Side(str(row["side"]).lower())
        price_type = PriceType(str(row["price_type"]).lower())
        raw_price = row["limit_price"]
        limit_price = None if raw_price is None else decimal_value(raw_price, "limit_price", positive=True)
        if price_type is PriceType.LIMIT and limit_price is None:
            raise ValueError("limit orders require a structured limit_price")
        if price_type is PriceType.MARKET and limit_price is not None:
            raise ValueError("market orders cannot carry limit_price")
        business_date = date.fromisoformat(str(row["business_date"]))
        valid_until = date.fromisoformat(str(row["valid_until"]))
        if valid_until < business_date:
            raise ValueError("valid_until cannot precede business_date")
        strategy_version = str(row["strategy_version"]).strip()
        data_release_id = str(row["data_release_id"]).strip()
        instruction_id = str(row["instruction_id"]).strip()
        if not instruction_id or not strategy_version or not data_release_id:
            raise ValueError("instruction_id, strategy_version and data_release_id are required")
        return cls(
            instruction_id=instruction_id, symbol=symbol, side=side, quantity=quantity,
            price_type=price_type, limit_price=limit_price, business_date=business_date,
            valid_until=valid_until, strategy_version=strategy_version, data_release_id=data_release_id,
        )


@dataclass(frozen=True)
class MarketState:
    symbol: str
    business_date: date
    data_state: DataState
    data_release_id: str
    last_price: Decimal | None
    previous_close: Decimal | None
    upper_limit: Decimal | None
    lower_limit: Decimal | None
    suspended: bool | None
    one_price_limit_up: bool | None
    one_price_limit_down: bool | None

    def __post_init__(self) -> None:
        if not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("market-state symbol must be a six-digit code")
        if not self.data_release_id:
            raise ValueError("market state requires a data release id")
        for field in ("last_price", "previous_close", "upper_limit", "lower_limit"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, decimal_value(value, field, positive=True))


@dataclass(frozen=True)
class PositionState:
    symbol: str
    total_shares: int
    sellable_shares: int
    average_cost: Decimal

    def __post_init__(self) -> None:
        if self.total_shares < 0 or self.sellable_shares < 0 or self.sellable_shares > self.total_shares:
            raise ValueError("invalid total or T+1 sellable shares")
        object.__setattr__(self, "average_cost", decimal_value(self.average_cost, "average_cost"))
        if self.average_cost < 0:
            raise ValueError("average_cost cannot be negative")


@dataclass(frozen=True)
class PreflightResult:
    instruction_id: str
    accepted: bool
    reason_code: str
    reason: str
    rqalpha_order_book_id: str | None = None
    signed_quantity: int = 0


@dataclass(frozen=True)
class EngineOrder:
    order_id: str
    instruction_id: str
    symbol: str
    side: Side
    requested_quantity: int
    filled_quantity: int
    price_type: PriceType
    limit_price: Decimal | None
    status: OrderStatus
    reject_reason: str = ""


@dataclass(frozen=True)
class EngineFill:
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    commission: Decimal
    tax: Decimal
    slippage: Decimal
    realized_pnl: Decimal

    @property
    def fee_total(self) -> Decimal:
        # RQAlpha's executed price already contains slippage impact.  Treating
        # reported slippage as another cash fee would deduct it twice.
        return (self.commission + self.tax).quantize(_MONEY_QUANTUM)

    @property
    def cash_effect(self) -> Decimal:
        gross = Decimal(self.quantity) * self.price
        return ((-gross - self.fee_total) if self.side is Side.BUY else (gross - self.fee_total)).quantize(_MONEY_QUANTUM)


@dataclass(frozen=True)
class CashEntry:
    entry_id: str
    fill_id: str
    sequence_no: int
    amount: Decimal
    balance_after: Decimal


@dataclass(frozen=True)
class ClosingPosition:
    symbol: str
    total_shares: int
    sellable_shares: int
    average_cost: Decimal
    mark_price: Decimal
    market_value: Decimal
    floating_pnl: Decimal
    data_state: DataState


@dataclass(frozen=True)
class PositionEvaluation:
    symbol: str
    data_state: DataState
    evaluated: bool
    blocked_reason: str


@dataclass(frozen=True)
class AccountSnapshot:
    initial_capital: Decimal
    opening_cash: Decimal
    opening_realized_pnl: Decimal
    cash: Decimal
    market_value: Decimal
    total_equity: Decimal
    realized_pnl: Decimal
    floating_pnl: Decimal
    total_fees: Decimal

    def __post_init__(self) -> None:
        for field in (
            "initial_capital", "opening_cash", "cash", "market_value", "total_equity",
            "opening_realized_pnl", "realized_pnl", "floating_pnl", "total_fees",
        ):
            object.__setattr__(self, field, decimal_value(getattr(self, field), field))
        if self.initial_capital <= 0 or self.market_value < 0 or self.total_fees < 0:
            raise ValueError("initial capital must be positive; market value and fees cannot be negative")


@dataclass(frozen=True)
class SimulationPackage:
    run_id: str
    idempotency_key: str
    environment: str
    business_date: date
    data_release_id: str
    strategy_version: str
    source_commit: str
    engine_name: str
    engine_version: str
    predecessor_run_id: str | None
    opening_positions: tuple[PositionState, ...]
    instructions: tuple[OrderInstruction, ...]
    orders: tuple[EngineOrder, ...]
    fills: tuple[EngineFill, ...]
    cash_entries: tuple[CashEntry, ...]
    closing_positions: tuple[ClosingPosition, ...]
    evaluations: tuple[PositionEvaluation, ...]
    snapshot: AccountSnapshot

    def __post_init__(self) -> None:
        if self.environment not in {"development", "shadow"}:
            raise ValueError("M3.1 accepts development or shadow simulation only")
        if self.engine_name != ENGINE_NAME or self.engine_version != ENGINE_VERSION:
            raise ValueError("simulation package engine identity does not match pinned RQAlpha")
        if not all((self.run_id, self.idempotency_key, self.data_release_id, self.strategy_version, self.source_commit)):
            raise ValueError("simulation package identity and lineage fields are required")
        expected_run_id, expected_key = simulation_identity(
            self.environment, self.business_date, self.data_release_id,
            self.strategy_version, self.source_commit, self.predecessor_run_id,
        )
        if self.run_id != expected_run_id or self.idempotency_key != expected_key:
            raise ValueError("simulation run_id and idempotency_key must be deterministic from business lineage")
        for instruction in self.instructions:
            if (
                instruction.business_date != self.business_date
                or instruction.strategy_version != self.strategy_version
                or instruction.data_release_id != self.data_release_id
            ):
                raise ValueError("instruction lineage must match its simulation package")

    def manifest(self, reconciliation: dict[str, Any]) -> dict[str, Any]:
        components: dict[str, Iterable[Any]] = {
            "opening_positions": self.opening_positions,
            "instructions": self.instructions, "orders": self.orders, "fills": self.fills,
            "cash_entries": self.cash_entries, "positions": self.closing_positions,
            "evaluations": self.evaluations,
        }
        hashes = {name: hashlib.sha256(canonical_json(list(rows))).hexdigest() for name, rows in components.items()}
        return {
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "environment": self.environment,
            "business_date": self.business_date,
            "data_release_id": self.data_release_id,
            "strategy_version": self.strategy_version,
            "source_commit": self.source_commit,
            "engine_name": self.engine_name,
            "engine_version": self.engine_version,
            "predecessor_run_id": self.predecessor_run_id,
            "simulation_only": True,
            "activation_state": "disabled_acceptance",
            "authoritative_account_write": False,
            "counts": {name: len(tuple(rows)) for name, rows in components.items()},
            "hashes": hashes,
            "snapshot": self.snapshot,
            "reconciliation": reconciliation,
        }
