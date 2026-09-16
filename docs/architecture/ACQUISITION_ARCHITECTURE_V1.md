# XHS Acquisition Architecture V1（冻结版）

冻结日期：2026-09-16
冻结对象：`src/xhs_ingest/` 当前已实现的采集层架构（P0 Vertical Slice + P1 状态层）

本文档只描述**已经存在并经过验证的架构**。已规划但未实现的内容（分页契约、sync runner、context 复用等）归入 §7 未解决风险，不构成本架构的一部分。不引入任何新技术：全部组件基于 Playwright（persistent context）、httpx、SQLite（stdlib）与标准库文件操作。

技术选型依据：`docs/ACQUISITION_REVIEW.md`（外部三方案对比结论）。状态边界依据：`docs/P1_STATE_CONTRACT_V2.md`。垂直切片证据：`docs/P0_SMOKE_TEST.md`。

---

## 1. 架构总览

```text
┌────────────────────────────────────────────────────────────────┐
│ CLI (cli.py): login / favorites / ingest                       │
└───────────────┬────────────────────────────────────────────────┘
                ▼
XhsPlaywrightCollector (collector.py)          [Collector Protocol]
   ├─ BrowserSession 职责（内嵌，未拆分模块）
   │    persistent Chromium profile (.xhs-profile/)
   │    launch_persistent_context / interactive_login / check_auth
   ├─ ResponseInterceptor 职责（内嵌，page.on("response") 闭包）
   │    /api/sns/web/v2/note/collect/page   → 收藏列表数据
   │    /api/sns/web/v1/feed                → 详情 fallback
   └─ Parser 职责·采集侧（内嵌，page.evaluate JS）
        __INITIAL_STATE__.note.noteDetailMap 提取 + DOM 文本异常检测
                │ sanitized raw dict
                ▼
normalize_note (normalizer.py)                  [纯函数 Parser]
                │ CanonicalPost
                ▼
download_post_media (media.py)                  [httpx 下载器]
                │ 原子写 + SHA256
                ▼
data/<note_id>/{assets/, raw.json, canonical.json, post.md}

StateStore (state.py)                           [边界外·只管 workflow state]
   SQLite(.xhs-state/sync.db)：发现/生命周期/预算/错误
   当前无调用方（sync runner 未建）
```

数据提取优先级（`ACQUISITION_REVIEW.md` §3 脆弱性阶梯的落地）：**API 响应拦截为主 → SSR 内存状态为辅 → DOM 文本为异常检测兜底**。绝不依赖 Vue 私有变量下标（如 `_rawValue[1]`）。

---

## 2. 组件职责

### 2.1 BrowserSession 职责

**当前实现位置**：`collector.py` 内嵌方法簇（`_launch_context` / `interactive_login` / `check_auth`），非独立模块。冻结的是职责契约，不是代码形态。

| 职责 | 行为 |
|---|---|
| 会话载体 | `chromium.launch_persistent_context(user_data_dir=".xhs-profile/")`：完整 Chromium User Data 目录（Cookie 树、localStorage、IndexedDB、硬件指纹），凭证由浏览器原生管理，**不做 Cookie 文本化导出** |
| 指纹配置 | viewport 1440×900；UA 固定 Chrome/124；`--no-sandbox`、`--disable-blink-features=AutomationControlled` |
| 登录自举 | `interactive_login()`：headed 窗口人工扫码/短信；登录态判定 `guest === false && redId`（严格区分匿名访客与认证会话——tamnd 教训）；二维码截图存 profile 便于远程扫码 |
| 会话探测 | `check_auth()`：headless 打开 explore 页，读 `__INITIAL_STATE__.user` 判定 |
| 生命周期 | **每次 `list_favorites` / `fetch_note` 调用各自启动并关闭一个 context**（见 R5） |

**明确不负责**：请求签名（x-s 等）、代理池、指纹随机化、多账号。

### 2.2 Collector 职责

**当前实现位置**：`collector.py:XhsPlaywrightCollector`，实现 `Collector` Protocol（`list_favorites` / `fetch_note`）。

`list_favorites(limit=20) → PageResult`：

1. explore 页定位用户 profile URL（DOM 链接 → `__INITIAL_STATE__` userId 双路径）；
2. 未认证（guest session）→ `AUTH_REQUIRED`，fail-closed；
3. 进入 `?tab=fav&subTab=note`，点击"收藏"tab；
4. 滚动驱动加载（上限 6 次，直到拦截条数 ≥ limit）；
5. 拦截载荷 → `FavoriteRef` 列表（按 note_id 去重），token 写入缓存；
6. 空列表时**只在页面出现显式空态文案/元素才返回成功空集**，否则抛 `UNKNOWN_ACQUISITION_FAILURE`（防伪造空收藏）。

`fetch_note(ref_or_id) → sanitized raw dict`：

1. 输入归一：FavoriteRef / 完整 URL / 裸 note_id（裸 ID 从 token 缓存解析，缺失时触发一次 `list_favorites(50)` 补解析）；
2. 导航 note 页；404 / `error_code=300031` → `TOKEN_INVALID`；删除/违规文案 → `CONTENT_UNAVAILABLE`；
3. SSR 提取 `noteDetailMap[targetId]`（含非目标键容错回退）；
4. 拦截的 `/v1/feed` payload 作为 fallback；
5. 均失败 → `PARSE_FAILED`；
6. 所有返回经 `sanitize_raw_data()` 递归脱敏（剔除 cookie/authorization 键）。

**明确不负责**：分页游标管理（见 R1）、状态持久化、重试策略、媒体下载。

### 2.3 ResponseInterceptor 职责

**当前实现位置**：collector 方法内的 `page.on("response", handler)` 闭包，非独立模块。

| 端点 | 用途 | 当前提取 | 已知缺口 |
|---|---|---|---|
| `/api/sns/web/v2/note/collect/page` | 收藏列表数据主通道 | 仅 `data.notes`（JSON、`success=true` 时） | **丢弃响应中的 `cursor`/`has_more`**（R1）；JSON 解析异常静默吞掉 |
| `/api/sns/web/v1/feed` | note 详情 fallback | 完整 payload 保留 | 同上，异常静默 |

设计原则（冻结）：拦截是对抗 DOM/内存树漂移的最稳提取点——Web/H5 共享的后端 REST 契约比前端结构稳定。滚动只是**促使浏览器发起这些请求的手段**，不是数据源。

### 2.4 Parser 职责

分两层，均为纯转换、无 I/O：

**采集侧提取（collector.py 内嵌 JS）**——`page.evaluate` 读 `window.__INITIAL_STATE__.note.noteDetailMap`（中等脆弱性：SSR 序列化结构）；DOM `body.innerText` 文本匹配用于异常检测（验证码/频控/删除/空态文案，高脆弱性，见 R11/R12）。

**规范化（normalizer.py:normalize_note）**——纯函数，多形态解包（API `data.items[].note_card` / SSR `noteDetailMap` / 裸 note card）→ `CanonicalPost`：

- note_id 缺失 → `PARSE_FAILED`（fail-closed，绝不静默空串——ACQUISITION_REVIEW §3.2 准则）；
- 字段缺失表现为 `None`，不默认填充；
- 计数解析兼容 `int/str/"1.2万"/"2.5w"`；
- 图片 URL 优先级：`url_default` → `url` → `url_pre` → `info_list[-1]`；
- 视频：`stream.{h264,h265,av1}[].master_url` 取**首个命中**（非最高 size，见 R10）；URL 去重；扩展名推断；
- raw payload 完整保留进 `CanonicalPost.raw`（诊断生命线）。

**明确不负责**：schema 版本化、字段级校验规则、tags/时间字段（P0 已识别未纳入 canonical）。

### 2.5 StateStore 边界

**当前实现位置**：`state.py:StateStore`（SQLite，schema v2，WAL + synchronous=FULL）。**采集层所有组件均不知道它的存在**；对接是未来 sync.py 的职责。

边界（P1_STATE_CONTRACT_V2 §1 冻结）：

- **拥有**：workflow state——发现集合、9 态生命周期、单一 fetch 预算（attempt_count + last_failure_stage）、per-file 媒体执行记录（media_state，filename 严格查找键）、sync 元信息；
- **不拥有且永不触碰**：`data/` 下的文件系统产物。raw.json/canonical.json/媒体字节/`.complete` 的完整性验证归 sync.py；
- **并发模型**：单连接、单写者、非线程安全；第二个进程由 SQLite 文件锁响亮拒绝；
- **队列语义**：`get_fetch_queue()` 与 `get_media_resume_queue()` 两个互斥 SQL 投影，并集覆盖全部非终态工作。

### 2.6 相邻组件（一并冻结）

- **media.py**：httpx 顺序下载；单文件 `.<name>.tmp` → 非零校验 → 流式 SHA256 → `Path.replace()` 原子落盘；单文件失败不阻断其余；整篇失败返回 False/抛错（CLI exit 3）。无重试、无 skip-if-verified（sync.py 职责）。
- **renderer.py**：CanonicalPost → post.md，纯渲染，恢复流程不读。

---

## 3. FavoriteRef schema（冻结）

`models.py:FavoriteRef`——列表到详情的唯一交接契约：

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `note_id` | str | ✓ | 稳定去重主键，一切状态的键 |
| `source_url` | str | ✓ | 可复用访问 URL；有 token 时携带 `?xsec_token=...&xsec_source=pc_fav` |
| `title` | str\|None | | 展示用 |
| `author_name` / `author_id` | str\|None | | 展示用 |
| `xsec_token` | str\|None | | **两阶段生命线**：详情访问无有效 token 即 404/300031 |
| `cover_url` | str\|None | | 封面 |
| `raw` | dict | | 经脱敏的原始 listing item（诊断留证） |

**Token 流转（冻结）**：listing 拦截 → FavoriteRef.xsec_token → fetch_note 消费；旁路写入 `.xhs-profile/tokens_cache.json`（note_id → token），供裸 note_id 输入解析。该缓存的读写异常当前被静默吞掉、无时效信息（R4）。

**承载容器 `PageResult`**：`items / next_cursor / has_more / completion_proof / raw`。当前诚实性缺口（冻结为已知状态，P1 修复目标）：`next_cursor` 从未赋值；`has_more` 是 `len(items) >= limit` 推断而非服务端证据；`completion_proof` 仅为 `FOUND_N_ITEMS`（发现证明，非完成证明）。唯一可信的完成证据是 `EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE`。

---

## 4. Failure Modes（冻结 taxonomy）

`errors.py:AcquisitionStatus` 十态，全部经 `AcquisitionError(status, message, details)` 携带结构化信息：

| Status | 触发点（当前代码） | 当前处理 | P1 语义（已冻结于 contract/plan，未实现） |
|---|---|---|---|
| `AUTH_REQUIRED` | guest session（list_favorites 前置检查） | CLI exit 2 | 中止运行，`xhs-ingest login` 后重跑 |
| `RISK_CONTROLLED` | 验证码/滑块 selector 或文案 | exit 2 | 中止运行，人工过验证 |
| `RATE_LIMITED` | 页面频控文案 | exit 2 | 运行级暂停 ≤2 次后中止 |
| `TOKEN_INVALID` | 404 / `error_code=300031` | exit 2 | 跨运行重试（下次枚举刷新 token） |
| `CONTENT_UNAVAILABLE` | 删除/违规文案 | exit 2 | terminal → FINAL_FAILED |
| `PARSE_FAILED` | SSR/feed 均无结构化载荷；normalizer 缺 note_id | exit 2 | 有限重试后 FINAL_FAILED |
| `NETWORK_ERROR` | **（定义存在，无触发点——Playwright 超时/连接异常未映射，见 R3）** | — | 自动重试 ≤2 次 |
| `MEDIA_DOWNLOAD_FAILED` | httpx 非 200 / 0 字节 | 记录进 MediaItem.error，exit 3 | per-file 重试 + no-progress 升级 |
| `UNKNOWN_ACQUISITION_FAILURE` | 空列表无空态证明；profile 定位失败；CLI 兜底 | exit 2 / 4 | 防御性重试，永不推进完成 |
| `SUCCESS` | （定义存在，无持久化写入方） | exit 0 | COMPLETE 状态转移 |

Fail-closed 总则（冻结）：任何错误不得伪装成功；空载荷不得推进完成；字段缺失表现为 None 而非空值。媒体失败时 detail/canonical/post.md 仍落盘（保数据），仅退出码区分（0/2/3/4）。

---

## 5. 落盘契约（冻结）

```text
data/<note_id>/
├── assets/image_NN.<ext>, video_01.mp4     字节事实（tmp + 原子 rename + SHA256）
├── raw.json                                 内容事实（服务端返回的脱敏原件）
├── canonical.json                           派生只读模型
└── post.md                                  派生渲染
```

权威分层与 `.complete` 封条语义见 P1_STATE_CONTRACT_V2 §1（本架构引用该契约，不重复）。当前 P0 状态：raw/canonical/post.md 为直接 `open(w)` 非原子写、无 `.complete`（P1 提交协议目标）。

---

## 6. 架构决策记录（V1 为什么是这样）

| 决策 | 依据（ACQUISITION_REVIEW） | 放弃的替代 |
|---|---|---|
| Playwright persistent profile 作会话底座 | ONEMULE 验证；凭证留在浏览器沙箱 | Cookie 文本化导出（易失效/泄露）；逆向签名登录 |
| API 响应拦截为主提取通道 | REST 契约稳定性 > SSR > DOM（§3.1 阶梯） | DOM/CSS 选择器抓取；Vue 私有变量下标 |
| 滚动仅作请求触发手段，完成证据必须来自服务端 `has_more` | ytf606 证据（§4.2） | "连续 N 次长度不变即结束"（ONEMULE 致命误判） |
| 不做直连签名 API（x-s 逆向） | 维护成本与失效风险（§2.2） | tamnd 式纯 HTTP + 自维护签名 |
| 媒体独立 httpx + 原子写 + SHA256 | 三项目共同教训（§5.3） | 浏览器上下文内下载 |
| SQLite 作状态层 | P1 contract；stdlib 零依赖 | JSONL / 每 note 状态文件 / 事件日志 |

---

## 7. 未解决风险（按严重度排序）

| # | 风险 | 影响 | 状态 |
|---|---|---|---|
| R1 | **分页契约未实现**：拦截器丢弃 `cursor/has_more`；滚动上限 6 次；`has_more` 靠条数推断；`FOUND_N_ITEMS` 非完成证明 | 1000+ 收藏无法证明全量；误判结束即静默漏采 | P1 已设计（plan §5.2），未实现 |
| R2 | **服务端字段名未实测**：`data.cursor`/`has_more` 的确切键名未对真实 payload 验证（Sonnet 置信度 60%） | 终止条件可能永远验证不到 | P1 Day 1 必须实测 |
| R3 | **NETWORK_ERROR 无触发点**：Playwright 导航超时/连接失败落入 UNKNOWN | 可重试的传输错误被当未知处理 | P1 collector 修改项 |
| R4 | **token cache 静默失败**：读写异常 `except: pass`；无获取时间/来源/时效 | token 刷新决策无依据；缓存损坏不可见 | P1 顺手修复项 |
| R5 | **每调用一个浏览器 context**：1000 次 fetch = 1000 次浏览器启停 | 批量不可行（耗时/内存/风控暴露面） | P1 context 复用（try/finally 保证关闭） |
| R6 | **滚动停滞 ≠ 自然终止**：网络卡顿/风控滑块表现为"滚不动" | 停滞被误判为完成（ONEMULE 教训） | P1 停滞 fail-closed 设计，未实现 |
| R7 | **长枚举风控**：高频翻页有滑块风险（P0 实测预警） | 全量枚举需多次运行凑齐 | P1 节流+抖动；本质靠可恢复性兜底 |
| R8 | **媒体签名 URL TTL**：master_url 签名数小时过期 | 跨运行补采必遇 403 | P1 已设计 403→URL 刷新升级，未实现 |
| R9 | **LivePhoto 内容丢失**：实况照片内嵌短视频流未采集（tamnd 证据：`LivePhoto: bool` + 附属 stream） | 静态图下载丢失动态本质 | **未排期**——内容级缺口，需扩 normalizer |
| R10 | **视频非最高画质**：取首个命中 codec 的首个 master_url，未按 size 排序（ytf606 算法未采纳） | 可能存到较低码率流 | 未排期——normalizer 一行级改动 |
| R11 | **SSR/DOM 漂移脆弱性**：noteDetailMap 路径、异常文案匹配依赖当前前端结构 | 站点改版 → PARSE_FAILED/误分类增多 | 靠 fail-closed + raw 留证兜底；监控靠退出码 |
| R12 | **空态判定依赖页面文案**（"暂无收藏"等字符串） | 文案变更 → 空收藏被误判为异常 | 接受；宁可误报不可伪造空集 |
| R13 | **sync runner 缺位**：StateStore 无调用方；media 无 skip-if-verified；metadata 非原子写 | 增量/恢复/幂等整体未生效 | P1 后续阶段主体工作 |

---

## 8. 冻结声明

以上组件职责、schema、failure taxonomy、落盘契约为 V1 冻结基线。后续演进（P1 分页契约、context 复用、sync runner、`.complete` 提交协议）落地时须以新增版本（V2）的方式修订本文档，不得原地改写 V1 语义；R9/R10 类内容级缺口在排期前不得被静默"顺手"修改——它们改变字节事实，属于 schema 语义变更。
