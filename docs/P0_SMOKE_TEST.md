# P0 Vertical Slice 真实环境 Smoke Test 报告

## 基本信息

- **Date**: 2026-09-16
- **Collector**: `XhsPlaywrightCollector` (基于 Playwright Persistent Context 自建 Acquisition Adapter)
- **Session Profile**: `.xhs-profile/` (独立持久化 Profile，已入 `.gitignore`)
- **Verification Status**: **PASS** (满足 Definition of Done)

---

## 1. 链路验证记录

```text
真实登录态 (Persistent Session)
       ↓
收藏夹枚举 (Favorites Enumeration)
       ↓
单篇笔记详情 (Note Detail Acquisition)
       ↓
规范化对象 (CanonicalPost Normalization)
       ↓
媒体原子下载 (Media Download: Image & Video)
       ↓
本地文件系统落盘 (data/<note_id>/)
```

---

## 2. Favorites Enumeration

- **目标页面**: `/user/profile/<user_id>?tab=fav&subTab=note`
- **采集手段**: Playwright 浏览器会话驱动 + Edith API 网络响应拦截 (`/api/sns/web/v2/note/collect/page`) + DOM Feed 提取
- **枚举结果**: 成功列出真实收藏夹第一页（20 篇笔记）：

| # | Note ID | Author | Title |
|---|---|---|---|
| 1 | `6aa174a8000000002901b985` | 西资卡 | 东京必吃list✨5家平价米其林&顶美饭 |
| 2 | `6aa62ab4000000001001fc4f` | 艾康的AI自留地 | 一个月 20 刀的 Antigravity CLI，可能被很多人低估了 |
| 3 | `6aa6478c000000001901f146` | 狐狸大喵 | 上海本地人 我求求你了 |
| 4 | `6a97a054000000002802f761` | J同学qej | Agent面试连环追问，你能坚持到第几关 |
| 5 | `6a86f9f2000000003b017811` | LinDa的日常分享 | 上海巨难吃的店。一起避雷。 |
| ... | ... | ... | (共 20 篇) |

- **Token 捕获**: 成功从 API 载荷中提取并缓存每个条目的可复用 `xsec_token`。

---

## 3. Selected Note Detail & Acquisition

选取真实视频笔记进行全量深度验证：

- **Selected note**: `6aa174a8000000002901b985`
- **Title**: 东京必吃list✨5家平价米其林&顶美饭
- **Author**: 西资卡 (ID: `58f71b7a82ec390875c5d8e3`)
- **Note Type**: `video`
- **Text Body**: 完整提取正文、推荐店铺清单与话题标签（无截断、无虚构字段）
- **Stats**:
  - Likes: 174
  - Collects: 294
  - Comments: 22
  - Shares: 56

---

## 4. Media Download

- **Images**: 1 张封面高分辨率图 (`image_01.jpg`, 228,872 bytes)，SHA256 校验完成
- **Video**: 1 份高码率主视频流 (`video_01.mp4`, 49,270,630 bytes, 47 MB)，SHA256 校验完成
- **原子落盘**: 临时文件 (`.tmp`) 写入 -> 内容完整性校验 -> 原子重命名 (`replace`)

---

## 5. Output Path & Verification

```text
data/6aa174a8000000002901b985/
├── assets/
│   ├── image_01.jpg   (224 KB)
│   └── video_01.mp4   (47 MB)
├── canonical.json     (2.5 KB)
├── post.md            (2.1 KB)
└── raw.json           (40 KB)
```

- **`raw.json`**: 完整保留 Acquisition 层原始结构化数据（包含 `noteDetailMap` 全文字段、原始视频流地址参数等，已执行敏感 Cookie/凭证脱敏）。
- **`canonical.json`**: 符合最小 Canonical Schema 规范，关联媒体本地相对路径与 SHA256 校验和。
- **`post.md`**: 包含 YAML Frontmatter 元数据、作者与源站链接、正文、本地图片引用及本地 HTML5 视频播放器引用。

---

## 6. Secondary Verification (多图笔记)

额外验证一篇真实多图笔记的落盘能力：

- **Note ID**: `6aa62ab4000000001001fc4f` ("一个月 20 刀的 Antigravity CLI，可能被很多人低估了")
- **Media**: 成功并发下载全部 15 张图片 (`image_01.jpg` ~ `image_15.jpg`)，全部校验通过。

---

## 7. Failures & Edge Cases

- **测试期间捕获并解决的问题**:
  1. **匿名 Session 识别**: 小红书前端会在访客状态下派发空/临时 Cookie，初版判断有误。已修正为严格校验 `guest === false && redId`。
  2. **Token Refusal (404)**: 小红书笔记详情访问必须携带有效 `xsec_token`。已在采集层打通从收藏列表到笔记详情的 Token 自动传递与本地缓存机制。
  3. **空列表防御**: 未能证实页面存在显式“暂无收藏”凭证时，拒绝输出成功空列表，严格保障 Fail-Closed。
- **当前实机 Smoke Test 运行**: **0 失败**。

---

## 8. Known Limitations

1. **首次登录依赖人工扫码**: 当前采用真实持久化 Session 方案，初次必须通过 `xhs-ingest login` 扫码登录。
2. **CDN 媒体签名时效性**: 视频与高清图片签名带有 TTL 时间戳，必须在获取详情后立即下载落盘。
3. **高频风控限制**: 小红书对高频列表翻页存在滑块验证码风险，后续进入批量采集阶段需要增加自适应退避与节流控制。
