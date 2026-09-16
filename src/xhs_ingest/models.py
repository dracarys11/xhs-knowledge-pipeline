"""Data models for XHS acquisition and canonical representation."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def get_current_iso_time() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FavoriteRef:
    """Reference to an item listed in favorites."""
    note_id: str
    source_url: str
    title: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    xsec_token: str | None = None
    cover_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageResult:
    """Result of listing a page of favorites."""
    items: list[FavoriteRef]
    next_cursor: str | None = None
    has_more: bool | None = None
    completion_proof: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "completion_proof": self.completion_proof,
            "raw": self.raw,
        }


@dataclass
class MediaItem:
    """Represents a media asset (image or video) associated with a note."""
    type: str  # "image" or "video"
    url: str
    filename: str
    local_path: str | None = None
    download_status: str = "PENDING"  # "PENDING", "COMPLETED", "FAILED"
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Author:
    id: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Stats:
    liked_count: int | None = None
    collected_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CanonicalPost:
    """Minimal Canonical Schema for a single note."""
    platform: str
    note_id: str
    source_url: str
    collector: str
    collected_at: str
    author: Author
    content: dict[str, str]  # {"title": ..., "text": ...}
    media: list[MediaItem]
    stats: Stats
    raw: dict[str, Any]

    def to_dict(self, include_raw: bool = True) -> dict[str, Any]:
        data = {
            "platform": self.platform,
            "note_id": self.note_id,
            "source_url": self.source_url,
            "collector": self.collector,
            "collected_at": self.collected_at,
            "author": self.author.to_dict(),
            "content": self.content,
            "media": [m.to_dict() for m in self.media],
            "stats": self.stats.to_dict(),
        }
        if include_raw:
            data["raw"] = self.raw
        return data

    @property
    def is_media_complete(self) -> bool:
        if not self.media:
            return True
        return all(m.download_status == "COMPLETED" for m in self.media)
