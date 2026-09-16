# Acquisition Contract v2

设计日期：2026-09-16
状态：设计文档，未修改任何代码
角色：Staff Engineer 契约收口设计

依据：

- `docs/architecture/ACQUISITION_ARCHITECTURE_V1.md`（冻结的组件职责与 R1–R13 风险表）
- `docs/PRE_SYNC_RUNNER_IMPLEMENTATION_REVIEW.md`（阻塞项清单；本文档直接回应其 §2 全部发现）
- `docs/SYNC_RUNNER_DESIGN.md`（D-A~D-D 依赖项的正式化）
- `src/xhs_ingest/collector.py`、`models.py`、`errors.py`、`normalizer.py` 当前实现
- `docs/P1_STATE_CONTRACT_V2.md`（v2.1 语义，经 Codex review 修正）

约定：全文区分 **Current Reality**（代码今天是什么样，含文件行号）与 **Target Contract**（本契约规定的行为）。不假设不存在的代码——`parser.py` 今天不存在，`collector.session()` 今天不存在，`iter_favorites()` 今天不存在。

---

# Verdict

**SyncRunner 不能在当前 collector 接口上实现——本契约是其唯一前置。** Pre-implementation review 的判断成立：State 主干（61 tests）可直接复用，但采集层缺四块契约能力：

1. **Session 生命周期**：每次调用新起 browser context（R5），无运行级复用；
2. **枚举契约**：`list_favorites` 是聚合返回而非逐服务端页，cursor/has_more 被丢弃（R1），完成证明不可信；
3. **Detail 契约**：`fetch_note` 会隐式重枚举收藏夹并吞掉其异常（collector.py:472-482），且成功结果不保证是请求的 note（SSR/feed fallback 取任意条目，collector.py:530-559）；
4. **错误边界**：Playwright 传输异常未被映射为 `NetworkAcquisitionError`（D-B），response/token cache 错误被静默吞掉（D-D、review §2.6）。

本契约将这四块定义为一组最小接口改动：**一个 session 对象、一个生成器、一个严格的 fetch、一组纯函数 parser**。不新增技术、不做并发、不逆向 API。异常 taxonomy 复用 `errors.py` 现有九类（名称保留，避免 61 个测试与 state `error_status` 字符串的无谓迁移）。State 层不在本契约范围内（其缺口由 review §6 Gate 0 处理），但 §StateRunner Interaction 固定两层交接面。

---

# Proposed Contract

## 1. Session 生命周期

**Current Reality**：`XhsPlaywrightCollector` 的 `list_favorites`（collector.py:246）与 `fetch_note`（collector.py:442）各自调用 `_launch_context()`（collector.py:87）并在 `finally` 关闭——枚举与 detail 生命周期完全分离，1000 次 fetch = 1000 次浏览器启停。`interactive_login`/`check_auth` 另起 context（合法，保留）。

**Target Contract**：

```text
SyncRunner（唯一编排者）
    │  with collector.session() as s:          ← 谁创建：runner；何时：进入采集前
    │
    ├── s.iter_favorites(limit=None)           ← 枚举（Page 轮换，context 不轮换）
    │       └── 逐页 yield → runner 逐页 checkpoint
    │
    ├── s.fetch_note(ref) × N                  ← detail，复用同一 context
    │
    └── 退出 with                               ← 谁关闭：__exit__（结构性唯一 owner）
```

规则（全部为 MUST）：

| # | 规则 |
|---|---|
| S-1 | `session()` 返回 `CollectorSession`；`__enter__` **即刻**打开 persistent context（浏览器启动失败 = 单一、明确的失败点，映射 `NetworkAcquisitionError`），`__exit__` 关闭。惰性打开被明确拒绝——它引入隐式状态机 |
| S-2 | session 内一切方法复用同一 context；**任何 session 路径代码不得调用 `_launch_context()`** |
| S-3 | Page 可以轮换（每次 fetch 新开 page 合法）；context 不轮换 |
| S-4 | **crash 责任**：进程崩溃 → 无人为 cleanup（OS 回收进程；Chromium user-data-dir 下次启动自恢复，P0 已验证该模式）；Python 异常路径 → `with` 的 finally 保证关闭；`close()` 幂等，**关闭失败只记日志，不得覆盖已在传播的业务异常** |
| S-5 | context 中途死亡（浏览器进程退出/连接断开）→ session 进入 dead 态：后续一切调用抛 `NetworkAcquisitionError`；**不自动重启**（重启是运行级决策，归 runner 的中止语义，见 SYNC_RUNNER_DESIGN §7.2） |
| S-6 | 节流分工：枚举内部的滚动间隔（1.5–3s 抖动）属于 session（它驱动请求）；note 之间的间隔属于 runner（它是策略） |
| S-7 | 非 sync 命令（`login`/`favorites`/`ingest` CLI）保留现有逐调用 context 行为，走 legacy 单调用方法；**sync runner 禁止调用 legacy 方法**。两条路径共用同一套 parser 与提取逻辑，不得复制两份（review §2.1） |

不设计任何并发：单 session、单线程、顺序调用是本契约的前提而非选项。

## 2. FavoriteRef contract（Acquisition → SyncRunner 的唯一输入）

**Current Reality**（models.py:13-26）：`note_id`、`source_url` 必填；`title/author_name/author_id/xsec_token/cover_url` 可选；`raw` 默认空 dict。**没有 `discovered_at`，没有 `source` 字段。**

**Target Contract**：

```python
@dataclass
class FavoriteRef:
    note_id: str                                  # REQUIRED  去重主键，一切状态的键
    source_url: str                               # REQUIRED  可复用 URL（有 token 时携带 query）
    discovered_at: str = field(default_factory=get_current_iso_time)
                                                  # REQUIRED（NEW）页面拦截时刻，ISO-UTC；
                                                  # collector 必须显式传入，default 仅为构造兼容
    source: str = "favorites"                     # NEW  发现面判别符；P1 恒为 "favorites"，
                                                  #      为 P2 boards 预留命名空间（不预建行为）
    xsec_token: str | None = None                 # OPTIONAL  两阶段生命线
    title: str | None = None                      # OPTIONAL
    author_name: str | None = None                # OPTIONAL
    author_id: str | None = None                  # OPTIONAL
    cover_url: str | None = None                  # OPTIONAL
    raw: dict[str, Any] = field(default_factory=dict)
                                                  # REQUIRED（语义上）：collector 填充的脱敏
                                                  # listing item；仅诊断用
```

**日志规则（MUST）**：

| 允许进日志 | 禁止进日志 |
|---|---|
| `note_id`、`title`、`author_name`、`source`、`discovered_at` | `xsec_token`（capability token，等同凭证） |
| | `source_url`（query 中含 token；如需记录必须脱敏为 `.../explore/<id>?xsec_token=…`） |
| | `raw`（含 track id 等） |
| | `cover_url`（签名 CDN URL） |

该规则同样约束 `AcquisitionError.details`：错误详情携带 `note_id` 合法，携带未脱敏 URL/token 非法（现状代码存在违规样例，如 `TokenInvalidError` 的 `details={"url": page.url}`，迁移项 M-5）。

**为什么 SyncRunner 不应重新获取 token**（契约级理由，非偏好）：

1. **没有第二个来源**：token 只能来自收藏列表枚举。runner 在 detail 阶段重推导 = 隐式二次枚举——成本、风控暴露面、且破坏 S-1/S-6 的单 session 节流模型；
2. **不新鲜**：本运行枚举刚刷新的 snapshot 已是 runner 可得的最优 token；重推导至多等价、通常更差（如现状只看前 50 条，解析不到历史收藏，collector.py:476）；
3. **吞错风险**：现状隐式枚举把一切异常 `except Exception: pass`（collector.py:481-482）——AUTH/RISK/RATE 被吞掉后由后续流程误分类。这正是 review §2.4 判定为 conflict 的根因；
4. **职责分层**：token 是站点事实（acquisition 拥有），重试节奏是工作流决策（runner+state 拥有）。token 失效 → `TokenInvalidError` → state 记 `RETRYABLE_FAILED` → **下次运行的枚举以 refresh-when-present 语义自然刷新**（COALESCE：新 page 带 token 才覆盖，缺失值永不抹掉已知 token——review §2.4 的精确表述，取代"每次枚举刷新全部 token"的旧说法）。

## 3. Favorites enumeration contract

**Current Reality**：`list_favorites(limit=20)` 聚合返回——拦截器只取 `data.notes`（collector.py:260-272，JSON 异常吞掉），cursor/has_more 丢弃；滚动硬上限 6 次；`has_more = len >= limit` 推断（collector.py:433）；`completion_proof="FOUND_N_ITEMS"`；空列表仅在页面显式空态时成功（fail-closed 已存在，保留）。

**Target Contract**：`iter_favorites(limit: int | None = None) -> Iterator[PageResult]`

- **yield 单位 = 一个服务端响应**（一次 `/api/sns/web/v2/note/collect/page` body），不是滚动批次、不是聚合列表。跨页去重交给 State 主键（upsert 幂等），collector 不做跨页过滤；
- 每个 yield 的 `PageResult`：`items`（本页 refs，含 `discovered_at`/`source`）、`next_cursor`（服务端值）、`has_more`（服务端值）、`completion_proof`、`raw`（本页证据）；
- **终止语义（核心）**：正常结束必须以恰好一个"终止页"收尾——
  - 末页服务端 `has_more == false` → 该页 `completion_proof="SERVER_HAS_MORE_FALSE"`；
  - 页面显式空态（枚举开始前，DOM 验证）→ yield 唯一一页 `items=[]` + `completion_proof="EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE"`；
  - `limit` 截断 → 停止迭代，最后一页 `completion_proof=None`（runner 记 `ENUMERATION_LIMITED`，不得记完成）；
  - 其余所有页 `completion_proof=None` 且 `has_more=True`。
- **失败 = 抛 typed error，禁止用空结果表达**：
  - 滚动停滞（连续 5 次滚动无新拦截响应且无服务端终止证据）→ `UnknownAcquisitionError`（中止而非完成——ONEMULE 教训，plan §5.2）；
  - 响应 schema 无效 → `ParseFailedError`（见 §6）；
  - AUTH/RISK/RATE/传输错误 → 对应 typed error；
  - **任何路径都不允许 `return []` 或 yield 空聚合来代表"出错了"**。空 items 只有两种合法出现：显式空态终止页、服务端合法空页（`has_more=true` 的空页照常 yield，继续滚动）；
- **逐页 checkpoint：是**。contract 提供逐服务端页的 yield 粒度，runner 在每个 yield 后立即 `upsert_notes(page.items)`（单事务，发现即持久）。collector 不知道 State 存在——粒度即契约；
- **current-user proof**（review §2.5.1，Phase 1 安全条件）：认证判定 = `__INITIAL_STATE__.user.userInfo` 的 `guest === false && redId`（严格，非 DOM 头像/链接启发式）；profile URL 从认证用户的 `userPageData/userInfo.userId` 派生，**禁止**把页面上任意 profile 链接（如 explore feed 作者链接）当作当前用户。无法证明当前用户 → `AuthRequiredError`（拿不到证明）或 `ParseFailedError`（状态损坏），不得静默降级；
- `PageResult` 模型本身不变（models.py:29-44 字段已足够，review §2.2 确认）——变的是**填充语义**。

## 4. Detail fetch contract

**Current Reality**：`fetch_note(ref_or_id: str | FavoriteRef)`；token 缺失时隐式 `list_favorites(50)` 且吞异常（collector.py:472-482）；SSR fallback 在目标键缺失时返回 map 中任意第一项（collector.py:537-546）；feed fallback 返回任意非空 payload（collector.py:554-559）；无 requested-vs-returned 身份校验。

**Target Contract**：session 路径签名收紧为——

```python
def fetch_note(self, ref: FavoriteRef) -> dict[str, Any]:
```

| # | 规则 |
|---|---|
| F-1 | **只接受 `FavoriteRef`**。裸 note_id/URL 便利入口仅保留在 legacy 逐调用方法上（服务 `ingest` CLI）；session 路径传错类型 = `TypeError`（编程错误，不是采集错误） |
| F-2 | **禁止内部重新枚举**：session 路径的 `fetch_note` 不得调用 `iter_favorites`/`list_favorites`，无任何隐式 token 重推导 |
| F-3 | **消费 snapshot 原样**：用 `ref.source_url`（含其 token query）导航。token 缺失不构造、不猜测——裸 URL 照常尝试（公开笔记可成功），被拒（404/`error_code=300031`）→ `TokenInvalidError` |
| F-4 | **身份证明（review §2.5.2）**：返回的 payload 的 note_id **必须** == `ref.note_id`。SSR 提取只取 `noteDetailMap[ref.note_id]`，键缺失不得回退到任意条目；feed fallback 必须校验其 note id；不匹配或拿不到可证明的目标 → `ParseFailedError`（details 携带 `{requested, received}`，均不含 URL/token）。runner 在 normalize 后做第二道断言（纵深防御），但**契约本身必须成立**，不依赖 runner 兜底 |
| F-5 | 返回值 = 脱敏 raw detail dict，形态与现状一致（`{"note": {"noteDetailMap": {...}}}` 或 feed payload）——normalizer 的输入契约不变 |
| F-6 | 失败分类：页面显式删除/违规 → `ContentUnavailableError`；访客态/登录墙 → `AuthRequiredError`；导航超时/连接失败/目标已关闭/context dead → `NetworkAcquisitionError`（**新映射**，现状落入 UNKNOWN）；结构化载荷不可得或身份不匹配 → `ParseFailedError`；404/300031 → `TokenInvalidError` |

## 5. Exception taxonomy

**Current Reality**：`errors.py` 已有完整九态 taxonomy + typed 异常 + `to_dict()`，且 state 的 `error_status` 字符串与之一致。名称与用户清单略有出入：`AuthRequiredError`（≠ AuthenticationError）、`RateLimitedError`（≠ RateLimitError）、`ParseFailedError`（≠ ParseError）。

**Target Contract**：**名称保留，不重命名**——重命名是纯 churn（61 tests、exit-code 语义、state 校验全要跟改），契约冻结现有名称并给出对应关系：

| 本契约角色 | 现有类型 | 分类（仅分类，不含重试逻辑） |
|---|---|---|
| AuthenticationError | `AuthRequiredError` | user-action（重新 `login`）；运行级 |
| NetworkAcquisitionError | `NetworkAcquisitionError` | retryable（传输类；含 context dead） |
| RateLimitError | `RateLimitedError` | retryable-after-cooldown；运行级 |
| ParseError | `ParseFailedError` | retryable-capped（载荷可能不同）；schema 漂移信号 |
| ContentUnavailableError | `ContentUnavailableError` | terminal |
| —（保留） | `TokenInvalidError` | retryable-cross-run（下次枚举 refresh-when-present） |
| —（保留） | `RiskControlledError` | user-action（人工过验证）；运行级 |
| —（保留） | `MediaDownloadError` | retryable per-file（发生在 media 层，列出仅为封闭集） |
| —（保留） | `UnknownAcquisitionError` | defensive retryable-capped；**永不代表成功** |

对 runner 的暴露面：session 两个方法可能抛出的全部异常 ∈ `AcquisitionError` 子类（+`ValueError`/`TypeError` 级编程错误）。**任何裸 `Exception` 逃逸到 runner 都是契约违规**——这就是 D-B 的含义。`details` 必须日志安全（§2 规则）。

**重试逻辑不属于本层**：分类表只声明方向；节奏、次数、预算、FINAL/RETRYABLE 裁决全部属于 SyncRunner + StateStore（预算已由 State 原子执行，runner 读 `transition()` 返回值即可——review §3.6 的修正表述，本文档确认）。

## 6. Response parser boundary

**Current Reality**：`parser.py` **不存在**。解析内嵌于 collector：拦截闭包吞掉 JSON 异常（collector.py:271-272）；`page.evaluate` 返回任意形状直接使用；normalizer 是下游纯函数（缺 note_id 已 fail-closed）。

**Target Contract**：解析职责抽为**纯函数**（建议新文件 `parser.py`，无 I/O、可用 fixture 直接测试；落点为迁移建议，契约只冻结行为）：

```text
interception（I/O：page.on("response") 捕获原始 body）
        │  只捕获，不过滤、不吞错
        ▼
parse_collect_page(data) / parse_note_detail(...)  （纯函数）
        │
        ├── 形状有效 → ParsedPage / raw detail
        └── 形状无效 → ParseFailedError（携带安全 details）
                 ✗ 永远不 → 空列表 / None / 静默跳过   ← 本节核心禁令
```

判无效（任一即 `ParseFailedError`）：非 JSON、非 dict、`success != true`（可识别的 auth 语义除外→ `AuthRequiredError`）、`data.notes` 缺失或非 list、item 缺 `note_id`、`cursor`/`has_more` 缺失或类型不符（`has_more` 必须 bool）、detail 目标键缺失。**合法空页**（`notes=[]` + 完整 cursor/has_more）是有效结果，不是错误——"invalid → error" 与 "valid-empty → empty" 的区分就是本节全部内容。

拦截器职责相应收缩为：捕获原始 body + HTTP 状态码（供限流分类），**不做任何解释性过滤**。现状"静默吞掉"的三个点（response JSON、token cache 读写、`_check_page_anomalies` 之外的 evaluate 异常）全部改为：能分类的抛 typed error，不能分类的包成 `UnknownAcquisitionError`。

字段名诚实声明：`data.cursor`/`has_more` 的确切键名未实测（Sonnet 置信 60%）。契约规定 Phase 1 Day 1 用真实拦截 payload 确认并固化为 fixture；在确认之前严格校验**不得上线**为默认行为——fail-closed 的方向是"宁可中止不可假装完成"，但把未验证的键名当硬校验会让每次枚举都中止。

---

# Interfaces (伪代码即可)

```python
# === collector.py（新增/修改，Target 形态） =============================

class CollectorSession(Protocol):
    """Run-scoped acquisition surface. Single context, single thread, ordered."""

    def iter_favorites(self, limit: int | None = None) -> Iterator[PageResult]:
        """Yields one PageResult per intercepted server response, in order.
        Normal termination ends with exactly one proof-bearing page
        (SERVER_HAS_MORE_FALSE | EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE).
        Any failure raises a typed AcquisitionError after earlier yields
        have been delivered (already-yielded pages stay valid work)."""

    def fetch_note(self, ref: FavoriteRef) -> dict[str, Any]:
        """Detail for exactly `ref`. No implicit enumeration, no token
        re-derivation (F-1..F-4). Returned payload's note_id == ref.note_id
        or ParseFailedError."""

    def close(self) -> None: ...          # idempotent; never masks a
    def __enter__(self) -> "CollectorSession": ...   # propagating exception
    def __exit__(self, *exc_info: object) -> None: ...


class XhsPlaywrightCollector:
    def session(self, *, headless: bool = True) -> CollectorSession:
        """Opens the persistent context eagerly (S-1). Non-sync callers
        keep the legacy per-call list_favorites()/fetch_note() path."""

    # legacy, per-call, CLI-only —— unchanged semantics
    def list_favorites(self, limit: int = 20) -> PageResult: ...
    def fetch_note(self, ref_or_id: str | FavoriteRef) -> dict[str, Any]: ...


# === parser.py（新模块，纯函数） ==========================================

@dataclass(frozen=True)
class ParsedPage:
    items: list[FavoriteRef]     # validated: note_id present; sanitized raw
    cursor: str | None           # server value, None only when server omits
    has_more: bool               # REQUIRED by contract; presence validated

def parse_collect_page(
    data: Mapping[str, Any], *, discovered_at: str, source: str = "favorites",
) -> ParsedPage:
    """Raises ParseFailedError on any invalid shape (§6 list).
    NEVER substitutes an empty result for invalid input."""

def extract_note_detail(
    ssr_state: Mapping[str, Any] | None, feed_payloads: Sequence[Mapping[str, Any]],
    *, requested_note_id: str,
) -> dict[str, Any]:
    """Identity-proofed extraction (F-4): only noteDetailMap[requested_note_id]
    or a feed payload whose note id matches. Otherwise ParseFailedError."""
```

---

# Failure Model

采集层对 runner 的完整失败面（= §5 表的应用视角）：

| 场景 | 抛出 | runner 语义（引用，不实现） |
|---|---|---|
| 未登录 / session 过期（枚举或 detail） | `AuthRequiredError` | 运行级中止，exit 2，`login` 后重跑 |
| 风控 / 验证码 | `RiskControlledError` | 运行级中止，exit 2，人工处理后重跑 |
| 页面频控信号 / API 响应限流状态 | `RateLimitedError` | 运行级暂停 ≤2 次后中止 |
| 导航超时 / 连接失败 / context dead | `NetworkAcquisitionError` | note 级重试 ≤2；context dead → 运行级中止 |
| 响应 schema 无效 / 身份不匹配 / 拿不到结构化载荷 | `ParseFailedError` | note 级 `RETRYABLE_FAILED(stage=NORMALIZE)`，capped |
| 404 / 300031 | `TokenInvalidError` | note 级 `RETRYABLE_FAILED(stage=FETCH)`，下次枚举刷新 token |
| 笔记删除 / 违规 | `ContentUnavailableError` | `FINAL_FAILED`，terminal |
| 滚动停滞无终止证明 | `UnknownAcquisitionError` | 枚举中止（exit 2），已 yield 页保留 |
| 未分类异常 | `UnknownAcquisitionError`（包装，含安全 details） | defensive retryable，永不推进完成 |

三条横切规则：(1) 失败永远 typed，禁止裸异常逃逸、禁止空结果代表失败、禁止吞错后继续；(2) `details` 日志安全（§2）；(3) 本层零重试逻辑——所有节奏/预算/FINAL 裁决在 runner+state。

---

# StateRunner Interaction

两层交接面（唯一接触点）：

```text
CollectorSession                          StateStore
─────────────────                         ─────────
iter_favorites() ──yield PageResult──▶ upsert_notes(page.items)   # 每页一事务
                    （refs 含 discovered_at/source/xsec_token）
fetch_note(ref) ◀── NoteState 快照重建 ── get_fetch_queue()
      │
      ├─ raw dict ──▶ normalize ──▶ 原子写 raw.json ──▶ DETAIL_SUCCESS
      └─ typed error ──▶ transition(RETRYABLE_FAILED,
                        error_status=<taxonomy>, failure_stage=FETCH|NORMALIZE)
                                  │
                                  └─▶ State 原子裁决 RETRYABLE vs FINAL（预算内不变式），
                                      runner 只读返回的 NoteState.status
```

- **runner-local helper，不入 State/Collector**（review §5.3）：`NoteState → FavoriteRef` 重建（含 token/source_url 缺失守卫）、原子文件写、COMPLETE 守卫、skip-if-verified、退出码计算、本地单实例锁、`normalize(raw).note_id == ref.note_id` 第二道断言；
- token 链路单向：枚举 → FavoriteRef → State snapshot（refresh-when-present）→ 下次 fetch_note 消费；runner 永不反向求 token（§2 理由）；
- 本契约不修改 State；review §3 列出的 State 缺口（COMPLETE 扫描 API、全量 DISCOVERED 接纳、原子 meta 批写、resume-normalize 转移）属 Gate 0，与本契约并行收口后再进 Phase 2。

---

# Migration Impact

**Current Reality → Target 差距与迁移步骤**（顺序即 review §6 的 Phase 1，验收全部用 fixture/fake，不登录真实账号）：

| # | 差距（Current Reality） | 迁移步骤（Migration Steps） | 验收 |
|---|---|---|---|
| M-0 | 服务端 cursor/has_more 键名未实测 | Day 1：真实 session 捕获 2–3 页 payload，确认键名，固化为 fixture | fixture 进库；键名记录进本文档附录 |
| M-1 | 无 session/iter_favorites | 实现 `session()` + 生成器 + 终止页语义 + 停滞检测 + current-user proof | 多页/末页/缺 has_more/cursor 重复/空+has_more/停滞 fixture 测试 |
| M-2 | fetch_note 隐式枚举 + 任意条目 fallback | session 路径收紧为 `fetch_note(FavoriteRef)`；删除隐式 `list_favorites(50)`；实现 F-4 身份证明 | token 缺失/失效、wrong-note payload、SSR 目标键缺失测试 |
| M-3 | 传输异常逃逸为裸异常 | Playwright timeout/连接错误统一映射 `NetworkAcquisitionError`；context dead 语义 | mock context 死亡 → 后续调用全 typed |
| M-4 | 三个静默吞错点 | response JSON、token cache、evaluate 异常改为 typed/包装 | parse-drop 不再产生空结果 |
| M-5 | `details` 携带未脱敏 URL | 错误详情脱敏（token query 遮蔽）；FavoriteRef 日志规则落地 | 断言日志/详情无 token |
| M-6 | FavoriteRef 无 discovered_at/source | 字段新增（default 保持构造兼容）；collector 显式填充 | refs 字段完整性测试 |
| M-7 | 解析内嵌 collector | 抽 `parser.py` 纯函数；拦截器收缩为捕获 | 纯函数 fixture 直测 |

**不破坏的东西**：`models.PageResult` 形状、normalizer 输入契约、`errors.py` 九态名称与 `to_dict`、state `error_status` 字符串、legacy CLI 三命令行为、WAL/FULL state 语义。**破坏面**：FavoriteRef 新字段（带默认，非破坏）；collector 公开面新增（非破坏）；session 路径取代 sync runner 对 legacy 方法的使用（新增约束，无代码破坏）。61 个现有测试应保持全绿——collector 无既有测试，新增测试全部为 fixture 驱动。

---

# Non-goals

**不引入**：API 逆向/直连签名请求（x-s 等）、message queue、并发（worker/并行媒体/并行 page）、workflow engine、distributed lock。（run-scoped **本地**单实例锁是 runner-local helper，允许且属于 SYNC_RUNNER_DESIGN，不是本层契约。）

**不处理（保持 P2）**：LivePhoto 内嵌视频流（架构 V1 R9）、视频最高码率按 size 选择（R10）、media generation identity（manifest 与磁盘文件的代际匹配规则——Phase 4 实现时按 review §6 的"media identity 与新 raw generation 匹配规则"细化，不改本契约）、token TTL 追踪、boards/评论/多账号、代理与指纹轮换、媒体内容级校验。

---

# Open Questions

1. **服务端分页字段名**（M-0）：`data.cursor` 还是顶层 `cursor`？`has_more` 是否恒为 bool？Phase 1 Day 1 必须实测，否则严格校验形同虚设。
2. **长 session 稳定性**：一个 context 连续数百次导航是否触发降级/风控？P0 只有短 session 证据（review 置信 65%）。Phase 1 结束时需要一次真实小规模 smoke（如 20 note）校准；契约的立场是不自动重启、靠可恢复性兜底，若实测频繁死亡需重估。
3. **限流信号质量**：目前靠页面文案启发式。拦截器已改为记录 API 响应 HTTP 状态码——461/429 等状态是否比文案可靠、如何映射 `RateLimitedError`，Phase 1 用 fixture + 实测定案。
4. **detail 页的 AUTH 语义**：笔记详情存在匿名可见的情形，`AuthRequiredError` 在 fetch_note 上可能极罕见甚至不可达——分类保留，但 Phase 1 观测其真实出现率，必要时收敛为枚举专属。
5. **`discovered_at` 时钟源**：契约取拦截时刻（响应到达），非 yield 时刻——两者在同步生成器中几乎等价，差异仅在背压下；已选拦截时刻，记录以免实现者再纠结。
6. **`source` 值域**：P1 恒 `"favorites"`；P2 引入 boards 时是否用 `board:<id>` 形态，留给那时的契约版本决定，现在只预留字段。
7. **PageResult.raw 的诊断粒度**：是否逐页记录 HTTP 状态与耗时（风控调参价值高、成本低）——倾向 yes，实现时一并落地，不影响契约形状。
