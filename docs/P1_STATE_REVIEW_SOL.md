# P1 State Persistence Layer — Implementation Review

审查日期：2026-09-16
审查对象：`src/xhs_ingest/state.py`（472 行）、`tests/test_state.py`（376 行）
参考依据：`docs/P1_INCREMENTAL_SYNC_PLAN.md`、`docs/P1_REVIEW_SONNET.md`

---

## Verdict

**实现质量高于预期，可以进入 sync.py 开发阶段。**

state.py 准确实现了 P1 设计的核心语义：状态机 + 原子事务 + 崩溃恢复路径。P1_REVIEW_SONNET 提出的三个 Critical Risk（media_state 字段权威来源、DETAIL_SUCCESS raw.json 缺失、attempt_count 上限）在实现中都有对应处理，且处理方式是正确的。测试集覆盖了真实的 failure mode，而不是走形式。

存在 **2 个需要在 sync.py 层处理的设计隐患**（不是 state.py 本身的 bug，但 sync.py 若假设错误会导致正确性问题），以及 **3 个值得注意的实现细节**。没有需要立即修改 state.py 的阻塞问题。

---

## Correct Decisions

**1. `FETCHING` 和 `MEDIA_SYNCING` 作为纯崩溃痕迹的实现是正确的**

`recover_interrupted()` 在一个事务里批量重置两类中间态（L412–419），不是逐行重置，没有"部分重置"窗口。`FETCHING → PENDING` 保留 attempt_count（L413），`MEDIA_SYNCING → DETAIL_SUCCESS` 不加 attempt_count（L417）。这两条语义都对：

- fetch 崩溃消耗 attempt 防崩溃循环；
- media 崩溃不消耗 detail attempt（media 有自己的 retry 机制）。

**2. attempt_count = 5 的取舍合理**

P1_REVIEW_SONNET Risk 3 建议从 3 升到 5。实现采纳（L36），注释也解释了原因（吸收 1–2 次环境崩溃）。

**3. `upsert_notes` 的 COALESCE 策略是精确的**

ON CONFLICT 只刷新 listing snapshot 字段（xsec_token, source_url, title, last_seen_in_listing_at），不触碰 status、attempt_count、media_state（L218–223）。这保证了"枚举不会覆盖已有进度"的幂等语义。`COALESCE(excluded.xsec_token, notes.xsec_token)` 确保新枚举没有 token 时不会抹掉已知好 token（P1_REVIEW_SONNET Adjustment 验证）。

**4. `transition()` 的错误字段清除行为正确**

非失败目标状态（不在 `_ERROR_TARGETS`）的转移会把 `last_error_status / last_error_message` 清空为 NULL（L332–333），而不是保留旧错误信息。测试 `test_error_recorded_then_cleared_on_recovery` 直接覆盖了这条语义。

**5. `record_media_result` 的 filename 校验是防御性的**

L365 检查 filename 不含路径分隔符，不是空串，不是 `.` / `..`。这防止了上层传错 `"assets/image_01.jpg"` 而默默创建嵌套 JSON 结构的潜在 bug。

**6. WAL + FULL synchronous 配置是正确的**

`journal_mode=WAL` + `synchronous=FULL`（L155–156）提供了"每个提交事务在进程任意时刻崩溃后仍可恢复"的最强保证。注释说明了为什么这里的写放大可以忽略（每个 note/file 一次事务，吞吐量极低）。

**7. `sync_meta` 预写 NEVER_RUN 默认值**

P1_REVIEW_SONNET Adjustment 1 要求。实现在 `_init_schema` 里用 `INSERT OR IGNORE` 预写三个 key（L166–173），避免 sync.py 到处做 `if value is None` 的 NULL 分支。

**8. media_state 的权威来源注释清晰**

模块 docstring 第 11–14 行明确：`url` 字段是"构建时从 normalizer 输出捕获的 provenance"，**不应被视为权威或假设未过期**，恢复时必须从 raw.json 重新派生 URL。`record_media_result` 的 docstring 也重复了这条规则（L361）。这直接回应了 P1_REVIEW_SONNET Risk 1。

---

## Critical Issues

### Issue 1：`get_queue()` 把 `DISCOVERED` 和 `MEDIA_PARTIAL` 混入同一队列，但调用者需要区分处理路径

**影响：sync.py 若不做区分，会走错处理分支**

`get_queue()` 返回的 `NoteState` 可能有三种需要不同处理的状态：
- `DISCOVERED` → 需要先 `mark_pending` 再 fetch（或直接进入 fetch，但语义上 DISCOVERED 还没"入队"）；
- `PENDING` / `RETRYABLE_FAILED` → 正常 fetch 路径；
- `MEDIA_PARTIAL` → 不走 fetch，直接读 raw.json + 补齐媒体。

**当前 `get_queue()` 的 WHERE 子句**（L266–270）：
```sql
WHERE status IN ('DISCOVERED', 'PENDING')
   OR (status = 'RETRYABLE_FAILED' AND attempt_count < ?)
```

注意：`MEDIA_PARTIAL` **不在**这个查询里——这意味着处于 `MEDIA_PARTIAL` 的 note 在当前实现中**永远不会被 sync.py 处理**，除非 sync.py 单独查询它们。

但 plan §6 说：
> "`DETAIL_SUCCESS` / `MEDIA_PARTIAL` | 不调 fetch_note。读 raw.json → normalize 重建 media 清单"

这是一条合理的设计（MEDIA_PARTIAL 走快速路径），但 state.py 没有提供"获取 MEDIA_PARTIAL 队列"的方法，也没有在 `get_queue()` 里包含它。**sync.py 必须独立处理 MEDIA_PARTIAL，否则这些 note 会被遗忘。**

**此问题不在 state.py 本身**，但 state.py 应该提供一个 `get_media_partial_queue()` 方法或在 `get_queue()` 的文档里明确说明"MEDIA_PARTIAL 不在此返回，调用者需单独查询"，防止 sync.py 作者假设 `get_queue()` 返回了所有待处理工作。

**最小处理**：在 `get_queue()` 的 docstring 里补充一句：
> "DETAIL_SUCCESS and MEDIA_PARTIAL notes are intentionally excluded; callers must query them separately via a dedicated method for the media resume path."

并考虑提供 `get_media_resume_queue()` 方法。

---

### Issue 2：`transition()` 在事务外执行 `get_note()` 读回，存在 TOCTOU 窗口

**影响：单线程设计中无实际危害，但假设如破坏则结果不可预测**

`transition()` 的结构（L312–342）：
```python
with self._conn:           # 事务 1 开始
    row = self._conn.execute(...)   # 读当前状态
    ...
    self._conn.execute("UPDATE ...")  # 写
# 事务 1 提交

result = self.get_note(note_id)      # 事务 2：读回返回值
assert result is not None
return result
```

`get_note()` 的读在事务 1 提交之后、事务 2 开始之前执行。在单连接、单线程的使用模式下，这个窗口里没有其他 writer，所以实际上安全。

问题是：docstring 说"单写者设计"，但代码里**没有任何机制强制这一点**。如果 sync.py 将来引入 ThreadPoolExecutor 处理媒体下载（一个常见优化），且多线程共享同一个 StateStore 实例，这个 TOCTOU 就会变成真实的 race condition。

**此问题现在不危险**，但应该在 StateStore 的类 docstring 里明确标注"不线程安全，调用方必须保证单线程或单进程访问"，而不只是说"单写者"（单写者可以被误解为"同时只有一个写操作"而非"只有一个线程"）。

---

## Design Concerns

### Concern 1：`mark_pending()` 的角色和 `transition()` 的职责重叠，可能导致混乱

`mark_pending()` 是专门的批量 `DISCOVERED → PENDING` 方法（L241–252），内部用 `executemany` + 条件 UPDATE（`AND status = 'DISCOVERED'`），不经过 `TRANSITIONS` 检查。

而 `transition()` 也支持 `DISCOVERED → PENDING`（TRANSITIONS 字典里有此路径），且会做 transition 合法性校验。

两条路径并存，但行为有细微差异：
- `mark_pending()` 是幂等的批量操作，非 DISCOVERED 的 note 被静默跳过；
- `transition()` 是严格单条操作，非法状态会抛异常。

sync.py 作者需要知道什么时候用哪个。如果 sync.py 对 DISCOVERED note 调用了 `transition(PENDING)` 而不是 `mark_pending()`，行为是对的但效率低。更危险的是反过来：对已经是 PENDING 的 note 调用 `mark_pending()` 会静默失败（因为 `AND status = 'DISCOVERED'` 不匹配），sync.py 不会收到任何报错。

**建议**：在 `mark_pending()` 的 docstring 里明确它是"枚举后批量入队"专用方法，并说明它对非 DISCOVERED note 静默跳过。

### Concern 2：`record_media_result` 没有检查 note 状态是否在 `MEDIA_SYNCING`，只检查 `MEDIA_PHASE_STATUSES`

`MEDIA_PHASE_STATUSES = {DETAIL_SUCCESS, MEDIA_SYNCING, MEDIA_PARTIAL}`（L111–113）。

这意味着 sync.py 可以在 `DETAIL_SUCCESS` 状态下（还没有调 `transition(MEDIA_SYNCING)`）就开始记录 media 结果。从语义上说，这是允许的（DETAIL_SUCCESS 代表"detail 完成，媒体未完成"，记录媒体结果是合理的），但 plan §3.3 描述的完整流程是：

```
DETAIL_SUCCESS → MEDIA_SYNCING → (record media results) → COMPLETE / MEDIA_PARTIAL
```

如果 sync.py 跳过了 `MEDIA_SYNCING` 这一步，直接在 `DETAIL_SUCCESS` 里记录媒体结果，那崩溃恢复时 `recover_interrupted()` 就找不到任何 `MEDIA_SYNCING` 状态的 note 来重置，而这些 note 会停留在 `DETAIL_SUCCESS`——这实际上也是正确的恢复起点（因为 recover 把 `MEDIA_SYNCING → DETAIL_SUCCESS`，结果一样）。

所以这不是 bug，但它意味着 `MEDIA_SYNCING` 状态的存在是"必须由 sync.py 主动进入"的，state.py 不强制这一点。如果 sync.py 忘了，媒体崩溃恢复路径的可观测性会变差（看不到"有多少 note 的媒体阶段被中断了"）。

### Concern 3：`status_counts()` 里出现未知状态值不会报错

`_row_to_state()` 在 L460 做 `NoteStatus(row["status"])`——如果 DB 里有一个不在枚举里的状态值（比如未来 schema 迁移后降级到旧版代码），会抛 `ValueError`，崩溃而不是降级处理。

对于诊断方法 `status_counts()`，这是可以接受的严格行为（宁可崩溃也不返回错误数据）。但 sync.py 在 startup 时调用 `recover_interrupted()` 之后如果也遍历所有 note，同样会遭遇这个问题。**这是一个防御性提示**，不是紧急 bug，但值得在代码注释里记录。

---

## Recommended Changes

以下均为小改动，不涉及逻辑重写。

**RC-1（高优先级）：为 `get_queue()` 补充 MEDIA_PARTIAL 排除的说明，并提供 `get_media_resume_queue()` 方法**

```python
def get_media_resume_queue(self, limit: int | None = None) -> list[NoteState]:
    """Notes in DETAIL_SUCCESS or MEDIA_PARTIAL that need the media resume path.
    
    These are intentionally excluded from get_queue() because they do NOT
    go through fetch_note; they resume from raw.json on disk.
    """
    sql = (
        "SELECT * FROM notes "
        "WHERE status IN ('DETAIL_SUCCESS', 'MEDIA_PARTIAL') "
        "ORDER BY rowid"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = self._conn.execute(sql, params).fetchall()
    return [self._row_to_state(row) for row in rows]
```

理由：这确保 sync.py 的"全部待处理工作"= `get_queue()` + `get_media_resume_queue()`，不依赖 sync.py 作者记住这条规则。

**RC-2（中优先级）：在 StateStore 类 docstring 里明确线程安全限制**

```python
class StateStore:
    """Durable note/sync state. Single-connection, single-writer, NOT thread-safe.

    Callers must ensure access from a single thread/process. SQLite's WAL mode
    allows concurrent readers, but this class holds one connection and does not
    use thread locks. A future concurrent-download optimization must either pass
    StateStore only to the main thread or replace it with a connection pool.
    """
```

**RC-3（低优先级）：`mark_pending()` docstring 补充静默跳过说明**

在 L242 的 docstring 里加：
> "Non-DISCOVERED notes are silently skipped (idempotent for already-queued notes)."

---

## Should Not Change

**1. 不拆分 `attempt_count` 为 detail_attempts + media_attempts**

Media 有自己的文件级 retry（在 sync.py 或 media.py 里），不需要在 state.py 层再记录 media attempt 次数。media_state 里的 `status: FAILED` 已经是充分的信息。拆分只会增加 schema 复杂度，收益为零。

**2. 不建 media 独立表**

结论同 P1_REVIEW_SONNET：1000 note 规模下 JSON 列完全够用，`filename` 作为查找键在 Python 层操作比 SQL JOIN 更简单直观。

**3. `TRANSITIONS` 字典是实现状态机而不是注释状态机**

L87–108 的 `TRANSITIONS` 字典让非法状态转移在测试中可以被精确捕获（`test_illegal_transition_rejected_and_unchanged`），不是仅靠人工审查。这是正确的做法，不应简化为 if-else 链。

**4. `assert result is not None`（L341）不应改为 if 检查**

这个 assert 在语义上是正确的：row 在事务内已被确认存在，读回失败是不可能的正常情况（除非 DB 损坏，那 assert 崩溃也是正确行为）。如果改为 `if result is None: raise`，会掩盖一个本不应发生的状态。

**5. WAL + FULL synchronous 不应降级到 NORMAL**

这个场景的写速率（每 note 一次事务，顺序处理）完全不是瓶颈，降级到 `synchronous=NORMAL` 仅为了性能没有意义，反而在电源故障时失去 durability 保证。

**6. 文件级 filename 的路径分隔符校验不应改为 `Path(filename).name == filename`**

当前的显式字符串检查（L365）比 `Path` 构建更可读，边界条件（空串、`.`、`..`）也更明确。

---

## Confidence

**整体：90%**

| 领域 | 置信度 | 说明 |
|---|---|---|
| 状态机正确性 | 95% | TRANSITIONS 字典覆盖全，崩溃痕迹语义正确，测试全部通过 |
| 事务原子性 | 92% | 每个公开 mutation 都用 `with self._conn:` 包裹，WAL+FULL 配置正确 |
| Media state 设计 | 88% | filename 作为查找键、url 的权威来源说明清晰；Issue 1 是未来的 sync.py 风险，不是当前的 bug |
| 恢复路径 | 90% | FETCHING→PENDING 和 MEDIA_SYNCING→DETAIL_SUCCESS 在一个事务里批量完成，测试覆盖了 attempt_count 保留语义 |
| 测试质量 | 88% | 覆盖了真实 failure mode（非法转移、崩溃恢复、filename 注入、token COALESCE）；唯一缺口是没有测试 MEDIA_PARTIAL 不在 get_queue() 里这一行为 |
| 线程安全假设 | 75% | 单线程假设正确但文档不足，将来引入并发时容易被忽视 |

**关键结论**：state.py 作为独立模块可以信任。进入 sync.py 开发阶段的前置条件是处理 Issue 1（MEDIA_PARTIAL 队列），否则处于 MEDIA_PARTIAL 状态的 note 在 sync.py 里会被静默遗忘。
