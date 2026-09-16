# P1 State Contract v2 Implementation Code Review

审查日期：2026-09-16  
审查范围：`src/xhs_ingest/state.py`、`tests/test_state.py`  
规范依据：`P1_STATE_CONTRACT_V2.md`、`P1_STATE_REVIEW_SOL.md`、`P1_REVIEW_SONNET.md`

## Verdict

**不通过（Changes Required）。当前实现尚不能被视为完整符合 P1 State Contract v2。**

双队列、主要状态转移、单次 mutation 事务和两条基础 crash recovery 路径已经实现，且现有测试全部通过；但仍有两个阻塞正确性的高风险问题：

1. **schema v2 既没有迁移，也没有版本不匹配时 fail closed。** 旧 v1 数据库可以被成功打开，但随后读取 note 时因缺少 `last_failure_stage` 列崩溃。
2. **detail retry budget 没有在状态机中闭合。** 达到上限的 `RETRYABLE_FAILED` 会成为非终态但不在默认队列中的 stranded row；达到上限时从 `FETCHING` 恢复为 `PENDING` 又会绕过预算继续执行。

此外，`FINAL_FAILED` 的人工重置唯一入口、`MEDIA_SYNCING` 的强制性、未知 DB 状态 fail-closed，以及 `failure_stage` 与实际失败阶段的绑定均未由 `state.py` 强制。因而目前的正确性依赖未来 `sync.py` 永远按约定调用，而不是由 state contract 自身保证。

验证结果：

- `./.venv/bin/pytest -q tests/test_state.py`：**38 passed**。
- `./.venv/bin/pytest -q`：**49 passed**。
- 额外旧库探针：以 v1 schema、`schema_version=1` 和一条 `PENDING` 记录初始化数据库后，`StateStore` 打开成功，`schema_version` 仍为 `1`；第一次 `get_note()` 抛出 `IndexError: No item with that key`（缺少 `last_failure_stage`）。

结论不是“代码整体不可用”，而是：**新建空库上的 happy path 可用；升级、预算耗尽和 API 误用边界尚未达到 v2 的 fail-closed 要求。**

## Correct Implementation

### 1. 双队列的 SQL 投影与文档定义一致

- `get_fetch_queue()`（`state.py:291-319`）包含 `DISCOVERED`、`PENDING`，以及 `attempt_count < max_attempts` 的 `RETRYABLE_FAILED`；排除 `DETAIL_SUCCESS`、`MEDIA_PARTIAL`、`COMPLETE`、`FINAL_FAILED`。
- `get_media_resume_queue()`（`state.py:321-342`）只包含 `DETAIL_SUCCESS` 和 `MEDIA_PARTIAL`。
- 两者均按 `rowid`，即发现顺序返回，并支持 `limit`。
- docstring 明确 media resume 工作不应走 `fetch_note`，也明确必须先调用 `recover_interrupted()`。
- `test_fetch_queue_excludes_media_resume_notes`、`test_media_resume_queue_returns_detail_success_and_partial`、`test_queues_are_disjoint_and_cover_non_terminal_work` 对普通、未耗尽预算的状态集合覆盖良好（`test_state.py:246-319`）。

### 2. 主要状态转移默认 fail closed

- `TRANSITIONS`（`state.py:99-121`）与合同 §2 的转移表逐项一致。
- `transition()` 会先将目标转换成 `NoteStatus`，再检查当前状态到目标状态的合法性；非法转移在 UPDATE 前抛出 `ValueError`（`state.py:383-405`）。
- 进入 `FETCHING` 时，状态与 `attempt_count + 1` 在同一个 UPDATE、同一个事务内提交（`state.py:408-419`）。
- 非失败目标会清除旧的 error/stage 快照，避免成功状态携带陈旧错误。

### 3. 正常 crash recovery 路径正确

- `FETCHING → PENDING` 保留已经消耗的 `attempt_count`。
- `MEDIA_SYNCING → DETAIL_SUCCESS` 保留 `media_state`，不消耗新的 detail attempt。
- 两类恢复在同一个 `with self._conn` 事务中批量执行（`state.py:482-500`）。
- 恢复后，普通 `FETCHING` row 会进入 fetch queue，普通 `MEDIA_SYNCING` row 会进入 media resume queue（`test_state.py:322-351`）。在调用顺序符合“open → recover → query”时，不会因 transient status 留下 stranded state。
- state 层不检查 `raw.json` 是正确的职责边界；恢复后由 sync runner 做磁盘守卫，符合合同 §1。

### 4. 事务、upsert 与连接生命周期的基本实现合理

- WAL 与 `synchronous=FULL` 在连接初始化时设置（`state.py:176-186`）。
- 每个公开 mutation 使用 `with self._conn`：页级 upsert、状态转移、单媒体结果、恢复和 meta 更新均以事务提交。
- `upsert_notes()` 的 conflict update 只刷新 listing snapshot，不覆盖 status、attempt、media state；缺失的新 token/URL/title 通过 `COALESCE` 保留旧值（`state.py:226-268`）。
- `close()` 和 context manager 均已提供。默认 sqlite 连接的同线程限制也会阻止直接跨线程共享连接。
- `retry_failed()` 会原子地执行 `FINAL_FAILED → PENDING`，并清除 attempt 与错误字段（`state.py:344-357`）；该专用方法本身语义正确。

### 5. 测试覆盖了若干真实边界

现有测试不只是数量充足；它确实覆盖了：基本非法转移零副作用、未知 note、重复 upsert 不覆盖进度、队列分离、两类恢复、恢复保留媒体进度、filename 路径注入防护、error 清除与人工 retry reset。

## Bugs Found

### B1 — Blocker：schema version 只是写入标签，没有校验、迁移或 fail-closed

证据：

- 代码把 `SCHEMA_VERSION` 从 1 提升为 `2`，并新增 `notes.last_failure_stage`（`state.py:43-60`）。
- `_init_schema()` 使用 `CREATE TABLE IF NOT EXISTS`；它不会给已有 `notes` 表增加列。
- `schema_version` 使用 `INSERT OR IGNORE`（`state.py:190-202`），因此旧库中的 `1` 不会更新，也不会触发错误。
- `_row_to_state()` 无条件读取新列（`state.py:537-550`）。

实际结果是“打开旧库成功、业务读取时延迟崩溃”，而不是迁移或在启动时给出明确 schema 错误。这也与合同开头和 §8 的“Schema 无变更、无需迁移”直接矛盾。当前代码已经发生 schema 变更，因此不能继续沿用该结论。

风险：真实用户从 v1 升级后，会在运行中而不是启动时失败；部分 meta 或枚举操作可能已先发生，错误定位也会被伪装成普通运行时异常。

### B2 — Blocker：retry exhaustion 没有形成状态机不变量

合同 §4.1 要求：可重试失败发生时，若 `attempt_count >= max`，必须进入 `FINAL_FAILED`。当前实现只在 `get_fetch_queue()` 查询时对 `RETRYABLE_FAILED` 加 `< max` 条件；`transition()` 不接收或检查预算（`state.py:361-419`）。

因此存在两类错误：

1. `attempt_count == 5` 的 row 仍可进入 `RETRYABLE_FAILED`，随后从默认 fetch queue 消失，但其 status 又不是终态。这是 stranded state，也使“两个队列覆盖全部非终态工作”的声明不成立。
2. `FETCHING` 在第 5 次尝试中崩溃后，`recover_interrupted()` 将其改成 `PENDING`；fetch queue 对所有 `PENDING` 无条件放行，因此第 6 次及更多次 fetch 仍可开始。重复 crash 可以无限绕过预算。

`test_fetch_queue_respects_retry_budget`（`test_state.py:200-230`）甚至将第一种行为固定为预期：达到 cap 的 note 保持 `RETRYABLE_FAILED`，只是在默认查询中被隐藏；改变查询参数后它又重新出现。这与 v2 合同明确相反。

### B3 — High：`FINAL_FAILED` 可以绕过唯一人工重置入口

合同规定 `retry_failed()` 是 `FINAL_FAILED → PENDING` 的唯一入口，并负责同时清零 attempt 和错误。虽然转移表需要保留这条逻辑边，但通用 `transition()` 并未禁止调用它（`state.py:118-120, 361-419`）。

调用 `transition(note_id, PENDING)` 可直接离开终态，同时保留旧 `attempt_count`，却清掉错误字段。结果既绕过显式 `--retry-failed`，又产生与专用 `retry_failed()` 不同的状态。现有测试只验证专用方法成功，没有验证通用方法必须拒绝该路径。

### B4 — High：D3 的 `MEDIA_SYNCING` 强制步骤未由 state API 执行

合同 v2 将 `transition(MEDIA_SYNCING)` 提升为任何媒体工作前的强制步骤。当前 `MEDIA_PHASE_STATUSES` 同时允许 `DETAIL_SUCCESS`、`MEDIA_SYNCING`、`MEDIA_PARTIAL`（`state.py:123-126`），`record_media_result()` 因而可以在未进入 `MEDIA_SYNCING` 时提交文件结果（`state.py:449-478`）。

测试 `test_record_and_get_media_state` 和 `test_media_state_replaces_entry_by_strict_filename`（`test_state.py:441-469`）正是在 `DETAIL_SUCCESS` 下直接记录媒体，固化了 v1 的可选语义。数据本身未必损坏，但 D3 要求的“媒体工作正在发生”的 crash trace 与可观测性无法保证。

### B5 — High：未知数据库状态并非全局 fail closed，可能被队列静默遗忘

合同 §2/§6 要求 DB 出现枚举外状态时硬错误。`get_note()`/`transition()` 经 `NoteStatus(...)` 会报错，但：

- 两个 queue 查询只匹配已知字符串，未知状态 row 会被静默排除。
- `recover_interrupted()` 会静默忽略未知状态。
- `status_counts()` 不做 `NoteStatus` 校验，而是把任意字符串直接加入结果字典（`state.py:529-534`）。
- schema 没有 status `CHECK` constraint，启动时也没有完整性验证。

因此主运行路径完全可能把版本不兼容或损坏 row 当成“没有工作”，违反 evidence-first 与 fail-closed 原则。

### B6 — Medium：`failure_stage` 只校验字面值，没有绑定实际失败路径

当前仅保证：stage 属于 `FETCH | NORMALIZE | MEDIA`，且传 stage 时同时传了 `error_status`（`state.py:396-407`）。它不保证：

- 失败目标必须有 `error_status` / `failure_stage`；
- `FETCH`/`NORMALIZE` 来自 detail 路径；
- `MEDIA` 来自 `MEDIA_SYNCING` 或 `MEDIA_PARTIAL` 路径；
- stage 与 error taxonomy 相容。

例如现有 `test_failure_stage_recorded_then_cleared` 在当前状态为 `FETCHING` 时直接记录 `failure_stage="MEDIA"`（`test_state.py:380-398`），这不是合同描述的 media resume no-progress 路径。故该字段目前是可选自由标签，不足以可靠解释预算为何被消耗。

### B7 — Medium：单进程约束没有被 SQLite 配置真正强制

类 docstring 声明单线程/单进程，但 WAL 并不会保证第二个进程必然 `SQLITE_BUSY`；短事务通常可以交错成功。`transition()` 是先 SELECT 当前状态、再按 `note_id` UPDATE，UPDATE 没有把 expected current status 放入 WHERE（`state.py:384-419`）。两个进程可能都基于旧状态通过校验，再发生 last-writer-wins。

这是合同明确列出的操作约束而非当前单进程 CLI happy path 的立即阻塞项，但“SQLite 文件锁会响亮拒绝第二个 sync”并不是现有实现能够保证的事实。

## Contract Mismatch

| Contract requirement | Implementation | Assessment |
|---|---|---|
| `get_fetch_queue()` 精确状态集合 | SQL 与文档一致 | 符合；但达到 cap 的 retryable row 使“覆盖全部非终态”声明失真 |
| `get_media_resume_queue()` = DETAIL_SUCCESS + MEDIA_PARTIAL | 精确实现，且注明先 recovery | 符合 |
| 恢复后不留 transient stranded state | 两个 transient 均被重置 | 正常预算下符合；FETCHING-at-cap 会被恢复成绕过预算的 PENDING |
| 达到 detail budget 必须 FINAL_FAILED | state 层不执行，仅查询时隐藏 | **不符合** |
| FINAL_FAILED 只能由 `retry_failed()` 离开 | 通用 `transition()` 也允许 | **不符合** |
| MEDIA_SYNCING 是媒体工作强制前置 | record API 允许 DETAIL_SUCCESS/MEDIA_PARTIAL | **不符合** |
| 未知 status 硬错误 | 部分 API 报错，queue/recovery/status_counts 可忽略或接受 | **不符合** |
| Schema 无变更、无需迁移 | 实现新增列并升到 v2，但无迁移/版本拒绝 | **文档与实现冲突** |
| 单线程 mutation | sqlite 默认同线程连接 + 文档说明 | 基本符合 |
| 单进程第二实例响亮失败 | 无进程锁，WAL 允许交错 | 未被实现保证 |
| state 不承担磁盘内容事实 | state.py 不访问 data 目录 | 符合 |

另一个需要澄清的合同内在边界是：fetch queue 把 `DISCOVERED` 称为“安全网”，但 `DISCOVERED → FETCHING` 是非法转移。调用方必须识别该状态并先执行 `DISCOVERED → PENDING`；它不能把返回的每一项统一直接 transition 到 `FETCHING`。当前 API 可以实现正确流程，但 contract/runner 必须保留这个分支。

## Recommended Changes

按优先级建议以下最小修改；不要求引入新表、工作流框架或拆分 detail/media 持久计数。

1. **先解决 schema 策略。** 在打开数据库时读取并严格校验 schema version。若保留 `last_failure_stage`，提供 v1→v2 的单事务迁移并验证列存在；若不迁移，则在启动时抛出明确的不兼容错误。同步修正文档中“schema 无变更”的陈述。对未知/更高版本必须 fail closed。
2. **把预算耗尽变成 mutation 不变量，而不是 queue filter。** 失败提交必须原子决定 `RETRYABLE_FAILED` 或 `FINAL_FAILED`；crash recovery 也必须处理 `FETCHING` 已到 cap 的情况，不能恢复成无条件可运行的 `PENDING`。不得留下达到 cap 的非终态隐藏 row。
3. **封闭 `FINAL_FAILED` 的通用出口。** `transition()` 应拒绝 `FINAL_FAILED → PENDING`，只允许 `retry_failed()` 执行带 reset 的原子操作，或使用不可被普通调用绕过的专用内部路径。
4. **落实 D3。** `record_media_result()` 只应接受 `MEDIA_SYNCING`；修改当前两个从 `DETAIL_SUCCESS` 直接写 media 的测试，使测试先进入 `MEDIA_SYNCING`。
5. **定义 failure record 的最小不变量。** 若保留 `last_failure_stage`，至少要求 failure target 具有规范化 `error_status`，并决定 stage 是强约束还是纯诊断字段。若是强约束，应按来源状态/处理阶段拒绝明显不可能的组合；若只是诊断字段，应修改“正确绑定/解释预算”的文档表述。
6. **增加启动完整性检查。** 校验 schema、所有 status 枚举、必要列以及关键数值约束；`status_counts()` 也必须对未知 status 报错，不能默默接受。可考虑 schema CHECK，但不能用它替代升级验证。
7. **明确并执行单实例策略。** 如果 P1 继续要求单进程，需使用能够覆盖整个 sync 生命周期的实例锁；仅依赖 WAL/短事务不足。若只把它视为调用方前置条件，至少不要声称 SQLite 会可靠拒绝第二实例。
8. **补充真实 failure-mode 测试：**
   - 用真实 v1 schema 打开数据库，验证迁移成功或启动时明确拒绝；另测未知/未来 schema version。
   - 第 5 次可重试失败必须成为 `FINAL_FAILED`，并验证两个队列中不存在 stranded 非终态。
   - 第 5 次 `FETCHING` 中 crash、关闭连接、重新打开并 recovery，确认不会产生第 6 次自动 fetch。
   - 通用 `transition(FINAL_FAILED, PENDING)` 必须失败且无副作用。
   - 在 `DETAIL_SUCCESS` / `MEDIA_PARTIAL` 直接写 media 必须失败；在 `MEDIA_SYNCING` 成功。
   - 未知 DB status 在启动或首次状态扫描时必须硬失败。
   - failure stage 与来源路径不匹配必须失败，或明确测试其仅为非权威诊断值。
   - crash 测试至少包含 close/reopen；现有测试是在同一连接上直接调用 recovery，验证了映射规则，但没有验证进程中断后的持久恢复边界。

应保持不变的部分：两个队列作为 status 投影、单个 `attempt_count`、`media_state` JSON、raw/canonical/SQLite 的职责边界、WAL + FULL、主线程写入，以及 `FETCHING`/`MEDIA_SYNCING` 作为 crash trace 的总体模型。

## Confidence

**高（94%）。**

- 对双队列、状态转移、事务和测试覆盖的判断来自逐行代码与全部 49 个测试的实际执行。
- schema 不兼容已用真实 v1 形状的临时 SQLite 数据库复现，不是静态推测。
- retry cap、人工 reset 绕过、D3 和未知状态处理均可由当前公开 API 或 SQL 路径直接推出。
- 剩余不确定性主要在未来 `sync.py` 如何封装这些 API；但本次目标是审查 state contract 本身，不能把尚未存在的调用方纪律算作 `state.py` 已实现的保证。
