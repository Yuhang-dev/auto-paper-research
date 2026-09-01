# LLM-Wiki Research Harness V0

本项目用 Markdown/YAML 作为知识与过程记录的 source of truth，用 Skill
规定 Agent 如何搜索、摄取、链接和验证论文，用确定性脚本处理可重复的
初始化、归一化、去重、指标与校验工作。

当前首期主题：

> 稀疏化模型在长上下文领域的性能与瓶颈

主题边界与待验证的非共识假设见：

- `research/long-context-sparse-models/scope.md`

完整的模块技术栈、系统流程、总体架构和 Harness 内部实现见：

- [`docs/REVIEW_FIRST_HARNESS.md`](docs/REVIEW_FIRST_HARNESS.md)：面向科研综述的 Fast/Durable 双环、命令与证据边界
- [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md)
- [`docs/PARAMETERS_AND_METRICS.md`](docs/PARAMETERS_AND_METRICS.md)：全部可调参数、计算指标、公式与内部限制
- [`docs/CANARY_AND_EVALUATION.md`](docs/CANARY_AND_EVALUATION.md)：隔离联网测试、硬边界与分阶段验收
- [`docs/DONE_CRITERIA_ACTIVATION_CHECKLIST.md`](docs/DONE_CRITERIA_ACTIVATION_CHECKLIST.md)：正式 DoneCriteria 激活前审核项
- [`docs/OPENAI_COMPATIBLE_MODEL.md`](docs/OPENAI_COMPATIBLE_MODEL.md)：本地 OpenAI-compatible 模型接入与能力要求
- [`docs/RELEASE_V0.1.0_ALPHA2.md`](docs/RELEASE_V0.1.0_ALPHA2.md)：本地模型接入预发布包、迁移说明与异机测试步骤
- [`docs/RELEASE_V0.1.0_ALPHA1.md`](docs/RELEASE_V0.1.0_ALPHA1.md)：首个异机测试包的内容、安装、Canary 与已知限制

第一轮检索计划见：

- `research/long-context-sparse-models/search-runs/v0-discovery.yaml`

## LangGraph Research Harness

当前默认科研综述路径已经与知识库填充路径分离：

```text
Fast Research Loop
问题 → 多源检索 → source-role 平衡 Skim → 临时技术地图
     → Gap 驱动补搜 → Deep Read → EvidenceCard → 带引用综述

人工批准后：PromotionManifest → Full Ingest → staged-for-wiki
                         → 显式 publish-staged → 正式 Wiki
```

标准漏斗是 `50 → 20 Skim → 10 Deep Read → 最多 6 篇晋升`，Smoke 是
`8 → 4 → 2 → 0`。Fast Loop 使用确定性 Wiki ID 索引去重；正式 Wiki 由 promotion
和 publish 阶段更新。论文发现默认使用 DeepXiv；设置
`SEMANTIC_SCHOLAR_API_KEY` 后，系统为进入 Deep Read 的少量论文补充 citation 和
外部 ID。Deep Read 的来源策略优先保留原始论文：Smoke 2 篇，standard 至少 6 篇；
ResearchGate、Academia.edu 等镜像用于导航，正式证据来自原始论文或官方来源。
每轮额外生成 `coverage-matrix.yaml` 和 `research-gaps.yaml`；语义相似来源先保存为
关系候选，只有精确 DOI/arXiv/repository/URL 身份会自动合并。
使用说明见 `docs/REVIEW_FIRST_HARNESS.md`。

长时间运行的 Review 命令会在 stderr 使用单行心跳显示当前阶段、完成数、当前来源、
耗时和循环点号。阶段切换时只保留一条完成记录，不会持续追加点号。最终 JSON 仍从
stdout 输出。相同状态同时写入
`.harness/review-runs/<run-id>/state/progress.json`，`research review status` 会返回
该快照。可用 `HARNESS_PROGRESS=auto|live|plain|off` 调整终端显示；状态文件始终更新。

项目现在用 LangGraph 作为编排层。确定性的 Wiki parser、resolver、validator、
query 与 DeepXiv 脚本仍是普通 Python 能力。系统分为 inner tool loop、Skill
执行层和 outer research controller 三层。

```text
User task
   ↓
prepare turn
   ↓
agent ── no tool call ──→ END
   ↓ tool call
ToolNode
   ↓
observe result / count failure
   └────────────────────→ agent
```

外层保留一个确定性、只读的 V0 诊断步：

```text
START
  ↓
inspect_research        从 Wiki 与 search-run YAML 计算快照
  ↓
evaluate_gaps          生成可测量 Gap 候选
  ↓
measure_progress       计算新增论文/方法/claim/实验等 progress score
  ↓
check_done             Coverage + Quality + Saturation + Budget
  ↓
decide_next_action     在有限动作集合中确定下一步
  ↓
END                    返回一个控制决策
```

V1 在同一组确定性 contracts 上接入了可达的研究执行链：

```text
inspect → evaluate → check_done
                     │ continue
                     ▼
                  decide
                     ▼
            execute_action
              ├─ search ─────────→ plan / DeepXiv / screen
              ├─ ingest ─────────→ acquire PDF / extract / publish draft
              ├─ verify ─────────→ source checks / guarded promotion
              ├─ revise_evidence → bounded correction / reset to draft
              └─ analyze_claims ─→ needs-review assessment
                     ▼
                  inspect
                     ▼
             measure_progress
                     ▼
       update attempts by (gap, action)
                     └──────────────────↺
```

系统采用显式 action table，在 `search / ingest / verify / revise_evidence /
analyze_claims` 中选择动作并加载对应 Skill。模型负责 query 语义规划、候选
筛选、论文结构化抽取、证据语义比较和 claim 条件对齐；程序负责 ID、路径、下载边界、
页码和数值预检、schema、状态迁移、原子发布与回滚。网络授权、凭证和前置条件进入
执行前检查；检查结果记录在结构化 `ActionResult` 中。

状态分为三层：

```text
Markdown Wiki                领域知识真源
SQLite Store                 跨 thread 的显式研究笔记
SQLite Checkpointer          单个 thread 的消息、工具调用与运行状态
```

SQLite 默认位置：

```text
<当前仓库>\.harness\research-harness.sqlite3
```

`HARNESS_DB_PATH` 可以位于任意可写盘符；配置层要求使用持久化文件以及 `.db`、
`.sqlite` 或 `.sqlite3` 后缀。SQLite、WAL 和 SHM 文件均已加入 `.gitignore`。

每轮模型上下文由以下内容组成：

```text
system policy
  + 与当前问题相关的少量 research memory
  + 按 token budget 保留的最近消息
```

SQLite 保存完整可恢复线程状态；模型每轮接收经过预算裁剪的上下文。

### SkillRegistry V0

Harness 启动时会确定性扫描：

```text
skills/*/SKILL.md
```

并构建只读的 `SkillRegistry`。每个 `SkillSpec` 包含：

```text
name / description / instructions
Skill 根目录与 SKILL.md 路径
references / assets / scripts / agents 资源清单
```

`SKILL.md` 的 frontmatter 和正文会被解析；supporting resources 按需加载。
Registry 校验目录名、Skill 名、frontmatter 和资源路径，并把资源访问限定在 Skill
根目录。

当前实现范围：

```text
运行中：SkillRegistry + 按需资源读取 + Durable executor + 独立 Review synthesis graph
后续项：通用 SkillExecutor / 浏览器工作流 / 自动 PPT synthesis
```

Registry 负责发现和检查。Outer Loop 使用显式 action table：`search`
绑定 query planning、DeepXiv 和候选筛选；`ingest` 绑定受控 PDF 交接和
`PaperIngestPipeline`；`verify` 绑定来源复核与 guarded lifecycle transition；
`revise_evidence` 处理 verifier 已定位的事实矛盾或 locator 缺陷，最多两轮，修订后退回
`draft` 并等待独立复验；
`analyze_claims` 消费 verified evidence 并创建待独立复核的 assessment。每个动作由
程序绑定确定的 Skill 和脚本。

### Research contracts and Done gate

外层控制使用以下严格数据契约：

- `ResearchSnapshot`：分别计算 search-run 的 candidate facet coverage 与 Wiki 的
  evidence facet coverage，以及证据、上下文长度、工程指标和来源哈希；
- `ResearchGap`：稳定 ID、Gap 类型、可观测原因、证据、优先级、推荐动作和显式
  `blocking` 标志；
- `ResearchDecision`：有限动作、目标 Gap、理由和预期信息增益；
- `ResearchActionResult`：动作状态、是否真正尝试、工具调用、变更来源、错误码及
  `positive / negative_research_result / tool_failure / blocked` 语义；
- `ActionAttemptStats`：按 `(gap_id, action)` 记录尝试、无进展、工具失败和
  negative result，防止盲目重复；
- `NonConsensusAssessment`：存放在 Wiki 中的研究产物，合法结果包括
  `supported-consensus / contested / insufficient-evidence`；
- `DoneCriteria`：Coverage、Quality、Saturation 和 Budget 的确定性门槛。

DoneCriteria 0.2 使用 `evidence_facet_coverage` 判断 facet 门槛。候选覆盖用于
路由：候选缺失走 `search`，已有候选但无证据走 `ingest`，已有 draft 证据走
`verify`。完成条件还要求达到逐 context/metric 的证据数量、完成 non-consensus
review，并且 `open_blocking_gap_count == 0`。

`control_passes` 统计观察/评估轮次；`research_iterations` 在动作真正执行时增加，
并用于 `max_research_iterations` 预算。CLI 从实际运行计算 `action_attempted` 和
`tool_calls_delta`。

项目当前阈值在：

```text
research/long-context-sparse-models/done-criteria.yaml
```

该文件当前是 `status: draft`，终止决定由调研负责人审核。负责人确认门槛后将状态
显式改成 `active`，`check_done()` 随即启用自动 `finish`。

当前真实快照显示：1 个 planned search run、8 个 planned query、0 个候选、
1 篇 legacy draft Wiki paper、0 个结构化 experiment/claim/assessment；当前 10 个
facet 的 candidate coverage 与 evidence coverage 均为 `missing`。因此最高优先级
动作是执行已有 Q01–Q08，以真实检索结果驱动下一轮查询。

### Install

建议安装在独立 Conda `(paper-harness)` 环境，项目同时保留版本文件：

```powershell
D:\anaconda3\envs\paper-harness\python.exe -m pip install -r requirements-harness.txt
```

当前锁定：LangChain 1.3.17、LangGraph 1.2.11、
`langgraph-checkpoint-sqlite` 3.1.1、`langchain-openai` 1.6.0 和
PyYAML 6.0.2、Pydantic 2.10.3、`deepxiv-sdk` 1.0.0、Tavily Python SDK、
Requests 2.x 和 pypdf 6.16.2。

### Configure

首次运行时复制配置模板：

```powershell
Copy-Item .env.example .env.local
notepad .env.local
```

`.env.local` 集中保存模型 ID、Base URL、DeepSeek、DeepXiv、Semantic Scholar、
Tavily 和可选 GitHub 凭证。程序启动时自动加载该文件；当前 PowerShell 会话中同名
变量具有更高优先级。`.env.local` 已加入 `.gitignore`。

模板默认采用 DeepSeek 官方配置：

```text
Fast:      openai:deepseek-v4-flash
Reasoning: openai:deepseek-v4-pro
Base URL:  https://api.deepseek.com
```

使用本地 OpenAI-compatible 服务时，在 `.env.local` 中替换模型 ID 和两组 Base URL。
Harness 显式构造 `ChatOpenAI(model, base_url, api_key)`；模型 ID 使用
`openai:<served-model-id>`。`--allow-network` 授权 socket I/O。完整 endpoint 说明见
`docs/OPENAI_COMPATIBLE_MODEL.md`。

DeepXiv SDK 在获准的正式检索中延迟导入；其 tiktoken 编码缓存默认
定向到 `<当前仓库>\.harness\tiktoken-cache`；离线诊断和测试使用本地依赖。

### Run

先运行无网络诊断：

```powershell
D:\anaconda3\python.exe -B -m research_harness doctor
```

正式全流程前先跑 1-query 隔离 Canary：

```powershell
D:\anaconda3\python.exe -B -m research_harness `
  research canary long-context-sparse-models `
  --run-id retrieval-v1 --allow-network `
  --stop-after retrieval --max-actions 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --deadline-seconds 120 --provider-max-retries 0
```

Canary 写入 `.harness/canary/<run-id>/`，正式 Wiki、Search Run 和主 SQLite 保持原值。

执行一个可恢复任务：

```powershell
D:\anaconda3\python.exe -B -m research_harness run `
  "检查 Wiki 中 LongLoRA 的方法和证据缺口" `
  --thread sparse-review-01 --allow-network
```

继续使用相同 `--thread` 就会恢复该线程。交互模式：

```powershell
D:\anaconda3\python.exe -B -m research_harness chat `
  --thread sparse-review-01 --allow-network
```

`--allow-network` 开启 socket I/O，包括 localhost 模型调用；DeepXiv 正式检索同时
读取当前进程的 `DEEPXIV_TOKEN`：

```powershell
D:\anaconda3\python.exe -B -m research_harness run `
  "先预览 v0-discovery；确认计划后执行仍处于 planned 的查询" `
  --thread discovery-01 --allow-network
```

### Inspect state and memory

```powershell
D:\anaconda3\python.exe -B -m research_harness state `
  --thread sparse-review-01

D:\anaconda3\python.exe -B -m research_harness memories

D:\anaconda3\python.exe -B -m research_harness memories `
  --query "RULER context"

D:\anaconda3\python.exe -B -m research_harness tools

D:\anaconda3\python.exe -B -m research_harness skills list

D:\anaconda3\python.exe -B -m research_harness skills show search-paper

D:\anaconda3\python.exe -B -m research_harness skills read `
  ingest-paper references/evidence-policy.md

D:\anaconda3\python.exe -B -m research_harness research inspect `
  long-context-sparse-models

D:\anaconda3\python.exe -B -m research_harness research evaluate `
  long-context-sparse-models

D:\anaconda3\python.exe -B -m research_harness research step `
  long-context-sparse-models --thread outer-v0

# 离线检查完整 V1 路由；会在任何文件写入前以 network-disabled 停止
D:\anaconda3\python.exe -B -m research_harness research run `
  long-context-sparse-models --thread outer-v1
```

审核 Q01–Q08 后，显式授权 DeepXiv 网络执行：

```powershell
$env:DEEPXIV_TOKEN = Read-Host -Prompt "DeepXiv Token" -MaskInput

D:\anaconda3\python.exe -B -m research_harness research run `
  long-context-sparse-models --thread outer-v1 --allow-network
```

如果运行中按下 Ctrl+C，CLI 会在 Wiki 批次回滚和 LangGraph 上下文关闭后输出：

```text
Interrupted.
Thread: outer-v1
Checkpoint preserved.
Resume with the same --thread.
```

显式恢复命令默认采用 `replan`：保留同一 thread 的计数和历史，但先重新读取 Markdown/YAML
真源，再从最新 Gap 继续。这是包含外部 source of truth 的研究任务的推荐模式：

```powershell
D:\anaconda3\python.exe -B -m research_harness research resume `
  long-context-sparse-models --thread outer-v1 --allow-network
```

需要继续中断时的 LangGraph pending node 时，使用精确 checkpoint 模式：

```powershell
D:\anaconda3\python.exe -B -m research_harness research resume `
  long-context-sparse-models --thread outer-v1 --mode checkpoint --allow-network
```

`checkpoint` 模式内部调用 `graph.invoke(None, config)`；它要求 checkpoint 确实存在 pending
node，并要求当前 `--allow-network` 与中断时的授权完全一致。已正常结束的 checkpoint 应使用
`replan`，切换网络授权也应使用 `replan`。

该命令会由 Harness 自己记录动作、工具调用和进展。候选筛选会把最多 3 个 core paper
变为 `selected-for-ingest`。对选中的 arXiv candidate，在同一次 `--allow-network`
授权下，Ingest 可以受控下载公共 PDF 到当前仓库：

```yaml
local_pdf_path: sources/papers/arxiv-<id>.pdf
```

也可以人工放置 PDF 并设置该路径。自动下载面向显式选中的 arXiv candidate，并限制
HTTPS host、文件类型、目标目录和最大字节数。远程 query planning、screening、ingest、
verify、revise_evidence 与 analyze_claims 共用本次 `--allow-network` 授权；模型配置
完成后，语义动作进入 executor capability set。发布完成后 Harness 会把 candidate 改为 `ingested` 并
记录 canonical paper ID，避免下一轮重复处理同一 handoff。

Research memory 保存紧凑的 `observation / decision / preference / open-question`。
带 evidence ID 的内容可进入证据流程；其余内容保留为 `unverified-note`。

## Current pipeline

```text
research question
  -> search-paper
  -> validated query plan (existing or gap-directed)
  -> DeepXiv retrieval
  -> candidate search-run YAML
  -> structured relevance screening / selected handoff
  -> bounded arXiv PDF acquisition or explicit local PDF
  -> ingest-paper
  -> PaperIngestDraft
  -> shadow Wiki validation
  -> draft paper/method/benchmark/model/claim/experiment pages
  -> verify-evidence
       ├─ verified source records
       └─ actionable needs-review feedback
            -> revise-evidence (only for contradiction/locator defects)
            -> draft corrected record
            -> independent verify-evidence
  -> analyze-claims
  -> needs-review non-consensus assessment
  -> independent verify-evidence
  -> verified assessment or explicit unresolved result
```

检索结果首先形成 `candidate`。论文正文和证据流程将入选 candidate 转换为可验证的
Wiki claim。

## Skills

- `skills/search-paper/`：检索规划、DeepXiv 发现、候选集、coverage 与 gap；
- `skills/ingest-paper/`：把选中的论文转换为结构化 Wiki 页面；
- `skills/verify-evidence/`：来源、locator、实验条件和生命周期复核；
- `skills/revise-evidence/`：对 verifier 指定的 method/claim 做最多两轮受限修订；
- `skills/analyze-claims/`：比较 verified claims，生成共识/争议/证据不足 assessment。

`ingest-paper` 已迁移到 Wiki V0.2，并通过严格 Pydantic Draft、页级 locator、typed
entity 复用和 guarded Writer 接入 Outer Loop。模型输出结构化 Draft；Writer 在 shadow
Wiki 重建图并通过零 schema error 校验，再逐页原子发布。普通异常以及可捕获的
`KeyboardInterrupt` / `SystemExit` 会回滚整个页面批次并继续抛出原异常。重复摄取同一
Draft 为 no-change；人工 source page 保持原值。

## Wiki Engine V0.2

Wiki 保存调研 Harness 的领域持久状态，SQLite 保存对话与运行记忆。Markdown/YAML
页面是真源；`wiki/_generated/` 包含可删除、可重建的 JSON 索引。

当前 schema 支持八类实体：

```text
paper  method  experiment  claim  concept  benchmark  model  assessment
```

`paper / experiment / claim` 可用 `facets` 显式声明其证据覆盖维度。verified 实体
形成 `covered`，draft 实体形成 `partial`。`assessment` 作为 non-consensus 研究真源，
结果可以是 consensus、contested 或 insufficient-evidence。

实体以稳定 ID 标识，例如 `paper:longlora`；正文链接使用
`[[paper:longlora]]`，有语义的科研关系写入 YAML `relations`。完整合约见
`wiki/_meta/schema.yaml` 与 `wiki/_meta/relation-types.yaml`。

构建索引和验证：

```powershell
conda run -n base python -B -m tools.wiki index
conda run -n base python -B -m tools.wiki validate
```

搜索、实体查看和图查询：

```powershell
conda run -n base python -B -m tools.wiki search "sparse attention"
conda run -n base python -B -m tools.wiki show paper:longlora --body
conda run -n base python -B -m tools.wiki backlinks paper:longlora
conda run -n base python -B -m tools.wiki neighbors paper:longlora
conda run -n base python -B -m tools.wiki related paper:longlora --depth 2
```

按实验条件过滤：

```powershell
conda run -n base python -B -m tools.wiki query `
  --type experiment --benchmark ruler --min-context 32768
```

统计与严格验证：

```powershell
conda run -n base python -B -m tools.wiki stats
conda run -n base python -B -m tools.wiki validate --strict
```

普通 `validate` 将 ERROR 映射为非零退出码；`--strict` 同时把 WARNING 映射为非零
退出码。`tools.wiki` CLI 提供查询与校验能力；Harness 的 ingest、verify 和 analyze
流程通过 guarded Writer，在 shadow validation 通过后原子修改正式 Wiki 页面。

## DeepXiv environment

项目直接使用 Conda `(base)` 中的 `deepxiv-sdk`。

检查安装：

```powershell
conda run -n base python -m pip show deepxiv-sdk
```

凭据通过 `.env.local` 的 `DEEPXIV_TOKEN` 加载，仓库、YAML、命令参数和日志保存
非秘密配置：

```powershell
notepad .env.local
```

随后在同一个 shell 执行：

```powershell
conda run -n base python skills/search-paper/scripts/deepxiv_search.py `
  --run research/long-context-sparse-models/search-runs/v0-discovery.yaml
```

## Search scripts

初始化新主题：

```powershell
conda run -n base python skills/search-paper/scripts/new_search_run.py `
  --topic-slug "<topic-slug>" `
  --question "<research-question>"
```

离线预览查询：

```powershell
conda run -n base python skills/search-paper/scripts/deepxiv_search.py `
  --run "<search-run.yaml>" --dry-run
```

验证与确定性指标复算：

```powershell
conda run -n base python skills/search-paper/scripts/validate_search_run.py `
  "<search-run.yaml>" --fix-metrics
```

离线测试：

```powershell
conda run -n base python -B -m unittest discover `
  -s skills/search-paper/scripts/tests -v
```

Wiki Engine 测试：

```powershell
conda run -n base python -B -m unittest discover `
  -s tools/wiki/tests -v
```

LangGraph Harness 测试（无真实模型、无网络）：

```powershell
D:\anaconda3\python.exe -B -m unittest discover `
  -s research_harness/tests -v
```

## LoopEngineer rule

单次检索噪声记录在 run 内。稳定、可复现、跨 run 重复的问题升级为：

- Error Book 条目；
- Skill 决策规则；
- 校验器；
- 可复用脚本或函数。

Skill 修改由重复证据和人工确认共同触发。

## Deferred Wiki batch

较大规模的首轮调研使用 `research canary ... --defer-wiki`：先检索、筛选并把多篇
论文保存为结构化 staged drafts，Wiki 保持当前版本。随后用
`research publish-staged ... --target canary` 在隔离 Wiki 验收；确认后再显式使用
`--target formal`。完整 PowerShell 命令见
`docs/CANARY_AND_EVALUATION.md` 的“延迟 Wiki 的批量调研”。
