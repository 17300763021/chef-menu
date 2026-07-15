"""Create a tamper-evident online backup of the frozen legacy simulation ledgers."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BUCKET = "legacy-forensic-evidence"
LEGACY_TABLES = (
    "stock_positions",
    "stock_trade_history",
    "stock_auto_trade_orders",
    "stock_portfolio_snapshots",
    "stock_model_positions",
    "stock_model_orders",
    "stock_model_trade_history",
    "stock_model_portfolio_snapshots",
)
WORKFLOW_NAMES = {"Stock Tasks", "Stock Pending Requests", "Deploy GitHub Pages"}


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_env_file() -> dict[str, str]:
    path = ROOT / ".env.local"
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def env_value(name: str, fallback: dict[str, str]) -> str:
    return os.environ.get(name) or fallback.get(name) or ""


class OnlineClient:
    def __init__(self, url: str, service_key: str, github_token: str = "", repository: str = "") -> None:
        if not url or not service_key:
            raise RuntimeError("缺少 VITE_SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY。")
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.github_token = github_token
        self.repository = repository

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str], int]:
        request_headers = dict(headers or {})
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=90) as response:
                return response.read(), dict(response.headers.items()), response.status
        except HTTPError as error:
            payload = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed: {error.code} {payload}") from error

    def _supabase_headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": content_type,
        }

    def count_rows(self, table: str) -> int:
        _, headers, _ = self._request(
            "HEAD",
            f"{self.url}/rest/v1/{table}?select=id",
            headers={**self._supabase_headers(), "Prefer": "count=exact", "Range": "0-0"},
        )
        content_range = headers.get("Content-Range") or headers.get("content-range") or ""
        if "/" not in content_range:
            raise RuntimeError(f"{table} 未返回精确行数：{content_range}")
        return int(content_range.rsplit("/", 1)[1])

    def export_table(self, table: str, page_size: int = 1000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload, _, _ = self._request(
                "GET",
                f"{self.url}/rest/v1/{table}?select=*&order=id.asc&limit={page_size}&offset={offset}",
                headers=self._supabase_headers(),
            )
            batch = json.loads(payload.decode("utf-8"))
            if not isinstance(batch, list):
                raise RuntimeError(f"{table} 导出结果不是列表。")
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            offset += page_size

    def live_schema(self) -> dict[str, Any]:
        payload, _, _ = self._request(
            "POST",
            f"{self.url}/rest/v1/rpc/legacy_evidence_schema_snapshot",
            body=b"{}",
            headers=self._supabase_headers(),
        )
        return json.loads(payload.decode("utf-8"))

    def github_runs_and_logs(self, limit: int) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
        if not self.github_token or not self.repository or limit <= 0:
            raise RuntimeError("云端取证必须提供 GITHUB_TOKEN、GITHUB_REPOSITORY 和正数日志数量。")
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "legacy-evidence-capture",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload, _, _ = self._request(
            "GET",
            f"https://api.github.com/repos/{self.repository}/actions/runs?status=completed&per_page=50",
            headers=headers,
        )
        candidates = json.loads(payload.decode("utf-8")).get("workflow_runs", [])
        selected: list[dict[str, Any]] = []
        files: dict[str, bytes] = {}
        seen_names: set[str] = set()
        for run in candidates:
            name = str(run.get("name") or "")
            if name not in WORKFLOW_NAMES or name in seen_names:
                continue
            run_id = int(run["id"])
            metadata = {
                "id": run_id,
                "name": name,
                "event": run.get("event"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "head_sha": run.get("head_sha"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "html_url": run.get("html_url"),
            }
            log_bytes, _, _ = self._request(
                "GET",
                f"https://api.github.com/repos/{self.repository}/actions/runs/{run_id}/logs",
                headers=headers,
            )
            selected.append(metadata)
            files[f"workflow_logs/{run_id}-{safe_name(name)}.zip"] = log_bytes
            seen_names.add(name)
            if len(selected) >= limit:
                break
        if not selected:
            raise RuntimeError("没有取得任何已完成的 GitHub Actions 日志。")
        return selected, files

    def ensure_private_bucket(self) -> None:
        body = canonical_json({
            "id": BUCKET,
            "name": BUCKET,
            "public": False,
            "file_size_limit": 52428800,
            "allowed_mime_types": ["application/zip"],
        })
        try:
            self._request(
                "POST",
                f"{self.url}/storage/v1/bucket",
                body=body,
                headers=self._supabase_headers(),
            )
        except RuntimeError as error:
            if "409" not in str(error) and "already exists" not in str(error).lower():
                raise

    def upload_archive(self, path: str, archive: bytes) -> None:
        self._request(
            "POST",
            f"{self.url}/storage/v1/object/{BUCKET}/{quote(path, safe='/')}",
            body=archive,
            headers={**self._supabase_headers("application/zip"), "x-upsert": "false"},
        )

    def download_archive(self, path: str) -> bytes:
        payload, _, _ = self._request(
            "GET",
            f"{self.url}/storage/v1/object/{BUCKET}/{quote(path, safe='/')}",
            headers=self._supabase_headers("application/zip"),
        )
        return payload

    def insert_manifest(self, record: dict[str, Any]) -> None:
        self._request(
            "POST",
            f"{self.url}/rest/v1/legacy_evidence_manifests",
            body=canonical_json(record),
            headers={**self._supabase_headers(), "Prefer": "return=minimal"},
        )

    def fetch_manifest(self, evidence_id: str) -> dict[str, Any]:
        payload, _, _ = self._request(
            "GET",
            f"{self.url}/rest/v1/legacy_evidence_manifests?evidence_id=eq.{quote(evidence_id)}&select=*",
            headers=self._supabase_headers(),
        )
        rows = json.loads(payload.decode("utf-8"))
        if len(rows) != 1:
            raise RuntimeError(f"证据清单回读失败：{evidence_id}")
        return rows[0]


def safe_name(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def source_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def collect_repository_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for base in (ROOT / "supabase" / "migrations", ROOT / ".github" / "workflows"):
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    return files


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, files[name])
    return output.getvalue()


def current_account_snapshot(exports: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    def latest(table: str, field: str) -> dict[str, Any] | None:
        rows = exports[table]
        return max(rows, key=lambda row: str(row.get(field) or "")) if rows else None

    return {
        "legacy_open_positions": [row for row in exports["stock_positions"] if str(row.get("status") or "open").lower() == "open"],
        "legacy_latest_snapshot": latest("stock_portfolio_snapshots", "snapshot_time"),
        "model_open_positions": [row for row in exports["stock_model_positions"] if str(row.get("status") or "open").lower() == "open"],
        "model_latest_snapshot": latest("stock_model_portfolio_snapshots", "snapshot_time"),
    }


def build_evidence(client: OnlineClient, github_log_runs: int) -> tuple[bytes, dict[str, Any]]:
    exported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    exports: dict[str, list[dict[str, Any]]] = {}
    table_counts: dict[str, int] = {}
    table_hashes: dict[str, str] = {}
    files = collect_repository_files()

    for table in LEGACY_TABLES:
        count_before = client.count_rows(table)
        rows = client.export_table(table)
        count_after = client.count_rows(table)
        if count_before != len(rows) or count_after != len(rows):
            raise RuntimeError(f"{table} 行数不一致：before={count_before}, export={len(rows)}, after={count_after}")
        data = canonical_json(rows)
        exports[table] = rows
        table_counts[table] = len(rows)
        table_hashes[table] = sha256(data)
        files[f"tables/{table}.json"] = data

    files["schema/live_schema.json"] = canonical_json(client.live_schema())
    files["account_snapshots/current.json"] = canonical_json(current_account_snapshot(exports))
    workflow_runs, log_files = client.github_runs_and_logs(github_log_runs)
    files.update(log_files)
    file_hashes = {name: sha256(data) for name, data in sorted(files.items())}
    internal_manifest = {
        "format_version": 1,
        "exported_at": exported_at,
        "source_commit": source_commit(),
        "legacy_tables": list(LEGACY_TABLES),
        "table_counts": table_counts,
        "table_hashes": table_hashes,
        "file_hashes": file_hashes,
        "workflow_runs": workflow_runs,
        "immutability_boundary": "Supabase Storage has no object lock/versioning; integrity is anchored by an append-only database manifest and Git source commit.",
    }
    files["manifest.json"] = canonical_json(internal_manifest)
    archive = deterministic_zip(files)
    manifest = {
        **internal_manifest,
        "manifest_sha256": sha256(files["manifest.json"]),
        "archive_sha256": sha256(archive),
        "archive_size_bytes": len(archive),
    }
    return archive, manifest


def publish_evidence(client: OnlineClient, archive: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    digest = manifest["archive_sha256"]
    timestamp = manifest["exported_at"].replace(":", "").replace("-", "").replace(".", "")
    evidence_id = f"legacy-{timestamp}-{digest[:12]}"
    storage_path = f"sha256/{digest[:2]}/{digest}.zip"
    client.ensure_private_bucket()
    client.upload_archive(storage_path, archive)
    downloaded = client.download_archive(storage_path)
    if sha256(downloaded) != digest:
        raise RuntimeError("上传后的在线归档 SHA-256 复核失败。")
    record = {
        "evidence_id": evidence_id,
        "source_commit": manifest["source_commit"],
        "exported_at": manifest["exported_at"],
        "archive_sha256": digest,
        "archive_size_bytes": manifest["archive_size_bytes"],
        "storage_bucket": BUCKET,
        "storage_path": storage_path,
        "table_counts": manifest["table_counts"],
        "table_hashes": manifest["table_hashes"],
        "file_hashes": manifest["file_hashes"],
        "workflow_runs": manifest["workflow_runs"],
        "manifest": manifest,
        "verification_status": "verified",
    }
    client.insert_manifest(record)
    online = client.fetch_manifest(evidence_id)
    if online.get("archive_sha256") != digest or online.get("table_counts") != manifest["table_counts"]:
        raise RuntimeError("在线证据清单回读复核失败。")
    return online


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture frozen legacy ledger evidence")
    parser.add_argument("--github-log-runs", type=int, default=3)
    parser.add_argument("--manifest-out", type=Path, default=Path("legacy-evidence-manifest.json"))
    args = parser.parse_args()
    env = read_env_file()
    client = OnlineClient(
        env_value("VITE_SUPABASE_URL", env),
        env_value("SUPABASE_SERVICE_ROLE_KEY", env),
        env_value("GITHUB_TOKEN", env),
        env_value("GITHUB_REPOSITORY", env) or "17300763021/chef-menu",
    )
    archive, manifest = build_evidence(client, args.github_log_runs)
    online = publish_evidence(client, archive, manifest)
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_bytes(canonical_json({key: value for key, value in online.items() if key != "manifest"} | {"manifest": manifest}))
    print(json.dumps({
        "evidence_id": online["evidence_id"],
        "archive_sha256": online["archive_sha256"],
        "archive_size_bytes": online["archive_size_bytes"],
        "table_counts": online["table_counts"],
        "storage_path": online["storage_path"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
