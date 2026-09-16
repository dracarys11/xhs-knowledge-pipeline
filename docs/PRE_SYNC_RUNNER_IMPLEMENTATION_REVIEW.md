# Pre-SyncRunner Implementation Review

审查日期：2026-09-16  
审查对象：`docs/SYNC_RUNNER_DESIGN.md` 与当前代码快照  
审查性质：只读；未修改代码

## Verdict

**不能直接按 `SYNC_RUNNER_DESIGN.md` 开始完整实现。设计方向正确，但当前只有 State 主干可以直接映射；collector contract 尚未实现，State 还缺少 runner 所需的扫描/批量 API，并存在一条明确的 transition 冲突。**

可以保留现有整体架构与线性流程，不需要 workflow engine、queue service、并发或分布式组件。建议先做一个很小的“实现前契约收口”，然后按用户提出的四阶段逐步实现。

当前测试基线：

- `./.venv/bin/pytest -q`：**61 passed**。
- 其中 `tests/test_state.py`：**50 passed**。
- 当前没有 collector contract 测试，也没有 SyncRunner integration test。

审查期间工作区中的 `state.py` 与 `test_state.py` 曾被外部更新；本报告以最终稳定快照为准：`state.py` 686 行、`test_state.py` 788 行，测试全绿。

## 1. Direct Mapping Summary

| Design call / responsibility | Current code | Mapping result |
|---|---|---|
| `StateStore(...)` startup validation | 严格校验 schema version、列和 status | **Direct** |
| `recover_interrupted()` | 已实现，含 exhausted FETCHING → FINAL_FAILED | **Direct**，返回值比设计多 `fetching_finalized` |
| `retry_failed()` | 已实现 | **Direct** |
| COMPLETE guard scan | 没有列举 COMPLETE rows 的公开 API | **Missing State API** |
| `collector.session()` | 不存在 | **Missing Collector API** |
| `session.iter_favorites(limit=None)` | 不存在；只有聚合 `list_favorites(limit:int)` | **Missing Collector API** |
| server cursor / has_more | `PageResult` 有字段，但 collector 未填真实值 | **Model ready, implementation missing** |
| `upsert_notes(page.items)` | 已实现，单页事务 | **Direct** |
| `mark_pending(所有 DISCOVERED)` | 只有 `mark_pending(ids)`，没有列举/全量接纳 API | **Not directly mappable** |
| `get_media_resume_queue()` | 已实现 | **Direct** |
| `get_fetch_queue()` | 已实现，budget 已改为写入时强制 | **Direct**；设计中的 budget owner 需修订 |
| NoteState → FavoriteRef | 字段基本足够，无 helper | **Thin runner adapter**，不必新增 State 方法 |
| `fetch_note(ref)` | 已实现单次调用版 | **Partial**；不能复用 session，且目标身份未严格验证 |
| `normalize(raw)` | 已实现纯函数 | **Direct** |
| media resume normalize failure | 设计要求 RETRYABLE_FAILED；当前 transition 不允许完整路径 | **Conflict** |
| `record_media_result()` | 已实现，强制 MEDIA_SYNCING | **Direct** |
| `get_media_state()` | 已实现 | **Direct** |
| `status_counts()` | 已实现且未知状态 fail closed | **Direct** |
| enumeration conclusion meta | 有逐 key `set_meta()` | **Partial**；多 key 结论不是原子提交 |
| single-run process exclusion | 没有；设计错误依赖 SQLite WAL | **Missing runtime invariant** |

## 2. Collector Interface Review

### 2.1 Current interface does not satisfy the design

设计依赖：

```python
with collector.session() as session:
    for page in session.iter_favorites(limit=None):
        ...
    raw = session.fetch_note(ref)
```

当前 `Collector` Protocol 只有：

```python
list_favorites(limit: int = 20) -> PageResult
fetch_note(ref_or_id) -> dict
```

`XhsPlaywrightCollector` 每次调用都独立执行 `_launch_context()`，并在 finally 中关闭 context（`collector.py:246-440, 442-568`）。因此：

- `session()` 不存在；
- 没有 session-scoped collector 类型或 protocol；
- `fetch_note()` 无法接收已有 BrowserContext/Page；
- `iter_favorites()` 不存在；
- 当前 `list_favorites()` 只在整个滚动结束后返回一个聚合结果，runner 无法逐页 checkpoint；
- 非 sync 命令的逐调用兼容行为虽然可以保留，但需要与 session path 共用内部采集逻辑，不能复制两份 parser。

结论：设计 §0 的 D-A、D-C 不是“小补丁”，而是 SyncRunner 开始前必须先完成并测试的 collector contract。不过它仍属于 V1 已规划的演进，不是架构重做。

### 2.2 PageResult model is sufficient, current population is not

`models.PageResult` 已有 `items / next_cursor / has_more / completion_proof / raw`，无需为了分页新增另一个 page model。

当前实现的问题：

- interceptor 只提取 `data.notes`，丢弃 cursor/has_more（`collector.py:260-272`）；
- `next_cursor` 永远是默认 None；
- `has_more` 由 `len(items_by_id) >= limit` 推断（`collector.py:431-436`）；
- 最多滚动 6 次，不等于服务端终止；
- response JSON/schema 错误被静默吞掉；
- `FOUND_N_ITEMS` 是发现证明，不是完成证明。

设计中的 `iter_favorites()` 必须逐个 yield **服务端 page**，而不是把 DOM 滚动批次或最终聚合列表伪装成 page。每个 page 必须在 yield 前验证：items 形状、`has_more` 字段存在且为 bool、cursor 规则、重复/循环/stall 规则。只有末页可以携带 `SERVER_HAS_MORE_FALSE`。

### 2.3 Context lifecycle needs one explicit contract

设计要求运行内一个 persistent context、非 sync 命令保持一次调用一个 context。这个目标可行，但必须明确：

- `collector.session()` 返回的对象负责 `iter_favorites()`、`fetch_note()` 和 close；
- session 内所有方法不得再次调用 `_launch_context()`；
- context 的 finally close 只有一个 owner；
- Page 可以轮换，context 不轮换；
- context 死亡必须映射为 `NETWORK_ERROR` 并使本 run 中止；
- session close 本身不得覆盖已经记录的业务异常。

无需拆成独立 BrowserSession package；V1 已明确冻结的是职责边界，不是类文件结构。

### 2.4 Token path conflicts with the design

设计说 sync 不做运行内 token 重推导，依赖本次枚举刷新 State snapshot。当前 `fetch_note()` 在 token 缺失时会自行调用 `list_favorites(limit=50)`，并吞掉该调用的一切异常（`collector.py:472-482`）。这与设计冲突：

- 可能在 detail queue 中暗中开启第二次枚举；
- 只看前 50 条，无法解析历史收藏；
- AUTH/RISK/RATE 可能被吞掉，随后误分类；
- session path 若照搬会破坏单一 context/节流模型。

建议 contract 明确：sync session 的 `fetch_note(FavoriteRef)` 只消费传入 snapshot；token 缺失/失效返回结构化失败，不自行枚举。旧的裸 ID convenience fallback 可仅保留给 `ingest` 命令。

另外，设计 §3 所称“每次枚举刷新全部 token”并不精确：State upsert 使用 COALESCE，只有新 page 带 token 时才刷新；缺失 token 会保留旧值。文档应改为“refresh when present，永不以缺失值抹掉已知 token”。

### 2.5 Two collector correctness guards are missing from the design

这两项应并入用户建议的 Phase 1，而不是留给 runner 猜测：

1. **Current-user proof**：当前代码会把页面上任意 profile link 当作认证证据，并可能把第一个 profile link 当作当前用户 profile（`collector.py:281-335`）。Explore feed 作者链接可能造成误判。session 枚举必须使用可证明的当前用户 identity。
2. **Target-note proof**：SSR fallback 在 target key 缺失时返回 map 中任意第一项；feed fallback 返回任意非空 items payload（`collector.py:530-559`）。成功结果必须验证 payload note_id == requested note_id，否则 `PARSE_FAILED`。runner 可以再断言一次，但 acquisition contract 本身必须成立。

### 2.6 D-B and D-D remain unimplemented

- Playwright timeout/连接错误没有映射到 `NetworkAcquisitionError`。
- token cache load/save 仍以 `except Exception: pass` 静默吞掉。
- response handler 也静默吞掉 JSON/schema 失败。

因此设计的 retry matrix 当前无法直接使用。

## 3. StateStore API Review

### 3.1 Current State core is implementation-ready

最新 StateStore 已完成此前的核心修正：

- schema version、必要列、未知 status 在 open 时 fail closed（`state.py:216-279`）；
- retry budget 在 failure commit 时原子 coercion 到 FINAL_FAILED（`state.py:435-528`）；
- crash-at-cap recovery 直接 FINAL_FAILED（`state.py:591-625`）；
- `FINAL_FAILED` 无通用 transition 出口；
- `failure_stage` 与 taxonomy/source phase 有校验；
- `record_media_result()` 只允许 MEDIA_SYNCING（`state.py:532-587`）；
- 两个 queue、upsert、retry reset、meta、status counts 均可直接使用。

50 个 state tests 覆盖 cap exhaustion、close/reopen crash recovery、终态封闭、media gating、schema/status corruption。State 主干不需要再重写。

### 3.2 Missing method: COMPLETE scan

设计 §2 要扫描每个 COMPLETE note 做 `.complete` 守卫。当前公开 API 只有两个工作队列、`get_note(id)` 和 counts；COMPLETE 被两个队列有意排除。

runner 不应访问私有 `_conn`。必须新增一个窄的公开读取能力，例如：

- `get_notes_by_status(NoteStatus.COMPLETE, limit=...)`；或
- 专用 `get_complete_notes()` / iterator。

两者选一个即可，不需要 repository abstraction。若用通用 status query，还可以解决 DISCOVERED admission 的读取需求。

### 3.3 Missing method or design detail: admit all DISCOVERED

设计写的是 `store.mark_pending(所有 DISCOVERED)`，当前方法要求调用者传 IDs。现有代码可通过在 enumeration 中累计全部 page IDs 后调用，但这并不等价于“数据库中所有 DISCOVERED”，也无法覆盖上次中断后遗留且本次不再出现在收藏列表中的 row。

实现前应二选一并写死：

- 新增 `mark_all_discovered_pending()`，在单个 SQL 事务中完成；或
- 新增公开 status query，runner 读取 DISCOVERED IDs 后调用现有 `mark_pending(ids)`。

推荐窄的 `mark_all_discovered_pending()`：它与 D6 的“枚举接纳专用”一致，且避免把内部 row scanning 暴露给 runner。不要让 sync.py 直接写 SQL。

### 3.4 Missing atomic meta commit

设计要同时记录 enumeration status、proof、cursor，以及 finalize 的 run conclusion。当前只有单 key `set_meta()`；连续写多个 key 时 crash 会留下互相矛盾的组合。

最小选择：

- 新增 `set_meta_many(mapping)`，单事务写多个 key；或
- 把一次 enumeration/run conclusion 序列化成一个 JSON value，用一次现有 `set_meta()` 写入。

不需要 event log。设计必须指定其中一种，否则“完成 proof”本身没有原子边界。

### 3.5 Direct conflict: media-resume normalize failure has no legal transition

设计 §5.2：

```text
DETAIL_SUCCESS / MEDIA_PARTIAL
    → normalize(raw.json)
    → PARSE_FAILED 时 RETRYABLE_FAILED(stage=NORMALIZE)
```

当前 State 不能完整表达它：

- `DETAIL_SUCCESS → RETRYABLE_FAILED` 不在 `TRANSITIONS`；
- `MEDIA_PARTIAL → RETRYABLE_FAILED` 虽合法，但 `_MEDIA_STAGE_SOURCES` 对 MEDIA_PARTIAL 只允许 `failure_stage=MEDIA`，会拒绝 NORMALIZE。

这是实现前必须解决的 contract 冲突。最小修正是让“从已有 raw 重新 normalize 失败”成为合法失败路径，并让 stage 表达实际操作阶段，而不是仅由当前 lifecycle status 推断。不要在 runner 中伪装成 MEDIA failure，也不要直接降级 PENDING 丢掉 PARSE_FAILED 证据。

### 3.6 Budget ownership in the design is outdated

设计 §4 说“预算检查是 sync.py 职责”。当前实现已经把预算作为 State mutation invariant：runner 请求 RETRYABLE_FAILED，State 在 cap 时原子返回 FINAL_FAILED。

应修改设计为：

1. runner 分类错误并请求 `RETRYABLE_FAILED`；
2. StateStore 决定实际落地 `RETRYABLE_FAILED` 或 `FINAL_FAILED`；
3. runner 检查 `transition()` 返回的 `NoteState.status`，若被 finalization 则打印 `--retry-failed` 提示。

不要在两层重复比较 attempt_count，否则配置和 crash 边界会再次分叉。

### 3.7 Queue and `--max-notes`

两个 queue 都已有 `limit`，但设计没有说明 `--max-notes` 是：

- 每个 queue 各 N 个；还是
- media + fetch 合计 N 个。

这会影响可预测切片与测试。建议定义为整次运行的总 note budget；先消费 media resume，剩余额度再传给 fetch queue。无需新增 State API。

## 4. Design / Current Code Conflicts

### Blocking conflicts

1. `collector.session()` / `iter_favorites()` 在当前代码中不存在。
2. 真实 cursor/has_more 没有捕获；当前 `PageResult` 的非空 completion 语义不可用于 sync。
3. COMPLETE guard 无公开 State scan API。
4. “所有 DISCOVERED”没有直接 API 或明确的 ID 收集规则。
5. resume normalize failure 无合法 State transition。
6. budget owner 文档写 runner，当前实现正确地由 StateStore 原子执行。
7. 设计依赖结构化 NETWORK_ERROR，collector 仍会让 Playwright transport 异常逃逸为通用异常。

### Correctness conflicts that should be fixed in Phase 1

1. sync 设计禁止运行内 token 重推导，当前 `fetch_note()` 会隐式执行 `list_favorites(50)`。
2. collector success 不保证返回 requested note。
3. auth/current profile 判定弱于架构文档声称的严格 guest/current-user proof。
4. parser/response errors 仍可能被静默吞掉。

### Internal design inconsistencies

1. §9 说“纯恢复运行只有 media resume 时不打开浏览器”，但 runtime lifecycle 每次都先执行 Phase 2 全量 enumeration，而 enumeration 必须打开浏览器。若没有 `--no-enumerate` 或 debt-first 分支，这种 pure recovery run 实际不存在。应删除该承诺或明确触发条件；不必为此新增复杂模式。
2. §3 规定 enumeration 失败后跳过所有已有队列。状态不会丢，但连续在深页失败会让已有 debt 长期饥饿。至少应在设计中确认这是有意的 P1 取舍，而不是宣称“可恢复”就等于“最终会被消费”。
3. §10 再次声称 SQLite 文件锁会响亮拒绝第二进程。WAL 只在写事务重叠时锁住；两个短事务进程可以交错，一个进程还可能 recovery 另一个进程的活跃状态。需要 run-scoped **本地单实例锁**，不是 distributed lock。
4. State Contract v2 仍描述 `get_fetch_queue(max_attempts=...)` 和 query-time budget；当前代码已改成无 max 参数、write-time invariant。设计调用本身不受影响，但两份文档必须同步，避免实现者照旧 contract 写第二套预算逻辑。

## 5. Methods That Need To Be Added or Clarified

### Collector — required before runner skeleton

1. `session()`：运行级 context manager。
2. session-scoped `iter_favorites(limit: int | None)`：逐 server page yield `PageResult`。
3. session-scoped `fetch_note(FavoriteRef)`：复用 context，不隐式枚举 token。
4. 内部 response parser：严格读取 notes/cursor/has_more，异常不得吞掉。
5. Playwright transport → `NetworkAcquisitionError` 的统一映射。

这不要求把所有内部 helper 放进 public Protocol；public contract 只需让 runner 能安全使用 session、分页和 detail。

### StateStore — required or must be resolved in design

1. COMPLETE rows 的公开读取 API。
2. 全量 DISCOVERED admission API，或等价且明确的公开读取 + `mark_pending(ids)` 组合。
3. 原子 meta batch write，或单 JSON meta record 的明确约定。
4. media-resume NORMALIZE failure 的合法 transition/stage 规则。

### Runner-local helpers — do not add to StateStore/Collector

- `NoteState → FavoriteRef` 转换及 missing source/token guard；
- atomic text/json write；
- filesystem COMPLETE guard / skip-if-verified；
- exit-code calculation；
- cancellable backoff/pause；
- run-scoped local process lock；
- requested note_id 与 normalized post.note_id 的第二道 assertion。

## 6. Recommended Implementation Order

用户提出的四阶段拆分是正确方向。建议增加一个很小的 Gate 0，并对各阶段验收边界作如下限定。

### Gate 0 — Contract reconciliation, no runner implementation

只做四件事：

1. 修订设计中的 budget ownership：StateStore 原子执行，runner 读取返回状态。
2. 解决 resume normalize failure transition。
3. 决定 COMPLETE scan、DISCOVERED admission、atomic meta 的最小 State API。
4. 澄清 `--max-notes`、pure recovery browser lifecycle 和单实例锁。

验收：State tests 全绿，并新增 resume-normalize-failure 与新 API 测试。

### Phase 1 — Collector contract

按用户建议先实现：

- server pagination；
- run-scoped context 生命周期；
- token 处理。

同时必须纳入同一阶段的安全条件：

- current-user/auth proof；
- requested note identity proof；
- NETWORK_ERROR 映射；
- 不吞 response/token errors；
- 保留非 sync 命令的逐调用兼容入口。

验收只用 fixture/fake Playwright response，不登录真实账号：多页、末页、缺 has_more、cursor loop、empty+has_more、parse drop、context finally close、token missing/invalid、wrong-note payload。

### Phase 2 — SyncRunner skeleton, no media

实现范围严格限制为：

```text
bootstrap
local single-run lock
startup recovery
[retry_failed]
enumeration → per-page upsert → admission
queue routing / max-notes slicing
run abort flag
status summary / exit code / atomic meta conclusion
```

detail 与 media 使用 fake processor；skeleton 不下载、不渲染、不把任何真实 note 标 COMPLETE。对 media resume queue 只验证路由，不消费状态。

验收：恢复顺序、逐页 crash 后重开、枚举 failure、两个队列顺序、max-notes、exit 0/2/3/4/130、session finally close。

### Phase 3 — Detail pipeline

实现：

```text
NoteState → FavoriteRef
FETCHING
fetch retry/error routing
target identity assertion
normalize
atomic raw.json
DETAIL_SUCCESS
```

有媒体和零媒体都可以先停在 DETAIL_SUCCESS，把最终 artifact commit 统一留给 Phase 4；这样 Phase 3 不需要提前复制 media/finalize 逻辑。测试 detail crash windows、TOKEN/AUTH/RISK/NETWORK/PARSE、raw overwrite 与 attempt 语义。

### Phase 4 — Media pipeline and final commit

最后实现：

- raw guard 与 resume normalize；
- MEDIA_SYNCING；
- per-file retry + `record_media_result()`；
- skip-if-verified；
- 403/no-progress escalation；
- canonical/post/.complete 原子提交；
- COMPLETE guard repair/demotion；
- COMPLETE 最后写。

验收覆盖每个 crash window、partial resume、纯 skip 收口、403 refresh、no-progress cap、marker repair，以及 media identity 与新 raw generation 的匹配规则。

## 7. Final Recommendation

`SYNC_RUNNER_DESIGN.md` **可作为实现方向，但不能原样直接编码**。

进入实现的顺序应为：

```text
Gate 0: State/design contract reconciliation
    ↓
Phase 1: collector contract
    ↓
Phase 2: runner skeleton（无 detail/media side effects）
    ↓
Phase 3: detail pipeline
    ↓
Phase 4: media pipeline + COMPLETE commit
```

最关键的判断：

- **State 主干已通过，可以复用；只需补 runner-facing API 和一条 resume transition。**
- **Collector 当前接口不满足设计，必须先完成 Phase 1。**
- **不要一次写完整 runner。** 用户提出的分阶段方式能让每一步都具有独立、可验证的完成定义，是当前风险最低的实施路径。
