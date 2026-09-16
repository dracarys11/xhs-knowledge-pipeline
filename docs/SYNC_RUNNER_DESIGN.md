# Sync Runner 设计（P1 Phase 3）

设计日期：2026-09-16
状态：设计文档，未写代码
目标：单机 CLI 下可靠同步 1000+ 收藏——可中断、可恢复、可重试、幂等。

依据：

- `docs/P1_STATE_CONTRACT_V2.md`（状态契约：双队列、attempt 语义、提交协议、守卫分层）
- `docs/architecture/ACQUISITION_ARCHITECTURE_V1.md`（采集层冻结：组件职责、failure taxonomy、已知风险 R1–R13）
- `docs/P1_INCREMENTAL_SYNC_PLAN.md`（重试矩阵、退出码、枚举节流）

实现形态：新增 `src/xhs_ingest/sync.py` + CLI `sync` 子命令。**单线程、单进程、顺序处理**；无并发 worker、无消息队列、无 workflow engine、无分布式组件。

---

## 0. 前置依赖（collector 工作包，必须先于或同批落地）

sync runner 的正确性依赖四项 collector 修改（均为 P1 已规划、架构 V1 风险表条目）：

| # | 依赖 | 对应风险 | 若缺失的后果 |
|---|---|---|---|
| D-A | 分页契约：`iter_favorites(limit=None)` 逐页 yield `PageResult`（捕获服务端 cursor/has_more、停滞 fail-closed、滚动节流 1.5–3s 抖动） | R1/R2 | 无法证明全量枚举；本设计 §3 不成立 |
| D-B | Playwright 导航超时/连接异常 → `NetworkAcquisitionError` | R3 | 可重试传输错误被归为 UNKNOWN，重试矩阵失准 |
| D-C | 运行级 context 复用：`with collector.session() as s:` 内所有调用共享一个 persistent context；非 sync 命令保持逐调用行为 | R5 | 1000 次浏览器启停不可行 |
| D-D | token cache 读写异常日志化 | R4 | 缓存损坏不可见 |

---

## 1. Runtime Lifecycle

一次 `xhs-ingest sync` 的五个阶段，严格顺序，单遍执行：

```text
Phase 0  Bootstrap
         解析参数 → 打开 StateStore（未知 status 值 → 硬失败，契约 §6.3）
Phase 1  Startup Recovery（§2）
         recover_interrupted → [--retry-failed] → COMPLETE 守卫扫描
Phase 2  Enumeration（§3）
         全量枚举收藏，逐页 upsert；结束后批量 mark_pending
Phase 3  Media Resume Processing（§5）
         get_media_resume_queue() 逐 note 恢复
Phase 4  Fetch Queue Processing（§4）
         get_fetch_queue() 逐 note 完整采集
Phase 5  Finalize
         status_counts() 汇总 → sync_meta 写运行结论 → 退出码（§8）
```

规则：

- **单遍原则**：Phase 3、Phase 4 各扫一遍队列，无运行内第二遍。失败项靠下次运行或运行内即时重试（§7），不做调度循环。
- **顺序原则**：media resume 先于 fetch（最便宜、崩溃后续跑时 raw.json 里的签名 URL 最新鲜、先收口进行中的工作）。
- **中止检查点**：每个 note 处理前后检查运行级中止旗标（§7.2）；置位后停止领取新工作，在途 note 记录完结果再退出。
- Phase 2 中止（fail-closed 枚举失败）→ **直接跳到 Phase 5**，不处理任何 note。已发现的 note 安全处于 PENDING，零丢失；下次运行重新枚举。

运行规模算术（1000 收藏参考）：枚举 ≈ 50 页 × ~4s ≈ 3–5 分钟；note 处理 ≈ 1000 × (fetch ~8s + 间隔 2s + 媒体) ≈ 数小时。可靠性不靠单次跑完，靠任意时刻可中断可续跑；`--max-notes` 提供人为切片。

---

## 2. Startup Recovery Order

顺序有语义，不可调换：

```text
1. 打开 StateStore            未知 status 枚举值 → 立即崩溃（fail-closed，不猜测）
2. recover_interrupted()      FETCHING→PENDING（attempt 保留）
                              MEDIA_SYNCING→DETAIL_SUCCESS（媒体进度保留）
                              ← 必须先于任何队列查询，否则 MEDIA_SYNCING 在两队列均不可见（契约 §6.4）
3. [--retry-failed]           retry_failed()：FINAL_FAILED→PENDING，attempt 清零
4. COMPLETE 守卫扫描          对每个 COMPLETE note：
                                .complete 存在 → 跳过（O(1) stat，零后续动作）
                                .complete 缺失 → 两级校验（§6）：
                                  修复级通过 → 补写 .complete（不降级，Sonnet Adjustment 2 崩溃窗口）
                                  修复级失败 → COMPLETE→PENDING 降级（media_state 保留为期望值）
5. [--verify]                 深检模式：守卫扫描对全部文件重算 sha256（默认关闭）
6. 构建队列                    此时降级 note 已进入 fetch 队列，本运行即被处理
```

崩溃安全性来源：每个状态转移是独立事务（WAL + FULL），任意时刻 kill，Phase 1 重放即恢复；无专门"关机钩子"。

---

## 3. Acquisition → StateStore Flow

采集层不知道 StateStore（架构 V1 §2.5 冻结）；sync.py 是唯一桥接：

```text
with collector.session() as s:                      # §9：全程单 context
    for page in s.iter_favorites(limit=None):       # D-A：逐页 yield
        store.upsert_notes(page.items)              # 每页一个事务（发现即持久）
        # upsert 只刷新 listing 快照（token/url/title），永不触碰进度字段
    枚举结论 → sync_meta.last_enumeration_status / proof / cursor
store.mark_pending(所有 DISCOVERED)                  # 批量接纳，rowcount = 本次新增
```

- **发现即持久**：枚举中途崩溃，已 upsert 的页一个不丢；重跑从头重滚（滚动无法注入 cursor，plan §4.4 论证），upsert 幂等。
- **完成证据**：仅服务端 `has_more == false`（`SERVER_HAS_MORE_FALSE`）或页面显式空态。滚动停滞 → collector 抛错（fail-closed）→ 本运行中止（见下）。
- **枚举失败语义**：`AUTH_REQUIRED` / `RISK_CONTROLLED` / 停滞 → 记录 `ABORTED_<STATUS>` → 跳过 Phase 3/4 → exit 2。`RATE_LIMITED` → 暂停 60–120s（抖动）重试当前页，≤2 次后同样中止。**枚举中止时不处理已发现的 note**：简单、可预测，操作者稍后重跑即可，状态零丢失。
- token 流转：每次枚举刷新全部 `xsec_token`（upsert COALESCE），因此 TOKEN_INVALID 的 note 天然在下次运行获得新 token，无需运行内 token 重推导。

---

## 4. Fetch Queue Processing

队列来源：`get_fetch_queue()`（DISCOVERED / PENDING / 预算内 RETRYABLE_FAILED，发现序）。

每 note 管道（契约 §5.2 fetch 路径）：

```text
1. transition(FETCHING)                 # attempt_count +1（同事务）
2. ref = 由 NoteState 快照重建 FavoriteRef（note_id/xsec_token/source_url）
3. raw = fetch_note(ref)
     NETWORK_ERROR   → 运行内重试 ≤2 次（5s/15s 退避；重试不重新进入 FETCHING，
                       不消耗预算——预算计"fetch 开始"，运行内重试是其内部细节）
     TOKEN_INVALID   → RETRYABLE_FAILED(stage=FETCH)，跨运行重试
     CONTENT_UNAVAILABLE → FINAL_FAILED(stage=FETCH)，terminal
     AUTH/RISK       → 记录 note 错误 → 置运行中止旗标 → 结束循环
     RATE_LIMITED    → 运行级暂停（§7.2）
4. post = normalize(raw)                # PARSE_FAILED → RETRYABLE_FAILED(stage=NORMALIZE)
5. 原子写 data/<id>/raw.json            # tmp + os.replace
6. transition(DETAIL_SUCCESS)           # 证据：raw.json 在盘
7. media 阶段（§5 共享）                # 含 COMPLETE 提交协议
```

预算执行点：记录可重试失败时，若 `attempt_count >= 5` → 写 FINAL_FAILED 而非 RETRYABLE_FAILED，并**显眼日志提示 `--retry-failed`**（Sonnet Risk 3）。预算检查是 sync.py 职责，state 层不执行策略。

---

## 5. Media Resume Processing

队列来源：`get_media_resume_queue()`（DETAIL_SUCCESS / MEDIA_PARTIAL，发现序）。**不调 fetch_note**。

### 5.1 恢复入口守卫

```text
raw.json 存在？
  否 → transition(PENDING)（不计 attempt，WARNING 日志）→ 归入 fetch 队列，下次运行重采
  是 → 继续
```

（Sonnet Risk 2 的 fail-open 窗口闭合点。）

### 5.2 共享 media 阶段（fetch 路径第 7 步与 resume 路径共用同一函数）

```text
manifest = normalize(raw.json).media      # PARSE_FAILED → RETRYABLE_FAILED(stage=NORMALIZE)
transition(MEDIA_SYNCING)                 # 强制，先于任何文件工作（契约 D3）
new_completions = 0; url_expired = False
for item in manifest（顺序）:
    if verified(item.filename):           # §6 skip-if-verified 谓词
        continue                          # 已 COMPLETED 且磁盘匹配，无需重录
    download（每文件重试 ≤3 次，5s 退避）:
        成功      → record_media_result(COMPLETED, size, sha256, url=provenance)
                    new_completions += 1
        HTTP 403  → 不烧重试；record_media_result(FAILED, error="403")；
                    若本遍为 resume 遍 → url_expired = True
        其他失败  → 重试耗尽后 record_media_result(FAILED, error)
# ---- 收口判定（顺序即优先级）----
if manifest 全部 COMPLETED:
    原子写 canonical.json → post.md → .complete     # 提交序列，契约 §5.2
    transition(COMPLETE)                             # 整个序列的最后一次写
elif url_expired（仅 resume 遍）:
    先把本遍已完成文件落袋（上面循环已保证），然后
    transition(RETRYABLE_FAILED, MEDIA_DOWNLOAD_FAILED, stage=MEDIA, "URL_EXPIRED")
    → 下次运行走 fetch 队列换新 URL；已验证文件经 skip-if-verified 不重下
elif resume 遍 且 new_completions == 0 且 存在 FAILED:
    transition(RETRYABLE_FAILED, MEDIA_DOWNLOAD_FAILED, stage=MEDIA, "no progress")
    → no-progress 升级（契约 D2）：一切媒体工作经 detail 预算最终封顶
else:
    原子写 canonical.json → post.md（媒体现状渲染，P0 语义保留）
    transition(MEDIA_PARTIAL, 错误摘要, stage=MEDIA)
```

细节规则：

- **403 语义按遍区分**：初次遍（URL 分钟级新鲜）的 403 是普通媒体失败 → MEDIA_PARTIAL；resume 遍的 403 是签名过期 → 升级换 URL（plan §7.1）。
- **升级前落袋**：命中 403 不中断本遍，其余文件继续尝试，最大化已获进度后再升级。
- **纯 skip 遍**：全部文件验证命中 → 直接走 COMPLETE 收口，零网络。
- **崩溃痕迹**：文件循环中崩溃 → 停留 MEDIA_SYNCING → 下次启动恢复 DETAIL_SUCCESS，已提交文件一个不丢。
- 媒体 URL 一律从 raw.json 重新派生（normalizer 纯函数）；`media_state.url` 仅 provenance（契约 I-1）。

---

## 6. Artifact Validation Boundary

文件系统验证**全部属于 sync.py**（契约 §1 职责分层的执行侧）；state.py 与采集层永不触碰磁盘产物。

| 检查 | 时机 | 通过 | 不通过 |
|---|---|---|---|
| `raw.json` 存在 | resume 入口（每 note 一次） | 继续 media 阶段 | PENDING 降级（不计 attempt） |
| `.complete` 存在 | 启动守卫扫描（每 COMPLETE note 一次 stat） | 跳过该 note | 进入两级校验 |
| 修复级：canonical.json 存在 ∧ 其 media 清单全部文件 size 与记录一致（`--verify` 加 sha256） | `.complete` 缺失时 | 补写 `.complete`，不降级 | 降级 COMPLETE→PENDING |
| skip-if-verified：media_state[filename].status==COMPLETED ∧ 文件存在 ∧ size 一致 | media 阶段每 manifest 项 | 跳过下载 | 重下载并替换该条目 |
| 零字节 / 残留 `.tmp` | 隐含于 size 比对（记录 size 恒 >0） | — | 视为缺失，重下载 |

明确**不验证**（非目标）：媒体内容可解码性、视频可播放性、Content-Type、post.md 一致性（它是渲染产物，从不回流）。canonical.json 仅在修复级校验中被读取一次——那是维护性核对，不是恢复输入（契约 I-5 注）。

原子写 helper（`atomic_write_text(path, content)`：tmp + `os.replace`）为 sync.py 内部工具；`cli.py:ingest_note` 后续统一复用，非本阶段必须。

---

## 7. Failure Handling

### 7.1 note 级重试矩阵（错误类 × stage × 预算）

| 错误类 | 运行内 | 跨运行 | stage | 预算交互 |
|---|---|---|---|---|
| NETWORK_ERROR | 重试 ≤2（5s/15s） | RETRYABLE_FAILED | FETCH | 下次 FETCHING 消耗 1 |
| TOKEN_INVALID | 否 | RETRYABLE_FAILED（枚举自动刷新 token） | FETCH | 同上 |
| CONTENT_UNAVAILABLE | 否 | FINAL_FAILED（terminal） | FETCH | — |
| PARSE_FAILED | 否 | RETRYABLE_FAILED（载荷可能不同） | NORMALIZE | 同上 |
| MEDIA 传输失败 | 每文件 ≤3（5s） | MEDIA_PARTIAL → 下次 resume | MEDIA | 不直接消耗 |
| MEDIA 403（resume 遍） | 不重试 | RETRYABLE_FAILED(URL_EXPIRED) | MEDIA | 下次 FETCHING 消耗 1 |
| MEDIA 零进展（resume 遍） | — | RETRYABLE_FAILED(no progress) | MEDIA | 同上 |
| UNKNOWN | 防御性 | RETRYABLE_FAILED | 按发生点 | 同上；**永不推进完成** |

预算算术：单 note 最坏 fetch_note 调用数 = 5 次运行 × (1 + 2 运行内重试) = 15，有界。

### 7.2 运行级中止旗标

```text
AUTH_REQUIRED / RISK_CONTROLLED   → 立即置位（session 全局失效/防风控升级）
RATE_LIMITED                      → 暂停 60–120s（抖动）重试当前操作；单运行 ≤2 次暂停，超出置位
浏览器 context 嘎死（Playwright 连接级异常）→ 视同 NETWORK_ERROR 运行级 → 置位
```

置位后：停止领取新 note；在途 note 完成收口或记录错误；Phase 5 汇总后 exit 2。**任何错误先落库再继续/退出**——失败原因不允许只存在于 stderr。

### 7.3 中断信号

不注册信号处理器。Ctrl-C / SIGTERM / kill -9 依靠事务级一致性：在途 note 留 FETCHING / MEDIA_SYNCING 崩溃痕迹，下次启动 Phase 1 重放恢复。KeyboardInterrupt 打印"状态已保存，可直接重跑"后以 130 退出。

---

## 8. Exit Codes

| code | 含义 | 触发 |
|---|---|---|
| 0 | 队列处理完毕且全部 COMPLETE（含"无事可做"） | Phase 3/4 正常走完，无 MEDIA_PARTIAL/RETRYABLE_FAILED/FINAL_FAILED 残留 |
| 2 | 运行中止，状态已保存，可直接重跑 | AUTH/RISK、RATE 暂停耗尽、枚举停滞 fail-closed、context 死亡 |
| 3 | 运行走完但仍有未完成 note | 存在 MEDIA_PARTIAL / RETRYABLE_FAILED / FINAL_FAILED |
| 4 | 未预期异常 | 兜底 except |
| 130 | 用户中断 | KeyboardInterrupt（语义同 2，可重跑） |

优先级：2 > 3 > 0。运行中止且同时存在残留未完成 → 2（中止解释了不完整的原因）。

---

## 9. Browser Session Lifecycle

```text
Phase 2 开始前       collector.session() 打开唯一 persistent context（惰性：仅当有采集工作）
Phase 2–4            枚举与全部 fetch_note 共享该 context
Phase 5 / 任何退出路径  try/finally 关闭（Sonnet Adjustment 3）
```

- **关闭顺序**：运行级中止时，先让在途 note 的状态落库，再关 context——状态优先于资源清理。
- 媒体下载不占用浏览器（httpx 独立通道，架构 V1 §2.6）。
- 纯恢复运行（队列只有 media resume 且 raw.json 全在）不打开浏览器——sync 只在有 fetch 工作时进入 session。
- 非 sync 命令（favorites/ingest/login）保持架构 V1 的逐调用 context 行为，不受影响。
- context 死亡 = 运行级 NETWORK 中止（§7.2），不做自动重启浏览器（最小设计：重跑即恢复）。

---

## 10. Non-goals

| 不做 | 替代 |
|---|---|
| 并发 worker / 并发媒体下载 | 顺序处理；可靠性来自可恢复而非速度 |
| message queue / 任务表 | 双队列 SQL 投影（契约 §3） |
| workflow engine / DAG | §4/§5 两个线性管道 + TRANSITIONS 校验 |
| distributed lock / 分布式任何东西 | 单进程 + SQLite 文件锁响亮失败 |
| 运行内队列第二遍 / 调度循环 | 单遍原则；重试 = 下次运行或运行内即时重试 |
| token 运行内重推导 | 下次运行枚举天然刷新 |
| 定时 / daemon / 自动重跑 | 用户手动重跑（exit code 可脚本化） |
| 媒体内容级验证、RAG/OCR/UI/多账号、评论/boards | plan §9 / 架构 V1 边界不变 |
| config 文件系统 | CLI flags：`--max-notes` `--retry-failed` `--verify` `--state-db`（另有全局 `--output-dir` `--profile-dir` `--headed`） |

---

## 11. 实现拆分与测试要求

**文件**：新增 `src/xhs_ingest/sync.py`（runner + 原子写 helper + 常量表：`MAX_IN_RUN_RETRIES=2`、`MEDIA_FILE_RETRIES=3`、`BACKOFF_*`、`RUN_PAUSES_MAX=2`）；`cli.py` 增加 `sync` 子命令；`state.py`/`media.py`/`collector.py` 按 §0 依赖项修改。

**测试（fake collector + tmp_path，无网络）**——对齐 P0_TO_P1_REVIEW §5.7：

1. 多页枚举 → 逐页 upsert + 批量接纳；
2. 完整管道：fetch → raw.json → DETAIL_SUCCESS → 全媒体 → `.complete` → COMPLETE；
3. 崩溃于每个断点后重跑，恢复到正确续点（含"最后文件提交后、COMPLETE 转移前"的零网络收口）；
4. 全 COMPLETE 二次运行：零 fetch 调用、零下载、exit 0；
5. MEDIA_PARTIAL 重跑只下载失败文件（mock download 计数）；
6. resume 遍 403 → RETRYABLE_FAILED(URL_EXPIRED)，已验证文件不重下；
7. resume 遍零进展 → no-progress 升级；预算耗尽 → FINAL_FAILED + 日志提示；
8. RATE 暂停耗尽 → exit 2，已完成状态完好，未误标 COMPLETE；
9. raw.json 缺失的 DETAIL_SUCCESS → PENDING 降级且不计 attempt；
10. `.complete` 缺失两分支：修复（补写）与降级（重采）；
11. context 复用：中止路径下 session 仍被关闭（mock context manager 验证 try/finally）；
12. 启动恢复顺序：MEDIA_SYNCING 未恢复时不进任何队列，恢复后进 resume 队列。
