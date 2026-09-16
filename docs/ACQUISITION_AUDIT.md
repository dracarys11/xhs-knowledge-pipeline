# XHS Acquisition 技术审计

审计范围：公开仓库源码、测试、依赖、README、提交历史。审计基准：2026-09-15；以下结论以本次读取到的仓库 HEAD 为准，README 宣称与代码行为分开记录。

审计仓库：

- [jackwener/xhs-cli](https://github.com/jackwener/xhs-cli)
- [ONEMULE/xhs-favorites](https://github.com/ONEMULE/xhs-favorites)
- [ytf606/xhs2obsidian](https://github.com/ytf606/xhs2obsidian)
- [tamnd/xiaohongshu-cli](https://github.com/tamnd/xiaohongshu-cli)
- [hammershock/xhs-cli](https://github.com/hammershock/xhs-cli)

## A. Executive Conclusion

Recommended Primary Collector: **方案 3：仅参考代码，自建 acquisition adapter**。收藏枚举必须以已验证的浏览器页面/session 路径为主，并把每页原始快照、游标/滚动进度和完成证据纳入自己的状态模型。现有项目没有同时满足“收藏全量遍历、可恢复、错误不丢失、媒体可靠下载”的成品。

Recommended Browser/Auth Fallback: **ONEMULE/xhs-favorites 的 persistent Playwright profile + 手动扫码登录**。它是唯一把收藏主 feed、收藏夹列表、收藏夹条目作为明确能力实现的项目；但不是直接依赖：收藏分页实际上是滚动和固定迭代次数，必须由 adapter 增加可证明的终止条件。

Recommended Batch Model: **借鉴 tamnd 的 cursor 分页、请求节流/重试、JSONL streaming 和 typed response status；采用 xhs2obsidian 的 per-target synced ID/checkpoint 思路，但改成 per-item 原子提交**。每个 item 只有在 detail 和所需 media 策略完成后才能标记 done；列表接口返回空且没有明确 completion proof 时不得推进 checkpoint。

Recommended Media Implementation Reference: **tamnd 的 `Note`/`Image`/`Video.Masters` 多源数据模型，结合浏览器 session 内下载**。tamnd 能保留多 codec、多质量 master URL；xhs2obsidian 的 WebView 下载可复用为需要 Cookie/Referer 的 fallback。两者都不能直接当作可靠下载器：URL 有时效性，当前代码都缺少完整的 media manifest、校验、断点下载和失败重试闭环。

Do Not Use As Core:

- `jackwener/xhs-cli`：收藏实现只有浏览器 DOM/state 滚动，默认 `--max=50`，没有真实分页游标，也没有收藏夹分类；项目自身后续提交已将其标记为 legacy 并引导到另一个项目。
- `ytf606/xhs2obsidian`：收藏 API 路径和增量状态有参考价值，但 detail/media 错误被吞掉后仍会写入并标记同步，且没有 raw payload/error taxonomy。
- `hammershock/xhs-cli`：没有收藏枚举；导出媒体忽略下载失败且不带 Cookie，不能作为核心。
- `tamnd/xiaohongshu-cli`：不能作为收藏 collector；它适合作为 HTTP/session/status/batch 参考，不是目标能力的现成实现。

## B. Project Matrix

| Project | Favorites | Pagination | Auth | Note Detail | Media | Batch / Resume | Error Semantics | Activity | Recommendation |
|---|---|---|---|---|---|---|---|---|---|
| `jackwener/xhs-cli` | 支持主收藏 tab | 浏览器滚动；`page_limit=max_count/10`，无 cursor；默认 max 50 | Camoufox 注入 cookie；browser-cookie3 读 Chrome/Firefox/Edge/Brave；扫码 | state map；原始 dict，字段依赖页面；token 可缓存 | 无正式下载链；detail raw 可含 image/video | 无 checkpoint、JSONL、resume；单次 list | `LoginError`/`DataFetchError`；CLI 空列表成功，易把空结果当完成 | HEAD 3ce7141；2026-03-14；README/提交称 legacy | 只借鉴 cookie 提取和页面 state 解包；不作核心 |
| `ONEMULE/xhs-favorites` | 支持 `listSavedNotes`、`listSavedBoards`、`listBoardItems` | 滚动 `0..scroll`；默认 25 次；目标为 `limit+1`；返回 `next_cursor=null`，非全量游标 | persistent Playwright profile；手动扫码/浏览器登录；不读真实 Chrome profile | normalize `note_id/title/content/tags/images/videos/counts/time`；无 author_id、created/updated、raw | 保留图片 URL、多 codec video streams；无 acquisition 下载流程 | 无 durable checkpoint/JSONL/resume；review export 不是同步状态 | `AUTH_REQUIRED/RISK_CONTROLLED/EMPTY_STATE/SELECTOR_CHANGED` 等；无 RATE_LIMITED/TOKEN_INVALID/NETWORK 专类 | HEAD 4803ca6；2026-04-15；多次 release | 收藏路径最佳参考；必须自建分页完成证据和状态 |
| `ytf606/xhs2obsidian` | 支持 `/api/sns/web/v2/note/collect/page` | cursor + `has_more`；每批默认 5；理论可全量 | Obsidian Electron WebView；扫码/手机号；提取 cookie；不读真实 Chrome profile；persistent partition | list/detail 两阶段；有 title/body/user/tags/time/images/video/counts；无 raw、updated、xsec_source | WebView fetchBinary；视频选最大 stream；下载失败被吞 | `syncCursors/syncedIds/allSynced`；无 JSONL/SQLite；detail/media 失败仍可能标记 done | 普通 `Error`；没有区分 auth/risk/rate/token/parse；空成功响应可直接 allSynced | HEAD d69ae7f；2026-05-28；最近提交集中在功能扩展 | 借鉴 cursor + ID 状态和 Cookie-session media fallback；不直接依赖 |
| `tamnd/xiaohongshu-cli` | **不支持**收藏枚举；只有 `Board` 类型和 note 的 collect count | 其他 surface 使用 cursor；`user_posted` 明确 `has_more/cursor`；crawl 自己是 BFS frontier | cookie/环境变量/file；持久化匿名 `a1/webId` 12h；不读 Chrome、不扫码 | SSR/API；Note 含 body、author、counts、tags、images、multi-codec video、xsec、raw 可经 `Raw` 获取 | 保留 `Video.Masters`，但无内建媒体下载命令 | cursor、rate、retry/backoff、cache、JSONL；crawl 无 checkpoint/resume，输出会覆盖 | 最完整：login/walled/token/antibot/network/notfound/gone/empty；独立 exit code | HEAD b60f9a3；2026-08-19；近期持续修复状态语义 | 作为 HTTP/status/batch 参考；不作 favorites collector |
| `hammershock/xhs-cli` | **不支持**；只有 feed/user/note | feed cursor 可传；无 favorites | cookie JSON；扫码 API 登录；不读真实 Chrome；无 Playwright | API `note_card` raw；字段依赖接口；支持 token cache | export 下载图片/视频，但只 Referer、不带 Cookie；失败返回值被忽略 | retry/backoff/rate；无 checkpoint/JSONL/resume | 主要是数值 `XhsError`；无细分内容状态 | HEAD 14d7970；2026-04-26；仓库重命名/早期项目 | 仅参考简单导出字段；不使用为核心 |

### 矩阵解释

“支持分页”不等于“证明能完整遍历”。本审计把 `has_more + 下一游标 + 可恢复状态 + 非空/完成证明` 视为全量同步最低证据。按此标准，5 个项目都不能直接交付为收藏全量同步核心。

## C. Evidence

### 1. Favorites enumeration

| repo | file | function/class | behavior | risk |
|---|---|---|---|---|
| `ONEMULE/xhs-favorites` | [`src/providers/playwright-provider.js`](https://github.com/ONEMULE/xhs-favorites/blob/main/src/providers/playwright-provider.js) | `playwrightProvider.listSavedNotes` | 进入 `/user/profile/{profileId}?tab=fav&subTab=note`，读取 `__INITIAL_STATE__.user.notes._rawValue[1]`，调用 `collectFeedItems` 滚动页面。 | 页面 state 路径是脆弱 selector；滚动不是 API cursor；默认 25 次，且 `collectFeedItems` 在连续 3 次长度不变或达到 scroll 上限时返回 `has_more:false`，这可能只是加载失败。
| `ONEMULE/xhs-favorites` | 同上 | `listSavedBoards`, `listBoardItems`, `normalizeBoards`, `normalizeBoardFeedEntry` | 有收藏夹/专辑列表；board item 读 `boardFeedsMap`，能返回 `board_id/board_name/note_id/xsec_token/note_url`，并解析 `cursor/hasMore`。 | `listBoardItems` 最终仍由滚动触发；虽然读出 cursor，但没有用 cursor 发下一请求，也没有把 cursor 持久化。
| `jackwener/xhs-cli` | [`xhs_cli/client.py`](https://github.com/jackwener/xhs-cli/blob/main/xhs_cli/client.py) | `XhsClient.get_favorites` | 进入 `?tab=collect`，从 `user.collect/collectNotes/notes` 解包；按 `(max_count+9)//10` 次滚动，默认 max 50。 | 没有收藏夹分类；没有 cursor/has_more/total；达到 max 即停止，不能证明全量；state 不存在时 DOM fallback 可能返回空列表。
| `ytf606/xhs2obsidian` | [`src/xhs-api.ts`](https://github.com/ytf606/xhs2obsidian/blob/main/src/xhs-api.ts) | `XhsApi.fetchBookmarks` | 调用 `/api/sns/web/v2/note/collect/page`，传 `user_id/num/cursor`，返回 `items/cursor/hasMore`。 | 这是最接近真实分页的收藏实现，但 API 响应 shape、权限、错误码没有强校验；无收藏夹/专辑接口。
| `tamnd/xiaohongshu-cli` | [`xiaohongshu/`](https://github.com/tamnd/xiaohongshu-cli/tree/main/xiaohongshu) | public API surface | `rg` 可见无 `collect/page`、favorites command 或 board enumeration；`Board` 只是数据类型。 | 不能因为有 `CollectedCount`/`Board` 类型就判定支持收藏遍历。
| `hammershock/xhs-cli` | [`src/xhs_cli/api`](https://github.com/hammershock/xhs-cli/tree/main/src/xhs_cli/api) | API modules | 只有 note/feed/search/user/comment；无 favorites/collect endpoint。 | 不具备目标链的第一步。

收藏列表项的实际字段证据：ONEMULE 的 `normalizeSavedNotes`/`normalizeBoardFeedEntry` 返回 `note_id`、`xsec_token`、标题、作者、封面、like/comment count、可复用 `note_url`、board name；收藏夹本身返回 `board_id/name/item_count/privacy/desc`。它没有返回 raw item，也没有 author_id。

### 2. Authentication / session

- `jackwener`：[`auth.py`](https://github.com/jackwener/xhs-cli/blob/main/xhs_cli/auth.py) 明确实现保存 cookie、browser-cookie3 读取 Chrome/Firefox/Edge/Brave、Camoufox QR flow；[`client.py`](https://github.com/jackwener/xhs-cli/blob/main/xhs_cli/client.py) 启动 Camoufox 后注入 cookie。没有 localStorage/IndexedDB 恢复。cookie 文件权限有测试覆盖。失效主要靠 `get_self_info`/页面探测发现；状态仍大体归并为 Login/DataFetch。
- `ONEMULE`：[`session.js`](https://github.com/ONEMULE/xhs-favorites/blob/main/src/session.js) 使用 `chromium.launchPersistentContext(PROFILE_DIR)`，因此 cookie、localStorage 等由 Playwright profile 管理；它不是“读取用户已有 Chrome profile”，而是专用 profile。扫码由 Playwright CLI 打开真实浏览器完成。`classifySnapshot` 可区分 authenticated/auth_required/risk_controlled，但无法区分 rate limit、token invalid、network、parse。
- `xhs2obsidian`：[`login-modal.ts`](https://github.com/ytf606/xhs2obsidian/blob/main/src/login-modal.ts) 使用 Electron `persist:redbook-pull` WebView partition，优先读取 Electron session cookies（包含 HttpOnly），fallback 为 `document.cookie`，验证 `/user/me` 后把 cookie 字符串存入插件 settings。没有 Chrome profile 导入；不依赖 IndexedDB/localStorage 的持久化语义。过期时靠 `/user/me` 或 API 抛普通 Error。
- `tamnd`：[`session.go`](https://github.com/tamnd/xiaohongshu-cli/blob/main/xiaohongshu/session.go) 持久化匿名 `a1/webId` 12 小时；真实 `web_session` 只能来自 `--cookie`/环境变量/file。没有扫码或 Chrome profile。[`status.go`](https://github.com/tamnd/xiaohongshu-cli/blob/main/xiaohongshu/status.go) 和 client response path 对 login/walled/token/antibot/network 做 typed classification，是五者中最完整的鉴权失败语义。
- `hammershock`：[`auth.py`](https://github.com/hammershock/xhs-cli/blob/main/src/xhs_cli/auth.py) API QR login 后用 Set-Cookie 保存 JSON cookie；[`client.py`](https://github.com/hammershock/xhs-cli/blob/main/src/xhs_cli/client.py) 有 rate limit 和 retry，但没有浏览器 profile、localStorage 或 IndexedDB。

### 3. Note detail contract

| Field | jackwener | ONEMULE | xhs2obsidian | tamnd | hammershock |
|---|---|---|---|---|---|
| `note_id`, URL, title, body | Usually / URL assembled | Usually / URL assembled / content | Usually / URL not first-class in type / desc | Guaranteed by typed SSR/API conversion | Usually in raw `note_card` |
| `author_id`, `author_name` | Usually raw, not normalized | name usually; author_id not normalized | id/name in `XhsAuthor` | Guaranteed when source has it | Usually raw |
| created / updated | raw-dependent | `published_time` from `time`/`lastUpdateTime`, no separate updated | `time`; no updated | `Time` + `LastUpdateTime` | raw-dependent |
| tags | raw-dependent | Usually | Usually | Usually | raw-dependent |
| likes/comments/collect/share | raw-dependent | like/comment/collect; share not normalized | like/comment/share; collect absent from `XhsInteractInfo` | all four | raw-dependent |
| images/video | raw detail | image URLs + stream URLs | image URLs + one selected video URL | images + all master streams | raw detail |
| `xsec_token` | cache/input | input or URL; normalized detail does not return it | carried in `XhsNote` | carried and can be sourced from SSR | input/cache |
| raw source | No formal raw field | No; normalizers discard most raw | No | `Raw` client method exists, but `Note` command emits normalized record | `export` writes raw `meta.json` |

状态标记含义：Guaranteed = 类型/转换逻辑明确建模；Usually = 代码尝试读取但依赖接口/页面字段；Optional = 只在特定 note type/source 出现；Not available = 本项目目标路径没有实现。

### 4. Media

- Images：ONEMULE、xhs2obsidian、tamnd 都能从 detail 得到 `urlDefault/url/url_default`。代码没有证明“原图无水印”：它们只是选择站点返回的 default/info URL；不可将 URL 名称推断为无水印。URL 没有 TTL metadata。
- Video：ONEMULE 保留 `video.media.stream` 中各 codec 的 `masterUrl`；tamnd 保留 H264/H265/H266/AV1 全部 `Masters`；xhs2obsidian 选最大 `size` 的单条 stream。它们都证明“可获得候选播放 URL”，不证明 URL 永久有效或一定是最高质量。
- 本地下载：xhs2obsidian 的 [`SignManager.fetchBinary`](https://github.com/ytf606/xhs2obsidian/blob/main/src/sign-manager.ts) 在同 partition WebView 内 `fetch`，带 Referer，适合 Cookie/浏览器上下文；`VaultWriter` 对图片/视频失败只记录日志。hammershock 的 [`export.py`](https://github.com/hammershock/xhs-cli/blob/main/src/xhs_cli/export.py) 用独立 httpx，仅带 Referer，不带 Cookie，且调用方忽略 `_download` 的 false 返回。ONEMULE acquisition 路径不下载媒体。没有项目实现校验和、临时文件原子 rename、续传、媒体重试/manifest。

### 5. Batch / incremental / resume

- `tamnd` 的 `Client.runWithRetry` 有节流、retry/backoff、response cache；CLI `Output` 支持 JSONL；crawl 的 BFS frontier 和 `seenNote`/`seenUser` 去重清晰。但 `newJSONLWriter` 使用 `os.Create`，crawl 没有 checkpoint/frontier 持久化；中断后只能从头开始，且部分子请求错误通常只 progress 后继续，命令仍可返回成功。
- `xhs2obsidian` 的 `SyncEngine.sync` 用 `syncCursors`、`syncedIds`、`allSynced`，每处理一批就 `saveData()`。优点是有可恢复状态和按 ID 去重；问题是 detail/comments/media 失败被 catch 后继续写 note，并将 ID push 到 `syncedIds`。它把“已发现/已写入”与“已完整采集”混为一类。
- `ONEMULE`、`jackwener` 没有 durable batch state；`limit/scroll` 是调用级上限。ONEMULE 的 review export 只解决人工筛选输出，不是同步 checkpoint。

### 6. 明确的 fail-open 风险

1. `xhs2obsidian/src/xhs-api.ts:189-197`：`fetchBookmarks` 对 `data.notes/items` 缺失直接使用 `[]`，对缺失 `has_more` 使用 false；`SyncEngine.sync` 在 `items.length===0 && !hasMore` 时设置 `allSynced=true`。因此 malformed/权限降级/接口 shape 改变可能被误判为“全部同步完成”。
2. `xhs2obsidian/src/sync-engine.ts:76-117`：detail、评论和媒体链路的异常被记录后继续；之后 `syncedIds[target].push(listNote.id)`。这会造成“目录看似同步完成，但正文/媒体缺失且不会自动重试”。
3. `ONEMULE/src/providers/playwright-provider.js:220-257`：连续 3 次长度稳定或达到 scroll 上限即 `has_more:false`，没有确认页面没有 loading/risk/login/网络失败，也没有 server total；页面没继续加载可被当成自然终止。
4. `jackwener/xhs_cli/client.py:596-694`：收藏读取失败的 DOM fallback 可返回空数组，CLI `favorites` 对空数组正常打印 “No favorites found” 并退出 0；空收藏、页面结构变更和未授权结果没有统一的完成证明。
5. `tamnd/cli/meta.go:240-324`：crawl 的单个 note/user/related/comment 错误多数是 progress 后跳过，JSONL 中没有 error record 或 failed manifest；它不会把网络/权限失败伪装成 item，但整体 crawl 成功码不代表完整图已采集。

## D. Proposed Acquisition Contract

下面只定义 acquisition 最小接口，不是 production 实现。接口故意保留 raw payload 和 page proof，避免把站点字段过早压扁。

```python
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

@dataclass
class FavoriteRef:
    note_id: str
    source_url: str | None
    xsec_token: str | None
    board_id: str | None
    board_name: str | None
    listing_fields: Mapping[str, Any]
    raw: Mapping[str, Any] | None

@dataclass
class PageResult:
    items: list[FavoriteRef]
    next_cursor: str | None
    has_more: bool | None
    completion_proof: str | None  # e.g. explicit has_more=false, not “scroll stopped”
    raw: Mapping[str, Any] | None

@dataclass
class NoteResult:
    note_id: str
    source_url: str
    xsec_token: str | None
    raw: Mapping[str, Any]
    normalized: Mapping[str, Any]

class Collector(Protocol):
    def list_favorite_boards(self, checkpoint: str | None = None) -> PageResult: ...
    def list_favorites(
        self,
        board_id: str | None = None,
        checkpoint: str | None = None,
    ) -> PageResult: ...
    def fetch_note(self, ref: FavoriteRef) -> NoteResult: ...
```

Contract rules:

- `note_id` 是去重主键；`source_url` 必须保留实际可复用的 token/query，而不是只拼裸 URL。
- listing page 未返回明确 completion proof 时只能返回 `has_more=None`，不能返回完成。
- detail 必须保留 raw source；normalized fields 缺失必须表现为 `None`/状态，不得静默变成成功的空字符串。
- 每一页、每一个 note、每一个 media artifact 都有独立 status/checkpoint；不要求本轮实现 SQLite，但状态语义必须可落盘和恢复。
- collector 不负责 RAG、embedding、知识图谱、Obsidian UI 或复杂编排。

## E. Failure Taxonomy

至少需要以下独立状态；不得用一个通用 `Exception` 覆盖：

| Status | 含义 | 是否推进 favorite checkpoint |
|---|---|---|
| `AUTH_REQUIRED` | 没有 session、session 过期、页面明确要求登录 | 否 |
| `RISK_CONTROLLED` | 安全限制、验证码、IP/设备风险页 | 否 |
| `RATE_LIMITED` | 429、明确频控、需等待后重试 | 否 |
| `TOKEN_INVALID` | `xsec_token` 缺失/失效/笔记 token refusal | 否；可进入 token refresh 分支 |
| `CONTENT_UNAVAILABLE` | 已确认笔记不存在、删除、权限不可见 | 可记录 terminal failure；不应伪装为成功 |
| `PARSE_FAILED` | HTML/state/API payload 存在但 schema 无法解析 | 否 |
| `NETWORK_ERROR` | DNS、连接、超时、5xx 等传输问题 | 否 |
| `MEDIA_DOWNLOAD_FAILED` | detail 成功但某个图片/视频下载失败 | note 是否入库由策略决定；media artifact 必须可重试 |

建议额外保留 `EMPTY_CONFIRMED` 和 `COMPLETED`：前者要求服务端/页面明确表示空收藏，后者要求明确的分页终止证据。`EMPTY` 不能等同于“接口返回了空数组”。

现有项目对照：tamnd 已经覆盖 login/walled/token/antibot/network/notfound/gone/empty 的大部分底层语义；ONEMULE 覆盖 auth/risk/selector/empty；其余项目需要补齐 `RATE_LIMITED/TOKEN_INVALID/PARSE_FAILED/MEDIA_DOWNLOAD_FAILED`。

## F. Implementation Decision

### 方案 1：直接依赖某个 CLI/package

不采用。没有一个项目同时提供：收藏夹分类、可证明全量分页、note_id+xsec_token 链、raw detail、可恢复 batch、媒体失败可重试。直接依赖还会把各项目的页面 selector、固定 limit、错误吞掉行为带入核心。

### 方案 2：Vendor/复用其中模块

不作为主方案。可以局部复制思路，但 vendor 整个项目会引入与 Acquisition 无关的发布、评论、AI、Obsidian 或 crawl 图遍历，并且许可证、上游变更和站点 selector 漂移会增加维护面。

### 方案 3：仅参考代码，自建 adapter

采用。具体参考边界：

- **收藏入口与浏览器 fallback**：参考 ONEMULE 的 persistent Playwright session、`listSavedNotes`、`listSavedBoards`、`listBoardItems` 和 state normalizer。
- **HTTP/session/status**：参考 tamnd 的 cookie header、签名请求、节流、重试、refusal 分类和独立 exit/status 语义；但新增收藏 endpoint 不能未经验证就假设稳定。
- **增量状态**：参考 xhs2obsidian 的 cursor + ID 结构，但改成“discovered / detail_ok / media_ok / committed”分层，并禁止空 payload 推进完成。
- **媒体**：参考 tamnd 保留多 master stream；需要登录上下文时参考 xhs2obsidian WebView fetch；新增 media manifest、内容长度/type 校验、临时文件 + 原子提交和失败重试。
- **不复制**：jackwener 的固定 `max_count`/scroll 全量假设、xhs2obsidian 的吞错后标记 synced、hammershock 的无 Cookie 独立媒体下载、tamnd crawl 的无 checkpoint BFS。

最终判断：先实现并验证最小闭环 **favorites listing → note_id/xsec_token → note detail → media candidate**，再考虑下载和增量恢复；本轮不启动 production collector。
