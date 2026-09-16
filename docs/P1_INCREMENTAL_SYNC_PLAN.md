# P1 Incremental Sync 设计方案

设计日期：2026-09-16
状态：待 review（本阶段不修改任何代码）

输入依据：

- `docs/ACQUISITION_AUDIT.md`（acquisition contract、failure taxonomy、外部项目 fail-open 教训）
- `docs/P0_SMOKE_TEST.md`（真实环境 Vertical Slice 证据）
- `docs/P0_TO_P1_REVIEW.md`（Conditional Go 结论与 7 项最小必要改动）
- `src/xhs_ingest/` 当前实现与 `tests/`

---

## 1. Goal

P1 不是完整产品。P1 只解决一件事：**把已验证的单篇采集闭环，扩展为可中断、可恢复、可重试、可重复执行的批量同步**。

具体验收形态：

```text
xhs-ingest sync
  ↓
favorites 全量枚举（服务端 has_more=false 为完成证据）
  ↓
持久化 item 状态（SQLite，按 note_id）
  ↓
逐篇 ingest（fetch → normalize → raw 落盘 → media → canonical/post.md → 完成标记）
  ↓
任意时刻中断（崩溃 / 断网 / token 失效 / 风控）
  ↓
再次 xhs-ingest sync
  ↓
不重新扫描已完成内容，不重复下载，只补缺口
```

成功标准（对齐 P0_TO_P1_REVIEW §5 的前四项 + 第 7 项）：

1. 1000+ 收藏可分多次运行完成，每次运行从持久状态继续；
2. 已 COMPLETE 的 note 第二次运行零网络请求、零文件重写；
3. MEDIA_PARTIAL 的 note 只重试缺失/校验不符的媒体文件；
4. 任何失败都不会被误标为完成（fail-closed 贯穿）；
5. 单元测试覆盖多页分页、崩溃恢复、幂等重跑、部分媒体补齐、限流中断不误标完成。

---

## 2. Current State Analysis

### 2.1 当前数据流与 contract 盘点

```text
XhsPlaywrightCollector.list_favorites(limit=20)          [collector.py:246]
        ↓
PageResult(items, next_cursor≡None, has_more≈推断, completion_proof, raw)
        ↓
FavoriteRef(note_id, source_url, xsec_token, title, author, cover, raw)
        ↓  （token 同时写 .xhs-profile/tokens_cache.json）
XhsPlaywrightCollector.fetch_note(ref)                    [collector.py:442]
        ↓  （每次调用新起一个浏览器 context）
sanitized raw detail dict
        ↓
normalize_note(raw) → CanonicalPost                       [normalizer.py:99]
        ↓
download_post_media(post, assets_dir)                     [media.py:100]
        ↓
MediaItem: PENDING → COMPLETED | FAILED（内存态）
        ↓
data/<note_id>/{assets/, raw.json, canonical.json, post.md}
```

逐段评估：

| 环节 | 已有 contract | P1 需要扩展 |
|---|---|---|
| favorites → PageResult | `PageResult` 字段齐全；按 `note_id` 进程内去重；空列表 fail-closed（显式空态才返回） | 捕获服务端 `cursor/has_more`；滚动终止条件改为服务端证据；`FOUND_N_ITEMS` 伪证明废除 |
| PageResult → FavoriteRef | `note_id` 稳定主键 + `xsec_token` + 可复用 `source_url`，足以驱动 detail；token 落盘缓存 | 枚举结果需要 upsert 进持久状态，而不是只留在内存 |
| FavoriteRef → fetch_note | 404/300031→`TOKEN_INVALID`、删除→`CONTENT_UNAVAILABLE`、解析不出→`PARSE_FAILED`、验证码→`RISK_CONTROLLED`、频控→`RATE_LIMITED` | Playwright 超时/导航异常未映射为 `NETWORK_ERROR`；token 读写异常被静默吞掉；每次调用新起浏览器，1000 篇不可行 |
| raw → CanonicalPost | 缺 note_id 时 fail-closed `PARSE_FAILED`；raw 完整保留；normalizer 是纯函数（可从 raw.json 重建 media 清单——P1 resume 的关键前提） | 无（P1 不扩 canonical schema） |
| CanonicalPost → media | 单文件 `.tmp` + 非零校验 + SHA256 + 原子 rename；单文件失败不阻断其余 | 不检查已存在文件（重跑全量重下）；无单文件 retry/backoff；状态只在内存 |
| media → filesystem | 媒体失败时 raw/canonical/post.md 仍落盘，exit code 3 | 三个 metadata 文件非原子写；无 note 级完成标记；写入顺序（媒体先于 raw.json）不利于崩溃恢复 |

### 2.2 当前完全没有的东西

- 任何跨进程的 note 级状态（P0_TO_P1_REVIEW §2 的结论：目前是"异常分类模型 + 媒体内存状态"）；
- `sync` 命令与批次 runner；
- retry policy（taxonomy 有了，策略没有）；
- enumeration 完成证据（`next_cursor` 从未赋值，`collector.py:431-436` 的 `has_more` 只是 `len >= limit` 推断）。

---

## 3. Proposed State Model

### 3.1 存储介质比较

| 方案 | 实现成本 | 崩溃恢复 | 幂等能力 | 结论 |
|---|---|---|---|---|
| **SQLite（stdlib `sqlite3`）** | 低-中：建表 + 少量 CRUD（约 150 行），无新依赖 | 强：每个状态转移一条事务，进程任意时刻崩溃库内一致（WAL） | 强：`note_id PRIMARY KEY` 天然 upsert；retry 队列是一个查询 | **采用** |
| JSONL append-only 日志 | 低：append 容易 | 中：半行写入需容错；重放全量重建内存态 | 中：last-write-wins 重放；"取所有可重试且 attempts<3"需全量扫描；需要 compaction | 否：重试/退避路径上正确性代码反而更多 |
| 每 note 一个状态文件（`data/<id>/status.json`） | 低 | 中：单文件原子 rename 可用 | 中：跨 note 查询（进度汇总、retry 队列）要扫目录；"已发现但从未采集"的 note 无处安家（会为失败项制造空数据目录）；仍需一个全局文件存枚举元数据 → 两套机制 | 否 |
| 单个 JSON 状态文件 | 最低 | 中：tmp+rename 可原子，但每次转移全量重写，且所有状态押在一个文件上 | 中：全量载入内存后可行；规模增长后重写churn 与损坏半径大 | 否 |

选择 **SQLite** 的核心理由：P0_TO_P1_REVIEW §5.3 要求"每完成一个 note（或一个 page）立即提交状态"，这正是一条 `UPDATE ... WHERE note_id=?` 事务；retry 队列、attempt 计数、状态过滤都是 SQL 一行的事。1000 note × ~20 媒体的 media_state 规模对 SQLite 可忽略。审计文档也明确"不要求 SQLite，但状态语义必须可落盘和恢复"——这里落盘介质选 SQLite 是成本最低且崩溃语义最强的一种。

位置：默认 `.xhs-state/sync.db`（与 `.xhs-profile/`、`data/` 并列，加入 `.gitignore`），CLI 参数 `--state-db` 可覆盖。

### 3.2 Schema

```sql
CREATE TABLE notes (
  note_id                TEXT PRIMARY KEY,
  status                 TEXT NOT NULL,        -- 见 3.3
  xsec_token             TEXT,                 -- 枚举时的 ref 快照，供不重新枚举的 detail 重试用
  source_url             TEXT,
  title                  TEXT,                 -- 人类可读报告用
  attempt_count          INTEGER NOT NULL DEFAULT 0,
  last_error_status      TEXT,                 -- AcquisitionStatus 值
  last_error_message     TEXT,
  media_state            TEXT,                 -- JSON: [{filename, url, status, size_bytes, checksum_sha256}, ...]
  first_seen_at          TEXT NOT NULL,        -- created_at
  last_seen_in_listing_at TEXT,                -- 每次枚举刷新；P1 无行为，仅诊断
  updated_at             TEXT NOT NULL
);

CREATE TABLE sync_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
-- keys: schema_version, last_enumeration_status, last_enumeration_proof,
--       last_enumeration_cursor, last_run_at, last_run_exit_code
```

字段覆盖任务要求的 `note_id / status / attempt_count / last_error / updated_at / created_at`，另加 `media_state`（支持"只重试失败媒体"而不引入媒体表）和 ref 快照（支持 detail 级重试不依赖重新枚举）。

### 3.3 Status 语义（与任务给定列表对齐）

| Status | 含义 | 进入时机 | 离开时机 |
|---|---|---|---|
| `PENDING` | 已发现，未开始（= review 文档的 DISCOVERED） | 枚举 upsert 新 note；启动时 `FETCHING` 复位 | 开始 fetch |
| `FETCHING` | detail 抓取进行中。**只作为崩溃痕迹存在，永不是合法静止态** | fetch 开始（attempt_count +1，同一事务） | 成功→`DETAIL_SUCCESS`；失败→`RETRYABLE_FAILED`/`FINAL_FAILED`；崩溃→下次启动复位为 `PENDING` |
| `DETAIL_SUCCESS` | detail 已获取且 `raw.json` 已原子落盘（= DETAIL_OK）。媒体未开始或进行中 | raw.json 写入成功后 | 媒体全部完成→`COMPLETE`；部分失败→`MEDIA_PARTIAL` |
| `MEDIA_PARTIAL` | detail 完好，部分媒体 FAILED/未完成 | 媒体阶段结束后有失败项 | 下次运行补齐→`COMPLETE`；URL 过期→`RETRYABLE_FAILED` |
| `COMPLETE` | 全部产物 + 全部媒体校验通过 + `.complete` 标记已写 | `.complete` 标记写入后 | 永不（除非标记文件丢失被降级，见 §6） |
| `RETRYABLE_FAILED` | 失败但属于可重试错误类，且 attempt 未耗尽 | 可重试错误 | 下次 sync 重新入队 |
| `FINAL_FAILED` | attempt 耗尽，或 terminal 错误类（如 `CONTENT_UNAVAILABLE`） | terminal 错误 / 超限 | 仅 `--retry-failed` 人工重置 |

与 review 文档命名的映射：`DISCOVERED→PENDING`、`DETAIL_OK→DETAIL_SUCCESS`、`COMPLETED→COMPLETE`、`PERMANENT_FAILED→FINAL_FAILED`，另增 `FETCHING` 作为崩溃可观测痕迹。

`attempt_count` 语义：fetch 开始次数（与 `PENDING→FETCHING` 同一事务自增）。崩溃在 fetch 中也消耗一次 attempt——这是有意的，防止崩溃循环无限重试；max attempts 默认 3。

### 3.4 不做的事

- 不建媒体独立表（`media_state` JSON 够用，规模 ≤ 几十条/note）；
- 不存 raw payload 进库（raw.json 是它的家）；
- 不做多账号、多清单（boards）维度——单 favorites 流。

---

## 4. Checkpoint Design

### 4.1 原则

- **note 级即时提交**：每次状态转移、每个媒体文件完成，都是独立事务，写穿到盘。任何时刻 kill -9，库内都是一致且最新的。
- **枚举级批量提交**：每拦截到一个 collect/page 响应，就把该批 refs upsert 进库（一个事务）。枚举中途崩溃，已发现的 note 一个不丢。
- **state DB 是唯一权威**；`data/<note_id>/.complete` 标记文件是给人/外部工具看的冗余信号，两者互相校验（见 §6 降级守卫）。

### 4.2 保存什么

| 层级 | 内容 | 时机 |
|---|---|---|
| note 状态转移 | status, attempt_count, last_error_*, updated_at | 每次转移 |
| note 媒体进度 | media_state 中该文件的 status/size/sha256 | 每个文件原子落盘后 |
| 枚举进度 | 已发现的全部 refs（PENDING upsert）+ token/source_url 刷新 | 每个拦截页 |
| 枚举结论 | `sync_meta.last_enumeration_status/proof/cursor` | 枚举结束时（含失败结论） |
| 运行结论 | `sync_meta.last_run_at / last_run_exit_code` | sync 退出前 |

### 4.3 中断场景与恢复行为

| 场景 | 崩溃时状态 | 重新启动后的行为 |
|---|---|---|
| 枚举中途崩溃 | 部分 refs 已 upsert；`last_enumeration_status` 为空/旧 | **重新枚举**（从页面顶部重新滚动）。upsert 幂等，已发现的 note 不受影响。不重新抓任何 detail。 |
| fetch 中崩溃 | 该 note 停在 `FETCHING` | 启动时全部 `FETCHING → PENDING`（该次 attempt 已计数），重新入队 |
| raw 落盘后、媒体中崩溃 | `DETAIL_SUCCESS`，media_state 部分完成 | 读 `data/<id>/raw.json` 重新 normalize（纯函数），按 media_state + 磁盘校验跳过已完成文件，只补缺失项。**不再调 fetch_note** |
| 全文件写完、标记前崩溃 | `DETAIL_SUCCESS` + media_state 全 COMPLETED | 快速路径：补写 canonical/post.md 与 `.complete`，零网络 |
| 断网 | note 级 `NETWORK_ERROR` → `RETRYABLE_FAILED`（先经 2 次短退避 in-run 重试） | 其余 note 继续（媒体/详情互相独立）；失败的下次运行重试 |
| token 失效（单 note） | `TOKEN_INVALID` → `RETRYABLE_FAILED` | 不在本次运行内特殊处理；**下次 sync 开头的全量枚举天然刷新 token**（listing 每项带新 xsec_token，upsert 覆盖） |
| session 失效（全局） | `AUTH_REQUIRED` → 运行中止 | 已完成状态全部保留；用户 `xhs-ingest login` 后重新 sync |
| 风控/验证码 | `RISK_CONTROLLED` → 运行中止 | 同上；等待冷却后重跑 |

### 4.4 为什么枚举不保存 cursor 断点

诚实说明：当前枚举靠浏览器页面滚动驱动，前端在内存里维护自己的 cursor，我们**无法注入** cursor 让页面从中间继续；直接带 cursor 调 API 需要签名请求（tamnd 式 x-s 签名），P1 明确不做。因此枚举断点 = 重滚一遍。

这是可接受的取舍：枚举只拿轻量列表数据（1000 条约几十次滚动、几分钟），真正的成本在 1000 次 detail + 媒体下载，而那部分由 note 级状态完全保护。服务端返回的 `cursor` 仍会**捕获并记录**到 `PageResult.next_cursor` 和 `sync_meta`（诊断 + 为未来直连 API 留证据），但不作为恢复输入。

---

## 5. Pagination Design

### 5.1 现状（代码证据，非假设）

- 响应拦截器 `collector.py:260-272` 只取 `data.notes`，**响应中的 cursor 与 has_more 字段被丢弃**，未捕获未保存；
- 滚动循环 `collector.py:363-367`：硬上限 6 次滚动，`len(intercepted) >= limit` 即停；
- `collector.py:433`：`has_more = len(items_by_id) >= limit`，是推断不是服务端证据；恰好 20 条会被标"还有更多"，加载慢导致 19 条会被误判"结束"；
- `completion_proof="FOUND_N_ITEMS"` 只证明发现了 N 条；
- `Collector` protocol（`collector.py:34`）没有 cursor/checkpoint 输入参数。

结论：**当前不支持 cursor、不支持 page token、没有可靠的 end condition。**

### 5.2 P1 分页 contract

修改 `list_favorites`（语义扩展，非新方法）：

```python
def list_favorites(self, limit: int | None = None) -> PageResult:
    """limit=None 表示全量枚举：滚动直到服务端终止证据或 fail-closed 中止。"""
```

返回值规则：

1. **next_cursor**：取最后一个 collect/page 响应中服务端返回的 cursor。字段名以实测 payload 为准（候选 `data.cursor`；拦截时把整个 `data` 对象存入 `PageResult.raw.pages` 留证）。捕获不到 → `None`。
2. **has_more**：只接受服务端显式值；无法验证 → `None`。**`None` 永远不等于 False**。
3. **completion_proof** 收敛为三个合法值：
   - `SERVER_HAS_MORE_FALSE`（含末页 cursor）——枚举完成的唯一正证明；
   - `EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE`——沿用现有空态验证；
   - `None`——未完成/不可证明。
   `FOUND_N_ITEMS` 废除。
4. **终止条件**（滚动循环）：
   - 服务端任一响应确认 `has_more == false` → 正常结束；
   - 滚动停滞检测：连续 5 次滚动无新增拦截条目 **且** 无服务端终止证据 → 抛 `UnknownAcquisitionError`（**中止而非完成**——这正是审计对 ONEMULE "3 次长度不变即完成" 的反面教训）；
   - 移除 6 次硬上限，改为 `--max-items` 安全上限（默认不设，保护性可选）。
5. **节流**：滚动间随机 sleep 1.5–3s（P0 smoke 已实测高频翻页有滑块风险），比现有固定 2s 更保守。
6. 拦截到 `RISK_CONTROLLED` / `RATE_LIMITED` / `AUTH_REQUIRED` 立刻按 §7 语义中止，已 upsert 的发现结果保留。

### 5.3 关于 cursor 输入

Protocol 不增加 cursor 入参。恢复策略 = 重新枚举（§4.4 已论证）。把接口做成"接受 cursor 但实际不用"是撒谎式 API，不做。

---

## 6. Idempotency Rules

`xhs-ingest sync` 重复执行的行为矩阵（核心承诺：**重复运行收敛到同一状态，无重复网络工作，无重复下载**）：

| 对象状态 | 第二次 sync 的行为 |
|---|---|
| `COMPLETE` note | 跳过：不 fetch、不读 raw、不动文件。仅做一次廉价的 `.complete` 标记存在性检查 |
| `COMPLETE` 但 `.complete`/`canonical.json` 丢失（用户删了 data/） | 降级守卫：`COMPLETE → PENDING`，重新走完整采集。state 不允许对磁盘说谎 |
| `DETAIL_SUCCESS` / `MEDIA_PARTIAL` | 不调 fetch_note。读 `raw.json` → normalize 重建 media 清单 → 逐文件：磁盘存在且 size+sha256 与 media_state 记录一致 → 跳过；否则重新下载（原子覆盖）→ 重写 canonical.json / post.md → 全齐则 COMPLETE |
| `PENDING` / `RETRYABLE_FAILED`（attempts < 3） | 正常入队处理 |
| `FINAL_FAILED` | 默认跳过；`--retry-failed` 显式重置为 PENDING（attempt 清零）后重新入队 |
| 磁盘上已有媒体文件但 state 无记录（如手工恢复的孤儿） | 不信任磁盘孤证：按缺失处理，重新下载并用结果刷新 media_state |
| 残留 `.tmp` 文件 | 忽略并在重下载时被原子替换覆盖；不作为任何完成依据 |
| 已存在的 `canonical.json` / `post.md` | 在该 note 本次重新提交时原子重写（tmp + rename）；`raw.json` 一旦存在且 `DETAIL_SUCCESS` 则不重写 |
| 收藏列表里新增的 note | 枚举 upsert 为新 `PENDING`，正常采集（这就是增量发现机制） |
| 收藏列表里消失的 note（用户取消收藏） | 不删除、不改状态（`last_seen_in_listing_at` 留痕）。GC 是 P2 议题 |

全部 `COMPLETE` 时运行 sync：执行一次全量枚举（发现增量）→ 队列为空 → 打印汇总退出 0。这是幂等性的最终检验。

metadata 原子性配套（消除 P0 review §1.6 缺口）：`raw.json / canonical.json / post.md / .complete` 全部改为写 `.<name>.tmp` + `os.replace`，与媒体文件同一模式；`.complete` 永远是目录里最后一次写入。

---

## 7. Failure Handling

### 7.1 taxonomy → 重试策略矩阵

| Status | 自动重试 | 运行内行为 | 跨运行行为 | 用户动作 |
|---|---|---|---|---|
| `NETWORK_ERROR` | 是（≤2 次，5s/15s 退避） | note → `RETRYABLE_FAILED`，继续队列其余 note | 下次 sync 重试（attempt 计数） | 无需 |
| `MEDIA_DOWNLOAD_FAILED` | 是（单文件 ≤3 次，5s 退避） | 其余文件继续；note → `MEDIA_PARTIAL` | 下次只补失败文件 | 无需 |
| `RATE_LIMITED` | 是（运行级） | 暂停 60–120s（带抖动）后重试当前操作；单运行内 ≤2 次暂停，超出→中止运行 | 下次 sync 继续 | 冷却后重跑 |
| `TOKEN_INVALID` | 否（运行内） | note → `RETRYABLE_FAILED`，继续 | **下次 sync 的枚举自动刷新 token** 后重试 | 无需（持续失效则见下行） |
| `AUTH_REQUIRED` | 否 | **立即中止运行**（session 全局失效，继续只会全军覆没）；已完成状态保留 | `xhs-ingest login` 后重跑 | 扫码重新登录 |
| `RISK_CONTROLLED` | 否 | **立即中止运行**（继续会升级风控） | 冷却后重跑 | 可能需浏览器内过验证码 |
| `CONTENT_UNAVAILABLE` | 否 | note → `FINAL_FAILED`（terminal），继续队列 | 不重试 | 无 |
| `PARSE_FAILED` | 是（低上限） | note → `RETRYABLE_FAILED`（SSR/API 载荷在两次加载间确实可能不同），attempt 计数照常 | 重试至耗尽 → `FINAL_FAILED` | 若持续出现 = 站点 schema 漂移，需人工看 raw |
| `UNKNOWN_*` | 是（防御性） | 同 `RETRYABLE_FAILED`，**永不推进完成** | 同上 | 关注日志 |

补充规则（媒体 URL 时效）：MEDIA_PARTIAL 恢复时，若媒体下载命中 HTTP 403（CDN 签名过期的典型表现），不做无意义重试，note → `RETRYABLE_FAILED`（reason=URL_EXPIRED），下次运行**重新走 detail 获取新 URL**。这覆盖 P0 smoke 指出的"签名 URL 带 TTL"风险，且不需要引入 URL 年龄跟踪。

### 7.2 运行级 vs note 级

- **中止运行**（停发新工作，在途 note 记录结果后退出）：`AUTH_REQUIRED`、`RISK_CONTROLLED`、`RATE_LIMITED` 耗尽暂停次数。
- **note 级失败**（其余继续）：其余所有错误类。
- 任何错误都先落库再继续/退出——失败原因不允许只存在于 stderr。

### 7.3 配套错误映射修复（P1 顺手补齐，不是新功能）

- Playwright 导航超时 / 连接异常 → 包装为 `NetworkAcquisitionError`（当前会落到 CLI 的 `UNKNOWN` 分支）；
- `tokens_cache.json` 读写异常：记录 warning 日志，不再静默 `except: pass`。

### 7.4 sync 退出码（沿用现有语义，脚本可依赖）

| code | 含义 |
|---|---|
| 0 | 队列处理完毕且全部 COMPLETE（含"无事可做"） |
| 2 | 运行中止（AUTH/RISK/RATE 耗尽/枚举停滞 fail-closed），状态已保存，可恢复 |
| 3 | 运行走完但仍有 `MEDIA_PARTIAL` / `RETRYABLE_FAILED` / `FINAL_FAILED` |
| 4 | 未预期异常 |

---

## 8. Minimal File Changes

| 文件 | 变更 | 要点 |
|---|---|---|
| `src/xhs_ingest/state.py` | **新增**（~150 行） | SQLite 状态库：建表、`upsert_refs`（页批量）、`transition`（原子状态转移 + attempt 计数）、`record_media`（单文件提交）、`pending_queue()`、`reset_fetching()`、sync_meta 读写。WAL 模式 |
| `src/xhs_ingest/sync.py` | **新增**（~200 行） | sync runner：枚举 → upsert → 建队列 → 逐 note 管道（含 DETAIL_SUCCESS 快速路径）→ 运行级中止判断 → 汇总与退出码。重试/退避常量集中此处 |
| `src/xhs_ingest/collector.py` | 修改 | ① 拦截器捕获每响应完整 `data`（cursor/has_more，存 `PageResult.raw`）；② 滚动循环改为服务端终止条件 + 停滞 fail-closed + 移除 6 次上限 + 节流抖动；③ completion_proof 收敛；④ **运行级浏览器 context 复用**（当前 `fetch_note` 每次新起浏览器，1000 篇不可行；sync 期间单 context，退出时关闭）；⑤ Playwright 超时→`NETWORK_ERROR`；⑥ token cache 异常日志化 |
| `src/xhs_ingest/cli.py` | 修改 | 新增 `sync` 子命令（flags：`--max-items`、`--retry-failed`、`--state-db`）；`ingest_note` 管道重排为 raw.json 先行 + 全部 metadata 原子写 + `.complete` 标记（`ingest` 单篇命令同步受益） |
| `src/xhs_ingest/media.py` | 修改 | `download_media_item` 增加 skip-if-verified（存在 + size + sha256 匹配传入的期望值）与单文件 retry/backoff |
| `src/xhs_ingest/models.py` | 基本不动 | `FavoriteRef/PageResult` 字段已够用（这是 P0 留下的好底子） |
| `tests/test_state.py` | **新增** | upsert 幂等、转移原子性、FETCHING 复位、retry 队列过滤 |
| `tests/test_sync.py` | **新增**（fake collector，无网络） | 多页 cursor 枚举；崩溃于每个状态后 resume 到正确断点；全 COMPLETE 二次运行零调用；MEDIA_PARTIAL 只重下失败文件；RATE 中止后状态完好且未被误标完成 |
| `tests/test_media.py` | 扩展 | skip-verified、checksum 不符触发重下 |
| `.gitignore` | 修改 | 增加 `.xhs-state/` |

明确不做：不引入 scheduler/daemon、不引入配置文件系统、不做并发下载（顺序下载保持，1000 篇耗时靠可恢复性而不是速度解决）。

---

## 9. Non-goals

P1 明确不做（防止范围蔓延）：

- **内容理解**：OCR、ASR、Vision、embedding、RAG、知识图谱；
- **消费端**：Obsidian plugin、任何 UI、搜索；
- **账号与范围**：多账号、收藏夹分类（boards/专辑）、评论采集、用户主页、搜索；
- **传输优化**：Range 断点续传、并发下载、直连签名 API（x-s 签名逆向）；
- **智能调度**：定时任务、daemon、自动风控规避（节流只是礼貌性退避，不是规避）；
- **数据治理**：取消收藏的 GC、内容去重、canonical schema 扩展（tags/时间等留给 P2）、状态可视化。

审计文档 D 节的原则继续有效：collector 不负责 RAG/UI/编排；P1 只补"批量可恢复采集"这一层。

---

## 10. Risks

| 风险 | 影响 | 缓解 |
|---|---|---|
| **滚动停滞 ≠ 自然终止**：加载失败/风控可能表现为"滚不动" | 枚举被误判完成 → 永久漏采 | 停滞走 fail-closed 中止（exit 2），绝不标完成；服务端 `has_more=false` 是唯一正证明 |
| **`data.cursor`/`has_more` 字段名未实测确认** | 终止条件永远验证不到 → 每次枚举都中止 | fail-closed 方向安全（宁可不完成，不假装完成）；P1 实现首日先用真实 session 抓 2–3 页确认字段名（P0 已证明该环境可拦截此端点） |
| **长枚举触发滑块风控**（P0 smoke 已预警） | 枚举需多次运行才能完成 | 节流 + 抖动；RISK_CONTROLLED 即中止保留进度；可恢复性保证"多跑几次凑齐全量"是合法路径 |
| **媒体签名 URL 过期**（TTL） | MEDIA_PARTIAL 跨运行补采时 403 | §7.1 规则：403 → 重新走 detail 换新 URL，不浪费重试 |
| **SQLite 文件损坏/误删** | 状态全丢 | WAL + 单文件易备份；`data/*/.complete` 标记可人工重建 COMPLETE 集（恢复路径写入文档）；最坏情况 = 重新采集，不会产生错误数据 |
| **state 与磁盘漂移**（用户手动删改 data/） | 状态谎言 → 漏采或错跳 | §6 降级守卫：COMPLETE 依赖标记文件存在性校验；磁盘孤儿文件不作为完成依据 |
| **fetch 中崩溃消耗 attempt** | 反复崩溃可能把好 note 推到 FINAL_FAILED | 有意取舍（防崩溃循环）；`--retry-failed` 提供人工重置出口 |
| **站点 schema/selector 漂移** | PARSE_FAILED / UnknownAcquisitionError 增多 | 现有 fail-closed 语义全部保留；错误永不伪装成功；raw payload 留证便于诊断 |
| **1000 篇顺序下载耗时数小时** | 单次运行时间长 | 可恢复性把"一次跑完"问题转化为"多次跑完"；性能优化（并发等）明确推迟 |

---

## 设计自检（对照三条硬约束）

1. **可恢复**：每个状态转移、每个媒体文件、每个枚举页都是独立事务；七种中断场景（§4.3）均有确定恢复路径，无一需要"从头再来"。
2. **不丢数据**：detail 一到手立即原子落 raw.json（管道重排）；枚举发现即 upsert；失败原因先落库再继续。
3. **不误判成功**：`has_more=None ≠ False`；`FOUND_N_ITEMS` 废除；停滞中止而非完成；COMPLETE 需文件校验 + 标记双重证据；未知错误永不推进 checkpoint。
