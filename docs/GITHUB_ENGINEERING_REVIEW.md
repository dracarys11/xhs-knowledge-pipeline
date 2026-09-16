# GitHub Engineering Review

审查日期：2026-09-16
审查对象：
- `ONEMULE/xhs-favorites`（TypeScript/Node，npm 包，双 provider + MCP server）
- `tamnd/xiaohongshu-cli`（Go，单二进制，x-s 签名 + SSR 双路径）
- `ytf606/xhs2obsidian`（TypeScript，Obsidian 插件，增量同步 + AI 分类）

对比基准：`xhs-ingest` P0 实现 + P1 设计（P1_INCREMENTAL_SYNC_PLAN.md）

---

## 1. Session 生命周期

### tamnd
`session.go` 实现了一个匿名 session（`a1` + `webId` cookies）的持久化与 TTL 管理：
```go
const sessionTTL = 12 * time.Hour

type anonSession struct {
    A1        string    `json:"a1"`
    WebID     string    `json:"web_id"`
    CreatedAt time.Time `json:"created_at"`
}
```
- 12 小时后过期，自动重新 bootstrap 匿名 session；
- 有认证 session（`web_session` cookie）时通过 `--cookie` 传入，不自动 mint；
- `LoggedIn()` 通过检查 `web_session` cookie 是否存在来判断登录状态；
- `captureCookies()` 主动捕获每个响应的 Set-Cookie，包括 Aliyun WAF token（`acw_tc`，30 分钟 TTL）和 `abRequestId`——注释说明"之前的版本丢弃了这两个 cookie，导致每次请求都缺少浏览器正常携带的 token"。

### ONEMULE
使用持久 Playwright profile（独立 user data dir），不依赖 cookie 文件作为主要 auth 路径。有 `doctor` 命令检查 `authenticated` / `auth_required` / `risk_controlled` 三种状态，诊断完整。

### ytf606
Obsidian 插件模式，session 由插件内嵌的 cookie 维持，有账号设置界面显示登录状态和同步统计。

### xhs-ingest 对比
- **已有**：持久 Playwright profile，token 缓存到 `tokens_cache.json`，auth/risk 状态分类。
- **缺失**：session TTL 感知（不知道什么时候 session 已过期，只靠 AUTH_REQUIRED 错误发现）；不捕获响应 Set-Cookie（Aliyun WAF token 等中间层 cookie 会丢失）；没有 `doctor`/`status` 命令主动检查会话健康。

---

## 2. Cookies / Token 管理

### tamnd
`client.go` 的 cookie 管理是本次审查中最精细的：
```go
// captureCookies keeps every cookie either host hands out.
// acw_tc arrives from the Aliyun WAF with 30m lifetime; abRequestId alongside it.
// The previous version discarded both.
func (c *Client) captureCookies(resp *http.Response) { ... }
```
- 所有 cookie 放在 `sync.RWMutex` 保护的 in-memory map 里；
- 每次请求后自动 capture 响应 cookie；
- `cookieHeader()` 在每次请求时重新序列化所有 cookie；
- 请求用 `xhssign.Signer` 构造 `x-s` / `x-t` 签名参数（独立 `pkg/xhssign` 子包）。

### ONEMULE
双 provider 架构：签名 API 和 SSR HTML 解析都有实现，auth 走 Playwright provider 不需要手动签名。

### ytf606
依赖 Obsidian 插件生命周期维持 session，随机延迟模拟人类操作。

### xhs-ingest 对比
- **已有**：xsec_token 是从 listing 捕获的，每 note 跟随 FavoriteRef 流动，这是正确的。
- **缺失**：没有响应 Cookie 捕获机制（Aliyun WAF token 丢失可能导致某些请求被风控）；没有 x-s 请求签名（限制了能访问哪些 API 端点，favorites 分页受限）。
- **优于同类**：xsec_token 跟随状态落盘到 SQLite（`notes.xsec_token`），重试时不需要重新枚举——这比其他三个项目都更彻底。

---

## 3. Rate Limit

### tamnd
`client.go` 实现了基于 `sync.Mutex` 的速率控制器：
```go
func (c *Client) throttle(ctx context.Context) error {
    c.mu.Lock()
    now := c.now()
    wait := c.next.Sub(now)
    if c.next.Before(now) {
        c.next = now.Add(c.cfg.Rate)
    } else {
        c.next = c.next.Add(c.cfg.Rate)
    }
    // release lock, sleep wait duration
}
```
- 令牌桶语义（不是固定 sleep），允许突发后自动补偿；
- `Rate` 是 `Config` 字段，可配置；
- 有 `DryRun` 模式只打印请求不实际发出；
- `retry_test.go` 存在，说明有独立 retry 模块（代码截断未见全文）。

### ONEMULE
通过 Playwright 浏览器速率隐式控制，没有暴露明确的速率配置。

### ytf606
README 提及"随机延迟"（订阅账号拉取时），没有更多细节。

### xhs-ingest 对比
- **P1 设计**：1.5–3s 随机 sleep，比固定 2s 更保守。RATE_LIMITED 暂停 60–120s，最多 2 次后中止。
- **P1 缺失**：throttle 是每个 fetch 之间的固定 sleep，不是令牌桶。tamnd 的实现更精确（多请求时不会累积延迟债务）；但对单线程顺序处理的 xhs-ingest 来说，差别不大。
- **待确认**：P1 的节流参数（1.5–3s）只在设计文档里，尚未实现。

---

## 4. Retry

### tamnd
`retry_test.go` 存在，包含 5522 bytes 的测试代码，说明 retry 逻辑有独立实现和完整测试。从 `errors.go` 看，APIError 包含 `Status`（Status 类型），可以据此分类重试决策。每个错误有 `Hint` 字段说明该怎么处理。

### ONEMULE
没有在 README/docs 里暴露 retry 策略，auth 失败靠 `doctor` 诊断后人工重新登录。

### ytf606
没有明确 retry 文档，重试依赖下一次定时同步。

### xhs-ingest 对比
- **P1 设计**超越所有三个项目：
  - 错误 taxonomy（`NETWORK_ERROR`, `TOKEN_INVALID`, `RATE_LIMITED`, `CONTENT_UNAVAILABLE` 等）已完整定义；
  - 分类了运行内重试（短退避）、跨运行重试（state DB）、terminal 错误（`FINAL_FAILED`）；
  - SQLite attempt_count 防止崩溃循环。
- **当前实现缺口**：retry policy 在 state.py 里有语义基础，但具体的退避逻辑还没写（在 sync.py 里）。

---

## 5. Cache

### tamnd
```go
type cache struct {
    dir string
    ttl time.Duration
}
// key = SHA256(request signature); 2-char subdir sharding
func (c *cache) path(key string) string {
    sum := sha256.Sum256([]byte(key))
    h := hex.EncodeToString(sum[:])
    return filepath.Join(c.dir, h[:2], h+".json")
}
```
- 基于请求签名的 SHA256 hash 作为 key；
- 按 hash 前 2 位分目录（类 git object store 结构）；
- TTL 基于文件 mtime 判断；
- 默认 1 小时，可配置；
- 用于 API 响应缓存，避免重复请求。

### ONEMULE / ytf606
没有在源码层看到明确的请求级缓存。

### xhs-ingest 对比
- **缺失也是正确的**：xhs-ingest 的"缓存"是 `raw.json`（完整 payload 持久化）+ SQLite state（note 级完成状态）。这比 tamnd 的 TTL 缓存更强：TTL 缓存过期后数据消失，raw.json 永久保存。
- **不需要加**：如果 raw.json 已经是持久化的缓存，再加 TTL response cache 是重复投资。tamnd 的缓存是"避免重复网络请求"用的，xhs-ingest 用 `DETAIL_SUCCESS` 状态跳过整个 fetch 路径，效果等价。

---

## 6. File Naming

### tamnd
`xhs crawl --out ./data` 输出目录结构未在 README 中详细说明，主要输出是 JSONL stream。

### ONEMULE
输出 CSV + JSON + HTML review bundle，文件命名没有特别规则。

### ytf606
文件命名规则最详细：
```
RedNote/
├── Bookmarks/
│   ├── [专辑名]/          # 按收藏夹分组
│   └── 笔记标题.md        # 以 title 作为文件名
├── Media/
│   └── 笔记标题/          # 以 title 作为媒体目录名
│       ├── 1.jpg
│       └── video.mp4
└── Users/[博主昵称]/
    ├── _profile.md
    └── 笔记标题.md
```
- 用 `title` 作为文件名，人类友好但有冲突风险（同名 title）；
- 媒体目录和笔记 MD 同名；
- 专辑按 listing 返回的 board 名称自动分目录；
- 图片命名是 `1.jpg`, `2.jpg` 等序号。

### xhs-ingest 对比
```
data/<note_id>/
├── assets/                 # 媒体文件
│   ├── image_01.jpg        # 类型+序号命名
│   └── video_01.mp4
├── raw.json
├── canonical.json
└── post.md
```
- 用 `note_id` 作为主目录——**更稳定，不冲突，可作为外键**；
- 媒体文件名来自 normalizer 生成的 `filename` 字段；
- **ytf606 的 title-based 命名面临碰撞风险**（用户收藏了两篇 title 相同的帖子）；
- **xhs-ingest 的设计是正确的**：`note_id` 是稳定主键，`title` 在 post.md 里有。

---

## 7. Incremental Update

### tamnd
**无增量**。tamnd 是一个流式读取工具，`xhs crawl` 输出到目录，每次运行全量写入，没有 state 管理。这是有意为之的设计取舍——tamnd 是 pipeline 工具，不是同步工具。

### ONEMULE
**无增量**（P1 设计目标期望的那种）。list-notes 每次读取当前收藏，但没有持久化 note 级状态、没有 COMPLETE 标记、没有 checkpoint。**这正是 P0_TO_P1_REVIEW 发现的 ONEMULE"3 次长度不变即完成"的 fail-open 教训的来源**。

### ytf606
**有增量，但是 ID 集合方式**：
> "🔄 增量同步 记录已同步 ID，不重复拉取"

从功能描述看，是把已同步 note_id 记录下来，下次枚举时跳过。实现细节（用什么存储？是否有 FAILED 状态重试？是否有媒体级幂等？）不可见，但描述暗示是简单的 ID set，没有 xhs-ingest 的 7 状态机。

### xhs-ingest 对比
**显著优于全部三个**：
- SQLite 持久状态（7 个状态 + attempt_count + media_state）；
- note 级 COMPLETE 标记（文件 + DB 双重）；
- 媒体文件级 skip-if-verified（size + SHA256）；
- MEDIA_PARTIAL 单独恢复路径；
- FETCHING/MEDIA_SYNCING 崩溃痕迹 + 启动时自动恢复。

这是目前看到的同类项目中最完整的增量同步设计。

---

## 8. CLI Design

### tamnd
CLI 设计是本次审查中最精良的：
```bash
xhs note <id> --token <t>
xhs search 'coffee' -n 100 | xhs note -   # stdin pipe
xhs user <id> --notes -n 50
xhs crawl - --out ./data --comments
```
- 输出格式：`-o table|json|jsonl|csv|tsv|yaml|url|raw`；
- `--fields a,b,c` 列选择；
- `--template '{{.note_id}} {{.title}}'` Go template 渲染；
- `-n` 限制记录数；
- stdin 读取 ID（`-` 参数）实现 pipeline；
- 终端输出 table，pipe 输出 JSONL（自动检测 tty）；
- Docker 镜像 + 预编译二进制发布；
- `DryRun` 模式打印请求不执行。

### ONEMULE
```bash
xhs-favorites bootstrap --client codex --pretty
xhs-favorites login
xhs-favorites doctor --pretty
xhs-favorites list-notes --limit 10 --pretty
```
- `--pretty` flag 控制格式；
- `bootstrap` 自动配置 MCP client；
- `doctor` 诊断命令；
- 有 MCP server 模式（同一 binary 两种入口）；
- 发布到 npm，`go install` 风格的安装体验。

### ytf606
图形界面（Obsidian 插件设置页），非 CLI。对用户更友好但不可脚本化。

### xhs-ingest 对比
当前 CLI（基于 Click）：
```bash
xhs-ingest login
xhs-ingest favorites
xhs-ingest ingest <url>
```
- **缺失**：没有 `status`/`doctor` 命令，无法查看当前同步状态；
- **缺失**：没有 `--dry-run` 模式；
- **缺失**：没有 pipe-friendly 输出（JSONL）；
- **缺失**：没有 `--fields` / `--template` 选择性输出；
- **P1 新增**：`sync --max-items N --retry-failed --state-db <path>` 是正确方向，但还不够。

---

## 哪些缺失

按优先级排序（只列 P1 范围内有意义的）：

### 必须考虑

**1. 响应 Cookie 捕获（参考 tamnd）**
tamnd 明确记录了丢弃 Aliyun WAF token（`acw_tc`）是已知 bug，修复后改善了请求成功率。xhs-ingest 的 Playwright 浏览器会自动处理 cookie，但 httpx 下载媒体时不会。媒体下载用的是普通 httpx，如果 CDN 需要特定 cookie 才能正常响应，当前实现会静默失败（HTTP 403）。
→ **建议**：media.py 的 `download_media_item` 可以接受一个可选的 cookie 字典，从 Playwright page 的 cookies 里传入。

**2. `status` / `doctor` 命令**
ONEMULE 的 `doctor` 命令很实用：主动检查 authenticated/auth_required/risk_controlled，而不是等到 sync 运行失败后才发现。xhs-ingest 的 `status_counts()` 已经存在于 state.py，但没有暴露成 CLI 命令。
→ **建议**：P1 的 `sync` 命令里加一个 `--status` 子模式，打印 state DB 的计数摘要（COMPLETE / PENDING / FAILED / MEDIA_PARTIAL 等），5 行就能实现。

### 值得注意

**3. 收藏夹分类（boards）目录**
ytf606 把收藏按专辑自动分子目录（`Bookmarks/[专辑名]/`）。P1_INCREMENTAL_SYNC_PLAN.md §9 明确"不做 boards/专辑"是非目标——这是对的，P1 不能扩大范围。但用户如果有大量分类收藏，平铺的 `data/<note_id>/` 结构会比 ytf606 的分目录结构难浏览。记录在案，作为 P2 候选。

**4. 序号命名 vs. filename-from-normalizer**
ytf606 用 `1.jpg, 2.jpg` 序号命名媒体，xhs-ingest 用 normalizer 生成的 filename。两者各有利弊：序号简单，但如果 note 被更新（媒体顺序变化），序号不稳定；filename-from-normalizer 更语义化，但 filename 生成规则要严格（P1_STATE_REVIEW_SOL.md Issue 1 已覆盖这个问题）。当前方案不需要改变。

**5. Markdown frontmatter 结构**
ytf606 的 frontmatter 包含 `id`, `author`, `type`, `url`, `tags`, `aiTags`, `category`, `createdAt`, `syncedAt`, `likes`, `comments`。xhs-ingest 的 post.md 目前没有 YAML frontmatter（只有内容）。这对 Obsidian 集成和后续搜索/过滤有影响。不是 P1 阻塞，但值得在 P2 的 canonical schema 扩展时考虑。

---

## 哪些已经更好

**1. 失败分类的精确程度**
tamnd 的 `errors.go` 设计优秀（APIError 有 Code / Hint / Status / Endpoint），但是面向 API 客户端场景。xhs-ingest 的 AcquisitionStatus taxonomy（`TOKEN_INVALID`, `CONTENT_UNAVAILABLE`, `RISK_CONTROLLED`, `RATE_LIMITED`, `PARSE_FAILED`, `NETWORK_ERROR`）是面向 Playwright browser automation 场景设计的，覆盖了 xhs-ingest 实际遇到的失败模式，更贴切。

**2. 媒体下载的原子性和完整性**
tamnd 是 JSONL 流式输出工具，不下载媒体。ONEMULE 下载媒体但没有看到 SHA256 校验。ytf606 下载媒体但实现细节不可见。xhs-ingest 的 `.tmp` + 非零校验 + SHA256 + 原子 rename 是同类项目中最严格的。

**3. 增量同步的正确性**
ONEMULE 的"3 次长度不变 = 完成"是 fail-open（审计文档中的反面教训）。ytf606 是 ID set，没有 media 级幂等。xhs-ingest 的 SQLite 状态机 + media_state + skip-if-verified（size + SHA256）才是真正的增量同步正确性保证。

**4. Fail-closed 原则的系统性**
其他项目在错误时的行为基本上是"记录日志然后继续"或"抛异常停止"。xhs-ingest 有明确的 fail-closed 规则：任何不确定性都走失败路径而不是成功路径，`has_more=None ≠ False`，停滞中止而非完成。这是工程质量的显著差异。

---

## 哪些值得加入

按价值/成本比排序：

| 特性 | 来源 | 价值 | 实现成本 | 建议 |
|---|---|---|---|---|
| `xhs-ingest status` 命令（打印 state_counts） | ONEMULE doctor 启发 | 高：debug 必需 | 极低（~10 行） | P1 顺手加 |
| 媒体下载时传入 Playwright cookies | tamnd captureCookies 启发 | 中：减少 403 | 低（media.py 改 signature） | P1 评估 |
| Dry-run 模式 | tamnd | 中：测试友好 | 低（sync.py 里 flag） | P2 |
| JSONL 输出（可 pipe） | tamnd | 中：脚本化 | 低（cli.py 加 `-o jsonl`） | P2 |
| Markdown frontmatter | ytf606 | 中：Obsidian 集成 | 低（renderer.py 改） | P2 |
| 请求级 response cache（TTL） | tamnd | 低：有 raw.json 替代 | 中 | 不建议 |
| Board/专辑目录分组 | ytf606 | 低（P1 非目标） | 中 | P2 |
| x-s 签名直连 API | tamnd | 低（风险高，维护重） | 高 | 明确 non-goal |
| MCP server 模式 | ONEMULE | 低（当前无需求） | 高 | 明确 non-goal |

---

## 总结

这次审查的主要结论：

1. **xhs-ingest 的工程路线是正确的**。其他项目的普遍缺陷（fail-open 分页、无持久状态、无媒体级幂等）正是 xhs-ingest P1 要解决的问题。

2. **tamnd 的 client.go 值得学习**：cookie 捕获（含 WAF token）、令牌桶限速、干净的错误类型设计，都是生产级细节。其中 cookie 捕获对 xhs-ingest 的媒体下载有直接参考价值。

3. **ytf606 的 frontmatter schema 值得参考**：`id, author, url, tags, likes, comments, createdAt, syncedAt` 是 P2 canonical schema 扩展的天然候选。

4. **ONEMULE 的 doctor 命令是低成本高价值的补充**：xhs-ingest 的 state.py 已经有 `status_counts()`，暴露成 CLI 只需要几行。

5. **没有任何一个项目比 xhs-ingest 的增量同步设计更完整**。
