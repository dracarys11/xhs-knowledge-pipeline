"""Media downloader: downloads images and videos with atomic rename and fail-closed semantics."""

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

import httpx

from .errors import MediaDownloadError
from .models import CanonicalPost, MediaItem

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_REFERER = "https://www.xiaohongshu.com/"


def download_media_item(
    item: MediaItem,
    assets_dir: Path,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> MediaItem:
    """
    Downloads a single media asset to assets_dir atomically.
    Writes to .<filename>.tmp first, validates non-zero size, and renames.
    Updates item download_status, size_bytes, checksum_sha256, and local_path.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    target_path = assets_dir / item.filename
    temp_path = assets_dir / f".{item.filename}.tmp"

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": DEFAULT_REFERER,
        "Accept": "*/*",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
        close_client = True

    try:
        with client.stream("GET", item.url, headers=headers) as response:
            if response.status_code != 200:
                raise MediaDownloadError(
                    f"HTTP error {response.status_code} downloading {item.url}",
                    details={"status_code": response.status_code, "url": item.url},
                )

            hasher = hashlib.sha256()
            total_bytes = 0

            with open(temp_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        hasher.update(chunk)
                        total_bytes += len(chunk)

            if total_bytes == 0:
                raise MediaDownloadError(
                    f"Downloaded 0 bytes for {item.url}",
                    details={"url": item.url},
                )

            # Atomic rename
            temp_path.replace(target_path)

            item.local_path = str(target_path)
            item.download_status = "COMPLETED"
            item.size_bytes = total_bytes
            item.checksum_sha256 = hasher.hexdigest()
            item.error = None
            return item

    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        item.download_status = "FAILED"
        item.error = str(exc)
        if isinstance(exc, MediaDownloadError):
            raise
        raise MediaDownloadError(
            f"Failed downloading {item.filename} from {item.url}: {exc}",
            details={"filename": item.filename, "url": item.url, "error": str(exc)},
        ) from exc

    finally:
        if close_client:
            client.close()


def download_post_media(
    post: CanonicalPost,
    assets_dir: Path,
    raise_on_failure: bool = True,
) -> bool:
    """
    Downloads all media assets for a note into assets_dir.
    If raise_on_failure is True, raises MediaDownloadError on any failure.
    Returns True only if all media items are successfully downloaded.
    """
    if not post.media:
        return True

    assets_dir.mkdir(parents=True, exist_ok=True)
    failed_items: list[dict[str, Any]] = []

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for item in post.media:
            try:
                download_media_item(item, assets_dir, client=client)
            except Exception as exc:
                failed_items.append({
                    "filename": item.filename,
                    "url": item.url,
                    "error": str(exc),
                })

    if failed_items:
        if raise_on_failure:
            raise MediaDownloadError(
                f"{len(failed_items)}/{len(post.media)} media asset(s) failed to download",
                details={"failures": failed_items},
            )
        return False

    return True
