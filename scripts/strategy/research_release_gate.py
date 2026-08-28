"""Bind exact market and fundamental evidence into one immutable M4 release.

Market evidence is read from the established market TiDB connection.  The
fundamental component and the compact release record live in the separate
research TiDB connection.  No component is selected by recency.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect
from scripts.strategy.baseline_contracts import content_sha256, load_complete_strategy_spec
from scripts.strategy.research_contracts import (
    ComponentState,
    M4ResearchRelease,
    REQUIRED_COMPONENTS,
    ResearchComponent,
)


BINDING_SCHEMA_VERSION = "m4-research-binding-spec-v1"
STORE_SCHEMA_VERSION = "m4-research-release-store-v1"
RELEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS m4_research_releases (
  release_id VARCHAR(160) NOT NULL PRIMARY KEY,
  store_schema_version VARCHAR(64) NOT NULL,
  release_schema_version VARCHAR(64) NOT NULL,
  business_date DATE NOT NULL,
  strategy_version VARCHAR(96) NOT NULL,
  authoritative TINYINT NOT NULL,
  simulation_orders_allowed TINYINT NOT NULL,
  actionable_research_ready TINYINT NOT NULL,
  fundamental_dataset_id VARCHAR(160) NOT NULL,
  manifest_sha256 CHAR(64) NOT NULL,
  manifest_json LONGTEXT NOT NULL,
  published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_m4_research_release_date (business_date)
)
"""


def _query_one(connection: Any, query: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"M4 exact component query returned {len(rows)} rows")
    return rows[0]


def _component_spec(binding: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    components = binding.get("components")
    if not isinstance(components, Mapping) or not isinstance(components.get(name), Mapping):
        raise ValueError(f"M4 binding is missing component: {name}")
    row = components[name]
    if not str(row.get("dataset_id", "")).strip() or not str(row.get("manifest_sha256", "")).strip():
        raise ValueError(f"M4 binding component is not explicitly pinned: {name}")
    return row


def _market_components(connection: Any, binding: Mapping[str, Any]) -> dict[str, ResearchComponent]:
    result: dict[str, ResearchComponent] = {}
    history_spec = _component_spec(binding, "history")
    history = _query_one(
        connection,
        """SELECT business_end,authoritative,simulation_orders_allowed,accepted,
                  global_symbol_count,manifest_sha256
           FROM m2_history_runs WHERE dataset_id=%s AND mode='full' AND shard_index IS NULL""",
        (history_spec["dataset_id"],),
    )
    industry_spec = _component_spec(binding, "industry")
    industry = _query_one(
        connection,
        """SELECT as_of_date,base_history_dataset_id,authoritative,simulation_orders_allowed,
                  accepted,scope_count,manifest_sha256
           FROM m2_industry_runs WHERE dataset_id=%s AND mode='full'""",
        (industry_spec["dataset_id"],),
    )
    daily_spec = _component_spec(binding, "daily")
    daily = _query_one(
        connection,
        """SELECT target_session,base_history_dataset_id,authoritative,simulation_orders_allowed,
                  accepted,expected_symbol_count,primary_row_count,manifest_sha256
           FROM m2_daily_runs WHERE dataset_id=%s""",
        (daily_spec["dataset_id"],),
    )
    index_spec = _component_spec(binding, "index")
    index = _query_one(
        connection,
        """SELECT business_end,authoritative,simulation_orders_allowed,accepted,
                  primary_row_count,manifest_sha256
           FROM m2_index_runs WHERE dataset_id=%s""",
        (index_spec["dataset_id"],),
    )
    flow_spec = _component_spec(binding, "flow")
    flow = _query_one(
        connection,
        """SELECT business_date,expected_symbol_count,available_symbol_count,data_available,
                  authoritative,simulation_orders_allowed,boundary_accepted,manifest_sha256
           FROM m2_flow_runs WHERE dataset_id=%s""",
        (flow_spec["dataset_id"],),
    )
    expected_history_id = str(history_spec["dataset_id"])
    if str(industry[1]) != expected_history_id or str(daily[1]) != expected_history_id:
        raise RuntimeError("M4 market components do not share the pinned history baseline")
    rows = {
        "history": (history_spec, history[0], history[1], history[2], history[3], history[4], history[4], history[5]),
        "industry": (industry_spec, industry[0], industry[2], industry[3], industry[4], industry[5], industry[5], industry[6]),
        "daily": (daily_spec, daily[0], daily[2], daily[3], daily[4], daily[5], daily[6], daily[7]),
        "index": (index_spec, index[0], index[1], index[2], index[3], index[4], index[4], index[5]),
    }
    for name, (spec, through, authoritative, simulation, accepted, expected, available, digest) in rows.items():
        if bool(authoritative) or bool(simulation) or not bool(accepted):
            raise RuntimeError(f"M4 component escaped the accepted research-only boundary: {name}")
        if str(digest) != str(spec["manifest_sha256"]):
            raise RuntimeError(f"M4 component manifest hash mismatch: {name}")
        result[name] = ResearchComponent(
            name=name, dataset_id=str(spec["dataset_id"]), manifest_sha256=str(digest),
            through_date=date.fromisoformat(str(through)), state=ComponentState.ACCEPTED,
            expected_count=int(expected), available_count=int(available),
        )
    if bool(flow[4]) or bool(flow[5]) or not bool(flow[6]):
        raise RuntimeError("M4 flow component escaped the accepted research-only boundary")
    if str(flow[7]) != str(flow_spec["manifest_sha256"]):
        raise RuntimeError("M4 component manifest hash mismatch: flow")
    flow_state = ComponentState.ACCEPTED if bool(flow[3]) else ComponentState.DISABLED_OPTIONAL
    result["flow"] = ResearchComponent(
        name="flow", dataset_id=str(flow_spec["dataset_id"]), manifest_sha256=str(flow[7]),
        through_date=date.fromisoformat(str(flow[0])), state=flow_state,
        expected_count=int(flow[1]), available_count=int(flow[2]) if flow_state is ComponentState.ACCEPTED else 0,
    )
    return result


def _fundamental_component(connection: Any, binding: Mapping[str, Any]) -> ResearchComponent:
    spec = _component_spec(binding, "fundamental")
    row = _query_one(
        connection,
        """SELECT as_of_date,base_history_dataset_id,authoritative,simulation_orders_allowed,
                  accepted,expected_symbol_count,successful_symbol_count,excluded_symbol_count,
                  manifest_sha256,manifest_json
           FROM m2_fundamental_runs WHERE dataset_id=%s AND mode='full'""",
        (spec["dataset_id"],),
    )
    if str(row[1]) != str(_component_spec(binding, "history")["dataset_id"]):
        raise RuntimeError("M4 fundamental component does not share the pinned history baseline")
    stored_manifest = json.loads(str(row[9]))
    gates = stored_manifest.get("gates")
    if (
        stored_manifest.get("dataset_id") != str(spec["dataset_id"])
        or stored_manifest.get("accepted") is not True
        or stored_manifest.get("authoritative") is not False
        or stored_manifest.get("simulation_orders_allowed") is not False
        or not isinstance(gates, list)
        or len(gates) != 11
        or any(gate.get("critical") is not True or gate.get("passed") is not True for gate in gates)
    ):
        raise RuntimeError("M4 fundamental manifest does not prove all eleven critical research gates")
    if bool(row[2]) or bool(row[3]) or not bool(row[4]) or int(stored_manifest["failed_symbol_count"]) != 0:
        raise RuntimeError("M4 fundamental component is not an accepted failure-free research release")
    if str(row[8]) != str(spec["manifest_sha256"]):
        raise RuntimeError("M4 component manifest hash mismatch: fundamental")
    expected = int(row[5])
    resolved = int(row[6]) + int(row[7])
    if (
        int(stored_manifest["expected_symbol_count"]) != expected
        or int(stored_manifest["successful_symbol_count"]) != int(row[6])
        or int(stored_manifest["excluded_symbol_count"]) != int(row[7])
    ):
        raise RuntimeError("M4 fundamental stored row and manifest inventory do not reconcile")
    if expected != 1403 or resolved != expected:
        raise RuntimeError("M4 formal fundamental inventory must resolve all 1,403 frozen symbols")
    return ResearchComponent(
        name="fundamental", dataset_id=str(spec["dataset_id"]), manifest_sha256=str(row[8]),
        through_date=date.fromisoformat(str(row[0])), state=ComponentState.ACCEPTED,
        expected_count=expected, available_count=resolved,
    )


def build_bound_release(
    market_connection: Any, research_connection: Any, binding: Mapping[str, Any]
) -> M4ResearchRelease:
    if str(binding.get("schema_version", "")) != BINDING_SCHEMA_VERSION:
        raise ValueError("unsupported M4 binding schema")
    business_date = date.fromisoformat(str(binding.get("business_date", "")))
    strategy_version = str(binding.get("strategy_version", ""))
    complete_spec = load_complete_strategy_spec()
    if strategy_version != str(complete_spec["strategy_version"]):
        raise RuntimeError("M4 binding strategy version is not the accepted complete contract")
    market = _market_components(market_connection, binding)
    fundamental = _fundamental_component(research_connection, binding)
    components = tuple(
        fundamental if name == "fundamental" else market[name]
        for name in REQUIRED_COMPONENTS
    )
    binding_hash = content_sha256(binding)
    release = M4ResearchRelease(
        release_id=f"m4-release-{business_date.isoformat()}-{binding_hash}",
        business_date=business_date,
        strategy_version=strategy_version,
        components=components,
    )
    if not release.actionable_research_ready:
        raise RuntimeError("M4 bound release is not actionable-research ready")
    return release


def publish_bound_release(connection: Any, release: M4ResearchRelease) -> dict[str, Any]:
    manifest = release.to_mapping()
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with connection.cursor() as cursor:
            cursor.execute(RELEASE_SCHEMA)
            cursor.execute("SELECT manifest_sha256 FROM m4_research_releases WHERE release_id=%s", (release.release_id,))
            existing = cursor.fetchone()
            if existing:
                if str(existing[0]) != release.manifest_sha256:
                    raise RuntimeError("M4 release id already has different content")
                connection.rollback()
                return {"release_id": release.release_id, "idempotent_replay": True}
            cursor.execute(
                """INSERT INTO m4_research_releases VALUES
                (%s,%s,%s,%s,%s,0,0,1,%s,%s,%s,CURRENT_TIMESTAMP(6))""",
                (release.release_id, STORE_SCHEMA_VERSION, release.schema_version,
                 release.business_date.isoformat(), release.strategy_version,
                 release.component("fundamental").dataset_id, release.manifest_sha256, payload),
            )
        connection.commit()
        return {"release_id": release.release_id, "idempotent_replay": False}
    except Exception:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    binding = json.loads(args.binding.read_text(encoding="utf-8"))
    research_config = TiDBConfig.from_env()
    market_config = TiDBConfig.from_env(prefix="TIDB_MARKET")
    if (research_config.host, research_config.port, research_config.user, research_config.database) == (
        market_config.host, market_config.port, market_config.user, market_config.database
    ):
        raise RuntimeError("M4 market read and research write connections must be distinct")
    market_connection = connect(market_config)
    research_connection = connect(research_config)
    try:
        release = build_bound_release(market_connection, research_connection, binding)
        result = publish_bound_release(research_connection, release)
    finally:
        market_connection.close()
        research_connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(release.to_mapping(), ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"event": "m4_research_release_bound", **result}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
