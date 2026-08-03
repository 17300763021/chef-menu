"""Bind accepted M2 components into one immutable, research-only release."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect


RELEASE_SCHEMA_VERSION = "m2-data-release-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS m2_data_releases (
  release_id VARCHAR(160) NOT NULL PRIMARY KEY,
  schema_version VARCHAR(64) NOT NULL,
  business_date DATE NOT NULL,
  history_dataset_id VARCHAR(160) NOT NULL,
  industry_dataset_id VARCHAR(160) NOT NULL,
  fundamental_dataset_id VARCHAR(160) NOT NULL,
  index_dataset_id VARCHAR(160) NOT NULL,
  daily_dataset_id VARCHAR(160) NOT NULL,
  flow_dataset_id VARCHAR(160) NOT NULL,
  flow_data_available TINYINT NOT NULL,
  authoritative TINYINT NOT NULL,
  simulation_orders_allowed TINYINT NOT NULL,
  accepted TINYINT NOT NULL,
  manifest_sha256 CHAR(64) NOT NULL,
  manifest_json LONGTEXT NOT NULL,
  published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_m2_release_business_date (business_date)
)
"""


def _one(cursor: Any, query: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    cursor.execute(query, params)
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"M2 release prerequisite query returned {len(rows)} rows")
    return rows[0]


def build_release(connection: Any, business_date: date) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(SCHEMA)
        history = _one(cursor, """SELECT dataset_id,business_end,authoritative,simulation_orders_allowed,manifest_sha256
            FROM m2_history_runs WHERE mode='full' AND shard_index IS NULL AND accepted=1
            ORDER BY business_end DESC LIMIT 1""")
        industry = _one(cursor, """SELECT dataset_id,as_of_date,base_history_dataset_id,authoritative,simulation_orders_allowed,manifest_sha256
            FROM m2_industry_runs WHERE mode='full' AND accepted=1 ORDER BY as_of_date DESC LIMIT 1""")
        fundamental = _one(cursor, """SELECT dataset_id,as_of_date,base_history_dataset_id,authoritative,simulation_orders_allowed,manifest_sha256
            FROM m2_fundamental_runs WHERE mode='full' AND accepted=1 ORDER BY as_of_date DESC LIMIT 1""")
        index = _one(cursor, """SELECT dataset_id,business_end,authoritative,simulation_orders_allowed,manifest_sha256
            FROM m2_index_runs WHERE accepted=1 ORDER BY business_end DESC LIMIT 1""")
        daily = _one(cursor, """SELECT dataset_id,target_session,base_history_dataset_id,authoritative,simulation_orders_allowed,manifest_sha256
            FROM m2_daily_runs WHERE accepted=1 ORDER BY target_session DESC LIMIT 1""")
        flow = _one(cursor, """SELECT dataset_id,business_date,data_available,authoritative,simulation_orders_allowed,manifest_sha256
            FROM m2_flow_runs WHERE boundary_accepted=1 ORDER BY business_date DESC, published_at DESC LIMIT 1""")
    component_rows = {
        "history": {"dataset_id": str(history[0]), "through": str(history[1]), "manifest_sha256": str(history[4])},
        "industry": {"dataset_id": str(industry[0]), "through": str(industry[1]), "manifest_sha256": str(industry[5])},
        "fundamental": {"dataset_id": str(fundamental[0]), "through": str(fundamental[1]), "manifest_sha256": str(fundamental[5])},
        "index": {"dataset_id": str(index[0]), "through": str(index[1]), "manifest_sha256": str(index[4])},
        "daily": {"dataset_id": str(daily[0]), "through": str(daily[1]), "manifest_sha256": str(daily[5])},
        "flow": {"dataset_id": str(flow[0]), "through": str(flow[1]), "data_available": bool(flow[2]), "manifest_sha256": str(flow[5])},
    }
    unsafe = []
    if bool(history[2]) or bool(history[3]):
        unsafe.append("history")
    if bool(industry[3]) or bool(industry[4]):
        unsafe.append("industry")
    if bool(fundamental[3]) or bool(fundamental[4]):
        unsafe.append("fundamental")
    if bool(index[2]) or bool(index[3]):
        unsafe.append("index")
    if bool(daily[3]) or bool(daily[4]):
        unsafe.append("daily")
    if bool(flow[3]) or bool(flow[4]):
        unsafe.append("flow")
    if unsafe:
        raise RuntimeError(f"M2 release component escaped research-only boundary: {unsafe}")
    history_id = str(history[0])
    lineage = {"industry": str(industry[2]), "fundamental": str(fundamental[2]), "daily": str(daily[2])}
    mismatched = sorted(name for name, base_id in lineage.items() if base_id != history_id)
    if mismatched:
        raise RuntimeError(f"M2 release components do not share one history baseline: {mismatched}")
    if date.fromisoformat(str(industry[1])) < business_date or date.fromisoformat(str(fundamental[1])) < business_date:
        raise RuntimeError("industry or fundamental knowledge boundary is stale for requested release")
    if date.fromisoformat(str(index[1])) < business_date or date.fromisoformat(str(daily[1])) < business_date:
        raise RuntimeError("index or daily market lineage is stale for requested release")
    manifest = {
        "manifest_version": "m2-data-release-manifest-v1",
        "schema_version": RELEASE_SCHEMA_VERSION,
        "business_date": business_date.isoformat(),
        "authoritative": False,
        "simulation_orders_allowed": False,
        "accepted": True,
        "components": component_rows,
        "flow_policy": "optional verified enhancement; unavailable flow disables the factor and cannot be represented as zero or neutral data",
    }
    manifest["release_id"] = f"m2-release-{business_date.isoformat()}-{sha256(manifest)}"
    return manifest


def publish_release(connection: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    release_id = str(manifest["release_id"])
    digest = sha256(manifest)
    components = manifest["components"]
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT manifest_sha256 FROM m2_data_releases WHERE release_id=%s", (release_id,))
            existing = cursor.fetchone()
            if existing:
                if str(existing[0]) != digest:
                    raise RuntimeError("M2 release id already has different content")
                connection.rollback()
                return {"release_id": release_id, "idempotent_replay": True}
            cursor.execute(
                """INSERT INTO m2_data_releases VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,1,%s,%s,CURRENT_TIMESTAMP(6))""",
                (release_id, manifest["schema_version"], manifest["business_date"], components["history"]["dataset_id"],
                 components["industry"]["dataset_id"], components["fundamental"]["dataset_id"],
                 components["index"]["dataset_id"], components["daily"]["dataset_id"], components["flow"]["dataset_id"],
                 int(bool(components["flow"]["data_available"])), digest,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
        connection.commit()
        return {"release_id": release_id, "idempotent_replay": False}
    except Exception:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("m2-release"))
    args = parser.parse_args()
    connection = connect(TiDBConfig.from_env())
    try:
        manifest = build_release(connection, args.business_date)
        result = publish_release(connection, manifest)
    finally:
        connection.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"event": "m2_release_accepted", **result}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
