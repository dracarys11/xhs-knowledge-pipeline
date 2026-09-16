"""Renderer: converts CanonicalPost into user-facing Markdown document post.md."""

from .models import CanonicalPost


def render_post_markdown(post: CanonicalPost) -> str:
    """Renders CanonicalPost into standard Markdown with metadata and media links."""
    title = post.content.get("title") or "无标题笔记"
    text = post.content.get("text") or ""
    author = post.author
    stats = post.stats

    # Frontmatter
    lines = [
        "---",
        f"title: {repr(title)}",
        f"note_id: {repr(post.note_id)}",
        f"author_name: {repr(author.name)}",
        f"author_id: {repr(author.id)}",
        f"source_url: {repr(post.source_url)}",
        f"collected_at: {repr(post.collected_at)}",
        f"collector: {repr(post.collector)}",
    ]
    if stats.liked_count is not None:
        lines.append(f"liked_count: {stats.liked_count}")
    if stats.collected_count is not None:
        lines.append(f"collected_count: {stats.collected_count}")
    if stats.comment_count is not None:
        lines.append(f"comment_count: {stats.comment_count}")
    if stats.share_count is not None:
        lines.append(f"share_count: {stats.share_count}")
    lines.append("---")
    lines.append("")

    # Heading & Metadata Bar
    lines.append(f"# {title}")
    lines.append("")
    author_link = (
        f"[{author.name}](https://www.xiaohongshu.com/user/profile/{author.id})"
        if author.id
        else author.name
    )
    lines.append(f"- **Author**: {author_link}")
    lines.append(f"- **Source**: [{post.source_url}]({post.source_url})")
    lines.append(f"- **Collected At**: {post.collected_at}")

    stats_display = []
    if stats.liked_count is not None:
        stats_display.append(f"❤️ {stats.liked_count}")
    if stats.collected_count is not None:
        stats_display.append(f"⭐ {stats.collected_count}")
    if stats.comment_count is not None:
        stats_display.append(f"💬 {stats.comment_count}")
    if stats.share_count is not None:
        stats_display.append(f"🔄 {stats.share_count}")
    if stats_display:
        lines.append(f"- **Stats**: {' | '.join(stats_display)}")
    lines.append("")

    # Main Body
    lines.append("## Content")
    lines.append("")
    if text.strip():
        lines.append(text.strip())
    else:
        lines.append("*(无文字内容)*")
    lines.append("")

    # Media References
    if post.media:
        lines.append("## Media")
        lines.append("")
        for idx, m in enumerate(post.media, 1):
            rel_path = f"assets/{m.filename}"
            if m.type == "image":
                lines.append(f"![Image {idx}]({rel_path})")
                lines.append("")
            elif m.type == "video":
                lines.append(f"<video controls src=\"{rel_path}\" width=\"100%\"></video>")
                lines.append(f"*(Video: [{m.filename}]({rel_path}))*")
                lines.append("")

    return "\n".join(lines)
