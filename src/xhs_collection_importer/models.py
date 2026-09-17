"""Data models for collection discovery (P2.2 Phase 1)."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BoardMeta:
    collection_id: str
    name: str
    desc: str
    privacy: int
    reported_notes_count: int
    source: str
    captured_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectionSnapshot:
    snapshot_id: str
    captured_at: str
    source_user_id: str
    source: str
    total_reported: int
    boards_count: int
    collections: list[BoardMeta] = field(default_factory=list)
    board_listing_status: str = "UNVERIFIED"
    member_relationship_status: str = "NOT_STARTED"
    publication_status: str = "NOT_PUBLISHED"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["collections"] = [b.to_dict() if isinstance(b, BoardMeta) else b for b in self.collections]
        return data
