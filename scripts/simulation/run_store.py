"""Single-RPC publication boundary for reconciled V2 simulation packages."""

from __future__ import annotations

from typing import Any, Protocol

from .contracts import SimulationPackage, canonicalize, stable_id
from .reconciliation import reconcile


class RpcClient(Protocol):
    def rpc(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...


def publication_payload(package: SimulationPackage) -> dict[str, Any]:
    reconciliation = reconcile(package)
    manifest = package.manifest(reconciliation)
    manifest["manifest_sha256"] = stable_id("sha256", manifest).removeprefix("sha256-")
    return {
        "p_manifest": canonicalize(manifest),
        "p_opening_positions": canonicalize(package.opening_positions),
        "p_instructions": canonicalize(package.instructions),
        "p_orders": canonicalize(package.orders),
        "p_fills": canonicalize(package.fills),
        "p_cash_entries": canonicalize(package.cash_entries),
        "p_positions": canonicalize(package.closing_positions),
        "p_evaluations": canonicalize(package.evaluations),
    }


class SimulationRunStore:
    def __init__(self, client: RpcClient) -> None:
        self.client = client

    def publish(self, package: SimulationPackage) -> dict[str, Any]:
        payload = publication_payload(package)
        result = self.client.rpc("publish_v2_simulation_run", payload)
        if not isinstance(result, dict) or str(result.get("run_id", "")) != package.run_id:
            raise RuntimeError("simulation publication returned an invalid run identity")
        return result
