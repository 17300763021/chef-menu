"""Create compact, non-authoritative M4 contract/framework evidence."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from scripts.strategy.baseline_contracts import content_sha256, load_complete_strategy_spec
from scripts.strategy.research_contracts import M4ResearchRelease, release_from_mapping


def build_report(
    *, require_qlib: bool, research_release: M4ResearchRelease | None = None,
    require_full_release: bool = False,
) -> dict[str, object]:
    spec = load_complete_strategy_spec()
    try:
        qlib_version = version("pyqlib")
    except PackageNotFoundError:
        qlib_version = None
    if require_qlib and qlib_version != "0.9.7":
        raise RuntimeError("pinned pyqlib 0.9.7 is required for framework acceptance")
    if require_full_release:
        if research_release is None:
            raise RuntimeError("formal M4 acceptance requires an explicitly bound research release")
        fundamental = research_release.component("fundamental")
        if (
            not research_release.actionable_research_ready
            or fundamental.expected_count != 1403
            or fundamental.available_count != 1403
        ):
            raise RuntimeError("formal M4 acceptance requires the resolved full 1,403-symbol release")
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
    if research_release is not None:
        report["research_release_id"] = research_release.release_id
        report["research_release_sha256"] = research_release.manifest_sha256
        report["research_components"] = [
            {
                "name": component.name,
                "dataset_id": component.dataset_id,
                "manifest_sha256": component.manifest_sha256,
                "state": component.state.value,
                "expected_count": component.expected_count,
                "available_count": component.available_count,
            }
            for component in research_release.components
        ]
    report["evidence_sha256"] = content_sha256(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-qlib", action="store_true")
    parser.add_argument("--research-release", type=Path)
    parser.add_argument("--require-full-release", action="store_true")
    args = parser.parse_args()
    release = None
    if args.research_release is not None:
        release = release_from_mapping(json.loads(args.research_release.read_text(encoding="utf-8")))
    report = build_report(
        require_qlib=args.require_qlib,
        research_release=release,
        require_full_release=args.require_full_release,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
