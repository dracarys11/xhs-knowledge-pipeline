## Verdict

**Needs contract adjustment**

当前架构方向不需要重做。StateStore v2.1 的核心能力已经足以支撑单线程、单进程的 SyncRunner：双队列、崩溃恢复、严格 transition、写入时 retry 封顶、逐媒体执行状态和 schema fail-closed 均已实现，现有测试也全部通过（`61 passed`）。

但 `SYNC_RUNNER_DESIGN.md` 还不能原样映射为代码。阻塞点分为两类：

1. 设计与已经 harden 的 StateStore 语义不一致，包括 retry budget 的责任归属、resume normalize 失败路径、`DISCOVERED` 的消费方式和退出码判定。
2. 设计依赖的 collector contract 尚未实现，包括真实分页、运行级 browser context、网络错误映射和 token 行为；当前 `PageResult` 只是形状存在，分页字段不具备设计要求的证据语义。

因此可以开始实施工作，但第一步必须是修订这些窄契约并完成 collector 前置包；不能直接从现有文档照写完整 `sync.py`。这属于 **contract adjustment**，不是 architecture revision。

当前代码到设计的阶段映射如下：

| Runtime phase | 输入 | 设计调用 | 当前输出能力 | 失败/缺口 |
|---|---|---|---|---|
| Bootstrap | CLI 参数、DB 路径、输出目录 | 打开 `StateStore` | schema/version/status 严格校验；WAL + FULL | 没有覆盖整次运行的单实例保证；SQLite WAL 不会可靠拒绝第二个进程 |
| Startup Recovery | DB 中的 crash trace | `recover_interrupted()`、可选 `retry_failed()`、扫描 COMPLETE | 两种中间态恢复与预算封顶已实现 | 无公开 API 枚举 COMPLETE，无法实现 artifact guard scan |
| Enumeration | 已认证 browser session | `session()` + `iter_favorites()`，逐页 `upsert_notes()` | `FavoriteRef`/`PageResult` 类型和页级 upsert 已存在 | session、迭代分页、可靠 cursor/has_more 均不存在 |
| Media Resume | `DETAIL_SUCCESS` / `MEDIA_PARTIAL`、raw.json | `get_media_resume_queue()`，normalize，媒体补齐 | resume 队列与 per-file state 已存在 | normalize 失败无法按设计转入 `RETRYABLE_FAILED(stage=NORMALIZE)` |
| Fetch | `DISCOVERED` / `PENDING` / `RETRYABLE_FAILED` | `FETCHING` → fetch → normalize → raw → detail/media | fetch queue、严格状态机、`FavoriteRef` 重建字段已存在 | `DISCOVERED → FETCHING` 非法；collector 不能复用 session，且没有目标 note identity 保证 |
| Finalize | 全状态计数、运行结果 | `status_counts()`、meta、exit code | 计数与单 key meta 写入已存在 | exit 3 的定义遗漏多种非 COMPLETE 状态；多 key 运行证据不是原子提交 |

## Blocking Issues

### 1. Media resume 的 PARSE_FAILED 路径在当前状态机中不可执行

设计在 `SYNC_RUNNER_DESIGN.md §5.2` 规定：从 `DETAIL_SUCCESS` 或 `MEDIA_PARTIAL` 读取 raw.json，`normalize()` 失败后进入 `RETRYABLE_FAILED(stage=NORMALIZE)`。

当前实现不允许这条路径：

- `DETAIL_SUCCESS` 没有到 `RETRYABLE_FAILED` 的 edge（`state.py:128`）。
- `MEDIA_PARTIAL` 虽可到 `RETRYABLE_FAILED`，但 failure-stage 绑定把该来源限制为 `MEDIA`，拒绝 `NORMALIZE`（`state.py:137,162-164,477-490`）。

这会使合法的恢复失败无法落库，只能冒泡成 exit 4，或者诱使实现者绕过状态机。实现前必须二选一并写入契约：允许 resume-originated NORMALIZE failure，或把不可重现的 raw.json 明确定义为 evidence 失效并降级到 `PENDING`。不能由 `sync.py` 临时猜测。

### 2. Fetch loop 对 `DISCOVERED` 的伪代码会触发非法 transition

`get_fetch_queue()` 明确返回 `DISCOVERED`（`state.py:368-393`），但设计的每条 note 第一步直接调用 `transition(FETCHING)`。状态机只允许 `DISCOVERED → PENDING → FETCHING`（`state.py:119-124`）。

枚举成功后应把“本次完成枚举实际看见的 ID 集合”传给 `mark_pending(ids)`；fetch loop 仍需对安全网中残留的 `DISCOVERED` 明确先接纳，或设计必须证明它们不可能出现。不要实现 `mark_pending(数据库内所有 DISCOVERED)`：枚举中断遗留、后来已取消收藏的旧记录也会被无条件接纳，超出本次枚举证据。

### 3. COMPLETE artifact guard 没有可用的 StateStore 查询 API

Startup Recovery 要扫描每一条 COMPLETE note，但 StateStore 只有 `get_note(note_id)` 和 `status_counts()`，没有按状态列出记录的方法（`state.py:641-668`）。`sync.py` 不应访问私有 `_conn`。

需要一个窄的只读 API，例如按单一状态查询，或专用 `get_complete_notes()`。否则设计中的 `.complete` 修复/降级流程不能通过公开 contract 实现。

### 4. Retry budget 的所有权与 v2.1 实现冲突

设计仍写“预算检查是 sync.py 职责，state 层不执行策略”（`SYNC_RUNNER_DESIGN.md §4`）。当前 v2.1 已正确地把预算封顶放入 mutation transaction：请求 `RETRYABLE_FAILED` 时，达到 5 次会原子落为 `FINAL_FAILED`（`state.py:450-455,509-524`）；crash-at-cap 也由 recovery 直接终结（`state.py:591-625`）。

应更新设计为：runner 只请求 retryable failure，StateStore 决定并返回实际状态；runner 根据返回的 `NoteState.status` 记录终结日志。若 runner 再做一份预算判断，会形成两个 policy owner，并可能因读写窗口产生分歧。

### 5. Collector 的三个运行时前置 contract 尚不存在

当前 `Collector` Protocol 只有 `list_favorites(limit)` 与 `fetch_note()`（`collector.py:31-38`）：

- 没有 `session()`；每次 list/fetch 都各自启动并关闭 persistent context（`collector.py:252-254,438-440,484-485,566-568`）。1000+ note 下无法满足设计的运行级生命周期。
- 没有 `iter_favorites()`。list 只滚动最多 6 次，并按 `limit` 截断（`collector.py:362-367,406`）。
- 拦截器只保存 `data.notes`，丢弃 server cursor/has_more（`collector.py:260-272`）；返回的 `has_more` 是 `len(items) >= limit` 推断，`next_cursor` 永远为空，`FOUND_N_ITEMS` 不是完成证据（`collector.py:431-435`）。

因此 `PageResult` 的数据类字段虽然存在（`models.py:28-44`），但还不能作为 SyncRunner 的全量枚举证据。设计中 D-A、D-C 必须先实现并以 fake/fixture payload 测试后，Enumeration 才可接线。

### 6. Collector 不能保证返回的是请求的 note

SSR 找不到目标 key 时，当前代码返回 map 中第一个非空条目；feed fallback 也返回第一份含 items 的 payload，不检查 item identity（`collector.py:530-559`）。normalizer 同样从第一个 map/item 提取（`normalizer.py:63-85`）。

在批量同步中，这可能把 note B 的 raw/canonical 写入 note A 的目录，并把 A 推进成功状态，比显式失败更严重。collector 应优先修正目标筛选；无论如何，`sync.py` 在写 raw.json 和 `DETAIL_SUCCESS` 前必须验证 `post.note_id == queued NoteState.note_id`，不匹配按 `PARSE_FAILED` fail closed。

### 7. Token 行为与设计中的生命周期规则冲突

设计要求枚举刷新 token，fetch 使用 `NoteState → FavoriteRef`，TOKEN_INVALID 留待下次枚举刷新。当前 `fetch_note()` 在 token 缺失时会隐式调用 `list_favorites(50)`，吞掉所有异常，再继续访问（`collector.py:472-482`）。在未来 session 内，这会造成隐藏的二次枚举、可能嵌套启动 context，并把 AUTH/RISK/NETWORK 原因抹掉。

必须在 collector contract 中明确：SyncRunner 传入 `FavoriteRef` 时不得隐式枚举；缺失/失效 token 应结构化失败。P0 裸 note_id 命令是否保留 token-cache convenience path，可以独立决定，不应污染 sync 路径。

### 8. Exit code 0 的条件与状态全集不一致

设计文字说 exit 0 代表“全部 COMPLETE”，但触发表只检查没有 `MEDIA_PARTIAL / RETRYABLE_FAILED / FINAL_FAILED`。`DISCOVERED`、`PENDING`、`DETAIL_SUCCESS` 仍可能因 `--max-notes`、中断边界或守卫降级而存在。若照表实现，切片运行可能错误返回 0。

Finalize 必须基于全状态计数定义：只要已跟踪集合中存在任何非 COMPLETE 状态，正常走完应返回 3；运行级中止仍按优先级返回 2。`FINAL_FAILED` 也应继续使结果为 3，除非未来另行定义“终态失败也算同步完成”，本阶段不应引入这种语义。

### 9. “SQLite 文件锁会拒绝第二实例”不是现有保证

StateStore 使用 WAL 和短事务，但没有持有整个运行周期的写锁（`state.py:202-212`）。两个进程可以交错短事务；`transition()` 又是先读当前状态再 UPDATE（`state.py:465-524`），不能依赖 SQLite 自动让第二个 SyncRunner 启动失败。

这不要求 distributed lock。最小边界是在 `sync.py` Bootstrap 使用本机、运行级、fail-fast 的单实例锁，或把“操作者必须保证单实例”明确降格为不受代码保护的前置条件并停止声称会响亮失败。若目标是可靠恢复，前者应在 skeleton 阶段完成。

## Non-blocking Issues

1. **枚举元数据不是一个原子证据包。** `set_meta()` 一次只写一个 key（`state.py:629-639`），而设计要提交 status/proof/cursor。若 status 先写，崩溃可能留下“完成”但 proof/cursor 未更新的撕裂快照。可以新增批量事务 API，或把一次枚举结论编码成单个 JSON value；关键是规定完成状态最后且原子可解释。

2. **Enumeration abort 状态描述不准确。** 设计说枚举中止后“已发现的 note 安全处于 PENDING”，但当前 flow 是逐页 upsert 为 DISCOVERED、仅完整枚举后 mark_pending。中止后它们应保持 DISCOVERED；这本身是安全的，但文档与测试期望要一致。

3. **“纯 media recovery 不打开浏览器”与固定生命周期冲突。** 当前五阶段总是先 Enumeration，而 Enumeration 必须打开浏览器；§9 又称仅有 media debt 时不打开浏览器。必须明确是否允许跳过枚举。若每次 sync 都要求 fresh enumeration，则删掉“纯恢复零浏览器”的承诺即可，不必增加新模式。

4. **NETWORK_ERROR 类型存在但 collector 不产生它。** Playwright `goto`、context 断连等异常没有映射，通常会落入普通异常；测试仅验证 error 类枚举，不验证 collector mapping（`errors.py:73-75`，`tests/test_failure_semantics.py:21-30`）。这是设计 D-B 的已知前置项，应在 collector 阶段补测试。

5. **token cache 仍 fail-open 静默。** 读写均 `except Exception: pass`，写入也不是原子替换（`collector.py:70-85`）。它不是 SQLite 的正确性来源，但会掩盖 token 解析退化；D-D 应至少日志化并避免半写文件。

6. **媒体错误分类不足以覆盖本地永久错误。** `download_media_item()` 会把网络、HTTP、磁盘写失败统一包装成 `MEDIA_DOWNLOAD_FAILED`（`media.py:46-93`）。runner 可从 `details.status_code` 识别 HTTP 403，但 ENOSPC/permission 等本地错误不应被盲目执行每文件三次。P1 可先 fail closed 并 exit 2/4，之后再细化，不需要扩 taxonomy 才能写 skeleton。

7. **按 filename 恢复隐含“manifest 顺序稳定”假设。** normalizer 用位置生成 `image_01.*`（`normalizer.py:164-197`），StateStore 又把 filename 当严格 identity。重新 fetch 后若媒体顺序或扩展名变化，旧 COMPLETED 条目可能错误命中新 manifest 的另一张图。进入 Phase 4 前必须定义刷新 raw 后何时可沿用旧 media_state；这不阻塞 skeleton/detail，但阻塞媒体 skip-if-verified 的正确性声明。

8. **现有测试没有集成证据。** `tests/` 没有 collector 测试、pagination fixture、session lifecycle 测试或 SyncRunner 测试。`61 passed` 证明的是现有模块单元 contract，不证明设计阶段之间可组合。

9. **State contract 文档含历史语义。** `P1_STATE_CONTRACT_V2.md` 仍描述 query-time `max` 过滤、旧 transition 副本及“无需迁移”；`P1_STATE_CODE_REVIEW_CODEX.md` 是 hardening 前审查，列出的多个 blocker 已由当前代码修复。实现时应以当前 v2.1 代码为准，并对基线文档做最小勘误，避免开发者照旧结论回退正确行为。

## Required API additions

### StateStore

1. **COMPLETE 查询 API（必需）**：增加窄只读方法，返回 COMPLETE 的 `NoteState`，供启动 artifact guard 使用。不要让 sync.py 读取 `_conn`。
2. **原子 meta 提交（建议在实现前确定）**：`set_meta_many(mapping)`，或把 enumeration/run summary 固定为一个 JSON meta key。若采用单 key 方案，无需新增方法，但设计必须明确。
3. **resume normalize failure transition（必需的契约调整）**：不是增加新服务层，而是让 StateStore transition 表与 chosen recovery 语义一致；当前公开 API 无法表达设计路径。

不需要新增“task table”、独立 media queue 表或第二套 attempt counter。现有 `get_fetch_queue()`、`get_media_resume_queue()`、`transition()`、`retry_failed()`、`recover_interrupted()`、`record_media_result()` 和 `status_counts()` 足够。

### Collector

1. **运行级 session API（必需）**：`collector.session()` 返回一个有明确 close/finally 生命周期的 scoped collector；同一 session 内 enumeration 与 fetch 共用一个 persistent browser context。
2. **分页迭代 API（必需）**：`iter_favorites(limit=None)` 逐个产出非累计 `PageResult`，保留服务端 cursor/has_more 原始证据；停滞、字段缺失和响应解析失败必须抛结构化错误，不能伪造完成。
3. **session 内 fetch contract（必需）**：接受 `FavoriteRef`，不隐式重开浏览器或触发 token 补枚举；确保返回 payload 对应请求 note_id。
4. **transport error mapping（必需）**：把 Playwright 超时、导航和 context 断连映射为 `NetworkAcquisitionError`，并保留 AUTH/RISK/RATE/TOKEN 的现有区分。

`FavoriteRef` 和 `PageResult` 类型本身无需新增字段；需要修的是生产与消费语义。

### sync.py 内部边界

以下属于 runner 内部 helper，不应下沉到 StateStore 或 Collector：

- atomic JSON/text write 与 `.complete` seal；
- artifact guard、skip-if-verified、可选 sha256 深检；
- `NoteState → FavoriteRef` 适配与 `post.note_id` identity guard；
- 运行内 retry/backoff、运行级 abort flag、exit-code 汇总；
- 本机单实例生命周期锁；
- per-run counters/log summary。

媒体字节下载继续留在 `media.py`。SyncRunner 应逐项调用 `download_media_item()` 才能在每个文件后提交 `record_media_result()`；不要用 `download_post_media()` 作为批量恢复的状态边界。

## Recommended implementation order

1. **Gate 0 — 修订窄契约，不写 runner 主流程。** 统一 resume normalize 失败路径、DISCOVERED 接纳、retry budget owner、exit 0/3 条件、单实例保证和 enumeration metadata 提交顺序；补 COMPLETE 查询 API。同步标注旧 review 中哪些问题已由 v2.1 修复。

2. **Phase 1 — Collector contract。** 实现并测试 `session()`、`iter_favorites()`、真实 cursor/has_more、停滞 fail-closed、network mapping、token 不隐式补枚举、target note identity。先用固定 API payload fixture 验证分页终止，再做受控 smoke test；不要用真实大规模抓取作为首个验证手段。

3. **Phase 2 — SyncRunner skeleton，不碰媒体。** 只实现 Bootstrap、单实例保护、Startup Recovery、Enumeration、队列选择、abort/Finalize 和 exit code。用 fake collector 验证多页 upsert、枚举中断、recovery-before-query、`--max-notes` 留有工作时 exit 3、session finally close。

4. **Phase 3 — Detail pipeline。** 接入 `FETCHING`、运行内 network retry、目标 note identity guard、normalize、atomic raw.json、`DETAIL_SUCCESS` 和 terminal/retryable failure mapping。测试每个写入/transition 断点 crash 后的重跑行为。

5. **Phase 4 — Media pipeline。** 先解决 manifest filename identity 的刷新规则，再实现 raw guard、`MEDIA_SYNCING`、逐文件 retry/record、resume 403/no-progress 升级、canonical/post/.complete 提交顺序和 COMPLETE guard repair。测试最后文件完成后到 COMPLETE 转移前的崩溃窗口。

6. **最后接 CLI。** 在上述 fake-driven integration tests 通过后再加入 `sync` 子命令和真实 profile smoke test。保持单线程、单遍双队列，不引入 workflow engine、消息队列、并发 worker或 distributed lock。

## Confidence

**高（93%）**。

- 结论基于所列五份设计/审查文档、当前 `state.py`、`collector.py`、`models.py`、`media.py`、normalizer/CLI 及全部测试的逐项映射。
- 实际运行 `./.venv/bin/pytest -q`：**61 passed in 0.29s**。
- 对 StateStore 已实现能力的置信度高：retry exhaustion、close/reopen recovery、queue separation、schema fail-closed、media gating 均有直接测试。
- 主要不确定性在真实 XHS 收藏接口的 cursor/has_more 字段与滚动触发行为；当前代码和测试没有真实分页证据，因此本审查不会把 `PageResult` 的类型存在误判为分页能力已经完成。
