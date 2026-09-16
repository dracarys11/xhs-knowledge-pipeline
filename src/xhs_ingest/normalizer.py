"""Normalizer: converts raw acquisition responses into CanonicalPost."""

import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ParseFailedError
from .models import Author, CanonicalPost, MediaItem, Stats, get_current_iso_time


def _parse_count(value: Any) -> int | None:
    """Parse interaction counts that might be int, string, or Chinese units (e.g. '1.2万')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        val_str = value.strip().replace("+", "")
        if not val_str:
            return None
        try:
            if "万" in val_str:
                num = float(val_str.replace("万", ""))
                return int(num * 10000)
            if "w" in val_str.lower():
                num = float(val_str.lower().replace("w", ""))
                return int(num * 10000)
            return int(float(val_str))
        except (ValueError, TypeError):
            return None
    return None


def _clean_url(url: str) -> str:
    """Ensure URL has scheme and strip fragments."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def _extract_extension(url: str, default: str = ".jpg") -> str:
    """Extract clean file extension from URL."""
    path = urlparse(url).path
    match = re.search(r"\.(jpg|jpeg|png|webp|gif|mp4|mov|webm)$", path, re.IGNORECASE)
    if match:
        ext = match.group(0).lower()
        if ext == ".jpeg":
            ext = ".jpg"
        return ext
    return default


def _extract_inner_note(raw_data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Locates the inner note data and returns (note_data, full_raw).
    Handles API responses, SSR state maps, and raw note cards.
    """
    raw_dict = dict(raw_data)

    # If wrapped in API response { "data": { "items": [ { "note_card": ... } ] } }
    if "data" in raw_dict and isinstance(raw_dict["data"], dict):
        data = raw_dict["data"]
        if "items" in data and isinstance(data["items"], list) and data["items"]:
            first_item = data["items"][0]
            if isinstance(first_item, dict):
                if "note_card" in first_item and isinstance(first_item["note_card"], dict):
                    return first_item["note_card"], raw_dict
                if "noteCard" in first_item and isinstance(first_item["noteCard"], dict):
                    return first_item["noteCard"], raw_dict
        if "note_card" in data and isinstance(data["note_card"], dict):
            return data["note_card"], raw_dict

    # If wrapped in SSR state { "note": { "noteDetailMap": { "<id>": { "note": ... } } } }
    if "note" in raw_dict and isinstance(raw_dict["note"], dict):
        note_section = raw_dict["note"]
        if "noteDetailMap" in note_section and isinstance(note_section["noteDetailMap"], dict):
            detail_map = note_section["noteDetailMap"]
            for _, item in detail_map.items():
                if isinstance(item, dict) and "note" in item and isinstance(item["note"], dict):
                    return item["note"], raw_dict
                if isinstance(item, dict) and ("title" in item or "desc" in item or "noteId" in item):
                    return item, raw_dict
        if "title" in note_section or "desc" in note_section or "noteId" in note_section:
            return note_section, raw_dict

    # If wrapped in { "note_card": ... }
    if "note_card" in raw_dict and isinstance(raw_dict["note_card"], dict):
        return raw_dict["note_card"], raw_dict
    if "noteCard" in raw_dict and isinstance(raw_dict["noteCard"], dict):
        return raw_dict["noteCard"], raw_dict

    # Otherwise assume raw_dict itself is the note card
    return raw_dict, raw_dict


def normalize_note(
    raw_data: Mapping[str, Any],
    source_url: str | None = None,
    collector_name: str = "xhs_playwright_collector",
) -> CanonicalPost:
    """
    Normalizes a raw XHS note payload into a CanonicalPost.
    Preserves raw_data completely.
    Raises ParseFailedError if note identity is missing.
    """
    if not raw_data:
        raise ParseFailedError("Empty raw payload provided to normalizer")

    note, full_raw = _extract_inner_note(raw_data)

    # Note ID resolution
    note_id = (
        note.get("note_id")
        or note.get("noteId")
        or note.get("id")
        or full_raw.get("note_id")
        or full_raw.get("noteId")
    )
    if not note_id:
        raise ParseFailedError(
            "Could not find note_id in raw payload",
            details={"raw_keys": list(raw_data.keys())},
        )
    note_id = str(note_id)

    # Canonical source URL
    if not source_url:
        xsec_token = note.get("xsec_token") or note.get("xsecToken")
        if xsec_token:
            source_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_fav"
        else:
            source_url = f"https://www.xiaohongshu.com/explore/{note_id}"

    # Author
    user = note.get("user") or {}
    author_id = str(user.get("user_id") or user.get("userId") or user.get("id") or "")
    author_name = str(user.get("nickname") or user.get("nickName") or user.get("name") or "Unknown Author")
    author = Author(id=author_id, name=author_name)

    # Content
    title = str(note.get("title") or "")
    text = str(note.get("desc") or note.get("content") or "")
    content = {
        "title": title,
        "text": text,
    }

    # Stats
    interact_info = note.get("interact_info") or note.get("interactInfo") or {}
    stats = Stats(
        liked_count=_parse_count(interact_info.get("liked_count") or interact_info.get("likedCount") or note.get("liked_count")),
        collected_count=_parse_count(interact_info.get("collected_count") or interact_info.get("collectedCount") or note.get("collected_count")),
        comment_count=_parse_count(interact_info.get("comment_count") or interact_info.get("commentCount") or note.get("comment_count")),
        share_count=_parse_count(interact_info.get("share_count") or interact_info.get("shareCount") or note.get("share_count")),
    )

    # Media items
    media_items: list[MediaItem] = []
    seen_urls: set[str] = set()

    # Images
    image_list = note.get("image_list") or note.get("imageList") or []
    img_idx = 1
    for img in image_list:
        if not isinstance(img, dict):
            continue
        # Extract best URL
        img_url = (
            img.get("url_default")
            or img.get("urlDefault")
            or img.get("url")
            or img.get("url_pre")
            or img.get("urlPre")
        )
        if not img_url and "info_list" in img and isinstance(img["info_list"], list) and img["info_list"]:
            # Pick highest resolution or last in info_list
            img_url = img["info_list"][-1].get("url")
        if not img_url and "infoList" in img and isinstance(img["infoList"], list) and img["infoList"]:
            img_url = img["infoList"][-1].get("url")

        if img_url:
            clean_url = _clean_url(img_url)
            if clean_url and clean_url not in seen_urls:
                seen_urls.add(clean_url)
                ext = _extract_extension(clean_url, default=".jpg")
                filename = f"image_{img_idx:02d}{ext}"
                media_items.append(
                    MediaItem(
                        type="image",
                        url=clean_url,
                        filename=filename,
                    )
                )
                img_idx += 1

    # Video
    video = note.get("video") or {}
    if isinstance(video, dict):
        media_sub = video.get("media") or {}
        stream = media_sub.get("stream") if isinstance(media_sub, dict) else {}
        if isinstance(stream, dict):
            # Prefer h264 or h265 master_url / masterUrl
            video_url = None
            for codec in ["h264", "h265", "av1"]:
                streams = stream.get(codec)
                if isinstance(streams, list) and streams:
                    for s in streams:
                        candidate = s.get("master_url") or s.get("masterUrl")
                        if candidate:
                            video_url = candidate
                            break
                if video_url:
                    break

            if video_url:
                clean_video_url = _clean_url(video_url)
                if clean_video_url and clean_video_url not in seen_urls:
                    seen_urls.add(clean_video_url)
                    ext = _extract_extension(clean_video_url, default=".mp4")
                    filename = f"video_01{ext}"
                    media_items.append(
                        MediaItem(
                            type="video",
                            url=clean_video_url,
                            filename=filename,
                        )
                    )

    return CanonicalPost(
        platform="xhs",
        note_id=note_id,
        source_url=source_url,
        collector=collector_name,
        collected_at=get_current_iso_time(),
        author=author,
        content=content,
        media=media_items,
        stats=stats,
        raw=dict(full_raw),
    )
