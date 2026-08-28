"""Public Qlib adapter for M4 transparent scoring and deterministic ranking."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd

from scripts.strategy.baseline_contracts import FACTOR_NAMES, effective_factor_weights


FEATURE_COLUMNS = tuple(FACTOR_NAMES)


@dataclass(frozen=True, slots=True)
class QlibScoreResult:
    scores: pd.DataFrame
    effective_weights: tuple[tuple[str, Decimal], ...]
    qlib_dataset_type: str
    qlib_model_type: str


def _imports():
    try:
        from qlib.data.dataset import DatasetH  # type: ignore
        from qlib.data.dataset.handler import DataHandlerLP  # type: ignore
        from qlib.model.base import Model  # type: ignore
    except ImportError as error:
        raise RuntimeError("Qlib 0.9.7 is required; M4 has no non-Qlib scoring fallback") from error
    return DataHandlerLP, DatasetH, Model


def build_qlib_dataset(features: pd.DataFrame, *, segment_name: str = "score") -> Any:
    """Create DatasetH through the public DataHandlerLP.from_df interface."""
    DataHandlerLP, DatasetH, _ = _imports()
    if not isinstance(features.index, pd.MultiIndex) or tuple(features.index.names) != ("datetime", "instrument"):
        raise ValueError("Qlib feature frame requires datetime/instrument MultiIndex")
    if not features.index.is_unique or not features.index.is_monotonic_increasing:
        raise ValueError("Qlib feature frame must be unique and sorted")
    if any(column not in features for column in FEATURE_COLUMNS):
        raise ValueError("Qlib feature frame is missing canonical M4 factors")
    start = pd.Timestamp(features.index.get_level_values("datetime").min())
    end = pd.Timestamp(features.index.get_level_values("datetime").max())
    handler_frame = features.loc[:, FEATURE_COLUMNS].copy()
    handler_frame.columns = pd.MultiIndex.from_product([["feature"], FEATURE_COLUMNS])
    handler = DataHandlerLP.from_df(handler_frame)
    return DatasetH(handler=handler, segments={segment_name: (start, end)})


def _weighted_rank(
    prepared: pd.DataFrame, *, weights: Mapping[str, Decimal], flow_available: bool
) -> pd.DataFrame:
    if isinstance(prepared.columns, pd.MultiIndex):
        if "feature" not in prepared.columns.get_level_values(0):
            raise ValueError("Qlib prepared data does not expose the feature column set")
        prepared = prepared["feature"]
    missing = [name for name in FEATURE_COLUMNS if name not in prepared.columns]
    if missing:
        raise ValueError(f"Qlib prepared data is missing factors: {missing}")
    required = [name for name in FEATURE_COLUMNS if flow_available or name != "verified_capital_flow"]
    if prepared[required].isna().any().any():
        raise ValueError("Qlib cannot score missing required candidate factors")
    score = sum(prepared[name].astype(float) * float(weights[name]) for name in required)
    output = pd.DataFrame({"factor_score": score}, index=prepared.index)
    reset = output.reset_index()
    reset = reset.sort_values(["factor_score", "instrument"], ascending=[False, True], kind="mergesort")
    reset["rank"] = range(1, len(reset) + 1)
    reset["percentile"] = 1 - (reset["rank"] - 1) / max(len(reset), 1)
    return reset.set_index(["datetime", "instrument"]).sort_index()


def score_and_rank_with_qlib(
    features: pd.DataFrame,
    *,
    flow_available: bool,
    segment_name: str = "score",
) -> QlibScoreResult:
    """Score only through a Qlib Model using a Qlib DatasetH prepared frame."""
    DataHandlerLP, _, Model = _imports()
    observed = [name for name in FEATURE_COLUMNS if flow_available or name != "verified_capital_flow"]
    weights = effective_factor_weights(observed_factors=observed, flow_release_available=flow_available)
    dataset = build_qlib_dataset(features, segment_name=segment_name)

    class TransparentBaselineModel(Model):
        def fit(self, dataset: Any, **kwargs: Any) -> "TransparentBaselineModel":
            return self

        def predict(self, dataset: Any, segment: str = "test") -> pd.DataFrame:
            prepared = dataset.prepare(segment, col_set="feature", data_key=DataHandlerLP.DK_I)
            return _weighted_rank(prepared, weights=weights, flow_available=flow_available)

    model = TransparentBaselineModel().fit(dataset)
    scores = model.predict(dataset, segment_name)
    return QlibScoreResult(
        scores=scores,
        effective_weights=tuple((name, weights[name]) for name in FEATURE_COLUMNS),
        qlib_dataset_type=f"{type(dataset).__module__}.{type(dataset).__name__}",
        qlib_model_type=f"{type(model).__module__}.{type(model).__name__}",
    )
