# P1 State Contract v2

设计日期：2026-09-16
状态：设计文档，未修改任何代码
适用对象：`src/xhs_ingest/state.py`（已实现 v1）与后续 `sync.py`

输入依据：

- `docs/P1_INCREMENTAL_SYNC_PLAN.md`（原始设计）
- `docs/P1_REVIEW_SONNET.md`（设计 review：Risk 1 media_state 权威来源、Risk 2 raw.json 缺失、Adjustment 1 NEVER_RUN、Adjustment 2 两级校验）
- `docs/P1_STATE_REVIEW_SOL.md`（实现 review：Issue 1 队列混淆、Issue 2 线程安全、Concern 1–3）
- 当前 `state.py` 实现（9 状态 + TRANSITIONS + WAL/FULL 事务语义）

---

## 0. v1 → v2 变更摘要

| # | 变更 | 解决的问题 | 来源 |
|---|---|---|---|
| D1 | 工作分为**两个物理队列**（detail work / media resume work），retry 是路由策略不是第三个队列 | `get_queue()` 排除 MEDIA_PARTIAL 导致该状态 note 被静默遗忘的风险 | Sol Issue 1 |
| D2 | 新增 **no-progress 升级规则**：媒体恢复遍零进展 → 升级 RETRYABLE_FAILED，经 detail 预算封顶 | 关闭 v1 的无限跨运行媒体重试漏洞（永久 404 文件每运行重试一次，永不终止） | v2 新增（v1 未覆盖） |
| D3 | `transition(MEDIA_SYNCING)` 从"可选"提升为**媒体阶段开始前的强制步骤** | 防止 sync.py 跳过 MEDIA_SYNCING 使媒体阶段崩溃不可观测 | Sol Concern 2 |
| D4 | COMPLETE 进入条件与**提交顺序协议**显式化：transition(COMPLETE) 必须是 note 提交序列的最后一次写 | 严格化"不误判成功"的证据链 | 计划 §6 的形式化 |
| D5 | `.complete` 校验分为**修复级 / 降级级**两层 | 修复 Sonnet Adjustment 2 指出的崩溃窗口（DB 已 COMPLETE 但 marker 未写 → 不应触发全量重采） | Sonnet Adjustment 2 |
| D6 | `mark_pending` 与 `transition` 的角色分工写入 contract | 两条 DISCOVERED→PENDING 路径并存导致调用者困惑 | Sol Concern 1 |
| D7 | 明确**单线程变异约束**与进入 FETCHING **保留 media_state** | TOCTOU 窗口文档化；URL 刷新后不重复下载已验证文件 | Sol Issue 2；v2 澄清 |

Schema 无变更：v2 全部语义在现有 `notes` / `sync_meta` 列上表达。

---

## 1. 职责边界：每类事实只有一个所有者

### 1.1 四类 artifact 的事实定义

| Artifact | 记录的事实 | 权威范围 | 写入时机 | 可变性 |
|---|---|---|---|---|
| `data/<id>/raw.json` | "采集时刻 T 服务端为本 note 返回了什么" | **内容与媒体 URL 推导的唯一事实来源**。note_id、正文、作者、stats、媒体清单全部由它经 normalizer（纯函数）确定性派生 | detail fetch 成功并 normalize 通过后、进入 DETAIL_SUCCESS 前（原子写） | 重 fetch 时整体替换；否则不变 |
| `data/<id>/canonical.json` | "raw.json 的规范化投影 + 截至本次提交的媒体执行结果" | **无**。派生只读模型，供外部消费者；同步/恢复流程永不读取它做决策 | 每次 note 提交时重写（原子写） | 总是可由 raw.json + 磁盘媒体 + media_state 重建 |
| `.xhs-state/sync.db`（SQLite） | "做过什么、试过几次、错在哪、还剩什么" | **执行进度**：发现集合、lifecycle 状态、attempt 计数、错误分类、per-file 媒体执行记录（media_state）、枚举/运行元信息 | 每次状态转移 / 每个媒体文件 / 每个枚举页（独立事务） | 当前态快照，非日志 |
| `data/<id>/.complete` | "本目录是一套完整、自洽的成品" | **可见性封条**，不是正确性开关。给人与外部工具看；sync 用它做廉价的跳过检查 | note 提交序列的**最后一次**文件写入（原子写） | 只增不改；丢失可修复 |

`post.md` 与 canonical.json 同级：派生渲染产物，恢复流程不读。

### 1.2 权威性规则（invariants）

1. **I-1 内容权威**：内容问题（正文、清单、URL 列表）只看 raw.json。`media_state.url` 是构建时 provenance，**不得**作为权威或假设未过期（Sonnet Risk 1）。
2. **I-2 进度权威**：工作决策（跳过、入队、重试、完成）只看 SQLite + 按需 stat 磁盘。canonical.json / post.md / .complete 不参与决策（.complete 例外：作为跳过检查的触发器，见 §5.3）。
3. **I-3 字节权威**：assets/ 下的文件是媒体字节的事实。media_state 的 checksum/size 是**期望值**；验证 = 磁盘现实 vs 状态期望的比对，不匹配按缺失处理。
4. **I-4 状态可对磁盘撒谎，磁盘不能对状态撒谎**：DB 说 COMPLETE 但文件被删 → 守卫降级（§5.3）；磁盘上存在孤儿文件而状态无记录 → 不作为任何完成依据，按缺失处理。
5. **I-5 单向依赖**：raw.json → {canonical, post.md, media 清单}。渲染产物永不回流为恢复输入（维护性校验例外见 §5.3 注）。
6. **I-6 丢失半径**：SQLite 损坏 = 进度丢失，内容不丢失（raw/media 仍在，可据 `.complete` + canonical 部分重建 COMPLETE 集，最坏全量重采）；raw.json 损坏 = 该 note 内容丢失，进度降级重采。

### 1.3 组件写权限

| 组件 | 允许的操作 |
|---|---|
| 枚举（list_favorites 调用方） | `upsert_notes`（页批量）；不碰 status/attempt/media_state |
| sync runner（主线程） | 所有 `transition`、`mark_pending`、`retry_failed`、守卫降级、meta 读写 |
| 媒体执行（下载循环） | 只经主线程调用 `record_media_result`；自身不转移 note 状态 |
| state.py | 不触碰 `data/` 目录。跨越 state ↔ 磁盘的桥接（守卫、封条）只属于 sync runner |

---

## 2. 状态机参考：status 是证据断言

每个 status 断言一个可检验的事实。转移表与 v1 `TRANSITIONS` 完全一致（无变更）。

| Status | 断言的证据 | 静止态？ |
|---|---|---|
| `DISCOVERED` | 收藏列表声称此 note 存在。仅此而已 | 是 |
| `PENDING` | 已被接纳进工作队列 | 是 |
| `FETCHING` | 一个 detail fetch 正在进行。**纯崩溃痕迹**，启动恢复必须清除 | 否 |
| `DETAIL_SUCCESS` | `raw.json` 已在磁盘上且 normalize 可复现。**本状态存在的全部依据** | 是 |
| `MEDIA_SYNCING` | 媒体阶段正在进行。**纯崩溃痕迹**，恢复后回到 DETAIL_SUCCESS | 否 |
| `MEDIA_PARTIAL` | raw.json 完好；manifest 中存在未提交验证的文件 | 是 |
| `COMPLETE` | 完整证据链已提交并封条（§5） | 是（终态，守卫可降级） |
| `RETRYABLE_FAILED` | 最近一次尝试以可重试错误类失败，且预算未耗尽 | 是 |
| `FINAL_FAILED` | 预算耗尽或 terminal 错误类；仅人工 `--retry-failed` 可离开 | 是（终态） |

转移表（同 v1 实现，此处为规范副本）：

```text
DISCOVERED     → PENDING
PENDING        → FETCHING
FETCHING       → DETAIL_SUCCESS | RETRYABLE_FAILED | FINAL_FAILED | PENDING(恢复)
DETAIL_SUCCESS → MEDIA_SYNCING | COMPLETE(零媒体) | PENDING(raw.json 丢失守卫)
MEDIA_SYNCING  → COMPLETE | MEDIA_PARTIAL | RETRYABLE_FAILED | DETAIL_SUCCESS(恢复)
MEDIA_PARTIAL  → MEDIA_SYNCING | RETRYABLE_FAILED | PENDING(raw.json 丢失守卫)
COMPLETE       → PENDING(磁盘守卫降级)
RETRYABLE_FAILED → FETCHING | PENDING
FINAL_FAILED   → PENDING(仅 retry_failed())
```

Fail-closed 规则：DB 中出现枚举外的 status 值 = 硬错误（崩溃，不猜测降级）——Sol Concern 3 的行为确认为规范。

---

## 3. Work Queue Contract

### 3.1 两类物理工作 + retry 作为路由策略

**不假设只有一个队列，也不引入第三个存储。** 工作是 status 的 SQL 投影（视图语义），不是独立表：

| 工作类 | 定义 | 获取方式 | 处理路径 |
|---|---|---|---|
| **detail work** | 需要（重新）执行 `fetch_note` 的 note | `get_fetch_queue()`：`status IN ('DISCOVERED','PENDING') OR (status='RETRYABLE_FAILED' AND attempt_count < max)`，按 rowid（发现顺序） | 完整管道：fetch → normalize → raw.json → 媒体 → 提交 |
| **media resume work** | detail 证据已在磁盘、媒体阶段未完成的 note | `get_media_resume_queue()`：`status IN ('DETAIL_SUCCESS','MEDIA_PARTIAL')`，按 rowid（Sol RC-1；v1 未提供，**列为 sync.py 开发前置项**） | 不调 fetch_note：读 raw.json → normalize → skip-if-verified 补齐 → 提交 |

两类队列**互斥且合起来覆盖全部非终态工作**：DISCOVERED / PENDING / RETRYABLE_FAILED ∪ DETAIL_SUCCESS / MEDIA_PARTIAL = 除 COMPLETE / FINAL_FAILED 外的一切。`get_fetch_queue()` 的 docstring 必须声明"DETAIL_SUCCESS 与 MEDIA_PARTIAL 被有意排除"（D1，防遗忘）。

**retry 不是第三个队列**，是三种路由形式：

| retry 形态 | 机制 | 预算来源 |
|---|---|---|
| detail 级自动重试 | RETRYABLE_FAILED 且 `attempt_count < max` → 自动留在 fetch 队列 | detail 预算（§4.1） |
| 媒体文件级重试 | 媒体阶段内的**运行内即时重试**（每文件 ≤3 次，短退避；403 类立即走 URL 过期升级，不烧重试） | 无持久计数（§4.2） |
| 终态人工重置 | `retry_failed()`：FINAL_FAILED → PENDING，attempt 清零，错误清除。唯一入口是显式 `--retry-failed` | 重置后重新计数 |

### 3.2 运行内顺序（SHOULD）

```text
启动: open → recover_interrupted() → (先于任何队列查询)
枚举: 每拦截页 upsert_notes(单事务) → 批量 mark_pending(全部 DISCOVERED)
执行: ① media resume queue 先处理（最便宜、URL 最新鲜、清欠账）
      ② fetch queue 后处理
收尾: status_counts() 汇总 + sync_meta 写运行结论
```

media resume 优先的理由：崩溃后续跑时 raw.json 里的签名 URL 最新鲜；把进行中的工作先收口成 COMPLETE；代价最低。单遍原则：每运行对两个队列各扫一遍，**无运行内第二遍**（重试靠下次运行或运行内即时重试，不做调度循环）。

### 3.3 `mark_pending` vs `transition` 的角色分工（D6）

| 方法 | 角色 | 语义 |
|---|---|---|
| `mark_pending(ids)` | **枚举接纳专用**批量操作 | 幂等；非 DISCOVERED 的 note **静默跳过**（这是特性不是 bug）；返回接纳计数供运行报告 |
| `transition(id, …)` | **管道生命周期专用**单条操作 | 严格校验 TRANSITIONS，非法即抛 ValueError |

规则：sync.py 批量接纳只用 `mark_pending`；note 级生命周期只用 `transition`。fetch 队列包含 DISCOVERED 是安全网——即使忘了接纳，note 仍会浮现并被当作 detail work 处理。

---

## 4. Attempt Semantics

### 4.1 detail attempt（`attempt_count`，唯一持久预算）

- **单位**：note 进入 FETCHING 的次数（fetch 启动次数）。
- **递增**：与 PENDING→FETCHING / RETRYABLE_FAILED→FETCHING 同一事务原子递增。
- **预算**：`DEFAULT_MAX_ATTEMPTS = 5`（Sonnet Risk 3）。可重试失败发生时若 `attempt_count >= max` → FINAL_FAILED 而非 RETRYABLE_FAILED。
- **消耗场景**：一切进入 FETCHING 的转移，无论结局是成功、失败还是崩溃。
- **不消耗场景**：媒体阶段全部工作；守卫降级（COMPLETE/DETAIL_SUCCESS → PENDING 本身不计，后续 fetch 照常计）；`retry_failed()` 是唯一清零点（也清 last_error）。
- **单调性**：attempt_count 只增不清（人工重置除外）。降级重采保留历史计数是简化取舍，出口是 `--retry-failed`。
- **进入 FETCHING 保留 media_state**（D7）：URL 刷新重采后，skip-if-verified 仍能跳过磁盘上完好的旧文件；manifest 外的陈旧条目按查找键语义自然忽略。

### 4.2 media attempt（无持久计数）

- **单位**：媒体阶段内单文件下载尝试。**不持久化计数**（Sol "Should Not Change" #1：media_state 的 per-file status 已是充分信息）。
- **运行内预算**：每文件每次媒体遍 ≤3 次即时重试（短退避）；HTTP 403 类不烧重试，立即触发 URL 过期升级。
- **跨运行封顶——no-progress 升级规则（D2，v2 新增）**：

> 一次 **media resume 遍**结束时，若 `(新提交为 COMPLETED 的文件数 == 0)` 且 `存在 ≥1 个 FAILED 文件`，则该 note → `RETRYABLE_FAILED`（`last_error_status = MEDIA_DOWNLOAD_FAILED`，message 注明 `no progress in media resume pass`）。

  - 仅适用于 **resume 遍**（遍开始时状态为 DETAIL_SUCCESS / MEDIA_PARTIAL）。初次媒体遍（紧跟新鲜 fetch）零进展 → 照旧 MEDIA_PARTIAL，避免网络整体故障时批量烧 detail 预算。
  - 效果：永久 404 文件的 note 走 `MEDIA_PARTIAL → (resume 零进展) → RETRYABLE_FAILED → 重 fetch → …` 循环，每循环消耗 1 次 detail attempt，5 次后 FINAL_FAILED。**一切媒体工作经此规则被 detail 预算最终封顶，不存在无限重试。**
  - 有进展的遍（哪怕 14/15 完成）不升级，note 留在 MEDIA_PARTIAL 等下次。

### 4.3 崩溃恢复与预算

| 事件 | detail 预算 | media 预算 | 恢复动作 |
|---|---|---|---|
| FETCHING 中崩溃 | **消耗 1**（防崩溃循环，有意取舍） | — | 启动恢复 FETCHING→PENDING，计数保留 |
| MEDIA_SYNCING 中崩溃 | 0 | 0（已提交文件保留；未提交文件下次重试） | 启动恢复 MEDIA_SYNCING→DETAIL_SUCCESS |
| 最后一个文件提交后、COMPLETE 转移前崩溃 | 0 | 0 | 恢复为 DETAIL_SUCCESS + 全量 media_state；下次 resume 遍全部 skip-if-verified 命中 → 零网络完成收口 |
| raw.json 写入后、DETAIL_SUCCESS 转移前崩溃 | 0 | — | note 仍在 FETCHING → 恢复 PENDING 重 fetch（raw.json 被下次原子写覆盖；此窗口只浪费一次抓取，不产生错误状态） |
| AUTH/RISK 运行中止 | 已启动的照常计 | — | 状态已落库，下次运行继续 |

---

## 5. COMPLETE 证据与提交协议

### 5.1 进入 COMPLETE 的必要条件（全部满足）

设 M = normalize(raw.json) 得到的媒体 manifest：

1. **转移合法性**：当前状态 ∈ {MEDIA_SYNCING, DETAIL_SUCCESS}（后者仅零媒体 note）；
2. **detail 证据在位**：`raw.json` 存在（DETAIL_SUCCESS 的不变量，进入前廉价 stat 复核）；
3. **manifest 闭合**：∀ item ∈ M：media_state 中存在同 filename 条目且 status=COMPLETED，含 size_bytes 与 checksum_sha256；
4. **字节在位**：∀ item ∈ M：`assets/<filename>` 存在且 size 与记录一致（sha256 已在下载提交时对流式字节计算并随原子 rename 封存；默认不重算，`--verify` 深检时重算）；
5. **渲染已提交**：canonical.json、post.md 已原子写入（媒体结果为最终值）；
6. **封条已盖**：`.complete` 已原子写入（内容为最小 JSON：`{note_id, finished_at}`）；
7. **状态最后落**：`transition(COMPLETE)` 是整个序列的**最后一次写**。

推论：COMPLETE 从不由文件存在性单独推断；它是"验证 → 渲染 → 封条 → 状态"顺序提交的结果。零媒体 note：M 为空，条件 3/4 平凡成立，DETAIL_SUCCESS → COMPLETE 直达。

### 5.2 note 提交协议（顺序规范）

**fetch 路径（detail work）**

```text
1.  transition FETCHING            (attempt+1)
2.  fetch_note → raw
3.  normalize(raw) → post          (失败按错误类 → RETRYABLE_FAILED / FINAL_FAILED)
4.  原子写 raw.json
5.  transition DETAIL_SUCCESS      ← 证据: raw.json 在盘
6.  transition MEDIA_SYNCING       ← 强制，先于任何文件工作 (D3)
7.  逐文件 (主线程):
      skip-if-verified 命中 → record_media_result(COMPLETED, 期望值沿用)
      否则下载(≤3 次重试)  → record_media_result(...)   [每文件独立事务]
8a. 全 COMPLETED → 原子写 canonical.json → post.md → .complete → transition COMPLETE
8b. 有失败     → 原子写 canonical.json → post.md → transition MEDIA_PARTIAL (错误摘要入 last_error)
```

**resume 路径（media resume work）**

```text
0.  守卫: raw.json 存在？
      否 → transition PENDING (不计 attempt, WARNING 日志) → 归入 fetch work (Sonnet Risk 2)
1.  normalize(raw.json)            (PARSE_FAILED → RETRYABLE_FAILED)
2.  transition MEDIA_SYNCING
3.  同上 7
4.  零进展判定 (§4.2) → RETRYABLE_FAILED 或按 8a/8b 收口
```

### 5.3 `.complete` 语义与两级校验（D5）

**写入侧**：`.complete` 是提交序列最后第二步（在 transition COMPLETE 之前），原子写。存在即声称"完整成品"。

**运行侧跳过检查**（每次 sync 对每个 COMPLETE note）：

| 触发 | 检查 | 动作 |
|---|---|---|
| `.complete` 存在（默认路径） | 无（O(1) stat） | 跳过该 note，零网络零重写 |
| `.complete` 缺失（或 `--verify`） | **修复级**：canonical.json 存在 ∧ 其 media 清单全部文件 size（`--verify` 时加 sha256）与记录一致 | 全部通过 → **补写 `.complete`，不降级**（修复 Sonnet Adjustment 2 的崩溃窗口：DB 已 COMPLETE、marker 未写、数据实完好） |
| 同上 | 任一失败 | **降级级**：`transition COMPLETE → PENDING`（media_state 保留为期望值，attempt 保留），归入 fetch work 重采 |

注：修复级读取 canonical.json 是**维护性校验**对"已提交声明"的核对，不违反 I-5（采集管道仍不读它）。

### 5.4 skip-if-verified 谓词

```text
verified(filename) ⟺ media_state[filename].status == COMPLETED
                     ∧ assets/<filename> 存在
                     ∧ size == media_state[filename].size_bytes
```

- size 是廉价绊线（零字节、截断、错文件都会触发不一致）；sha256 重算默认关闭，`--verify` 开启。
- 不匹配 → 视为缺失，重新下载并以新结果替换该 filename 条目。
- 磁盘孤儿（无状态记录的文件）：不查询、不信任、不删除，按缺失处理。

---

## 6. 运行约束

1. **单线程变异（D7）**：StateStore 单连接、**非线程安全**。所有 mutation 必须在主线程执行；媒体下载若未来引入并发，工作线程只回传结果、由主线程 `record_media_result`。`transition()` 的事务后读回在单线程下无 TOCTOU（Sol Issue 2 的文档化处置）。
2. **单进程**：并发第二个 sync 进程是操作错误；SQLite 文件锁使其以 SQLITE_BUSY **响亮失败**，这是正确行为，不加应用层锁。
3. **fail-closed 枚举**：未知 status 值 → 硬崩溃；非法转移 → ValueError 且零副作用；错误字段只允许出现在失败目标上。
4. **恢复先于队列**：任何队列查询前必须先 `recover_interrupted()`（否则 MEDIA_SYNCING 会同时缺席两个队列）。

---

## 7. 明确禁止的复杂度

| 禁止 | 替代物（已在 v2 内） |
|---|---|
| workflow engine / DAG / 步骤注册表 | §5.2 两个线性提交协议 + TRANSITIONS 表 |
| message queue / 任务表 | §3 队列 = status 上的 SQL 投影，无独立存储 |
| distributed lock / leader election | 单进程 + SQLite 文件锁响亮失败（§6.2） |
| event sourcing / 事件日志 / attempt 历史 | 当前态 + last_error 快照；历史靠运行日志（stderr） |
| 顺带禁止：媒体并发、调度 daemon、配置系统、多账号 | 顺序处理 + 用户手动重跑（计划 §9 不变） |

判断标准：任何新增机制若不能回答"它防住了哪个已列举的失败模式"，就不加。v2 的两个新机制（media resume 队列、no-progress 升级）分别对应"遗忘 MEDIA_PARTIAL"与"无限媒体重试"。

---

## 8. 对 state.py 的实现影响（后续执行项，本文档不改代码）

| 项 | 类型 | 对应 |
|---|---|---|
| 新增 `get_media_resume_queue()` | 方法（Sol RC-1 原样） | D1，**sync.py 开发前置条件** |
| `get_fetch_queue()` docstring 声明排除 DETAIL_SUCCESS / MEDIA_PARTIAL | 文档 | D1 |
| `mark_pending()` docstring 声明"枚举接纳专用，非 DISCOVERED 静默跳过" | 文档 | D6 |
| StateStore 类 docstring 声明"单线程、非线程安全" | 文档 | D7 |
| no-progress 升级、两级校验、提交协议、skip-if-verified 谓词 | **sync.py 职责**，state.py 无需改动 | D2/D4/D5 |

Schema、TRANSITIONS、attempt 语义、恢复语义均与 v1 实现兼容，无需迁移。
