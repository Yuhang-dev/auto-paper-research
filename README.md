# LLM-Wiki Research Harness V0

本项目用 Markdown/YAML 作为知识与过程记录的 source of truth，用 Skill
规定 Agent 如何搜索、摄取、链接和验证论文，用确定性脚本处理可重复的
初始化、归一化、去重、指标与校验工作。

当前首期主题：

> 稀疏化模型在长上下文领域的性能与瓶颈

主题边界与待验证的非共识假设见：

- `research/long-context-sparse-models/scope.md`

完整的模块技术栈、系统流程、总体架构和 Harness 内部实现见：

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

外层保留一个不调用模型、不修改来源文件的 V0 诊断步：

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
              └─ analyze_claims ─→ needs-review assessment
                     ▼
                  inspect
                     ▼
             measure_progress
                     ▼
       update attempts by (gap, action)
                     └──────────────────↺
```

系统不使用 LLM Router。Outer Loop 只在显式 action table 中选择 `search / ingest /
verify / analyze_claims`；每个动作再加载对应 Skill。模型负责 query 语义规划、候选
筛选、论文结构化抽取、证据语义比较和 claim 条件对齐；程序负责 ID、路径、下载边界、
页码和数值预检、schema、状态迁移、原子发布与回滚。网络未授权或凭证/前置条件缺失时，
图会返回结构化 `ActionResult` 并安全停止。

状态分为三层：

```text
Markdown Wiki                领域知识真源
SQLite Store                 跨 thread 的显式研究笔记
SQLite Checkpointer          单个 thread 的消息、工具调用与运行状态
```

SQLite 默认位置：

```text
D:\wiki-papersearch\.harness\research-harness.sqlite3
```

配置层会拒绝任何位于 `C:` 的 `HARNESS_DB_PATH`。SQLite、WAL 和 SHM 文件均已
加入 `.gitignore`。

模型上下文不是完整 checkpoint 历史，而是：

```text
system policy
  + 与当前问题相关的少量 research memory
  + 按 token budget 保留的最近消息
```

完整线程状态仍保存在 SQLite，可以恢复，但不会在每轮全部发送给模型。

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

`SKILL.md` 的 frontmatter 和正文会被解析；supporting resources 只在明确读取时
加载，不会全部塞进模型上下文。Registry 会拒绝目录名与 Skill 名不一致、损坏的
frontmatter、路径穿越和逃逸 Skill 根目录的资源。

当前边界刻意保持简单：

```text
已实现：SkillRegistry + 按需资源读取 + search/ingest/verify/analyze_claims 显式 executor
未实现：LLM Skill Router / 通用 SkillExecutor / citation expansion / synthesis subgraph
```

Registry 本身仍只负责发现和检查。Outer Loop 使用显式 action table：`search`
绑定 query planning、DeepXiv 和候选筛选；`ingest` 绑定受控 PDF 交接和
`PaperIngestPipeline`；`verify` 绑定来源复核与 guarded lifecycle transition；
`analyze_claims` 只消费 verified evidence 并创建待独立复核的 assessment。没有通用
Skill 解释器，也不会让模型自行选择任意脚本。

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

DoneCriteria 0.2 只允许 `evidence_facet_coverage` 满足 facet 门槛。候选覆盖只用于
路由：候选缺失走 `search`，已有候选但无证据走 `ingest`，已有 draft 证据走
`verify`。完成条件还要求达到逐 context/metric 的证据数量、完成 non-consensus
review，并且不存在 open blocking gap。

`control_passes` 只统计观察/评估轮次；`research_iterations` 只在动作真正执行时
增加，并用于 `max_research_iterations` 预算。CLI 不再接受人工填写的
`action_attempted` 或 `tool_calls_delta`。

项目当前阈值在：

```text
research/long-context-sparse-models/done-criteria.yaml
```

该文件当前是 `status: draft`。即使所有数值门槛满足，`check_done()` 也不会允许
自动 `finish`；必须由调研负责人审核后显式改成 `active`。

当前真实快照显示：1 个 planned search run、8 个 planned query、0 个候选、
1 篇 legacy draft Wiki paper、0 个结构化 experiment/claim/assessment；当前 10 个
facet 的 candidate coverage 与 evidence coverage 均为 `missing`。因此最高优先级
动作仍是执行已有 Q01–Q08，而不是继续凭空规划查询。

### Install

依赖已安装在 Conda `(base)`，项目同时保留精确版本文件：

```powershell
D:\anaconda3\python.exe -m pip install -r requirements-harness.txt
```

当前锁定：LangChain 1.3.17、LangGraph 1.2.11、
`langgraph-checkpoint-sqlite` 3.1.1、`langchain-openai` 1.6.0 和
PyYAML 6.0.2、Pydantic 2.10.3、`deepxiv-sdk` 1.0.0 和 pypdf 6.16.2。

### Configure

非敏感配置参考 `.env.example`。程序不会自动加载该文件；建议在当前
PowerShell 会话中设置：

```powershell
$env:HARNESS_MODEL = "openai:<本地服务暴露的模型 ID>"
$env:HARNESS_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
$env:OPENAI_API_KEY = Read-Host -Prompt "Local model API key" -MaskInput
```

Harness 会显式构造 `ChatOpenAI(model, base_url, api_key)`，不会再依赖 LangChain 猜测
provider。`HARNESS_MODEL` 必须保留 `openai:` 前缀，冒号后是本地服务 `/v1/models`
返回的精确模型 ID。本地 HTTP 调用仍属于 socket I/O，运行时必须显式传
`--allow-network`。不要把模型或 DeepXiv Key 写入 `.env`、YAML、SQLite research
memory、命令参数或日志。

若仍使用 DeepSeek 官方 endpoint：

```powershell
$env:HARNESS_MODEL = "openai:deepseek-v4-flash"
$env:HARNESS_MODEL_BASE_URL = "https://api.deepseek.com"
$env:OPENAI_API_KEY = Read-Host -Prompt "DeepSeek API Key" -MaskInput
```

DeepSeek 官方域名继续强制只允许 `deepseek-v4-flash`。完整 endpoint 优先级、JSON
mode 和 tool-calling 能力要求见 `docs/OPENAI_COMPATIBLE_MODEL.md`。

DeepXiv SDK 只在获准的非 dry-run 调用中延迟导入；其 tiktoken 编码缓存默认
定向到 `D:\wiki-papersearch\.harness\tiktoken-cache`，离线诊断和测试不会为了
导入 SDK 而联网。

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

Canary 只写 `.harness/canary/<run-id>/`，不修改正式 Wiki、Search Run 或主 SQLite。

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

Socket I/O 默认关闭，包括 localhost 模型。只有显式传入 `--allow-network` 才会调用
配置的模型 endpoint；DeepXiv 非 dry-run 检索还必须同时配置 `DEEPXIV_TOKEN`：

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

只有需要继续中断时尚未完成的 LangGraph pending node 时，才使用精确 checkpoint 模式：

```powershell
D:\anaconda3\python.exe -B -m research_harness research resume `
  long-context-sparse-models --thread outer-v1 --mode checkpoint --allow-network
```

`checkpoint` 模式内部调用 `graph.invoke(None, config)`；它要求 checkpoint 确实存在 pending
node，并要求当前 `--allow-network` 与中断时的授权完全一致。已正常结束的 checkpoint 应使用
`replan`，切换网络授权也应使用 `replan`。

该命令会由 Harness 自己记录动作、工具调用和进展。候选筛选会把最多 3 个 core paper
变为 `selected-for-ingest`。对选中的 arXiv candidate，在同一次 `--allow-network`
授权下，Ingest 可以受控下载公共 PDF 到 D 盘仓库：

```yaml
local_pdf_path: sources/papers/arxiv-<id>.pdf
```

也可以人工放置 PDF 并设置该路径。自动下载只支持显式选中的 arXiv candidate，并限制
HTTPS host、文件类型、目标目录和最大字节数。远程 query planning、screening、ingest、
verify 与 analyze_claims 均受同一次 `--allow-network` 授权；未配置模型时语义动作不会
进入 executor capability set。发布完成后 Harness 会把 candidate 改为 `ingested` 并
记录 canonical paper ID，避免下一轮重复处理同一 handoff。

Research memory 只接受紧凑的 `observation / decision / preference /
open-question`。没有 evidence ID 的内容会标为 `unverified-note`，不会自动升级为
Wiki 事实。

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
  -> verified source records
  -> analyze-claims
  -> needs-review non-consensus assessment
  -> independent verify-evidence
  -> verified assessment or explicit unresolved result
```

检索结果首先是 `candidate`，不能直接当作论文结论。只有读取论文正文并走
证据流程后，才能形成可验证的 Wiki claim。

## Skills

- `skills/search-paper/`：检索规划、DeepXiv 发现、候选集、coverage 与 gap；
- `skills/ingest-paper/`：把选中的论文转换为结构化 Wiki 页面；
- `skills/verify-evidence/`：来源、locator、实验条件和生命周期复核；
- `skills/analyze-claims/`：比较 verified claims，生成共识/争议/证据不足 assessment。

`ingest-paper` 已迁移到 Wiki V0.2，并通过严格 Pydantic Draft、页级 locator、typed
entity 复用和 guarded Writer 接入 Outer Loop。模型不能直接写 Markdown；拟写页面先在
shadow Wiki 重建图并通过零 schema error 校验，再逐页原子发布。普通异常以及可捕获的
`KeyboardInterrupt` / `SystemExit` 会回滚整个页面批次并继续抛出原异常；操作系统强杀或
断电不属于该进程内回滚保证。重复摄取同一 Draft 为 no-change。现有 legacy 页面继续兼容
读取，V1 默认不会覆盖人工 source page。

## Wiki Engine V0.2

Wiki 是调研 Harness 的领域持久状态层，不是对话记忆。Markdown/YAML 页面是
真源；`wiki/_generated/` 仅包含可删除、可重建的 JSON 索引。

当前 schema 支持八类实体：

```text
paper  method  experiment  claim  concept  benchmark  model  assessment
```

`paper / experiment / claim` 可用 `facets` 显式声明其证据覆盖维度。只有 verified
实体会形成 `covered`；draft 实体只形成 `partial`。`assessment` 是 non-consensus
研究真源，不存进 controller state，也不要求一定产出争议结论。

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

普通 `validate` 在有 WARNING 时仍返回成功，只有 ERROR 会失败；`--strict`
会让 WARNING 也导致非零退出码。`tools.wiki` CLI 不暴露任意写入或迁移命令；
Harness 的 ingest、verify 和 analyze 流程则会通过 guarded Writer 在 shadow validation
通过后原子修改正式 Wiki 页面。

## DeepXiv environment

项目使用 Conda `(base)` 中的 `deepxiv-sdk`，不使用 DeepXiv MCP。

检查安装：

```powershell
conda run -n base python -m pip show deepxiv-sdk
```

凭据只通过当前 shell 的 `DEEPXIV_TOKEN` 传递。不要把 Key 写入仓库、
YAML、命令参数或日志。PowerShell 7 可用遮罩输入：

```powershell
$env:DEEPXIV_TOKEN = Read-Host -Prompt "DeepXiv Token" -MaskInput
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

单次检索噪声先记在 run 内。只有稳定、可复现、跨 run 重复的问题，才升级为：

- Error Book 条目；
- Skill 决策规则；
- 校验器；
- 可复用脚本或函数。

Skill 不因一次异常结果自动改写自身。
