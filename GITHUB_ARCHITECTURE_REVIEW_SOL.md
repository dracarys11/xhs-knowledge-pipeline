# GitHub Architecture Pattern Review for xhs-ingest P1

审查日期：2026-09-16

审查对象：

- `ONEMULE/xhs-favorites`，快照 `4803ca65220b843196c9939943f94123c93c509c`
- `tamnd/xiaohongshu-cli`，快照 `b60f9a384ed32d002aaacbbff95fedb2bdc96682`
- `ytf606/xhs2obsidian`，快照 `d69ae7fc797b94034c781fde4ad0887c5d0e4db2`
- 当前项目 `docs/P1_STATE_CONTRACT_V2.md`
- 当前项目 `docs/P1_INCREMENTAL_SYNC_PLAN.md`

## Executive Conclusion

三个项目都不适合成为 `xhs-ingest` 的核心依赖，也没有一个项目同时解决了“可靠收藏枚举、逐 note durable state、媒体级恢复、严格错误语义”四件事。

最值得吸收的不是完整实现，而是四组局部模式：

1. **ONEMULE：持久浏览器 profile + 登录诊断 + capability-aware provider boundary。** 适合作为浏览器认证和页面状态诊断参考；其收藏分页不能作为全量完成证据。
2. **tamnd：响应状态分类 + 明确退出码 + 只重试真正 transient 的失败 + refusal 不进入缓存。** 这是三个项目中最成熟的 failure semantics；其 `crawl` 不是可恢复同步引擎，且没有 favorites acquisition。
3. **xhs2obsidian：直接使用 favorites `collect/page` 的 `cursor/has_more`，小批量逐页处理，并持久化 cursor 与 seen IDs。** 它证明了服务端分页字段及逐页同步形态确实存在；但其 detail/media 失败会被吞掉并最终标记 synced，是本项目必须避免的反例。
4. **共同启示：1000+ 同步必须是 at-least-once acquisition + idempotent commit。** 不能依赖一次长浏览器会话、一个最终导出文件、内存 `seen` 集或永久 `allSynced` 标志。

对当前 P1 的总体判断：`P1_STATE_CONTRACT_V2` 的 SQLite、双工作队列、raw/state truth boundary、媒体 checkpoint 和 COMPLETE 提交协议，整体上比三个外部项目更适合作为核心。但在编写 `sync.py` 前仍有几项遗漏必须收口：枚举与消费的饥饿问题、逐页持久化接口不匹配、单进程约束未真正执行、parse-failure 原始证据缺失、媒体 identity 与 raw generation 未绑定，以及 secret/logging policy。

## Review Method and Evidence Level

本审查以源码为主，README 只用于解释作者意图，不作为能力证明。没有登录真实账号，也没有执行线上抓取。

证据等级：

- **Code observed**：当前快照的具体函数和控制流；
- **Test observed**：仓库中的测试实际覆盖；
- **Documented**：README/文档声明，但未由代码闭合；
- **Inference**：由代码顺序或 failure window 推导，明确标注为推论。

ONEMULE 的本地测试实际运行结果为 `10 passed`，但没有覆盖深滚动分页、断点恢复或媒体下载。tamnd 的 Go 测试未在本机运行，因为环境没有 Go；仓库内存在较完整的 status/retry/unit tests。xhs2obsidian 没有 test script，也没有测试目录。

## Baseline: Current P1 Contract

当前项目的正确基线是：

- `raw.json` 是 detail payload 与媒体 manifest 的内容事实；
- SQLite 是发现、状态、attempt、错误与媒体执行进度的事实；
- filesystem assets 是媒体字节事实；
- `canonical.json`/`post.md` 是可重建派生产物；
- detail work 与 media resume work 是两个 SQL 投影；
- `COMPLETE` 只能在 raw、全部媒体、派生产物和 `.complete` 全部提交后最后写入；
- favorites 每次重新从顶部枚举，服务端 `has_more=false` 才是完成证据；
- 失败必须分类，不能把空列表、停滞或部分媒体成功当作同步完成。

外部模式只有在不破坏这些 invariant 时才应吸收。

## Project 1: ONEMULE/xhs-favorites

### Core architecture

| Area | Implementation | Assessment for 1000+ sync |
|---|---|---|
| Acquisition | 双 provider：公开 HTML `__INITIAL_STATE__` 的 API provider，以及 Playwright provider。收藏、收藏夹和收藏夹内容只走 Playwright；detail 默认 API 后 Playwright。Router 只在 `CAPABILITY_UNAVAILABLE` 时尝试下一 provider。 | provider boundary 清晰，但 favorites 主路径依赖页面 hydration 和滚动后的内存 state。 |
| Authentication | `chromium.launchPersistentContext(PROFILE_DIR)` 使用专用持久 profile；`login` 打开真实 Playwright 浏览器让用户扫码；每次操作前通过页面 snapshot 区分 authenticated/auth_required/risk_controlled。 | 很适合作为 browser auth fallback；不是读取用户真实 Chrome profile，而是专用 profile。 |
| Pagination | `collectFeedItems()` 反复读取 `__INITIAL_STATE__`，目标是 `limit+1`；连续 3 次长度不变或达到 `scroll` 上限即结束，并返回 `has_more=false`；`next_cursor` 永远为 null。 | **不可靠。** DOM/网络停滞与服务端终止被混为一谈；1000+ 时会 fail-open。 |
| Persistence | 持久化浏览器 profile；`export-review` 在所有读取结束后一次写 CSV/JSON/HTML/summary。没有 per-page/per-note checkpoint，没有 retry queue。HTML 内 localStorage 只保存人工勾选。 | 只能保存 session 和最终导出，不能恢复 acquisition 工作。中途失败会丢失本轮 export 进度。 |
| Retry/error | 有 `AUTH_REQUIRED`、`RISK_CONTROLLED`、`EMPTY_STATE`、`SELECTOR_CHANGED`、`NAVIGATION_TIMEOUT` 等结构化错误及独立退出码。无 token/rate/network/media taxonomy，无自动 retry/backoff。 | 错误比通用 Exception 好，但不足以驱动 P1 retry policy。 |

### Code evidence

- Persistent context 与专用 profile：[session.js L17-L52](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/session.js#L17-L52)、[constants.js L5-L8](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/constants.js#L5-L8)。
- 扫码/人工登录入口：[session.js L191-L213](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/session.js#L191-L213)。
- Auth/risk classification：[session.js L100-L130](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/session.js#L100-L130)、[extractors.js L334-L359](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/extractors.js#L334-L359)。
- 收藏读取链：[playwright-provider.js L1095-L1140](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/providers/playwright-provider.js#L1095-L1140)。
- 三次稳定/scroll 上限被判为完成：[playwright-provider.js L220-L259](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/providers/playwright-provider.js#L220-L259)。
- Provider fallback 仅接受 capability unavailable：[router.js L85-L110](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/router.js#L85-L110)。
- 最终 bundle 才写文件：[export.js L556-L637](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/export.js#L556-L637)。
- 错误枚举与退出码：[errors.js L1-L68](https://github.com/ONEMULE/xhs-favorites/blob/4803ca65220b843196c9939943f94123c93c509c/src/errors.js#L1-L68)。

### Patterns to absorb

1. **专用持久 browser profile。** 比导入散落 cookie 文件稳定，也能保留 localStorage/IndexedDB 等页面状态。当前项目应继续使用独立 profile，而不是读取和锁住用户日常 Chrome profile。
2. **独立 doctor/preflight。** 在启动长批次前，先验证 profile、auth、risk-control 与目标页面基本结构，能避免把 1000 项都送入同一种全局失败。
3. **Provider capability boundary。** 如果未来同时保留 public SSR 与 authenticated browser 两条 detail 路径，fallback 只能发生在明确的“能力不可用”，不能用另一 provider 掩盖 AUTH/RISK/TOKEN/PARSE 等语义失败。
4. **返回诊断 snapshot。** selector/schema 漂移时保留 URL、title 和有限页面文本，比只记录“parse failed”更有证据价值。
5. **收藏夹/board 信息作为可选 listing metadata。** 当前 P1 明确不做 boards，不应扩大范围；但 FavoriteRef 可继续允许 raw listing evidence，不要现在把 board 维度塞进状态主键。

### Patterns not to adopt

- 不采用“三次长度不变即结束”、固定 scroll 上限或 `items >= limit` 推导 `has_more`。
- 不采用“整个 export 成功后才一次写最终 JSON”的 checkpoint 模型。
- 不把 HTML localStorage 勾选状态误当同步 persistence。
- 不照搬 provider 自动 fallback 顺序；ONEMULE 的 API detail 若抛 `SELECTOR_CHANGED`，router 不会退到 Playwright，说明 capability/error mapping 仍需严格设计。
- 不把 Playwright 每个 operation 新开并关闭一个 context 的模式用于 1000 detail；应复用 context、轮换 page。

## Project 2: tamnd/xiaohongshu-cli

### Core architecture

| Area | Implementation | Assessment for 1000+ sync |
|---|---|---|
| Acquisition | 纯 Go HTTP client，自行生成签名；优先读取 SSR `__INITIAL_STATE__`，登录 cookie 存在时使用 signed API。`crawl` 用内存 BFS frontier 扩展 note→author/related/comments。 | 适合流式公共数据工具，不是用户 favorites collector；仓库没有 favorites/collect-page 实现。 |
| Authentication | 登录 cookie 由 `--cookie`、环境变量或 cookie file 提供；工具不扫码、不读取 Chrome profile。匿名 `a1/webId` session 自动 bootstrap 或生成，写 `session.json`，TTL 12 小时。 | 登录体验不适合当前项目；匿名 session 的独立生命周期设计可参考。 |
| Pagination | Search 用 page+`has_more`；user notes 与 comments 用 server cursor，并在 `!has_more`、空 cursor 或空 page 时停止。Iterator 遇错误 yield error 后停止。 | API cursor 形态清晰，但不覆盖 favorites；空 page 与 malformed page 的区分仍不够严格。 |
| Persistence | request cache 按签名 hash 落盘、TTL、tmp rename；匿名 session 落盘。`crawl` 将 note/user/comment 立即写 JSONL，但 frontier 和 seen set 只在内存，启动时 `os.Create` 会截断旧文件。 | “结果边产生边写”值得借鉴；不支持 resume、item state 或失败重试。 |
| Retry/error | `Status` 区分 empty/login/walled/token/notfound/gone/antibot/network/error；每种 exit code 独立。只有 `StatusNetwork` 自动 retry，带节流和递增退避；refusal 不缓存。 | 三个项目中最值得借鉴的 error semantics，但 `crawl` 上层吞掉单项失败。 |

### Code evidence

- Client 统一 pacing、cookie capture、cache 与 retry：[client.go L26-L72](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/client.go#L26-L72)、[client.go L177-L200](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/client.go#L177-L200)。
- Retry 只针对可重试状态，拒绝结果不缓存：[client.go L261-L310](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/client.go#L261-L310)。
- Empty 与 refusal 明确分离、cache policy：[status.go L56-L92](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/status.go#L56-L92)。
- HTTP/envelope/token 分类：[status.go L141-L186](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/status.go#L141-L186)、[status.go L199-L280](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/status.go#L199-L280)。
- 一状态一退出码：[cmd/xhs/main.go L28-L58](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/cmd/xhs/main.go#L28-L58)。
- 匿名 session TTL 与持久化：[session.go L14-L90](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/session.go#L14-L90)。
- Cursor pagination 参考：[user.go L235-L278](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/user.go#L235-L278)。
- BFS、内存去重、单项失败跳过：[cli/meta.go L192-L325](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/cli/meta.go#L192-L325)。
- JSONL writer 使用 `os.Create`，且 Close 在文件存在时不会返回先前 encode error：[cli/meta.go L344-L369](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/cli/meta.go#L344-L369)。
- Request cache 用 tmp rename，但 I/O 失败被静默忽略：[cache.go L24-L56](https://github.com/tamnd/xiaohongshu-cli/blob/b60f9a384ed32d002aaacbbff95fedb2bdc96682/xiaohongshu/cache.go#L24-L56)。

### Patterns to absorb

1. **“空结果是世界事实，拒绝是调用者状态”的区分。** 这应直接映射到当前 taxonomy：显式空收藏可以完成；AUTH/RISK/RATE/TOKEN 绝不能变成空 page。
2. **Error classification 先于 retry。** 只有 NETWORK/5xx 等短时可能变化的失败才立即重试；AUTH、TOKEN、ANTIBOT、CONTENT_UNAVAILABLE 走不同操作。当前 P1 的运行级与 note 级划分应保留。
3. **一类失败一个稳定机器语义。** CLI exit code、SQLite `last_error_status`、日志 summary 应共享同一 taxonomy，不能靠字符串解析。
4. **拒绝响应不缓存。** AUTH/RISK/RATE/TOKEN 是当时调用者状态，不能缓存成内容事实；这一原则也适用于 token cache、raw evidence 和任何未来 HTTP cache。
5. **全局节流器应支持 cancellation。** tamnd 的 limiter 由 context 打断，比裸 `sleep()` 更适合 Ctrl+C 和运行级中止。
6. **即时流式提交的原则。** JSONL 不是本项目的状态存储，但“每获得一个有用结果立即落盘，不等整个 crawl 完成”与 P1 的 page/note/media transaction 完全一致。
7. **保留 upstream code/message/endpoint。** SQLite 可继续存规范化 status，同时日志或 diagnostic evidence 应保留原始 code、message 和 endpoint，便于协议漂移审计。

### Patterns not to adopt

- 不复制 x-s/x-s-common signer 作为 P1 核心。它把协议逆向变化、fingerprint 和 API schema 维护成本引入 collector；当前目标更适合浏览器会话与网络拦截。
- 不采用内存 BFS frontier/seen map 作为 batch model。note 在 enqueue 时就标 seen，后续 fetch 失败只打印 `skip`，同一运行不重试，进程退出后 frontier 全丢。
- 不把 JSONL 当 resume state。当前 writer 会截断旧文件，也没有 pending/failed/checkpoint 语义；它只是 partial-output preservation。
- 不复制 crawl 的聚合成功语义。单 note/user/comment 错误被记录后继续，最终仍返回 nil；对 ingestion 来说会造成“部分失败但进程成功”。
- 不把 response cache 当同步 truth。缓存适合降低重复 GET，不证明 note 已 canonicalized 或媒体已落盘；带签名媒体 URL 还可能在 TTL 内已失效。
- 不直接照搬 pagination 的“空 page 即正常结束”。在 favorites acquisition 中，如果 `has_more=true` 却空 page、cursor 不前进或字段缺失，必须 fail-closed。
- 不依赖手工 cookie 字符串作为主认证路径；当前项目的 persistent browser profile 更符合个人账号和扫码登录目标。

### Important implementation inconsistency

CLI 文档/flag 描述称会 retry `429/5xx`，但代码将 429 分类为 `StatusWalled`，而 `Retryable()` 只接受 `StatusNetwork`。从安全角度，“429 不立即重试、让调用方冷却”更合理；应借鉴代码语义，而不是 README/flag 文案。这也说明外部 README 不能作为 contract。

## Project 3: ytf606/xhs2obsidian

### Core architecture

| Area | Implementation | Assessment for 1000+ sync |
|---|---|---|
| Acquisition | Electron/Obsidian 内维护隐藏 WebView，直接生成签名请求；favorites 调 `/api/sns/web/v2/note/collect/page`，detail 调 `/api/sns/web/v1/feed`。 | 是三个项目中 favorites API 链最完整的证据，但 signer/WebView 与 Obsidian 强耦合。 |
| Authentication | 持久 Electron partition 中扫码登录；通过 Electron session API 读取 HttpOnly cookie，fallback 到 `document.cookie`；调用 `/user/me` 验证后，将完整 cookie 字符串写入插件 settings。 | 登录验证思路好；cookie 明文持久化和日志泄漏风险不可接受。 |
| Pagination | 每页使用 server cursor/has_more；cursor、synced IDs、allSynced 持久化到 Obsidian plugin data。每页 detail+write 完成后才更新 cursor。 | 支持长 backfill，但永久 `allSynced` 会阻止之后发现新收藏；字段缺失被 `!!` 当 false，存在 fail-open。 |
| Persistence | `syncCursors[target]`、`syncedIds[target]`、`allSynced[target]` 是数组/JSON settings；Vault 按标题命名 Markdown 和媒体目录，文件存在即跳过。 | 不是 durable item state；无法表示 detail/media partial/error/attempt。标题冲突会覆盖不同 note。 |
| Retry/error | 全局请求间隔 30 秒，加随机批次延迟；没有主 acquisition retry policy。detail/comments/media 失败多处 catch 后继续；HTTP/API 错误为通用 Error。 | 节流保守，但 error semantics 与 recovery 不成立。 |

### Code evidence

- Favorites endpoint 与服务端 cursor/has_more：[xhs-api.ts L189-L210](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/xhs-api.ts#L189-L210)。
- Detail endpoint 与 token 传递：[xhs-api.ts L327-L344](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/xhs-api.ts#L327-L344)。
- 全局 30 秒 throttle：[xhs-api.ts L113-L132](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/xhs-api.ts#L113-L132)。
- Cursor/syncedIds/allSynced state：[types.ts L35-L60](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/types.ts#L35-L60)、[types.ts L93-L113](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/types.ts#L93-L113)。
- 主同步循环：[sync-engine.ts L22-L145](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/sync-engine.ts#L22-L145)。
- Detail 失败后继续用 listing 数据、写完即加入 synced IDs：[sync-engine.ts L75-L123](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/sync-engine.ts#L75-L123)。
- Media failure 被吞掉、文件存在即跳过：[vault-writer.ts L84-L138](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/vault-writer.ts#L84-L138)。
- 标题作为 note 和 media 路径：[vault-writer.ts L84-L97](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/vault-writer.ts#L84-L97)、[vault-writer.ts L146-L211](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/vault-writer.ts#L146-L211)。
- Persistent WebView login、HttpOnly cookie extraction 和 `/me` 验证：[login-modal.ts L58-L171](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/login-modal.ts#L58-L171)。
- Full cookie 被拼进 curl debug 日志：[sign-manager.ts L153-L177](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/sign-manager.ts#L153-L177)。
- Video/image 通过 WebView 读成 base64 后整块写入：[sign-manager.ts L189-L216](https://github.com/ytf606/xhs2obsidian/blob/d69ae7fc797b94034c781fde4ad0887c5d0e4db2/src/sign-manager.ts#L189-L216)。

### Patterns to absorb

1. **服务端 favorites pagination 是真实存在的。** `collect/page` 返回 notes/items、cursor、has_more，listing item 提供 note ID 与 xsec token。这支持当前计划优先截获服务端响应，而不是从 DOM 数量推断完成。
2. **小 page + 每页 durable admission。** batch size 5–10 的意义主要是控制列表 API 风控和缩小失败半径。当前项目应在收到每一页时立即 upsert refs；不必等全量 list 返回。
3. **cursor 只能在 page 被可靠接纳后推进。** xhs2obsidian 在处理完 page 后保存 cursor；当前项目虽然选择重头枚举，不用 cursor 恢复，也应记录“该页已完整解析并持久化”的 proof，不能先写 completion metadata 再 upsert refs。
4. **登录后必须主动验证。** 仅存在 cookie 不代表有效 session；调用等价于 `/me` 的低成本 preflight 或页面 auth snapshot 很有价值。
5. **统一请求节流。** 一个 collector/session 共享 limiter，避免 list、detail、comments 各自认为自己低频、合计却超限。P1 当前只做 favorites/detail/media，也应共享运行级 budget。

### Patterns not to adopt

- 不复制 direct signer、固定 fingerprint、WebView `x-rap-param` interception。这是高维护、客户端特定实现，不是 acquisition contract。
- 不使用 `syncedIds[] + cursor + allSynced` 代替 item state。它不能表达 partial failure、attempt、媒体进度和 artifact evidence。
- 不设置永久 `allSynced` latch。收藏是变化集合；到达末页只说明本次 enumeration 完成，不代表未来永远无新增。
- 不在 detail 失败后用列表卡片生成“完成”笔记。xhs2obsidian 在 catch 后仍写 listNote 并加入 synced ID，是明确 fail-open。
- 不在媒体下载失败后仍把 note 标 synced。媒体错误必须进入 `MEDIA_PARTIAL`，不能只写日志。
- 不用“文件存在”作为媒体完成证明；必须验证 size/checksum，并使用 `.tmp` + atomic rename。
- 不用标题作为 storage identity。不同 note 可同名，note 标题也可变化；当前 `data/<note_id>` 设计必须保留。
- 不把视频整块 base64 搬进 Electron/JS 内存。1000+ 场景应 streaming download；现有 xhs-ingest 媒体实现方向更好。
- 不持久化或打印完整 cookie。特别禁止像该项目一样将 `Cookie:` 拼入可复制的 curl 日志。
- 不把 `!!data.has_more` 当 schema validation；字段缺失与 false 必须是两个状态。

## Cross-Project Pattern Matrix

| Pattern | Source | Decision | How it maps to current P1 |
|---|---|---|---|
| Dedicated persistent browser profile | ONEMULE | **Adopt** | 作为 auth/browser acquisition 主路径；增加 doctor/preflight。 |
| Capability-based provider boundary | ONEMULE | **Adopt narrowly** | 只在 capability unavailable 时 fallback；不为 P1 引入通用 provider framework。 |
| DOM stability means end-of-list | ONEMULE | **Reject** | 继续坚持 server `has_more=false`；停滞是失败。 |
| Typed response status and stable exit code | tamnd | **Adopt** | 与 `AcquisitionStatus`、SQLite error、CLI exit summary 对齐。 |
| Retry only transient failures | tamnd | **Adopt** | NETWORK/5xx 短退避；AUTH/RISK/TOKEN/CONTENT 各走专用策略。 |
| Do not cache refusals | tamnd | **Adopt as invariant** | AUTH/RISK/RATE/TOKEN 不能成为 raw/content/empty evidence。 |
| In-memory BFS + JSONL output | tamnd | **Reject as state model** | JSONL 可用于诊断/导出，不能替代 SQLite work state。 |
| Direct signing client | tamnd, xhs2obsidian | **Reference only** | 用于理解 endpoint/fields；不成为 P1 核心依赖。 |
| Favorites cursor/has_more API | xhs2obsidian | **Adopt contract evidence** | Browser interception 捕获完整 response；逐页 upsert。 |
| Cursor + synced ID arrays | xhs2obsidian | **Reject** | 继续使用 note-level SQLite lifecycle。 |
| Shared throttling and randomized pacing | tamnd, xhs2obsidian | **Adopt** | 一个 session/run 统一控制 list/detail 请求节奏，支持取消。 |
| Permanent all-synced switch | xhs2obsidian | **Reject** | 每次 sync 都重新从头发现新增；完成只属于一次 enumeration run。 |
| Existing file means complete | xhs2obsidian | **Reject** | 保留 P1 size/checksum/state 三方校验。 |

## Missing or Under-Specified Problems in Current P1

以下问题不是要求扩展到 P2，而是直接影响 1000+ favorites 的 P1 正确性。

### 1. Full enumeration can still starve already discovered work

`P1_INCREMENTAL_SYNC_PLAN` 仍以“全量枚举完成 → 开始 ingest”为主流程；`P1_STATE_CONTRACT_V2` §3.2 也写成“枚举后执行”。如果每次长枚举都在第 600 项附近被风控，已 durable upsert 的 600 个 `PENDING` 可能每次都因下一次运行先重新枚举而长期得不到消费。

外部启示：xhs2obsidian 按 page 获取后立即消费，tamnd crawl 按 record 流式提交。当前项目不必复制其状态模型，但需要明确：启动先清 media/detail debt；枚举中断若不是 AUTH/RISK 等必须立即停止一切的全局错误，应允许消费已 durable admitted 的工作；至少不能让“必须先证明全量枚举完成”成为处理旧队列的前置条件。

### 2. `list_favorites() -> PageResult` 与“每拦截页立即 upsert”不匹配

计划要求每收到一个 collect/page 响应就独立事务 upsert，但单次 `list_favorites()` 最终返回一个聚合 `PageResult`，调用方无法在进程中途崩溃前接收已经截获的 page。除非 collector 内部直接依赖 StateStore，否则当前接口无法兑现 page checkpoint。

应在 acquisition boundary 明确 page iterator/callback/event sink；collector 仍不拥有 persistence，但 runner 必须能逐页接收。不要等方法返回后再批量写全部 refs。

### 3. “SQLite 会让第二进程 SQLITE_BUSY 失败”不是可靠互斥

`P1_STATE_CONTRACT_V2` §6.2 把 SQLite 文件锁当单实例保证。WAL 允许多个连接存在，短事务之间第二个 writer 可以成功；它不保证第二个 sync 在整个运行期持续失败。一个新进程还可能把另一个进程的活跃 `FETCHING/MEDIA_SYNCING` 当崩溃痕迹重置。

需要一个运行期 process lock，或 run ownership/lease。此处不需要 distributed lock，但必须把“单进程”从文档假设变成可执行 invariant。

### 4. Parse failure currently has no raw evidence

v2 提交协议是 `fetch -> normalize -> atomic write raw.json -> DETAIL_SUCCESS`。如果 normalize 失败，raw 尚未落盘；而原计划又说持续 `PARSE_FAILED` 时应人工查看 raw。两者矛盾。

Evidence-first 要求失败 payload 也可诊断，但不能冒充 `DETAIL_SUCCESS`。最小做法是把 sanitize 后的失败 payload 写到独立 diagnostic/quarantine artifact，并在错误记录中引用；不要用 canonical 或 stderr 代替原始证据。

### 5. `media_state` filename identity is not bound to raw generation

v2 明确在 URL refresh 后保留 media_state，并以 filename 命中旧完成项。若笔记被编辑、图片重排，新的 manifest 仍可能产生 `image_01.jpg`，旧 checksum/文件会被误认为新媒体已完成。xhs2obsidian 的标题/序号路径冲突展示了同一类 identity 错误。

需要把 completed media evidence 绑定到稳定媒体 identity 或 raw generation。若无法得到稳定 media ID，raw 整体替换后保守地重新验证/清理受影响 entry，比错误跳过安全。

### 6. Incremental discovery semantics must explicitly reject permanent completion

xhs2obsidian 的 `allSynced` 说明一个常见错误：到达一次末页后，未来 sync 直接返回，永远看不到新收藏。当前计划实际上选择了正确方向——每次从顶部全量枚举——但应把它写成 invariant：`SERVER_HAS_MORE_FALSE` 只完成当前 enumeration run，不会把 favorites source 标成永久 complete。

同时需要处理 cursor 不前进、cursor 循环、`has_more=true + empty page`、字段缺失和重复 page；这些都应是 `PARSE_FAILED`/enumeration incomplete，而不是结束。

### 7. Secret handling is absent from the P1 contract

三个外部项目暴露了认证实现的真实风险，尤其 xhs2obsidian 会保存完整 cookie 并在 debug curl 中输出。当前项目应明确：

- 日志、error meta、raw diagnostics 不得包含完整 cookie、Authorization、签名 header；
- persistent profile 和 token cache 使用 owner-only 权限；
- 错误 snapshot 做字段级 redaction；
- 测试验证 redaction，而不是只靠约定。

这属于 acquisition 安全边界，不是 UI 或 P2 范围。

### 8. Aggregate run result needs proof, not only status counts

tamnd `crawl` 展示了“逐项报错但最终 exit 0”的危险，xhs2obsidian 展示了“媒体失败只写日志但 synced ID 已提交”的危险。当前 P1 已定义 exit code，但还应要求 run summary 至少说明：枚举是否有完成证据、发现/已存在/完成/部分/可重试/终态数量、运行级中止原因。exit 0 必须同时满足 enumeration proof 与无未完成 work，不能只看本轮 queue 被遍历完。

### 9. Long-run resource and cancellation semantics need integration tests

1000 note 即使每篇只延迟 3 秒也接近一小时；xhs2obsidian 的 30 秒全局间隔会超过 8 小时。P1 已讨论 page rotation、SIGINT 和节流，但仍需测试：取消发生在 list、detail、raw write、media temp write、marker write 各阶段时，下一次运行从正确状态继续。浏览器 context 可长存，page 应周期轮换；timer/sleep 必须可取消。

### 10. Disk exhaustion and write failures must be first-class

图片/视频批量同步比数据库更可能先耗尽磁盘。任何 raw/media/canonical/marker write 的 `ENOSPC`、权限错误或 rename 失败都不能被归类为网络错误，也不能推进状态。应作为 local storage failure 停止或保留当前 note 可恢复状态。无需新建复杂状态，但 taxonomy/exit summary 需要能表达它。

## Recommended P1 Adjustments

在不扩大范围的前提下，建议在实现 `sync.py` 前只补以下 contract：

1. runner 启动先 recover，再优先 drain media resume 和已有 detail debt；枚举与消费不得形成全量 barrier。
2. favorites acquisition 改为逐页向 runner 交付，每页完整 schema validation 后立即 upsert；末页 proof 单独提交。
3. 加单实例运行锁；不要依赖 WAL 自动拒绝第二进程。
4. 明确 current-run enumeration state，不引入永久 `allSynced`。
5. 对 cursor 不前进、循环、缺失 `has_more`、空页但 has_more、page parse drop 设 fail-closed invariant。
6. 保存 sanitized parse-failure evidence，同时保持 `DETAIL_SUCCESS` 只代表可 normalize 的 raw。
7. 将 media completion 与 raw generation/稳定 media identity 绑定。
8. 将 auth/signature/cookie redaction 和 profile 权限写入 acquisition contract。
9. run exit 0 同时要求 enumeration completion proof 与无 incomplete item；输出可审计 summary。
10. integration tests 覆盖 page crash、item crash、media crash、SIGINT、第二进程、磁盘写失败和新增收藏后的再次发现。

## What Should Not Be Added

- 不引入 direct signing 作为 P1 主路径。
- 不引入 Obsidian、AI 分类、评论、搜索、账号订阅或收藏夹分类。
- 不引入通用 workflow engine、消息队列、event sourcing 或分布式锁。
- 不为性能先上并发；1000+ 的首要问题是恢复、风控与证据，不是吞吐。
- 不增加 response cache 作为同步正确性机制。
- 不使用 cursor 作为唯一增量边界；收藏列表会在头部新增，当前“每次从顶部重新枚举 + note_id upsert”更可靠。
- 不把外部项目的 `syncedIds`、JSONL、localStorage 或文件存在性当 state DB 替代品。

## Final Architecture Decision

继续采用当前项目的自建 adapter + SQLite state contract，不直接依赖三个项目中的任一 CLI/package。

- **Authentication/browser reference**：ONEMULE。
- **Failure taxonomy/retry/cache reference**：tamnd。
- **Favorites endpoint/pagination evidence**：xhs2obsidian。
- **Batch/recovery authority**：当前项目 `P1_STATE_CONTRACT_V2`，但必须先补齐本报告列出的 P1 contract gaps。

这三个项目的最大价值是提供独立证据：真实 favorites API 链确实可形成 `cursor -> note_id/xsec_token -> detail -> media`；同时也证明，如果只保存 cursor/seen IDs、吞掉 detail/media 错误或把滚动停滞当完成，系统看起来会“同步成功”，但并不具备可靠增量同步语义。
