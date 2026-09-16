# Xiaohongshu Acquisition 专项对比与技术启发报告

本报告聚焦于小红书（XHS）内容采集的核心底层——**Acquisition 层**。
针对以下三个具有代表性的开源实现进行深入的技术解构与横向对比：

1. **`ONEMULE/xhs-favorites`**（全浏览器驱动 / Playwright Persistent Profile 方案）
2. **`tamnd/xiaohongshu-cli`**（纯 Go / 无浏览器 / 匿名 Session 与逆向签名方案）
3. **`ytf606/xhs2obsidian`**（Electron 宿主 / 1px 隐藏 WebView 侧载加签与 REST API 混合方案）

> **审计聚焦**：登录方式、浏览器自动化、页面结构变化风险、API 与网页抓取差异、图片视频获取方式。不评价外部项目的高层整体架构（如 Obsidian 插件体系、MCP 服务包装或 CLI 交互外壳）。

---

## 1. 登录方式 (Authentication & Session Bootstrap)

### 1.1 三种截然不同的实现范式

| 项目 | 核心机制 | 凭证存储与管理 | 登录自举 (Bootstrap) 能力 | 鉴权有效性验证 |
|---|---|---|---|---|
| **`ONEMULE/xhs-favorites`** | **Playwright Persistent Context** | Chromium 原生 User Data Directory（`.xhs-favorites-profile/`） | **原生支持**：`--login` 命令启动可见窗口 (`headless: false`)，用户扫码或短信登录，关闭后持久化整个 Profile。 | 基于页面诊断分类器 `classifySnapshot`（检查 URL、标题、扫码弹窗 DOM）。 |
| **`tamnd/xiaohongshu-cli`** | **双层 HTTP 会话 (匿名/显式 Cookie)** | 本地 JSON 文件（`session.json`）仅存匿名 `a1/webId`（12h TTL）；认证 Cookie 仅走参数/环境变量。 | **无自举能力**：不支持扫码或浏览器交互。需用户从自身日常浏览器 DevTools 中手动截取 `web_session` 填入。 | 依赖 API 响应状态码及结构分类（`status.go`），精细区分 Token 无效、未登录或风控。 |
| **`ytf606/xhs2obsidian`** | **Electron WebView 会话注入与提取** | 隔离分区 `persist:redbook-pull`；同时将 Cookie 字符串扁平化提取到插件 `settings.json`。 | **原生支持**：呼出嵌入式 WebView 登录窗口供用户交互，登录后在主进程提取完整 Cookie（含 `HttpOnly`）。 | 调用后台 `/api/sns/web/v2/user/me` 接口探针判断 session 是否有效。 |

### 1.2 对自建 Acquisition 层的关键启发

1. **持久化浏览器 Profile 是最稳妥的底座**：
   `ONEMULE` 证明了使用独立的 Chromium User Data Profile 是对抗反爬指纹最自然的手段。它天然保留了完整的 Cookie 树、`localStorage`、IndexedDB 和 WebGL 硬件指纹，不需要自写代码处理复杂的 Cookie 刷新与加密存储。
2. **匿名态与认证态的清晰划分（来自 tamnd）**：
   小红书对于公开笔记详情允许通过 `a1 + webId` 匿名访问；但对于**个人收藏列表**，必须依赖 `web_session`。采集器应在初始阶段严格识别当前是否具备用户级鉴权，防止把匿名的访客会话当成认证会话。
3. **Cookie 文本化导出的安全与失效风险（来自 ytf606）**：
   `ytf606` 将 Cookie 提取为纯文本存储在 JSON 配置中，这种做法虽然便于用普通 HTTP 客户端发起请求，但在 Cookie 自然轮转、安全属性升级时极易失效，且存在泄露风险。保留原生持久化浏览器沙箱更安全。

---

## 2. 浏览器自动化 (Browser Automation)

### 2.1 自动化模式与介入深度

- **`ONEMULE/xhs-favorites`（全流程浏览器自动化）**:
  - **模式**：完全由 Playwright 驱动真实交互（`page.mouse.wheel(0, 1400)` 滚动页面、`page.waitForLoadState("networkidle")` 等待空闲）。
  - **数据提取**：不解析 HTML，而是通过 `page.evaluate()` 读取 Vue/Nuxt 的内存全局对象 `window.__INITIAL_STATE__`。
  - **代价**：启动开销大、单次运行耗时长；长时间滚动多批次时 Chromium 内存膨胀明显；“滚动判定”具有不确定性。
- **`tamnd/xiaohongshu-cli`（零浏览器自动化）**:
  - **模式**：完全抛弃浏览器，基于纯 Go 标准库 `net/http` 发起底层调用。
  - **优势**：极速、单机内存占用仅数 MB、极其适合无头服务器与长时间后台运行。
  - **代价**：必须自行维护设备指纹生成、请求头生成、签名算法逆向。一旦小红书反爬规则更新，整个采集器彻底瘫痪。
- **`ytf606/xhs2obsidian`（混合侧载加签黑盒 — 最具巧思的设计）**:
  - **模式**：既不是纯浏览器爬取，也不是纯逆向加签。它在后台常驻一个 `width: 1px; height: 1px` 的极小隐藏 Electron `<webview>`，注入一段轻量拦截脚本，Monkey-patch 了 `Headers.prototype.set`、`fetch` 和 `XMLHttpRequest`。
  - **运行逻辑**：当需要请求小红书 API 时，利用这个隐藏 WebView 内部的小红书原生 JS 环境生成 `x-rap-param` 和 `x-s` 签名头，主程序拦截到这些签名头后，直接通过普通 HTTP 库发起高速 REST API 调用。

### 2.2 对自建 Acquisition 层的关键启发

1. **“借鸡生蛋”的侧载加签启发**：
   `ytf606` 的 WebView 补丁方案给自建采集层指出了第三条路：如果既不想维护脆弱的纯逆向加签代码（容易过时），又不想让全量数据传输都承受沉重的无头浏览器渲染开销，可以通过后台无害上下文（或轻量 BrowserContext）作为“本地签名计算器”，把计算与批量传输解耦。
2. **滚动模拟在批量场景下的致命缺陷**：
   `ONEMULE` 依赖 `collectFeedItems` 循环盲滚，采用“连续 3 次数组长度无变化则认为结束”的策略。这在网络卡顿、接口延迟、风控滑块出现时，会直接发生**把中间故障当成全量同步结束**的致命误判。

---

## 3. 页面结构变化风险 (DOM / Selector / State Drift)

### 3.1 脆弱性分析与风险暴露面对比

```text
高脆弱性: DOM 选择器 / CSS 类名 (几乎每季度重构变动)
    ↓
中高脆弱性: Vue/Nuxt 内存路径 (window.__INITIAL_STATE__.user.notes._rawValue[1])
    ↓
中等脆弱性: 页面 SSR 序列化脚本 (<script id="__INITIAL_STATE__">)
    ↓
低脆弱性: 后台 REST API JSON Schema (/api/sns/web/v2/note/collect/page)
```

- **`ONEMULE`（极高风险点）**:
  - 深度依赖组件内部数组下标和私有属性：
    - `globalThis.__INITIAL_STATE__?.user?.notes?._rawValue?.[1]`
    - `globalThis.__INITIAL_STATE__?.board?.boardFeedsMap?._rawValue?.[boardId]`
  - 一旦小红书前端做了一次构建升级（例如从 Vue 2 彻底转到 Vue 3/Pinia，或者数组下标 `[1]` 因新增 Tab 变为 `[2]`），所有的收藏获取代码在不抛出任何网络错误的情况下**静默返回 null 或空列表**。
- **`tamnd`（中等风险点）**:
  - 不依赖 DOM，只依赖 SSR 初始 HTML 中的正则匹配：`window.__INITIAL_STATE__\s*=\s*(\{.+?\})<\/script>`。
  - 数据结构映射由强类型 Go struct 定义（如 `rawNote`, `rawVideo`）。如果服务端字段名称变更（如 `liked_count` 变更），反序列化不会报错而是静默为零值。
- **`ytf606`（最低风险点）**:
  - 全流程基于后端 REST API 数据契约。由于小红书 Web 端、移动 H5 和各类轻应用均共享部分 API，服务端的 REST API 结构具有强向后兼容约束，其字段稳定性显著高于前端界面的 DOM 和临时内存树。

### 3.2 对自建 Acquisition 层的关键启发

1. **绝对不要把数据提取建立在 Vue 内部私有变量下标上（如 `_rawValue[1]`）**。
2. **以 API 响应拦截为主，以 SSR State 为辅，以 DOM 提取为终极 Fallback**：
   在浏览器环境下，最稳妥的提取点不是页面 DOM，也不是全局变量，而是通过 `page.on("response", ...)` 捕获来自 `https://edith.xiaohongshu.com/api/sns/web/...` 的真实 HTTP JSON 响应。API 契约最稳定。
3. **严格防御零值与字段丢失（Fail-Closed）**：
   不能因为解析不到字段就默默填入空字符串或 0；当核心标识（如 `note_id`）丢失时，必须立即抛出 `PARSE_FAILED` 并保留原始 raw 载荷供人工诊断。

---

## 4. API 与网页抓取区别 (API vs SSR/Web Scraping)

### 4.1 核心对比矩阵

| 维度 | REST API 端点方式 (`ytf606` / `tamnd`) | 网页 / SSR 抓取方式 (`ONEMULE`) |
|---|---|---|
| **目标 URL** | `edith.xiaohongshu.com/api/sns/web/v2/note/collect/page`<br>`edith.xiaohongshu.com/api/sns/web/v1/feed` | `xiaohongshu.com/explore/<id>`<br>`xiaohongshu.com/user/profile/<uid>?tab=fav` |
| **分页真实性** | **真实游标**：返回显式 `cursor` 与 `has_more: boolean`，可提供确定性完成证明。 | **伪游标**：依赖滚动高度与前端加载机制，无确定性终止信号。 |
| **反爬拦截门槛** | **极高**：必须提供动态加密头 `x-s`, `x-t`, `x-s-common`, `x-b3-traceid`，缺少即 403/拒绝。 | **中等**：浏览器上下文自带合法环境，只要会话有效即可加载。 |
| **单篇详情参数** | 必须携带列表返回的 `xsec_token` 与 `xsec_source`。 | 同样需要携带 `?xsec_token=...`，否则会被 300031 重定向至 404。 |
| **传输开销** | 极小（纯业务 JSON，数 KB）。 | 极大（完整 HTML、字体、埋点脚本、CSS，单页数 MB）。 |

### 4.2 对自建 Acquisition 层的关键启发

1. **收藏夹全量同步必须基于 API 响应语义**：
   `ytf606` 的接口证明，`/api/sns/web/v2/note/collect/page` 能够返回全量的 `notes` 数组与服务端提供的 `cursor/has_more`。网页滚动只是**促使浏览器发起这个 API 请求的手段**，真正决定分页终止的必须是该 API 返回的 `has_more == false`。
2. **`xsec_token` 是贯穿两阶段的生命线**：
   `tamnd` 和 `ytf606` 均明确指出：笔记详情在没有 `xsec_token` 时会被系统拒绝。列表阶段捕获的 Token 必须完好无损地存入 `FavoriteRef` 并向后传递给详情采集环节。

---

## 5. 图片与视频获取方式 (Media Acquisition)

### 5.1 图片采集细节

- **多画质分发**:
  小红书在 API 中通常返回多种尺寸：`url_default`（默认中图/大图）、`url_pre`（低分辨率预览图）、以及 `info_list`（不同场景与尺寸数组）。
  - `ytf606`: 优先选取 `url_default`，缺失时回退到封面 `cover`。
  - `tamnd`: 额外解析出 `trace_id` 和宽高元数据。
- **实况图片（LivePhoto）的重要发现（来自 tamnd）**:
  `tamnd` 在 `rawNote.ImageList` 结构中明确提取了 `LivePhoto: bool` 及其附属的 `Stream.H264[].MasterURL`。这揭示了一个绝大多数采集器忽略的事实：**小红书的实况照片底层包含一段无声/伴音的短视频流**。若只按静态图片下载，会丢失 LivePhoto 的动态本质。

### 5.2 视频采集细节

- **多码率与多编码（Codec）流矩阵**:
  小红书的视频笔记不提供固定单一的 MP4 链接，而是在 `video.media.stream` 节点下提供多编码候选流（`h264`, `h265`, `av1`）。
  - `tamnd`: 保留了 H264 与 H265 全部候选流的码率、宽高、时长及 `video.video.md5` 校验值。
  - `ytf606`: 实现了**最高画质自动选择算法**：
    ```typescript
    const tracks = stream.h265 ?? stream.h264 ?? [];
    const best = [...tracks].sort((a, b) => (b.size ?? 0) - (a.size ?? 0))[0];
    const videoUrl = best.master_url;
    ```
    通过比对流的 `size` 字节数，自动选取体积最大（码率最高）的 `master_url`。
- **URL 签名时效性 (Signed URL TTL)**:
  所有项目均证明，视频 `master_url` 带有时间戳签名（`?sign=...&t=...`），有效期通常在数小时以内。在详情提取完成后，必须**紧接着执行下载**，不能在相隔数天后再去消费存留的媒体 URL。

### 5.3 本地落盘与完整性盲区（三个项目的共同教训）

| 缺陷点 | 现状事实 | 自建 Acquisition 必须补齐的底线 |
|---|---|---|
| **原子写入** | 三个项目均直接使用标准写流或 `fs.writeFile`，中途断电断网会产生 0 字节或截断文件。 | 必须使用 `.<filename>.tmp` 写入，校验通过后原子重命名 (`replace`)。 |
| **内容哈希** | 没有任何项目对下载产物做本地 SHA256 校验和记录。 | 必须计算并持久化 SHA256，用于幂等断点时的快速比对（skip-if-verified）。 |
| **错误吞噬 (Fail-Open)** | `ytf606` 下载图片失败仅打印日志，随后依然将笔记标记为已同步并记录 ID，造成永久缺失漏采。 | 任何媒体失败绝不能将笔记状态标记为 `COMPLETE`，必须降级为 `MEDIA_PARTIAL` 允许增量重试。 |

---

## 总结：对当前 XHS 采集系统的实施准则

1. **登录与会话**：遵循 `ONEMULE` 方案，采用持久化 Playwright Profile 维持真实用户沙箱，不碰逆向登录。
2. **列表与分页**：结合 Playwright 页面滚动交互与 `ytf606` 证明的 `/api/sns/web/v2/note/collect/page` 响应拦截，以服务端的 `has_more == false` 为唯一完成依据。
3. **详情与数据解析**：优先拦截 `/api/sns/web/v1/feed` 与 SSR `noteDetailMap`，提取完整的 `CanonicalPost`，并提取 `xsec_token` 规避 404。
4. **视频与实况图**：借鉴 `ytf606` 的按 size 挑选最高质量流策略，并借鉴 `tamnd` 兼容解析 `LivePhoto` 视频流。
5. **落盘与状态**：吸取所有开源项目的失误教训，严格实行**临时文件原子写入、SHA256 哈希校验、Fail-Closed 错误闭环**。
