# P0 → P1 Readiness Review

审查日期：2026-09-16

审查范围：

- `src/xhs_ingest/`
- `tests/`
- `docs/P0_SMOKE_TEST.md`

验证：`./.venv/bin/pytest -q -p no:cacheprovider`，结果为 **11 passed**。

## Executive Decision

**P1 Incremental Sync 应该开始，但结论是 Conditional Go。**

P0 已经证明单篇真实 Vertical Slice 可用：真实登录态能够枚举收藏第一页，从 `FavoriteRef` 携带 `note_id/xsec_token` 获取详情，规范化为 `CanonicalPost`，下载图片/视频，并生成本地文件。媒体单文件使用临时文件和原子重命名，空收藏结果也有 fail-closed 防护。

P0 尚未证明批量或增量能力。当前不能安全处理 1000 个收藏：没有真实分页 contract、没有批次 runner、没有持久化 note 状态、没有 checkpoint/resume、没有跨运行幂等，也没有 retry policy。这些缺口正是 P1 的核心工作，因此不阻止“开始 P1”，但阻止在补齐它们之前执行大规模同步。

## 1. 当前数据流与 Contract

```text
XhsPlaywrightCollector.list_favorites(limit)
             │
             ▼
 PageResult(items: list[FavoriteRef])
             │
             ▼
 FavoriteRef(note_id, source_url, xsec_token, listing metadata, raw)
             │
             ▼
 XhsPlaywrightCollector.fetch_note(ref)
             │
             ▼
 sanitized raw detail payload: dict[str, Any]
             │
             ▼
 normalize_note(raw)
             │
             ▼
 CanonicalPost(author, content, stats, media, raw)
             │
             ▼
 download_post_media(post, assets_dir)
             │
             ▼
 MediaItem(PENDING → COMPLETED | FAILED)
             │
             ▼
 data/<note_id>/
 ├── assets/*
 ├── raw.json
 ├── canonical.json
 └── post.md
```

### 1.1 favorites → PageResult

实现位置：`collector.py:list_favorites`。

输入 contract：

- `limit: int`，默认 20。
- 使用 persistent Playwright profile 中的真实登录态。

输出 contract：

- `PageResult.items: list[FavoriteRef]`
- `PageResult.next_cursor: str | None`
- `PageResult.has_more: bool | None`
- `PageResult.completion_proof: str | None`
- `PageResult.raw: dict`

实际行为：

- 监听 `/api/sns/web/v2/note/collect/page` 响应，并收集 `data.notes`。
- 页面最多滚动 6 次，直到拦截到的条目数达到 `limit`。
- 同一次调用内按 `note_id` 去重。
- 空列表只有在页面出现明确空状态时才成功返回，否则抛出 `UnknownAcquisitionError`。

Contract 缺口：

- `next_cursor` 从未赋值。
- response 中的 `cursor/has_more` 没有捕获或保存。
- `has_more` 只是 `len(items_by_id) >= limit` 的推断，不是服务端完成证据。恰好取到 20 条会被标记为还有更多；只取到 19 条也可能因加载慢而被标记为结束。
- `completion_proof="FOUND_N_ITEMS"` 只能证明发现了条目，不能证明分页完成。
- Collector protocol 没有 cursor/checkpoint 输入，因此无法请求“下一页”。

结论：第一批收藏可用；全量收藏 enumeration contract 尚未成立。

### 1.2 PageResult → FavoriteRef

实现位置：`models.py:FavoriteRef`、`collector.py:list_favorites`。

`FavoriteRef` 当前包含：

- 必需：`note_id`, `source_url`
- 可选：`title`, `author_name`, `author_id`, `xsec_token`, `cover_url`
- 原始 listing item：`raw`

这是合格的 detail acquisition 输入边界。`note_id` 可作为稳定去重键，`source_url/xsec_token` 足以连接到 `fetch_note`。token 还会写入 `.xhs-profile/tokens_cache.json`。

缺口：token cache 的读写异常被静默吞掉；没有 token 获取时间、来源或失效状态，无法支持 token refresh 决策。

### 1.3 FavoriteRef → fetch_note → raw detail

实现位置：`collector.py:fetch_note`。

输入：`FavoriteRef`、note ID 或完整 URL。

输出：脱敏后的原始 detail `dict[str, Any]`，主要形态为：

```text
{
  "note": {
    "noteDetailMap": {
      "<note_id>": <page detail entry>
    }
  }
}
```

fallback 为拦截到的 `/api/sns/web/v1/feed` 完整 payload。

已实现的失败语义：

- 404 / `error_code=300031` → `TOKEN_INVALID`
- 页面明确删除/不可见 → `CONTENT_UNAVAILABLE`
- 无法提取结构化详情 → `PARSE_FAILED`
- 页面验证码/风险控制 → `RISK_CONTROLLED`
- 页面频率提示 → `RATE_LIMITED`

Contract 缺口：

- Playwright navigation timeout、连接失败等没有统一转换为 `NETWORK_ERROR`，可能落入 CLI 的 unknown failure。
- token 只在“缺失”时尝试从 favorites 重新解析；已存在但失效的 token 没有 refresh/retry 路径。
- token 解析辅助调用的所有异常被 `except Exception: pass` 吞掉，丢失失败原因。

### 1.4 raw detail → CanonicalPost

实现位置：`normalizer.py:normalize_note`、`models.py:CanonicalPost`。

`CanonicalPost` 当前 contract：

- identity：`platform`, `note_id`, `source_url`
- provenance：`collector`, `collected_at`, `raw`
- author：`Author(id, name)`
- content：`title`, `text`
- stats：likes、collects、comments、shares
- media：`list[MediaItem]`

Normalizer 支持 API wrapper、SSR `noteDetailMap`、`note_card/noteCard` 和裸 note card。缺少 `note_id` 时 fail closed 为 `PARSE_FAILED`；原始 payload 完整保留。

边界问题：

- `CanonicalPost` 没有显式 `xsec_token` 字段，仅可能保留在 `source_url/raw`。
- CLI 调用 `normalize_note(raw_note)` 时没有显式传入 `FavoriteRef.source_url`；如果 detail payload 不回传 token，canonical source URL 可能退化为裸 URL。
- 当前 canonical schema 不包含 tags、created/updated time 或 note type。P0 smoke 证明原始数据中存在这些字段，但它们未成为 canonical contract。

这些不是启动 P1 的硬阻塞，但 P1 状态记录必须使用 `note_id`，不能依赖会过期的 URL/token。

### 1.5 CanonicalPost → media

实现位置：`media.py`。

`MediaItem` contract：

- identity/input：`type`, `url`, `filename`
- output：`local_path`, `size_bytes`, `checksum_sha256`
- state：`PENDING`, `COMPLETED`, `FAILED`
- failure detail：`error`

单文件下载行为：

- 写入 `.<filename>.tmp`
- 验证非零长度
- 计算 SHA256
- `Path.replace()` 原子落盘
- 异常时清理临时文件并设置 `FAILED`

整篇下载行为：

- 顺序遍历全部媒体。
- 单个媒体失败不会阻止其余媒体尝试。
- 有任一失败时返回 `False` 或抛 `MediaDownloadError`。

缺口：

- 不检查现有目标文件及 checksum，每次重跑都会重新下载并覆盖。
- 不支持只重试失败媒体。
- 下载状态只存在于内存，直到后续写入 `canonical.json`。
- 没有 retry/backoff、Range resume、Content-Type 校验或 Cookie/session fallback。
- Smoke 文档称多图“并发下载”，但当前实现是普通 `for` 循环顺序下载；文档与代码不一致。

### 1.6 media → filesystem

实现位置：`cli.py:ingest_note`。

写入顺序：

1. 创建 `data/<note_id>/assets/`
2. 下载媒体
3. 写 `raw.json`
4. 写 `canonical.json`
5. 写 `post.md`
6. 根据媒体是否完整返回 exit code 0 或 3

优点：媒体单文件写入是原子的；媒体失败时仍会保存可用 detail/canonical/Markdown，避免完全丢失已获取数据。

缺口：

- 三个 metadata 文件使用直接 `open(..., "w")`，不是临时文件 + 原子 rename。
- 没有 note-level completion marker 或 manifest。
- 目录存在不等于成功；`canonical.json` 存在也可能表示 media partial。
- 整个 note 的提交不是事务性的，崩溃可留下无法可靠解释的半成品目录。

## 2. 当前状态模型

| 业务状态 | 当前表示 | 是否持久化 | 结论 |
|---|---|---:|---|
| 未开始 | note 层没有表示；单个 `MediaItem` 初始为 `PENDING` | 否 | 缺失 note/job 状态 |
| 获取成功 | CLI exit code 0；内存中所有 media 为 `COMPLETED`；文件存在 | 没有独立状态记录 | 可推断，不可可靠查询 |
| detail 失败 | typed `AcquisitionError`，CLI 通常 exit code 2 | 否 | 进程结束后失败记录丢失 |
| media 失败 | `MediaItem.download_status="FAILED"` + `error`；CLI exit code 3 | 仅在 CLI 成功写完 canonical 后间接持久化 | 有局部表达，无独立重试队列 |
| 部分成功 | detail/canonical/Markdown 已写，部分 assets 成功，CLI exit code 3 | 目录可见，但无 note-level `PARTIAL` | 只能通过检查 canonical/media 推断 |
| 可重试失败 | taxonomy 有 `RATE_LIMITED`, `NETWORK_ERROR`, `MEDIA_DOWNLOAD_FAILED` | 否；没有 retryable flag、attempt count、next retry time | 分类存在，策略不存在 |
| 永久失败 | `CONTENT_UNAVAILABLE` 可被人理解为永久；`PARSE_FAILED`/`TOKEN_INVALID` 未明确分类 | 否 | 没有 terminal/permanent 状态 |

`AcquisitionStatus.SUCCESS` 已定义，但当前流程没有创建或持久化一个包含 `SUCCESS` 的运行/item 状态记录。

当前是“异常分类模型 + 媒体内存状态”，还不是 Incremental Sync 所需的“持久化 item lifecycle”。

## 3. 1000 个收藏的批量风险

### 3.1 基础限制

当前没有 batch ingest command。`favorites --ingest` 只处理列表中的第一篇；`ingest` 只处理一个目标。即使外部循环调用，`list_favorites` 也没有真实 cursor，最多通过 6 次滚动收集一个有限批次，不能证明覆盖 1000 个收藏。

### 3.2 故障场景

| 场景 | 当前行为 | 恢复 | 跳过已完成 | 重试失败项 | 避免重复下载 |
|---|---|---:|---:|---:|---:|
| 进程崩溃 | 媒体单文件不易形成损坏终态，但可能留下 `.tmp`、孤立 assets、截断 JSON/Markdown；无 checkpoint | 否 | 否 | 只能人工整篇重跑 | 否 |
| 网络断开 | media 变为 FAILED；Playwright 网络错误可能变成 unknown；无 retry/backoff | 否 | 否 | 只能人工重跑 | 否 |
| token 失效 | `TOKEN_INVALID`；只有 token 缺失时才重新枚举，失效 token 不自动刷新 | 否 | 不适用 | 无自动 refresh/retry | 不适用 |
| rate limit | 页面文本命中时抛 `RATE_LIMITED`；无 pause/backoff/checkpoint | 否 | 否 | 只能等待后人工重跑 | 否 |
| 单个媒体失败 | 其余媒体继续；canonical/Markdown 保存；exit code 3 | 部分数据保留 | 无法跳过整篇成功部分 | 不能只重试失败媒体 | 重跑会下载全部媒体 |

### 3.3 现有去重能力的边界

当前有两种进程内去重：

- favorites listing 按 `note_id` 去重。
- normalizer 按 media URL 去重。

这两者都不跨运行。filesystem 中已有 note/media 不会被读取为同步状态；现有文件不会根据 checksum 被跳过。因此当前不能满足：

- 崩溃后从最后安全位置恢复；
- 自动跳过已完整 note；
- 只重试 detail/media 失败项；
- 避免重复下载已验证媒体。

## 4. 测试与 Smoke Evidence

现有 11 个单元测试全部通过，覆盖：

- image/video normalizer 基本映射；
- media URL 去重；
- raw payload 保留；
- 单媒体原子写入与 checksum；
- media fail-closed；
- failure taxonomy 枚举映射；
- Markdown 输出。

`P0_SMOKE_TEST.md` 提供了真实环境证据：收藏第一页 20 条、一个视频笔记、一个 15 图笔记均完成落盘。这足以证明 P0 Vertical Slice，不足以证明 P1：

- 没有第二页/最终页 cursor 验证；
- 没有 1000 条或长时间运行验证；
- 没有崩溃恢复测试；
- 没有 rerun/idempotency 测试；
- 没有 token 过期、429、网络断开后的 retry 测试；
- 没有 partial media 跨运行恢复测试。

## 5. P1 最小必要改动

P1 可以开始，但最小范围必须限制在以下内容：

1. **真实分页 contract**：`list_favorites` 接受 cursor/checkpoint，捕获并返回服务端 `cursor/has_more`；只有明确 `has_more=false` 才认定 enumeration 完成。
2. **持久化 item 状态**：至少按 `note_id` 保存 `DISCOVERED / DETAIL_OK / MEDIA_PARTIAL / COMPLETED / RETRYABLE_FAILED / PERMANENT_FAILED`，同时保存 error status、attempt count 和更新时间。
3. **原子 checkpoint**：每完成一个 note（或一个明确 page）立即提交状态；进程崩溃后可以继续，不依赖内存列表。
4. **幂等文件判断**：已有 `COMPLETED` note 直接跳过；已有媒体只有在文件存在且 checksum/size 匹配时跳过；partial note 只重试缺失或失败媒体。
5. **最小 retry policy**：只对 `NETWORK_ERROR`、`RATE_LIMITED`、可刷新 token 和媒体传输失败进行有限重试/退避；`CONTENT_UNAVAILABLE` 进入 terminal 状态。未知/parse 错误不得推进完成 checkpoint。
6. **note-level 原子完成标志**：raw/canonical/Markdown 使用原子写；所有必需输出完成后再写 completion marker 或等价 committed 状态。
7. **最小验证**：增加 cursor 多页、崩溃后 resume、重复运行不重下、单媒体失败后只补失败项、token refresh/rate-limit 不误标完成的测试。

不应在 P1 同时扩大到 RAG、Embedding、Knowledge Graph、Obsidian plugin、复杂 UI 或高并发抓取。

## Final Answer

**开始 P1 Incremental Sync。**

理由：P0 已经完成其应有目标——证明真实单篇采集闭环和核心数据 contract 可行。当前缺失的全量分页、状态持久化、恢复、重试和幂等恰好是 P1 的主体，不应要求 P0 先实现 P1。

限制：在上述最小改动中的前四项完成并通过测试之前，项目仍不具备运行 1000 收藏批量同步的条件，也不应把任何大规模运行标记为“完整同步”。
