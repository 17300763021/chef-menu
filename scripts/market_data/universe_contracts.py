"""Point-in-time CSI 300/500 universe contracts for M2.2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from scripts.market_data.contracts import normalize_symbol


UNIVERSE_SCHEMA_VERSION = "m2-csi800-pit-universe-v1"
INDEX_SIZES = {"000300": 300, "000905": 500}


@dataclass(frozen=True, slots=True)
class IndexChange:
    index_code: str
    removed: tuple[str, ...]
    added: tuple[str, ...]

    @classmethod
    def build(cls, index_code: str, removed: list[str], added: list[str]) -> "IndexChange":
        if index_code not in INDEX_SIZES:
            raise ValueError(f"unsupported index: {index_code}")
        return cls(
            index_code=index_code,
            removed=tuple(sorted({normalize_symbol(value) for value in removed})),
            added=tuple(sorted({normalize_symbol(value) for value in added})),
        )

    def canonical(self) -> dict[str, object]:
        return {"index_code": self.index_code, "removed": list(self.removed), "added": list(self.added)}


@dataclass(frozen=True, slots=True)
class UniverseEvent:
    notice_id: int
    event_type: str
    announcement_date: date
    effective_session: date
    effective_basis: str
    attachment_url: str
    attachment_sha256: str
    changes: tuple[IndexChange, ...]

    def canonical(self) -> dict[str, object]:
        return {
            "notice_id": self.notice_id,
            "event_type": self.event_type,
            "announcement_date": self.announcement_date.isoformat(),
            "effective_session": self.effective_session.isoformat(),
            "effective_basis": self.effective_basis,
            "attachment_url": self.attachment_url,
            "attachment_sha256": self.attachment_sha256,
            "changes": [change.canonical() for change in sorted(self.changes, key=lambda value: value.index_code)],
        }


@dataclass(frozen=True, slots=True)
class CurrentUniverse:
    as_of_date: date
    members: dict[str, tuple[str, ...]]
    source_urls: dict[str, str]
    source_hashes: dict[str, str]

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": UNIVERSE_SCHEMA_VERSION,
            "as_of_date": self.as_of_date.isoformat(),
            "members": {key: list(self.members[key]) for key in sorted(self.members)},
            "source_urls": dict(sorted(self.source_urls.items())),
            "source_hashes": dict(sorted(self.source_hashes.items())),
        }
