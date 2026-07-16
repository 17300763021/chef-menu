"""M1 cloud runtime heartbeat, idempotency, recovery, and quota controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUNTIME_VERSION = "m1-cloud-runtime-v1"
QUOTA_PROVIDERS = ("github_actions_internal", "supabase_internal")


def read_env_file(path: Path = ROOT / ".env.local") -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_value(name: str, values: dict[str, str]) -> str:
    return os.environ.get(name, "").strip() or values.get(name, "").strip()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def idempotency_key(payload: dict[str, Any]) -> str:
    stable = {
        key: payload[key]
        for key in ("environment", "business_date", "job_type", "run_slot", "source_commit")
    }
    return f"cloud-job-{hashlib.sha256(canonical_json(stable)).hexdigest()}"


class CloudRuntimeClient:
    def __init__(self, url: str, service_key: str, attempts: int = 3) -> None:
        if not url or not service_key:
            raise RuntimeError("缺少 VITE_SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY。")
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.attempts = attempts

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        data = canonical_json(body) if body is not None else None
        headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request = Request(f"{self.url}/rest/v1/{path}", data=data, headers=headers, method=method)
            try:
                with urlopen(request, timeout=30) as response:
                    content = response.read()
                    return json.loads(content.decode("utf-8")) if content else None
            except HTTPError as error:
                message = error.read().decode("utf-8", errors="replace")
                if 400 <= error.code < 500:
                    raise RuntimeError(f"Supabase {error.code}: {message}") from error
                last_error = RuntimeError(f"Supabase {error.code}: {message}")
            except (URLError, TimeoutError) as error:
                last_error = error
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"Supabase request failed after {self.attempts} attempts: {last_error}")

    def rpc(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", f"rpc/{name}", payload)
        if not isinstance(result, dict):
            raise RuntimeError(f"RPC {name} returned an invalid response")
        return result

    def rows(self, table: str, query: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"{table}?{query}")
        if not isinstance(result, list):
            raise RuntimeError(f"Query {table} returned an invalid response")
        return result


def make_client() -> CloudRuntimeClient:
    values = read_env_file()
    return CloudRuntimeClient(
        env_value("VITE_SUPABASE_URL", values),
        env_value("SUPABASE_SERVICE_ROLE_KEY", values),
    )


def source_commit() -> str:
    return os.environ.get("GITHUB_SHA", "").strip() or os.environ.get("SOURCE_COMMIT", "").strip() or "local-development"


def build_claim(
    environment: str,
    job_type: str,
    run_slot: str,
    commit: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "environment": environment,
        "business_date": datetime.now(SHANGHAI).date().isoformat(),
        "job_type": job_type,
        "run_slot": run_slot,
        "source_commit": commit,
        "metadata": {"runtime_version": RUNTIME_VERSION, **(metadata or {})},
    }
    payload["idempotency_key"] = idempotency_key(payload)
    return payload


def complete_claim(client: CloudRuntimeClient, payload: dict[str, Any]) -> dict[str, Any]:
    claim = client.rpc("claim_cloud_job", {"p_payload": payload})
    if not claim.get("allowed"):
        return claim
    if claim.get("status") == "succeeded":
        return claim
    run_id = str(claim["run_id"])
    client.rpc("heartbeat_cloud_job", {"p_run_id": run_id, "p_metadata": {"phase": "active"}})
    finish = client.rpc(
        "finish_cloud_job",
        {
            "p_run_id": run_id,
            "p_status": "succeeded",
            "p_result_published": False,
            "p_error_message": "",
            "p_metadata": {"phase": "complete"},
        },
    )
    return {**claim, "finish": finish}


def current_half_hour_slot(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minute = 0 if current.minute < 30 else 30
    return current.replace(minute=minute, second=0, microsecond=0).strftime("%Y%m%dT%H%MZ")


def run_heartbeat(client: CloudRuntimeClient, slot: str | None = None) -> dict[str, Any]:
    payload = build_claim("shadow", "foundation_heartbeat", slot or current_half_hour_slot(), source_commit())
    result = complete_claim(client, payload)
    if not result.get("allowed"):
        raise RuntimeError(f"云端心跳被配额闸门阻止：{result.get('quota_decision')}")
    return result


def process_one_recovery(client: CloudRuntimeClient) -> dict[str, Any]:
    recovery = client.rpc("claim_cloud_recovery", {"p_claimed_by": f"github:{source_commit()}"})
    if not recovery.get("found"):
        return recovery
    recovery_id = str(recovery["recovery_id"])
    try:
        if recovery["job_type"] != "foundation_heartbeat":
            raise RuntimeError(f"尚未实现恢复处理器：{recovery['job_type']}")
        payload = build_claim(
            str(recovery["environment"]),
            str(recovery["job_type"]),
            f"recovery-{recovery_id}",
            source_commit(),
            {"recovery_id": recovery_id, "reason": recovery.get("reason", "")},
        )
        run = complete_claim(client, payload)
        if not run.get("allowed"):
            raise RuntimeError(f"恢复任务被配额闸门阻止：{run.get('quota_decision')}")
        client.rpc(
            "complete_cloud_recovery",
            {
                "p_recovery_id": recovery_id,
                "p_status": "completed",
                "p_source_run_id": run["run_id"],
                "p_error_message": "",
            },
        )
        return {**recovery, "completed": True, "run_id": run["run_id"]}
    except Exception as reason:
        client.rpc(
            "complete_cloud_recovery",
            {
                "p_recovery_id": recovery_id,
                "p_status": "failed",
                "p_source_run_id": None,
                "p_error_message": str(reason),
            },
        )
        raise


def set_all_quotas(client: CloudRuntimeClient, percent: int, hard_stop: bool = False) -> None:
    for provider in QUOTA_PROVIDERS:
        client.rpc(
            "set_cloud_quota_for_acceptance",
            {"p_provider": provider, "p_reported_percent": percent, "p_hard_stop": hard_stop},
        )


def run_acceptance(client: CloudRuntimeClient) -> dict[str, Any]:
    commit = source_commit()
    report: dict[str, Any] = {"runtime_version": RUNTIME_VERSION, "source_commit": commit}
    try:
        set_all_quotas(client, 0)

        duplicate_payload = build_claim(
            "development",
            "foundation_acceptance_nonessential",
            "duplicate-claim-v1",
            commit,
            {"acceptance": "ten_duplicate_claims"},
        )
        duplicate_results = [client.rpc("claim_cloud_job", {"p_payload": duplicate_payload}) for _ in range(10)]
        run_ids = {str(row["run_id"]) for row in duplicate_results}
        if len(run_ids) != 1:
            raise AssertionError(f"十次重复领取产生了 {len(run_ids)} 个运行结果")
        rows = client.rows(
            "cloud_job_runs",
            f"select=run_id&idempotency_key=eq.{quote(duplicate_payload['idempotency_key'], safe='')}",
        )
        if len(rows) != 1:
            raise AssertionError(f"幂等键对应 {len(rows)} 行，不是 1 行")
        complete_claim(client, duplicate_payload)
        report["duplicate_claims"] = {"attempts": 10, "unique_run_ids": 1, "database_rows": 1}

        quota_cases = []
        for percent, job_type, expected_allowed, expected_decision in (
            (79, "foundation_acceptance_nonessential", True, "normal"),
            (80, "foundation_acceptance_nonessential", False, "degraded_80"),
            (90, "foundation_heartbeat", True, "critical_only_90"),
            (100, "foundation_heartbeat", False, "blocked_100"),
        ):
            set_all_quotas(client, percent)
            payload = build_claim("development" if "nonessential" in job_type else "shadow", job_type, f"quota-{percent}-v1", commit)
            result = client.rpc("claim_cloud_job", {"p_payload": payload})
            if bool(result.get("allowed")) is not expected_allowed or result.get("quota_decision") != expected_decision:
                raise AssertionError(f"配额 {percent}% 行为错误：{result}")
            if expected_allowed:
                complete_claim(client, payload)
            quota_cases.append({"percent": percent, "allowed": result.get("allowed"), "decision": result.get("quota_decision")})
        report["quota_cases"] = quota_cases
    finally:
        set_all_quotas(client, 0)

    reference_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    monitor = client.rpc("monitor_cloud_job_health", {"p_reference_time": reference_time})
    recovery = process_one_recovery(client)
    report["recovery"] = {"monitor": monitor, "claim": recovery}
    report["quota_reset"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 cloud runtime")
    parser.add_argument("--mode", choices=["scheduled", "heartbeat", "recover", "acceptance"], default="scheduled")
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()
    client = make_client()
    if args.mode == "heartbeat":
        result: Any = {"heartbeat": run_heartbeat(client)}
    elif args.mode == "recover":
        result = {"recovery": process_one_recovery(client)}
    elif args.mode == "acceptance":
        result = run_acceptance(client)
    else:
        result = {"recovery": process_one_recovery(client), "heartbeat": run_heartbeat(client)}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
