"""Tests for markdown renderer."""

from xhs_ingest.models import Author, CanonicalPost, MediaItem, Stats
from xhs_ingest.renderer import render_post_markdown


def test_render_post_markdown():
    post = CanonicalPost(
        platform="xhs",
        note_id="66abc001122",
        source_url="https://www.xiaohongshu.com/explore/66abc001122?xsec_token=xyz",
        collector="test_collector",
        collected_at="2026-09-16T12:00:00Z",
        author=Author(id="author_123", name="创作者小张"),
        content={
            "title": "如何搭建个人知识库",
            "text": "第一步：确定知识库目标。\n第二步：建立采集流程。",
        },
        media=[
            MediaItem(type="image", url="https://img.xhscdn.com/1.jpg", filename="image_01.jpg"),
            MediaItem(type="video", url="https://video.xhscdn.com/1.mp4", filename="video_01.mp4"),
        ],
        stats=Stats(
            liked_count=100,
            collected_count=50,
            comment_count=10,
            share_count=2,
        ),
        raw={},
    )

    md = render_post_markdown(post)

    assert "# 如何搭建个人知识库" in md
    assert "创作者小张" in md
    assert "https://www.xiaohongshu.com/user/profile/author_123" in md
    assert "https://www.xiaohongshu.com/explore/66abc001122?xsec_token=xyz" in md
    assert "第一步：确定知识库目标" in md
    assert "![Image 1](assets/image_01.jpg)" in md
    assert "<video controls src=\"assets/video_01.mp4\" width=\"100%\"></video>" in md
    assert "❤️ 100" in md
    assert "⭐ 50" in md
