# P2 Plan — 收藏变为可用的知识资产

状态：**Planning only**。本文件不解除 P1 冻结，也不授权修改采集、状态或恢复实现。

## 1. P2 Objective

用户价值：用户已经收藏了数百篇内容，却难以回忆、浏览和复用。P2 的目标是让**已采集且完成验证**的收藏成为人可浏览、可检索、可追溯来源的本地知识资产。

推进顺序：**P2.1 人可用的导出 → P2.2 元数据增强（另行立项）→ P2.3 检索（另行立项）**。本轮只为 P2.1 定义首个 MVP 和 gate，不预先设计后两阶段。

P1 是输入边界：只能把本地 `COMPLETE` 且产物有效的笔记当作已完成输入；远端收藏枚举完成证明仍为 pending，P2 不得将“可导出条数”表述为“远端全部收藏”。

## 2. Non-goals

- 不做 Embedding、RAG、LLM 摘要/分类、知识图谱或问答。
- 不开发 Obsidian 插件、复杂 UI、云同步或自动发布。
- 不改变 P1 的 Collector、StateStore、SyncRunner、媒体下载及失败/重试语义；不因导出需求重开 P1 冻结。
- 不承诺远端全量覆盖；不为缺失的标签、发布时间或作者字段编造值。

## 3. First MVP

**P2.1 Export Layer**：从现有 `canonical.json` 和已落盘媒体生成独立的 Markdown Vault，用户可将该目录直接作为 Obsidian Vault 打开。导出是 P1 产物的下游投影，不反写 raw、canonical、状态库或现有 `data/` 目录。

每篇笔记以稳定 `note_id` 对应一份 Markdown，包含可读标题与正文、来源链接、作者、采集时间、可用的发布时间/标签，以及能在 Vault 内打开的图片/视频引用。字段仅在来源数据真实存在时呈现，并区分“发布时间”与“采集时间”。失败或不完整笔记不得伪装成完整导出。

## Input Contract

P2.1 Export 只读取：

- State DB 的只读快照，用于确定可导出范围与状态；
- 已验证的 `canonical.json`；
- 已存在的 `assets/` 媒体文件。

P2.1 **不得**调用 Collector、触发 Sync、修复缺失或损坏的产物、修改 P1 State DB 或原始 `data/`。输入不满足条件时，只记录并跳过该笔记；修复仍属于 P1 的独立流程，不是导出的隐式副作用。

### Exporter Input Contract v0.1

**Accepted（全部满足才进入 Vault）**：State DB 中状态为 `COMPLETE`；对应 `canonical.json` 存在且其 `note_id` 与 DB 相同；canonical 媒体清单与 DB 媒体记录一致；清单引用的本地 assets 均存在并通过输入校验。

**Rejected**：`FINAL_FAILED`、`RETRYABLE_FAILED`、`MEDIA_PARTIAL` 及其他非 `COMPLETE` 状态；缺失或无法读取的 canonical；`note_id` 不一致；媒体清单不一致或引用的 asset 缺失/无效。拒绝项不得生成看似完整的笔记。

计数边界：`eligible_complete` 是 DB 快照中的 `COMPLETE` 数；其中输入校验失败的笔记记为 `skipped` 并保留明确原因，实际输出失败记为 `failed`。非 `COMPLETE` 记录不属于输入集合，不计入 `skipped`；它们只体现在 `tracked - eligible_complete` 中。P2.2/P2.3 只能把已成功导出的笔记视为当前知识资产集合。

## 4. Acceptance Criteria

| Gate | 可检查证据 |
|---|---|
| 可浏览 | 用一组包含图文、视频、无媒体和缺字段笔记的已完成样本导出；在 Obsidian 直接打开 Vault，正文、来源链接及本地媒体引用均可使用，无需插件。 |
| 可追溯 | 导出的每篇笔记都能以 `note_id` 找回对应 `canonical.json`；作者、时间、标签只来自已有事实，缺失值不伪造。 |
| 可重复 | 对同一输入重复导出，笔记与媒体不重复、不丢失；中断后重跑仍得到一致的可浏览结果。 |
| Export Manifest | Vault 输出 `notes/`、`assets/`、`export_manifest.json`。Manifest 记录导出时间、源快照的 `tracked`/`eligible_complete`，以及本次 `exported`/`skipped`/`failed`；计数可与只读 State DB 快照及实际 Vault 文件核对，不能暗示远端全量完成。 |
| 视频证据边界 | 视频路径处理可标记为“已实现并经 fixture 测试”；当前真实数据的 820 个媒体均为图片，真实视频导出必须标记为“当前数据集未观察到”，不得写成已实测。 |
| P1 不受影响 | 导出前后 P1 DB 状态计数及 `data/` 下的 raw、canonical、封条和媒体字节不变；P1 回归测试通过。 |
| Vault 隔离 | 在受控测试工作区对比导出前后：Git diff 只涉及 Vault 导出输出；`data/` 文件清单与校验和不变；State DB 文件与逻辑内容不变。即使源数据被 Git 忽略，也必须用独立的文件/DB 校验验证，不能只看 `git diff`。 |
| 诚实的完成声明 | 报告导出成功、失败、跳过的条数；不把本地导出完成解释为远端收藏已完整枚举。 |

只有上述 gate 有可复核证据时，P2.1 才可标记完成。未达标项记录为 pending problem；不因此默认扩大到 P2.2/P2.3。
