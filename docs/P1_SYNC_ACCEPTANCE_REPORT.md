# P1 增量同步真实验收报告 (P1 Sync Acceptance Report)

**验收时间**: 2026-09-16  
**验收环境**: macOS / Python 3.13.7 / SQLite 3.43 (WAL 模式) / Playwright (Chromium) / HTTPX  
**状态数据库**: `.xhs-state/sync.db` (Schema v2)  
**产物目录**: `data/<note_id>/`  
**自动化测试**: 90/90 passed (`tests/`)

---

## 1. 已验证能力 (Verified Capabilities)

通过全量自动化回归测试（90 项）及针对真实小红书账号进行的连续 4 阶段真实验收（包括初始同步、状态恢复、幂等推进、产物损毁故障注入与自愈），以下能力已被严格证实：

### 1.1 真实环境端到端同步闭环
* **完整链路打通**：`Authenticated Session` → `Favorites Interception (/api/sns/web/v2/note/collect/page)` → `SQLite StateStore (v2 Schema)` → `Note Detail SSR/Feed Fetch` → `CanonicalPost Normalization` → `Media Streaming Download` → `Filesystem Commit`。
* **多媒介笔记全覆盖**：成功实测落盘纯图文笔记（1~15 张图片）、多图高清笔记（带 uhdr / spectrum webp 格式）、以及主视频笔记（视频流 + 封面图），落盘结构完全满足 DoD：
  ```text
  data/<note_id>/
  ├── raw.json
  ├── canonical.json
  ├── post.md
  ├── .complete
  └── assets/
      ├── image_01.jpg / ...
      └── video_01.mp4 (若包含视频)
  ```

### 1.2 启动级崩溃恢复 (Startup Crash Recovery)
* **中间态自动复位**：启动时执行 `store.recover_interrupted()`：
  * 将上次因进程意外终止/断网留存的 `FETCHING` 记录（如实测中的 `6a9d7bfa0000000028001e28`）原子复位为 `PENDING`，不额外扣减重试预算。
  * 将崩溃在媒体下载途中的 `MEDIA_SYNCING` 记录原子复位为 `DETAIL_SUCCESS`，以便直接复用已有 `raw.json` 进入媒体恢复。

### 1.3 媒体断点恢复管线 (Media Resume Pipeline)
* **独立恢复队列**：`run_sync` 优先消费 `store.get_media_resume_queue()`（涵盖 `DETAIL_SUCCESS` 与 `MEDIA_PARTIAL`）。
* **免网络请求恢复**：基于本地已有 `raw.json` 重新生成标准化对象，跳过耗时的浏览器详情页访问。
* **文件级 Skip-if-Verified**：对每项媒体检查本地已落盘文件大小与哈希，已完成的媒体直接跳过，仅向远端发起未下载/失败媒体的 HTTP 请求（实测笔记 `6a8d5895000000002a004888` 仅发 1 次请求补齐最后 1 张图即完成封签）。

### 1.4 物理产物守卫与自愈机制 (COMPLETE Artifact Guard & Healing)
* **拒绝虚假 COMPLETE**：启动阶段扫描所有标记为 `COMPLETE` 的笔记，逐一比对物理磁盘产物（`raw.json`、`canonical.json`、`post.md` 及全量媒体文件大小一致性）。
* **故障降级**：实测人为删除 `6a9683a70000000028003e34` 的 `canonical.json` 后再次运行，Guard 准确识别产物缺失，立即清除失效的 `.complete` 标记并原子降级至 `PENDING`。
* **管线自愈**：该降级笔记自动排入当次同步队列第一顺位，重新获取详情、生成 `canonical.json`、校验媒体并重新写回 `.complete`，成功自愈回 `COMPLETE`。
* **封签丢失补全**：对物理产物全部完好但仅缺失 `.complete` 文件的边界情况，Guard 自动补齐印章，不触发冗余重抓。

### 1.5 严格的媒体提交核验 (Media Commit Verification)
* **杜绝虚假封签**：在将笔记流转为 `COMPLETE` 前，执行 `_verify_media_on_disk()` 校验磁盘上所有媒体文件的物理存在与非零字节数；若下载器声称完成但物理文件不存在或截断，强制拦截并流转为 `MEDIA_PARTIAL`。

### 1.6 状态机契约与重试预算封顶 (Single-Owner State Machine)
* **严格流转**：所有状态跃迁受 `TRANSITIONS` 字典白名单约束，非法跃迁直接抛出 `IllegalTransitionError`。
* **预算原子控制**：每次进入 `FETCHING` 时自增 `attempt_count`；当进入 `RETRYABLE_FAILED` 达到 5 次上限时，StateStore 在单一事务内原子将其终结为 `FINAL_FAILED`，杜绝无限重试与多处决策冲突。

### 1.7 受控切片运行与安全退出码 (Safe CLI & Exit Code Semantics)
* **防呆参数**：`--max-notes` 设为必填，要求显式传入切片数字 `N` 或全量指令 `'all'`，杜绝因遗漏参数引发的全量无控抓取。
* **高可信退出码**：
  * `0`：仅当枚举明确证实完成（`enum_proven == True`）且数据库中 100% 笔记均为 `COMPLETE`。
  * `2`：会话级致命错误（AUTH_REQUIRED / RISK_CONTROLLED / RATE_LIMITED）或运行中止。
  * `3`：切片运行或有未完成笔记（实测 3 次切片运行全部正确返回 exit 3，未发生将切片误判为全量完成的情况）。

### 1.8 浏览器资源泄漏防护
* **标签页生命周期隔离**：`CollectorSession` 内的 `iter_favorites()` 与 `fetch_note()` 均配备 `finally: page.close()`，保证单次会话无论成功或异常均释放 Page 实例，支撑连续爬取。

---

## 2. 未验证能力 (Unverified Capabilities)

受限于受控切片测试策略及外部真实数据特征，以下能力在单元测试中已有覆盖，但**尚未在真实大流量网络环境下触发完整验证**：

1. **单次 1000+ 笔记无间断全量同步**：
   * 目前累计已成功同步 66 篇真实笔记，剩余 72 篇处于 `PENDING`。尚未执行一次性 `--max-notes all` 直至全部 138+ 篇抓取完毕。
2. **真实服务端 End-of-Stream 触底信号捕获**：
   * 因每次测试均使用 `--max-notes 10` 切片，滚动在达到第 10 篇后主动截断，尚未触发收藏夹末页的 `has_more == False` 或 DOM 纯空状态，真实全量达到 exit 0 的全链路未在真实生产网络中终验。
3. **真实场景重试预算耗尽终结 (`FINAL_FAILED`)**：
   * 单元测试已覆盖 5 次失败封顶逻辑；在真实网络中单篇笔记在第 2 次尝试时即重试成功，未发生真实触发 5 次超额转为 `FINAL_FAILED` 的情况。
4. **远端作者删帖/已取消收藏的增量感知**：
   * 单元测试覆盖了 `CONTENT_UNAVAILABLE -> FINAL_FAILED` 逻辑；但当前账号的 138 篇收藏笔记均为正常在线状态，未在真实网络触发删帖分支。
5. **超大媒体文件 (>500MB) 下载超时与断点恢复**：
   * 当前采集的视频文件最大为 47MB，更大体积的多媒体资源未触发。

---

## 3. 已知限制 (Known Limitations)

1. **单进程模型假设 (Single-Process Assumption)**：
   * 当前 StateStore 依赖 SQLite WAL 短事务保障 ACID，但未实现进程级文件互斥锁（如 `flock` / `fcntl`）。若操作者在同一机器上并发启动两个 `xhs sync` 进程，可能导致浏览器 Profile 锁竞争或状态更新交错。
2. **连续滚动停滞判断阈值 (Stall Threshold)**：
   * `iter_favorites` 目前设定为连续 5 次滚动无新网络响应即视为停滞并跳出。在弱网或小红书接口高延迟场景下，可能会提前判定停滞而中断枚举（系统会安全返回 exit 3，不丢失数据，但需操作者再次运行）。
3. **串行执行吞吐性能**：
   * 为最大程度降低风控风险，当前架构采用“单标签页导航 + 顺序抓取 + 单文件串行流式下载”。单篇笔记平均耗时 10~25 秒；百篇笔记约需 20~30 分钟，千篇全量预计耗时 3~5 小时。
4. **Web 端 Session 依赖性**：
   * 系统完全依赖人工通过 `xhs login` 写入 `.xhs-profile/` 的真实会话。若小红书服务端撤销 Session 或弹出图形滑块验证码，系统将报出 `AUTH_REQUIRED` / `RISK_CONTROLLED` 并以 exit 2 中止，需要人工重新介入。

---

## 4. P1 完成定义与评估 (Definition of Done Evaluation)

根据 `P1_INCREMENTAL_SYNC_PLAN.md` 与评审规范，对 P1 阶段目标进行逐项对账：

| P1 核心要求 | 状态 | 实测证据 |
|---|---|---|
| **SQLite 增量状态存储** | **已达标** | `sync.db` Schema v2 稳定支撑 138 篇笔记状态跟踪，支持中断恢复与队列分离 |
| **细粒度状态机 (Strict Transitions)** | **已达标** | 9 种状态白名单流转，双队列（Fetch / Media Resume）解耦，重试预算原子封顶 |
| **断点续传与幂等性** | **已达标** | Step 2 实测证实已完成笔记零冗余请求；异常中断记录自动复位继续推进 |
| **COMPLETE 产物守卫** | **已达标** | Step 4 证实磁盘产物损毁时准确降级，并在后续运行中全自动修复闭环 |
| **媒体核验与 Skip-if-Verified** | **已达标** | 逐文件记录尺寸与哈希，已下载文件不重复下载，未下载文件补全后才封签 |
| **退出码一致性与 CLI 防呆** | **已达标** | `--max-notes` 必填；未证实全量时绝对不返回 0；切片留存统一返回 3 |
| **工程质量与回归保护** | **已达标** | 90 个全量测试通过；代码遵循单一职责；无未管理外部依赖 |

**结论**：P1 阶段的核心目标“增量同步引擎与高可靠状态机”已全部达成，核心闭环经受住了真实环境与故障注入检验，**P1 阶段正式达到完成标准 (DONE)**。

---

## 5. P2 Backlog (后续演进建议)

在维持现有架构稳定、不引入大型工作流引擎与消息中间件的前提下，建议 P2 阶段聚焦以下维护与强化项：

1. **全量触底验证 (`--max-notes all`)**：
   * 执行一次完整全量同步，处理完剩余 72 篇笔记，实测验证真实网络中捕获 `has_more == False` 并最终输出 exit code `0` 的完整流程。
2. **单实例运行文件锁 (Runtime Single-Instance Lock)**：
   * 在 `run_sync` 启动入口增加基于 `.xhs-state/sync.lock` 的 `flock` / 独占排他锁，若已有实例在运行则直接退出并给出友好提示，防止操作失误。
3. **媒体并发下载加速 (Per-Note Intra-Media Concurrency)**：
   * 保持笔记级串行流转以防风控，但在单篇笔记内部（如包含 10~15 张图片时），使用线程池或异步下载器并发拉取静态图片字节流，可显著缩短单篇笔记的阻塞时间。
4. **风控异常的用户友好提示与自动挂起**：
   * 当捕获到 `RISK_CONTROLLED` 异常且检测到图形验证码时，支持短暂挂起并提示用户在 headed 模式下完成滑块，成功后自动恢复同步流程。
5. **远端状态同步 (Unfavorite / Remote Deletion Tracking)**：
   * 在全量枚举完成后，比对本地已记录但远端未出现的笔记，引入软删除或 `UNFAVORITED` 标记，反映远端取消收藏状态。
