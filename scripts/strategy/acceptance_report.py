"""Create compact, non-authoritative M4 contract/framework evidence."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from scripts.strategy.baseline_contracts import content_sha256, load_complete_strategy_spec


def build_report(*, require_qlib: bool) -> dict[str, object]:
    spec = load_complete_strategy_spec()
    try:
        qlib_version = version("pyqlib")
    except PackageNotFoundError:
        qlib_version = None
    if require_qlib and qlib_version != "0.9.7":
        raise RuntimeError("pinned pyqlib 0.9.7 is required for framework acceptance")
    report = {
        "schema_version": "m4-strategy-acceptance-evidence-v1",
        "strategy_version": spec["strategy_version"],
        "strategy_spec_sha256": content_sha256(spec),
        "activation_state": spec["activation_state"],
        "simulation_only": spec["simulation_only"],
        "simulation_orders_allowed": False,
        "research_input_origin": spec["research_input"]["adjusted_price_origin"],
        "qlib_version": qlib_version,
        "qlib_public_interfaces": ["DataHandlerLP.from_df", "DatasetH", "Model.fit", "Model.predict"],
        "authoritative_account_write": False,
    }
    report["evidence_sha256"] = content_sha256(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-qlib", action="store_true")
    args = parser.parse_args()
    report = build_report(require_qlib=args.require_qlib)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
