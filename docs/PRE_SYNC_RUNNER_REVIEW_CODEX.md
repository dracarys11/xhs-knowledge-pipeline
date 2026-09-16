# xhs-ingest v0.2 Pre-SyncRunner Integration Review

审查日期：2026-09-16  
审查性质：只读 integration review；未修改任何代码  
审查目标：判断当前代码是否具备进入 SyncRunner Design / Implementation 的条件

输入说明：请求中的 `docs/GITHUB_ARCHITECTURE_REVIEW_SOL.md` 在仓库中不存在；本次读取的是仓库根目录下同名文件 `GITHUB_ARCHITECTURE_REVIEW_SOL.md`。

## Verdict

**需要先修 State；同时需要先修 Acquisition。当前不应进入 SyncRunner Implementation。**

更精确地说：

- **SyncRunner Design 可以继续做边界收口和验收用例设计**，但不应把当前接口当成可实施基线。
- **SyncRunner Implementation 应等待本文 P0 blocker 关闭。** 否则 runner 只能通过调用纪律绕开组件缺陷，无法获得 State Contract v2 和 Acquisition Architecture 所承诺的 fail-closed 保证。

阻塞原因不是缺少 workflow engine、队列服务、并发或分布式能力；恰恰相反，是现有简单架构的几个核心 invariant 尚未在代码中成立：

1. State 层在 schema upgrade、retry exhaustion、终态人工重置和强制 `MEDIA_SYNCING` 上仍未落实 v2 contract。
2. Acquisition 层没有可信的 favorites pagination/completion proof，也无法逐页向 runner durable admission。
3. 详情 fallback 没有验证返回 note 的身份，可能将 B 的内容记到 A 的状态流程中。
4. auth/profile 定位存在 fail-open 路径；1000+ 场景仍是每篇新启一个 browser context。
5. 当前没有 collector 或端到端 integration tests 为以上链路提供证据。

测试实测：

- `./.venv/bin/pytest -q`：**49 passed in 0.26s**。
- 测试集中不存在 `test_collector.py` 或等价的 collector fixture 测试；49 个测试不覆盖服务端分页字段、逐页交付、当前用户 profile 解析、详情目标身份校验、token 失效传播或 browser session 复用。

## Review Baseline

本次不重新设计架构，采用已经冻结的方向：

```text
BrowserSession
    ↓
Collector
    ↓
ResponseInterceptor → FavoriteRef / PageResult
    ↓
Parser / normalize_note → CanonicalPost
    ↓
Media downloader

StateStore：独立 workflow truth；由未来 SyncRunner 编排
```

以下既有决策保持成立：专用 persistent Playwright profile、API response interception 为主、SSR 为 detail fallback、SQLite note-level state、两个状态投影队列、顺序执行、原子媒体落盘、raw/state/filesystem 三类事实分离。不建议引入任何新调度基础设施。

## 1. State Contract v2 落地情况

### 1.1 Queue API

已正确实现的部分：

- `get_fetch_queue()`（`state.py:291-319`）精确返回 `DISCOVERED`、`PENDING` 和预算内的 `RETRYABLE_FAILED`，并明确排除 media resume 工作。
- `get_media_resume_queue()`（`state.py:321-342`）精确返回 `DETAIL_SUCCESS`、`MEDIA_PARTIAL`。
- 两个队列按 `rowid` 排序、互斥；测试覆盖普通状态下的分离和 recovery 后路由（`test_state.py:246-351`）。

未闭合的部分：

- 达到 cap 的 `RETRYABLE_FAILED` 仍是非终态，但会从默认 fetch queue 消失；因此“双队列覆盖全部非终态”并不总成立。
- `DISCOVERED` 被作为 fetch queue 安全网返回，但不能直接 `DISCOVERED → FETCHING`；runner 必须识别它并先 transition 到 `PENDING`。API 可用，但不是完全同构的“取出即 fetch”队列。
- `MEDIA_SYNCING` 在 recovery 前不在任何队列是合同约定，不是 bug；runner 必须把 `recover_interrupted()` 固定为任何 queue query 前置动作。

### 1.2 `failure_stage`

`last_failure_stage` 已加入 schema/`NoteState`，值域限制为 `FETCH | NORMALIZE | MEDIA`，非失败目标会清除旧值。这部分方向正确。

但它当前只是一项可选诊断标签，并未真正绑定失败状态或执行路径（`state.py:396-413`）：

- failure target 可以完全不带 `error_status`/`failure_stage`；
- 当前状态为 `FETCHING` 时可以写 `failure_stage="MEDIA"`；现有测试还把这个不真实组合当作正常场景（`test_state.py:380-398`）；
- stage 与 `AcquisitionStatus` 没有相容性校验。

因此它能存值，但不能可靠回答“本次预算为什么被消耗”。若 runner 依赖它做路由或统计，会得到不受约束的数据。

### 1.3 Recovery semantics

基础映射正确：

- `FETCHING → PENDING`，attempt 保留；
- `MEDIA_SYNCING → DETAIL_SUCCESS`，media_state 保留且不新增 detail attempt；
- 两条 UPDATE 在同一个事务中执行（`state.py:482-500`）。

边界错误：

- 当 crash 发生在第 5 次 `FETCHING`，recovery 仍无条件改成 `PENDING`；`get_fetch_queue()` 对所有 `PENDING` 无条件放行，因而会开始第 6 次 fetch。
- 重复 crash 可以无限绕过预算。当前“crash 会消耗 attempt，因此可防 crash loop”的设计结论没有被实现。
- `MEDIA_SYNCING → DETAIL_SUCCESS` 后 raw.json 是否存在由 runner 守卫，这符合职责边界；不应把磁盘检查塞入 StateStore。

### 1.4 Transaction boundary

正确部分：

- 页级 `upsert_notes()`、单 note transition、单媒体结果、recovery 和 meta mutation 均使用连接事务。
- `PENDING/RETRYABLE_FAILED → FETCHING` 与 attempt increment 是同一个 UPDATE。
- listing upsert 不覆盖 workflow progress；token/source/title 使用 `COALESCE` 保留已知值。
- WAL + `synchronous=FULL`、显式 `close()` 和 context manager 都已实现。

限制与风险：

- `transition()` 的 UPDATE 只按 `note_id`，没有 expected-current-status 条件。当前单线程单进程假设下可接受，但 SQLite WAL 并不会使第二个 sync 进程在整个运行期必然失败；两个进程可以在短事务间交错。
- 第二个进程还可能把第一个进程的活跃 transient status 当作 crash trace 执行 recovery。单实例必须成为 runner 生命周期 invariant，不能继续依赖“SQLite 会响亮拒绝”。这不需要 distributed lock。
- `record_media_result()` 的单文件事务适合 runner 逐文件 checkpoint；`download_post_media()` 不应作为 runner 的不可分割状态提交单元，runner 应利用已有单文件下载函数逐项提交结果。

### 1.5 Schema version

这是 State 的明确 blocker：

- 实现声明 `SCHEMA_VERSION = "2"` 并新增 `last_failure_stage`（`state.py:43-60`）。
- 初始化只做 `CREATE TABLE IF NOT EXISTS` 和 `INSERT OR IGNORE schema_version`（`state.py:190-202`）。
- 旧 v1 表不会获得新列，旧 version 也不会被更新或拒绝。
- `_row_to_state()` 无条件读取新列（`state.py:537-550`）。

已在前次审查中用真实 v1 形状的临时 SQLite 库复现：StateStore 能打开、版本仍为 1，第一次 `get_note()` 才抛缺列 `IndexError`。这既不是 migration，也不是 startup fail-closed。它还与 `P1_STATE_CONTRACT_V2` 的“Schema 无变更、无需迁移”文字冲突。

### 1.6 State overall assessment

StateStore 的主干模型可保留，不需要重构；但它当前是“新空库 happy path 可用”，尚不是 SyncRunner 可以完全信任的 contract implementation。至少要关闭 schema、retry cap、`FINAL_FAILED` 唯一出口和 D3 强制性四项问题。

## 2. Acquisition Architecture 一致性

### 2.1 BrowserSession boundary

符合冻结架构的部分：

- 使用专用 Playwright persistent context，浏览器原生持久化 Cookie/localStorage/IndexedDB；未导出完整 Cookie 字符串（`collector.py:87-105`）。
- 提供 headed interactive login 和独立 auth check。
- 生命周期、采集、response interception 虽然都内嵌在 `XhsPlaywrightCollector`，但 V1 明确冻结的是职责而非类拆分，因此“没有独立 BrowserSession 类”本身不是违规。

进入 runner 前的缺口：

- 每次 `list_favorites()` / `fetch_note()` 都启动并关闭整个 persistent context（`collector.py:252-254, 438-440, 484-568`）。1000 notes 意味着约 1001 次浏览器启动，V1 自己已将其列为 R5；这不满足 1000+ 同步的可实施性。
- Collector Protocol 没有 run-scoped open/close 生命周期。未来 runner 无法在不依赖具体类私有方法的情况下复用 context、轮换 page、保证 finally close。
- `time.sleep()` 贯穿登录、列表和详情，无法被 runner cancellation/SIGINT 及时打断。

### 2.2 Authentication and current-user resolution

这里存在两个 fail-open 风险：

1. `list_favorites()` 把页面上任意 `a[href*="/user/profile/"]` 当作登录证据（`collector.py:281-300`）。Explore feed 本身可能包含作者 profile 链接，因此匿名页可能被误判为已登录；这弱于冻结架构宣称的 `guest === false && redId` 严格判定。
2. profile URL 定位首先返回页面上第一个 profile 链接（`collector.py:302-315`），没有证明它属于当前用户。若命中 feed 作者，collector 会导航到错误用户的 profile/favorites 页面。即便私人收藏无法加载，错误最终也可能被归为 unknown/empty，而不是明确 auth/current-user identity failure。

`check_auth()` 相对严格，但 `list_favorites()` 没有复用同一判定。进入 runner 前必须统一为可证明的 current-user identity；不能让 DOM 中“存在某个头像/某个 profile link”替代认证证据。

### 2.3 Collector boundary

`Collector` Protocol 已存在，提供 `list_favorites()` 与 `fetch_note()`，且 StateStore 未泄漏进 acquisition 层。这一点符合职责分离。

但当前协议仍是 P0 单次调用形态，不满足 page-durable runner：

- `list_favorites() -> PageResult` 只在整个滚动过程结束后一次返回聚合结果。
- runner 无法在每个 `/collect/page` response 到达并通过校验后立即 `upsert_notes()`。
- 若进程在第 20 页崩溃，已经拦截的前 19 页仍只在 collector 内存 list 中，全部丢失。

冻结的 State Contract 要求“每页单事务 admission”，而当前 Collector API 无法交付这个能力。需要在 Acquisition V2 中收口逐页交付边界，但 collector 仍不应直接依赖 StateStore。

### 2.4 ResponseInterceptor boundary and pagination truth

当前 interceptor 能捕获 `/api/sns/web/v2/note/collect/page`，只接受 JSON 且 `success` 为真，并提取 `data.notes`。这是正确主通道的雏形。

但真实 pagination contract 尚未实现：

- 响应中的 server `cursor` / `has_more` 被丢弃（`collector.py:260-272`）。
- `PageResult.next_cursor` 从未赋值。
- 非空结果的 `has_more` 用 `len(items_by_id) >= limit` 推断（`collector.py:431-436`），不是服务端事实。
- 固定最多滚动 6 次（`collector.py:362-367`）；慢网络、重复页或 1000+ favorites 都可能在没有终止证据时停止。
- 收集数小于 limit 时返回 `has_more=False`，可以把网络停滞、解析失败或风控未识别误报为末页。
- `FOUND_N_ITEMS` 只证明发现了 N 项，不是 enumeration completion proof。
- callback 的 JSON/schema 异常被 `except Exception: pass` 静默吞掉。若前面已有部分 items，最终仍会返回非空 `PageResult`，parser drop 不会使整次枚举失败。
- `PageResult.raw` 只保存总条数，不保存经过脱敏的原始页 envelope、字段位置或上游错误证据。

因此当前代码不能回答“所有收藏是否已经完整遍历”。这是 Acquisition 的首要 P0 blocker。

逐页实现时仍应坚持现有冻结方向：滚动仅触发请求；`server has_more is False` 才能证明当次 enumeration 完成；缺字段、cursor 不前进/循环、`has_more=True + empty page`、response parse drop 全部 fail closed。无需引入 cursor 作为永久同步状态。

### 2.5 FavoriteRef output and xsec_token lifecycle

已实现：

- `FavoriteRef` 明确定义了 `note_id`、`source_url`、`xsec_token`、作者、封面和 raw listing evidence（`models.py:12-25`）。
- listing parser 从 `note_id/id` 和 `xsec_token/xsecToken` 构造 ref；有 token 时 source URL 携带 `xsec_source=pc_fav`（`collector.py:369-401`）。
- State upsert 持久化 token/source URL，重复枚举可以刷新两者，缺失新 token 时保留旧 token。
- `fetch_note(FavoriteRef)` 接受该交接对象，P0 链路真实存在。

未闭合：

- token side cache 是非原子 JSON 写，读写异常全部静默吞掉，无时间/来源/version，也未设置 owner-only 权限（`collector.py:70-85`）。它只能是便利缓存，不能成为 runner truth。
- 裸 note ID 缺 token 时，`fetch_note()` 调 `list_favorites(limit=50)`；该调用的所有异常（包括 AUTH_REQUIRED、RISK_CONTROLLED、RATE_LIMITED）都被 `except Exception: pass` 吞掉（`collector.py:472-482`），随后以裸 URL 继续，可能把全局认证错误降级为 TOKEN_INVALID 或 PARSE_FAILED。
- 只查前 50 条无法为第 51 条以后的历史收藏解析 token；对于 state queue，runner 必须使用枚举持久化的 ref/token，而不是依赖该 fallback。
- `fetch_note(FavoriteRef)` 实际导航 `source_url`，不会用 `FavoriteRef.xsec_token` 重建/核对 URL。若两字段因调用方拼装或独立刷新而不一致，token 字段并不是真正的访问权威。
- token cache 更新没有记录获取时间；签名/token 失效时，当前 collector 也没有“重新枚举刷新后重试”的明确 API 结果，只能由未来 runner 猜测。

结论：FavoriteRef contract 已存在，但 token 生命周期尚未达到批量 runner 可依赖的程度。State 中的 listing snapshot 应是 runner 的持久事实，side cache 不应影响正确性语义。

### 2.6 Detail parser identity safety

这是独立于 pagination 的 P0 数据正确性问题：

- SSR JS 先查 `noteMap[targetId]`；若不存在，会返回 map 中第一条非空记录（`collector.py:530-548`）。
- feed fallback 返回第一份具有非空 `data.items` 的 payload，没有检查 item 的 note_id 是否等于请求目标（`collector.py:553-559`）。
- `normalize_note()` 会接受 payload 自身的 note_id，但 runner 很可能正在处理另一个 State row。

可能结果：runner 对 A 执行 `FETCHING`，collector 实际返回 B，normalizer 生成 B，若 runner 未做额外 guard，A 的状态可能进入 `DETAIL_SUCCESS`，而文件被写到 B 或 A/B 证据错配。State 与磁盘 truth 从此分叉。

Collector 必须保证成功返回的数据身份等于请求目标；不匹配必须 `PARSE_FAILED` 并保留脱敏诊断证据。不能把这个 guard 只留给未来 runner，因为它是 acquisition result contract 的一部分；runner 仍应做第二道 assertion。

### 2.7 Parser boundary

`normalize_note()` 是无 I/O 纯函数，支持 feed、SSR map 和裸 note card，并在 note_id 缺失时抛 `PARSE_FAILED`；raw payload 被完整保留。该职责边界正确。

仍需注意：

- author/title/text 的缺失会被转换成空字符串或 `Unknown Author`（`normalizer.py:137-149`），与 Architecture V1 所写“字段缺失表现为 None，不默认填充”并不一致。它不会伪造 note identity，但会模糊数据缺失证据。
- feed parser 默认取 `data.items[0]`，若上游没有先做 target identity filter，同样放大错 note 风险。
- media filename 是按当前列表顺序生成的 `image_01...`。重 fetch 后图片重排/替换时，State Contract D7 保留的旧 media_state 可能错误命中新 manifest 的同名文件。这一风险已由 `GITHUB_ARCHITECTURE_REVIEW_SOL.md` 指出，当前代码未解决。

### 2.8 Media integration readiness

可复用能力：

- `download_media_item()` 流式写固定 tmp，非零检查、SHA256、原子 replace；失败清 tmp 并抛结构化 `MediaDownloadError`。
- `download_post_media()` 顺序执行且不把部分失败报告为成功。
- 单文件函数足以让 runner 在每次下载后调用 `record_media_result()`，无需并发或新下载框架。

缺口：

- 没有内建 retry/backoff 或 403 URL-expired 分类；这可以由 runner policy 包装单文件函数，不要求重写 media.py。
- 本地 `ENOSPC`、权限错误、rename 失败都会被包装为 `MEDIA_DOWNLOAD_FAILED`，与网络/CDN 失败不可区分。runner 无法可靠决定“重试媒体”还是“停止运行并修复本地存储”。
- skip-if-verified 属于 runner，不应加到通用 downloader；但 media identity/raw generation 必须先有明确 guard，否则 filename 命中不安全。
- 测试未覆盖 interrupted stream、已存在正确/错误目标文件、403 分类、磁盘写失败，以及单文件结果写 State 后的 crash resume。

## 3. Integration Gap

### P0 blocker — 必须修复后才能进入 SyncRunner Implementation

#### State

1. **Schema v1→v2 策略**：实现单事务迁移，或在 open 时以明确错误拒绝旧/未知版本；不能延迟到首次 row decode 才崩溃。
2. **Retry budget 闭环**：达到 cap 的可重试失败必须原子进入 `FINAL_FAILED`；FETCHING crash recovery 不能通过 `PENDING` 绕过 cap；两个队列不得留下隐藏非终态。
3. **`FINAL_FAILED` 唯一出口**：通用 `transition(FINAL_FAILED, PENDING)` 必须被拒绝，只有 `retry_failed()` 可执行清零重置。
4. **D3 强制性**：`record_media_result()` 只允许 `MEDIA_SYNCING`；当前允许在 `DETAIL_SUCCESS/MEDIA_PARTIAL` 直接写 media，且测试固化了旧语义。

#### Acquisition

5. **真实逐页分页 contract**：保留并严格验证每个 collect response 的 notes、cursor、has_more；只有 server `has_more is False` 才能完成当次 enumeration；删除 `items >= limit` 与固定滚动次数产生的伪完成语义。
6. **逐页交付给 runner**：通过窄的 iterator/callback/page sink 让每页通过 schema validation 后立即交给 runner `upsert_notes()`；collector 不直接拥有 StateStore。当前聚合返回接口无法满足 page checkpoint。
7. **目标 note 身份绑定**：SSR 和 feed fallback 都必须只接受与 requested note_id 一致的 payload；不匹配 fail closed。runner 再做一次 `post.note_id == state.note_id` assertion。
8. **严格 auth/current-user proof**：不能用页面任意 profile link 证明登录，也不能把第一个 profile link 当当前用户。统一 `check_auth`、`list_favorites` 和 profile resolution 的 current-user 身份证据。
9. **运行级 browser session 生命周期**：支持一个 run 复用 context、有限次轮换 Page、最终可靠关闭。不能让 1000 note 触发 1000 次 browser context 启停。
10. **禁止吞掉影响 taxonomy 的异常**：response parse drop、token resolution 中的 AUTH/RISK/RATE/NETWORK 必须保留原类别并中止或按 policy 处理；不得 `except Exception: pass` 后继续产生成功/其他错误。

#### Cross-component invariants

11. **媒体 identity 与 raw generation 绑定**：在保留旧 media_state 前，必须能证明同 filename 仍代表同一媒体；否则重 fetch 后保守失效旧 entry。无需新媒体表，但不能只按位置文件名跳过。
12. **最小 integration test 链**：用录制/fixture payload（不登录真实账号）覆盖 page1→pageN→server end、FavoriteRef token→target detail、target mismatch fail-closed、每页 admission crash、detail crash、media crash 和 resume。没有这些测试，runner 无法以 evidence-first 方式验收。

### P1 — 进入 SyncRunner 前建议修复或明确写入 runner contract

1. `failure_stage` 是强 invariant 还是可选诊断字段必须二选一；若是强 invariant，校验 failure target、来源阶段和 status 的合法组合。
2. 启动时验证所有 DB status；queue/recovery/status_counts 不得静默忽略未知状态。
3. 用 run-scoped 本地单实例锁落实单进程约束，防止第二进程重置活跃 transient state。不是 distributed lock。
4. `NETWORK_ERROR` 必须有真实触发点：映射 Playwright timeout、连接失败和 response body 读取失败；UNKNOWN 不能代替 transient transport error。
5. token side cache 改为原子、可诊断、owner-only；明确它不是状态事实。对 runner 路径，优先使用 State listing snapshot。
6. 定义 token/source_url 一致性：访问 URL 必须由同一次 listing snapshot 的 note_id、token、source 组成，不能独立拼接潜在陈旧字段。
7. 保存 sanitized parse-failure/page-failure evidence，但不得把失败 payload 写成 `raw.json` 或推进 `DETAIL_SUCCESS`。
8. 为 profile、token cache 和 diagnostic artifact 明确权限/脱敏测试；当前 sanitizer 仅覆盖 key 中含 cookie/authorization 的数据。
9. 本地存储失败应区别于 `MEDIA_DOWNLOAD_FAILED`，至少在 run result 中能够停止并报告 ENOSPC/permission/rename failure。
10. run summary/exit 0 必须同时依赖本次 enumeration completion proof 和无未完成 item，不能只依赖“循环跑完”或 status count。
11. cancellation/SIGINT 必须停止派发新工作、关闭 browser，并让当前 note 停在可恢复状态；裸 `time.sleep()` 不应阻塞取消。
12. 对 `COMPLETE` 文件守卫、`.complete` 修补、raw 原子覆盖、canonical/post 原子写及提交顺序编写 runner integration tests。

### P2 — 可在 SyncRunner 基础闭环后处理

1. LivePhoto 附属视频提取。
2. 视频按 size/质量选择最佳 stream，而不是第一个 codec 的首个 URL。
3. author/title/text 缺失改为显式 optional schema，消除 `Unknown Author`/空字符串对缺失事实的掩盖。
4. 更丰富的浏览器诊断 snapshot 和 selector drift 可观测性。
5. `upsert_notes()` 对同一 batch 重复 note_id 的 inserted/updated 计数精确性，以及超大单批次 SQLite bind limit 防护；逐页小 batch 下不阻塞。
6. 包版本与文档元数据更新：`pyproject.toml` 仍声明 `version = "0.1.0"`、描述仍为 P0；请求称当前为 v0.2。
7. 将根目录 `GITHUB_ARCHITECTURE_REVIEW_SOL.md` 与文档引用路径统一，避免审查输入漂移。

## 4. Readiness by Contract

| Contract | Current status | Ready for runner? |
|---|---|---|
| State queue separation | 已实现，普通路径测试充分 | 条件就绪 |
| State retry/recovery budget | cap 与 crash recovery 可绕过 | **否** |
| State schema lifecycle | v2 新列无 migration/version reject | **否** |
| State media-phase trace | API 允许跳过 MEDIA_SYNCING | **否** |
| Browser authentication base | persistent profile 与登录入口存在 | 条件就绪；current-user proof 必须修 |
| FavoriteRef handoff | 模型、token、source URL 已存在 | 条件就绪；token consistency 必须修 |
| Favorites pagination | server cursor/has_more 被丢弃 | **否** |
| Per-page durable admission | Collector 只最终聚合返回 | **否** |
| Detail identity | fallback 可返回非目标 note | **否** |
| Parser | 纯函数、identity missing fail-closed | 基本就绪 |
| Media primitive | 顺序流式、tmp、hash、replace | 基本就绪 |
| 1000+ session lifecycle | 每 note 新 context | **否** |
| Integration evidence | 无 collector/跨组件测试 | **否** |

## 5. Minimum Entry Gate for SyncRunner

关闭 P0 不要求重新设计系统。进入 implementation 前只需达到以下可验证条件：

1. StateStore 对旧/未知 schema 在 open 时确定地迁移或拒绝。
2. 任意失败/crash 序列都不能产生超过 cap 的自动 fetch，也不能留下非终态但不在任何队列的 row。
3. 任意媒体结果写入前，note 必须处于 `MEDIA_SYNCING`。
4. favorites 每个 server page 都能独立交付和持久接纳；末页只能由显式 `has_more=false` 证明。
5. collector 成功返回 detail 时，payload note_id 必须等于请求 note_id。
6. auth 和 current-user profile identity 有单一、严格、测试化的判定。
7. 一个 collector run 可复用 browser context，并能可靠关闭；Page 可按既有计划轮换。
8. fixture integration test 能演示：page admission → FavoriteRef/token → correct detail → normalize → per-file media state → COMPLETE，以及在 page/detail/media 三个 crash 点重启恢复。

达到以上 8 条后，可以开始实现薄的、顺序的 SyncRunner。它只需要编排现有组件、执行文件提交协议和错误路由；不需要 workflow engine、queue service、concurrency 或 distributed architecture。

## Final Decision

```text
Verdict:
  需要先修 State
  需要先修 Acquisition
```

理由：State 的 durable lifecycle 仍可在 schema upgrade、retry cap 和终态出口处破坏 contract；Acquisition 尚不能提供可证明完整的 favorites pages，也不能保证 detail 属于请求 note。两边任一项未修，SyncRunner 都只能把不确定结果写成确定状态，放大而不是修复错误。

**因此：可以继续 SyncRunner 的设计评审与测试场景定义；不得开始依赖当前接口的 production implementation。**
