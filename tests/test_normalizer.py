"""Tests for normalizer: CanonicalPost parsing, raw preservation, and media deduplication."""

import pytest

from xhs_ingest.errors import ParseFailedError
from xhs_ingest.normalizer import _parse_count, normalize_note


def test_parse_count():
    assert _parse_count(123) == 123
    assert _parse_count("456") == 456
    assert _parse_count("1.2万") == 12000
    assert _parse_count("2.5w") == 25000
    assert _parse_count(None) is None
    assert _parse_count("") is None


def test_normalize_image_note():
    raw_payload = {
        "data": {
            "items": [
                {
                    "id": "66ab1234567890abcdef1234",
                    "model_type": "note",
                    "note_card": {
                        "note_id": "66ab1234567890abcdef1234",
                        "title": "测试小红书笔记标题",
                        "desc": "这里是测试正文内容。\n第二行文字。",
                        "xsec_token": "ABtesttoken123",
                        "user": {
                            "user_id": "5e1122334455",
                            "nickname": "测试作者",
                        },
                        "interact_info": {
                            "liked_count": "1.2万",
                            "collected_count": "345",
                            "comment_count": "67",
                            "share_count": "8",
                        },
                        "image_list": [
                            {"url_default": "https://ci.xiaohongshu.com/test_img_01.jpg"},
                            {"url_default": "https://ci.xiaohongshu.com/test_img_02.png"},
                            {"url_default": "https://ci.xiaohongshu.com/test_img_01.jpg"},  # Duplicate
                        ],
                    },
                }
            ]
        }
    }

    post = normalize_note(raw_payload)

    # Basic assertions
    assert post.platform == "xhs"
    assert post.note_id == "66ab1234567890abcdef1234"
    assert "ABtesttoken123" in post.source_url
    assert post.author.id == "5e1122334455"
    assert post.author.name == "测试作者"
    assert post.content["title"] == "测试小红书笔记标题"
    assert "这里是测试正文内容" in post.content["text"]

    # Stats
    assert post.stats.liked_count == 12000
    assert post.stats.collected_count == 345
    assert post.stats.comment_count == 67
    assert post.stats.share_count == 8

    # Media and deduplication
    assert len(post.media) == 2  # 3 items, 1 duplicate -> 2
    assert post.media[0].filename == "image_01.jpg"
    assert post.media[0].type == "image"
    assert post.media[0].url == "https://ci.xiaohongshu.com/test_img_01.jpg"
    assert post.media[1].filename == "image_02.png"

    # Raw preservation: full raw must be completely preserved without truncation
    assert post.raw == raw_payload


def test_normalize_video_note():
    raw_payload = {
        "note": {
            "noteDetailMap": {
                "67cd9876543210fedcba4321": {
                    "note": {
                        "noteId": "67cd9876543210fedcba4321",
                        "title": "视频笔记测试",
                        "desc": "这是视频笔记的正文",
                        "user": {
                            "userId": "usr_9988",
                            "nickName": "视频作者",
                        },
                        "video": {
                            "media": {
                                "stream": {
                                    "h264": [
                                        {"masterUrl": "https://sns-video-bd.xhscdn.com/stream1.mp4"}
                                    ]
                                }
                            }
                        },
                    }
                }
            }
        }
    }

    post = normalize_note(raw_payload)
    assert post.note_id == "67cd9876543210fedcba4321"
    assert post.author.id == "usr_9988"
    assert post.author.name == "视频作者"
    assert len(post.media) == 1
    assert post.media[0].type == "video"
    assert post.media[0].filename == "video_01.mp4"
    assert post.media[0].url == "https://sns-video-bd.xhscdn.com/stream1.mp4"


def test_normalize_missing_note_id_raises_parse_failed():
    invalid_raw = {"unexpected_key": "some_value"}
    with pytest.raises(ParseFailedError) as exc_info:
        normalize_note(invalid_raw)
    assert "Could not find note_id" in str(exc_info.value)
