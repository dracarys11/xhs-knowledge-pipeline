"""Tests for media downloader: atomic write, duplicate handling, and fail-closed behavior."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xhs_ingest.errors import MediaDownloadError
from xhs_ingest.media import download_media_item, download_post_media
from xhs_ingest.models import Author, CanonicalPost, MediaItem, Stats


def test_download_media_item_success(tmp_path: Path):
    item = MediaItem(type="image", url="https://example.com/test.jpg", filename="image_01.jpg")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_bytes.return_value = [b"mock image chunk 1", b"mock image chunk 2"]
    mock_response.__enter__.return_value = mock_response

    mock_client = MagicMock()
    mock_client.stream.return_value = mock_response

    download_media_item(item, tmp_path, client=mock_client)

    target_file = tmp_path / "image_01.jpg"
    assert target_file.exists()
    assert target_file.read_bytes() == b"mock image chunk 1mock image chunk 2"
    assert item.download_status == "COMPLETED"
    assert item.size_bytes == len(b"mock image chunk 1mock image chunk 2")
    assert item.checksum_sha256 is not None
    # Ensure temporary file is cleaned up
    assert not (tmp_path / ".image_01.jpg.tmp").exists()


def test_download_media_item_fail_closed_on_http_error(tmp_path: Path):
    item = MediaItem(type="image", url="https://example.com/not_found.jpg", filename="image_01.jpg")

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.__enter__.return_value = mock_response

    mock_client = MagicMock()
    mock_client.stream.return_value = mock_response

    with pytest.raises(MediaDownloadError):
        download_media_item(item, tmp_path, client=mock_client)

    assert item.download_status == "FAILED"
    assert not (tmp_path / "image_01.jpg").exists()
    assert not (tmp_path / ".image_01.jpg.tmp").exists()


def test_download_post_media_fail_closed(tmp_path: Path):
    item_ok = MediaItem(type="image", url="https://example.com/1.jpg", filename="image_01.jpg")
    item_fail = MediaItem(type="image", url="https://example.com/2.jpg", filename="image_02.jpg")

    post = CanonicalPost(
        platform="xhs",
        note_id="test_note_123",
        source_url="https://www.xiaohongshu.com/explore/test_note_123",
        collector="test",
        collected_at="2026-09-16T00:00:00Z",
        author=Author(id="a1", name="Author"),
        content={"title": "T", "text": "C"},
        media=[item_ok, item_fail],
        stats=Stats(),
        raw={},
    )

    with patch("xhs_ingest.media.download_media_item") as mock_download:
        def side_effect(item, assets_dir, client=None):
            if item.filename == "image_01.jpg":
                item.download_status = "COMPLETED"
                return item
            else:
                item.download_status = "FAILED"
                raise MediaDownloadError("Simulated download failure")

        mock_download.side_effect = side_effect

        # When raise_on_failure is False, must return False (never True)
        result = download_post_media(post, tmp_path, raise_on_failure=False)
        assert result is False
        assert post.is_media_complete is False

        # When raise_on_failure is True, must raise MediaDownloadError
        with pytest.raises(MediaDownloadError):
            download_post_media(post, tmp_path, raise_on_failure=True)
