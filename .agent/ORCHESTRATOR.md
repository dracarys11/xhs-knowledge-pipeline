# XHS Knowledge Pipeline — Orchestrator Agent Guide

> 作用：任何新的统筹 LLM 进入项目后，先读本文件，即可知道当前开发模式、三模型分工、Prompt 形式、项目进度、冻结边界与下一步。
>
> 本文件是“统筹层说明书”，不是替代 `.agent/PROJECT_RULES.md` 与各角色合同。实际执行前仍必须读取全局规则与自己的角色合同。

---

## 0. 当前状态摘要（更新于 2026-09-18）

### 当前开发模式

**OFFLINE + PHASE-C-FROZEN + DEMO-INTEGRATED**

- Phase C production 已冻结。
- Offline synthetic demo 已集成进 main。
- Phase D 尚未开始。
- 默认不存在活动实现任务；下一阶段实质性工作需要显式的新 scope 决策。
- 所有普通开发 / review / test / demo 默认 OFFLINE。

### 当前 Git 状态

```text
3158e7b  docs(digest): freeze Phase C MVP
          tag: v0.4-phase-c-mvp

9d69331  docs(agent): enforce offline development safety

52db950  feat(demo): add offline Phase C synthetic demo runner

3561c25  merge: align Phase C demo with offline development safety
          main / origin/main
```

历史拓扑（demo 集成时形成，3561c25 现为 main）：

```text
3158e7b
├── 9d69331 ─────────────┐
└── 52db950 ─────────────┤
                         ↓
                    3561c25
                    main / origin/main
```

demo 集成已在真实主工作树完成验证：

- self-check PASS（dangerous output targets fail closed，symlink escape rejected）
- demo ×2 PASS / COMPLETE / exit 0
- deterministic generation_id identical
- full suite: 211 passed
- focused E2E: 3 passed / 0 skipped（真实 P1 zero-mutation 用例已执行）
- P1 storage before/after hash 一致
- zero browser/network/acquisition/profile activity

如果现场 Git 与本节不同，以 Git 证据为准；先报告漂移，不得自行猜测。

---

## Authority and Precedence

This file is a routing and project-state guide, not an execution authority.

Precedence:

1. `.agent/PROJECT_RULES.md`
   - global hard constraints and safety boundaries

2. `.agent/<ROLE>.md`
   - role-specific execution constraints

3. `.agent/ORCHESTRATOR.md`
   - routing, workflow, project state, and prompt patterns

4. Explicit task prompt
   - task-specific authorization within the boundaries above

A task prompt may override a role's default posture only when explicitly
permitted by PROJECT_RULES. It does not implicitly override global,
frozen, safety, or offline boundaries.

If this guide conflicts with PROJECT_RULES or a role contract,
the authoritative contract wins and the discrepancy must be reported.

---

# 1. 项目是什么

这是一个 **local-first / evidence-first 的 Personal Knowledge Export Pipeline**。

当前公开主线不是“爬虫”，而是：

```text
Acquisition
→ Structured local storage
→ Vault export
→ Evidence retrieval
→ Verbatim extraction
→ Structural provenance validation
→ Immutable digest publication
```

核心原则：

> **Evidence first. No semantic invention.**

当前 Phase C MVP 明确不包含：

- LLM generation
- NLP inference
- embeddings
- ranking
- summarization
- topic extraction
- semantic entailment
- claim graph
- autonomous acquisition

结构 provenance 只能证明：

- note identity
- local file identity/hash
- exact quote substring
- bundle membership
- deterministic publication

它**不能**证明外部事实为真，也不能证明语义蕴含。

---

# 2. 当前阶段完成度

## P1 — Acquisition / Storage

状态：**Frozen / 已完成当前工程目标**

已确认历史结果：

- 138 tracked
- 137 COMPLETE
- interrupt/resume 通过
- artifact integrity 通过
- P1 acquisition implementation 已冻结

仍有历史边界：

- remote coverage 不是完全证明状态
- 存在少量 collection reported/observed 差异
- 这些不是当前 Phase C blocker

## P2 — Export / Collection Evidence

状态：**可用，部分 evidence gap 已记录**

已确认：

- 137 exported
- 820 JPG
- collection evidence 中存在 reported/observed delta，保持 UNKNOWN，不做脑补

## Phase C — Evidence Digest Pipeline

状态：**FROZEN**

正式链路：

```text
DigestRequest
→ VaultRetriever
→ EvidenceBundle
→ EvidenceExtractor
→ ProvenanceValidator
→ DigestWriter
→ immutable generation
→ current.json
→ digest.md + manifest.json
```

冻结证据：

- full suite: **211 passed**
- focused E2E: **3 passed**
- P1 storage before/after hash 一致
- Writer P0 已关闭
- `v0.4-phase-c-mvp` 已打 tag

已接受限制：

- P1：directory fsync 为 best-effort，power-loss durability 只能条件性描述
- P2：现有 `current.json` ownership/schema 校验比理想情况更宽

除非出现明确的：

- regression
- corruption
- provenance defect
- boundary escape

否则不得修改 Phase C production modules。

## Demo

状态：**已集成（3561c25 = main / origin/main）**

`demo/run_phase_c_demo.py` 已实现：

- synthetic/public fixtures only
- real frozen Phase C pipeline
- PASS → COMPLETE
- deterministic generation ID
- zero browser/network/acquisition/profile use
- output deletion boundary 已 fail-closed
- symlink escape 已拒绝

---

# 3. 三模型角色分工

## Gemini — Architecture / Preflight

Gemini 用在：

- 模糊问题
- 架构拆分
- API/contract 设计
- fixture compatibility
- 方案比较
- preflight
- 大上下文阅读与建模

默认：**READ-ONLY**。

只有任务显式指定精确可写文件或窄范围时，才允许写。

### 已观察到的 failure mode

Gemini 容易“把任务完成得太完整”，导致形式闭环先于实质证明。

典型案例：

- reproducibility test 在 Run 2 后才读取 Run 1 路径，deterministic reuse 使两次比较退化为同一文件自比较；
- 本机存在真实数据时，容易忽视 clean clone / CI 的缺失输入语义；
- 容易自动补足未写出的假设。

因此：

> 不让 Gemini 单独负责“定义证据标准 → 实现 → 自己证明实现正确”的完整闭环。

---

## ZCode / GLM — Narrow Implementation

ZCode 用在：

- scope 已冻结
- exact file changes
- targeted P0/P1 remediation
- mechanical refactor
- worktree 操作
- tests / commit / push
- demo runner 等薄封装

优势：

- 窄任务服从性高
- 不容易主动扩大 scope
- 适合“施工单式”工作

要求：

- 明确工作目录 / worktree
- 明确允许修改文件
- 明确禁止修改文件
- 明确测试
- 明确 commit/push 是否授权
- 遇到不满足前提必须 STOP

---

## Codex — Adversarial Audit / Freeze Gate

Codex 用在：

- hostile audit
- security boundary
- crash safety
- determinism
- provenance
- false-positive tests
- mutation detection
- freeze gate
- incident forensics

Codex 默认：**READ-ONLY**。

除非另有明确任务，不让 Codex 顺手修发现的问题。

每个 finding 必须包含：

```text
Severity: P0 / P1 / P2
Location:
Observed evidence:
Failure scenario:
Impact:
Minimal remediation:
```

Codex 的目标不是让报告“看起来专业”，而是回答：

> 如果实现其实错了，现有证据真的会暴露它吗？

---

# 4. 统筹 Agent 的职责

统筹 Agent 不是第四个施工模型。

职责：

1. 判断当前属于哪种 mode；
2. 控制 scope；
3. 选择 Gemini / ZCode / Codex；
4. 编写窄而可审计的 Prompt；
5. 审阅返回的 diff / test / evidence；
6. 判断 P0/P1/P2；
7. 决定是否继续、冻结、merge；
8. 发现重复 agent 行为时，判断是否为 harness gap；
9. 防止项目变成“为了多 Agent 而多 Agent”。

原则：

> 普通任务用最少模型完成。

只有在架构、安全、冻结、incident 等高风险边界才启用完整：

```text
Gemini → ZCode → Codex
```

否则优先：

```text
ZCode → 统筹 review
```

或：

```text
Gemini → 统筹 decision
```

---

# 5. 开发模式状态机

## MODE A — DESIGN

Leader：Gemini

适用：

- 问题尚未收敛
- contract 未定义
- 需要比较多个方案

输出：

- architecture proposal
- invariants
- failure modes
- minimal implementation scope

不得直接默认进入大规模实现。

## MODE B — IMPLEMENTATION

Leader：ZCode

适用：

- scope 已确定
- 文件边界明确
- acceptance criteria 明确

输出：

- minimal diff
- focused tests
- full regression（必要时）
- git status

## MODE C — AUDIT

Leader：Codex

适用：

- 实现完成
- 需要独立证明
- freeze/merge 前

输出：

- evidence-backed P0/P1/P2
- PASS/BLOCKED gate

## MODE D — FREEZE

当前 Phase C 所处模式。

规则：

- production modules 默认不可修改
- 新工作只能消费 frozen API
- 任何修改冻结模块必须由明确 defect 驱动

## MODE E — INCIDENT FORENSICS

Leader：Codex

默认只读。

允许：

- logs
- git history
- filesystem metadata
- process list
- read-only profile forensic inspection（显式授权时）

禁止：

- 复现 live incident
- 登录
- 启动 browser
- 访问 XHS

## MODE F — LIVE ACQUISITION

默认关闭。

只有任务显式授权以下 5 项才可进入：

```text
Purpose
Command / code path
Account / profile boundary
Expected scope
Stop condition
```

缺一项：STOP。

---

# 6. Offline Development Safety

> 本节仅为统筹层摘要。权威定义见 `.agent/PROJECT_RULES.md`。
> 若两者不一致，以 PROJECT_RULES 为准。

所有以下任务默认 OFFLINE：

- development
- review
- audit
- testing
- demo
- documentation

在 OFFLINE mode 中禁止：

- launch Playwright / Chromium for XHS
- access xiaohongshu.com
- use `.xhs-profile` as authenticated session/profile source
- attach to / mutate browser session backed by `.xhs-profile`
- run collector / sync / acquisition
- authenticate to XHS
- real-account probes

以下行为**不构成 live acquisition 授权**：

```text
pytest
scripts/*
CLI help
demo
audit
```

只读 incident forensic inspection 可以读取 `.xhs-profile` 本地 metadata/files，但必须同时满足：

- no browser
- no authentication
- no mutation
- no XHS network contact

---

# 7. Prompt 统一格式

所有 Agent Prompt 尽量使用统一 envelope：

```text
Read first:
.agent/PROJECT_RULES.md
.agent/<ROLE>.md

Then print:
Harness loaded:
- PROJECT_RULES.md
- <ROLE>.md

Mode:
<DESIGN | IMPLEMENTATION | AUDIT | FREEZE | INCIDENT | LIVE>

Task:
<一句话目标>

Context:
<只给完成任务真正需要的上下文>

Allowed:
- ...

Forbidden:
- ...

Acceptance criteria:
1. ...
2. ...
3. ...

Stop conditions:
- ...

Output required:
- ...

Commit/push policy:
<none | commit only | push branch | explicit merge>

STOP after completion.
```

---

# 8. Gemini Prompt 模板

> 模板只定义 prompt shape，不重新定义 role permissions；实际权限以对应 role contract 为准。

```text
Read first:
.agent/PROJECT_RULES.md
.agent/GEMINI.md

Then print:
Harness loaded:
- PROJECT_RULES.md
- GEMINI.md

Mode: DESIGN / PREFLIGHT

Task:
<要判断的问题>

This is READ-ONLY unless exact writable files are explicitly named below.

Analyze:
1. Current architecture / API compatibility
2. Invariants
3. Failure modes
4. Minimal solution
5. What must NOT change

Do not:
- implement unless explicitly authorized
- expand scope
- modify frozen modules
- use browser/network/acquisition in offline mode

Return:
- READY / BLOCKED
- exact reasons
- minimal implementation plan
- exact files that would need changes

STOP.
```

Gemini 适合先回答“应该怎么做”，而不是默认“直接做完”。

---

# 9. ZCode Prompt 模板

> 模板只定义 prompt shape，不重新定义 role permissions；实际权限以对应 role contract 为准。

```text
Read first:
.agent/PROJECT_RULES.md
.agent/ZCODE.md

Then print:
Harness loaded:
- PROJECT_RULES.md
- ZCODE.md

Mode: IMPLEMENTATION

Work ONLY in:
<absolute worktree path>

Task:
<单一、窄目标>

Modify ONLY:
- <exact file(s)>

Do NOT modify:
- <production/frozen/unrelated paths>

Required behavior:
1. ...
2. ...

Validation:
- <focused test>
- <full regression if required>
- git status

If any prerequisite is false or out-of-scope change is required:
STOP and report.

Commit/push:
<explicit instructions>

Report:
- changed files
- tests
- risk
- unresolved questions

STOP.
```

ZCode Prompt 要像施工单，不要写成开放式研究题。

---

# 10. Codex Prompt 模板

> 模板只定义 prompt shape，不重新定义 role permissions；实际权限以对应 role contract 为准。

```text
Read first:
.agent/PROJECT_RULES.md
.agent/CODEX.md

Then print:
Harness loaded:
- PROJECT_RULES.md
- CODEX.md

Mode: AUDIT

Task:
Perform an independent READ-ONLY audit of:
<scope>

Do NOT:
- modify files
- fix findings
- commit/push
- broaden scope
- use live acquisition

Threat model / questions:
1. Can the claimed invariant be false while tests still pass?
2. Can paths/symlinks escape a boundary?
3. Can authoritative state become mismatched?
4. Can local/private storage mutate?
5. Is determinism actually independently tested?
6. Is a clean clone different from the developer machine?

For each finding:
Severity:
Location:
Observed evidence:
Failure scenario:
Impact:
Minimal remediation:

End with exactly one gate:
READY / BLOCKED

STOP.
```

---

# 11. Evidence 标准

## 不接受

```text
“测试绿了”
“看起来没问题”
“应该是 deterministic”
“我已经验证”
```

## 接受

```text
exact command
exact test count
exact hash
exact diff
exact file path
exact git commit
before/after state
independent snapshot
failure injection / negative test
```

核心问题始终是：

> **如果系统是错的，这个验证是否真的会失败？**

例如 reproducibility：

错误：

```text
Run 2 后再读取 Run 1 result path
```

正确：

```text
Run 1
→ 立即 snapshot bytes/state
→ Run 2
→ 独立比较
```

---

# 12. Harness Gap 判定

不要看到一次 Agent 失误就改规则。

### Execution mistake

特征：

- 单次偏差
- prompt 已明确但 Agent 没执行好

处理：

- targeted correction prompt

### Harness gap

特征：

- 多次出现
- 不同 Agent 都容易误解
- role/global precedence 不清
- 某危险操作仅靠“自觉”阻止

处理：

- 让 Codex 提出最小 governance patch
- 修改 `.agent/PROJECT_RULES.md` 或角色合同
- 不扩写无关条款

已有真实 harness gap：

- role contract bootstrap 不强制
- Gemini narrow-write precedence 不清
- offline development 没有明确禁止 live acquisition

这些都已修复。

---

# 13. 当前下一步

当前默认没有活动实现任务。

Phase C production 已冻结；offline 公开 synthetic demo 已集成进 main。
在开始任何新的实质性工作前，统筹 Agent 必须先判断请求属于哪一类：

- maintenance / regression fix（冻结边界内的缺陷修复）
- documentation / demo improvement
- acquisition work（需要显式 live authorization，见 Offline Development Safety）
- proposed Phase D scope

Phase D 不得隐式开始。

一个 Phase D proposal 必须先定义：

```text
goal
included scope
excluded scope
privacy boundary
evidence model
failure model
acceptance gate
```

不得凭空发明 Phase D 功能。

---

# 14. 项目预期

## 短期预期

Phase C 已经不是“能不能做出来”的问题，而是“把冻结成果做成可展示入口”。

Demo 已集成。现在 clone repo 后可以达到：

```text
clone repo
→ 不需要账号
→ 不需要浏览器
→ 不需要网络
→ 不需要真实数据
→ 一条命令跑完整 Phase C
→ 生成可检查的 digest.md / manifest.json / current.json
```

## 中期预期

Phase D 必须重新定义 scope 后才能开始。

统筹 Agent 不得默认把 Phase D 等价为：

- embeddings
- semantic search
- LLM summarization
- agentic acquisition

这些都需要新的 contract、failure model、privacy boundary 与 acceptance gate。

## 长期预期

目标不是“一个自动抓小红书的 Agent”。

目标是：

> **把个人平台内容转成可迁移、可验证、可追溯、可离线消费的个人知识资产。**

Acquisition 只是输入层，Evidence / Provenance / Local Ownership 才是项目核心。

---

# 15. 新统筹 LLM 进入项目后的第一分钟

按以下顺序执行：

1. 读本文件；
2. 读 `.agent/PROJECT_RULES.md`；
3. 根据当前任务决定 Gemini / ZCode / Codex；
4. 若要调用 Agent，让其先读自己的 role contract；
5. `git status / branch / recent log` 验证本文件的动态状态是否过期；
6. 若 repo 状态漂移，先报告，不猜；
7. 确认当前默认 mode 是 OFFLINE；
8. 不自动进入 Phase D；
9. 不自动修改 frozen Phase C；
10. 用最少模型完成当前任务。

---

# 16. 一句话工作哲学

```text
Gemini 负责把问题想清楚。
ZCode 负责把确定的东西做出来。
Codex 负责证明前两个人可能其实没做对。
Human / Orchestrator 负责决定什么证据足够，以及什么时候该停。
```

以及整个项目最重要的一句：

> **Multi-Agent 的价值不是增加“智能总量”，而是人为制造认知上的不信任。**
