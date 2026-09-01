# Auto Paper Research

面向科研综述的双环 Research Harness。当前研究主题是：

> 稀疏化模型在长上下文领域的性能与瓶颈

系统先通过多源检索、轻量阅读、定向深读和证据推理生成一份带引用综述；人工确认关键论文后，再将它们完整摄取、验证并发布到 Markdown Wiki。

核心目标：

- 系统梳理技术谱系、代表方法、长文任务与 benchmark；
- 比较质量、上下文长度、latency、throughput、memory、KV cache 与 kernel 实现；
- 区分跨论文共识、非共识和证据不足；
- 沉淀可追溯的 EvidenceCard、技术地图、研究缺口、trajectory 与 Wiki；
- 用 Skill + 确定性脚本 + 循环反馈实现可复用的 LoopEngineer 工作流。

## 系统流程

```text
FAST RESEARCH LOOP（先回答研究问题）

研究问题
  → 多源检索
  → 元数据筛选
  → SourceSkim
  → 临时技术地图
  → 定向 Deep Read
  → EvidenceCard
  → Coverage / Gap 分析
  → 缺口驱动补搜
  → 带引用综述

                    ↓ 人工批准关键论文

DURABLE EVIDENCE LOOP（再建设长期 Wiki）

PromotionManifest
  → Full Ingest
  → Verify
  → staged-for-wiki
  → publish-staged
  → 正式 Markdown Wiki
```

Fast Loop 不写正式 Wiki。Skim 只负责筛选和导航，报告中的定量结论必须引用带原文定位的 EvidenceCard。正式 Wiki 只有在人工批准并执行 `publish-staged` 后更新。

## 当前能力

- LangGraph 双环编排与 SQLite checkpoint；
- DeepXiv、Tavily、GitHub 与可选 Semantic Scholar 元数据补全；
- 标题/摘要级 Skim 与论文优先的 Deep Read；
- PDF 获取、选择性文本抽取和原子 EvidenceCard；
- DOI、arXiv ID、规范 URL、GitHub `owner/repo` 去重；
- source role 平衡、Evidence coverage、Gap priority 与 saturation 计算；
- 跨论文可比性、共识、非共识和 `insufficient-evidence` 校验；
- 确定性 Markdown 综述渲染与完整引用检查；
- 人工批准的 Wiki 晋升、预览、原子发布和中断回滚；
- 运行心跳、trajectory、局部 Error Book 与跨运行 recurrence 聚合；
- 10 个显式绑定的研究 Skill。

## 调研规模

| Profile | 发现/输入 | Skim | Deep Read | Wiki 推荐上限 | 用途 |
|---|---:|---:|---:|---:|---|
| `smoke` | 8 | 4 | 2 | 0 | 最快联网链路验证 |
| `seed5` | 固定 5 篇 | 5 | 5 | 5 | 权威论文冷启动 |
| `standard` | 50 | 20 | 10 | 6 | 中等规模综述 |
| `literature50` | 最多 110 个候选 | 60，至少 50 篇论文 | 15，至少 13 篇论文 | 6 | 正式领域综述 |

`seed5` 使用 [`research/long-context-sparse-models/seed-papers.yaml`](research/long-context-sparse-models/seed-papers.yaml) 中的 5 篇近年代表论文。`literature50` 继续传入该清单，使这些论文优先进入首轮 Skim 和 Deep Read。

## 快速开始

### 1. 创建环境

```powershell
conda create -n paper-harness python=3.11 -y
conda activate paper-harness
python -m pip install -r requirements-harness.txt
```

后续命令都在已激活的 `(paper-harness)` 环境中执行。

### 2. 配置本机模型与检索服务

```powershell
Copy-Item .env.example .env.local
notepad .env.local
```

最小配置：

```dotenv
HARNESS_FAST_MODEL=openai:<served-fast-model-id>
HARNESS_FAST_MODEL_BASE_URL=http://<host>/v1
HARNESS_FAST_API_KEY=<key>

HARNESS_REASONING_MODEL=openai:<served-reasoning-model-id>
HARNESS_REASONING_MODEL_BASE_URL=http://<host>/v1
HARNESS_REASONING_API_KEY=<key>

DEEPXIV_TOKEN=<token>
TAVILY_API_KEY=<key>
SEMANTIC_SCHOLAR_API_KEY=<optional-key>
GITHUB_TOKEN=<optional-token>
```

模型服务采用 OpenAI-compatible 协议，`openai:<served-model-id>` 中的 ID 必须与服务端实际接受的模型名一致。`.env.local` 已被 Git 忽略；环境变量会覆盖文件中的同名配置。

长时联网调研默认对模型、检索、网页和 arXiv PDF 使用有限重试。Semantic Scholar
按同一 key 至少间隔 1.1 秒请求；429 会优先采用服务端 `Retry-After`，否则从 30 秒
开始退避。所有等待和尝试次数都可在 `.env.local` 中通过
`HARNESS_*_TIMEOUT_SECONDS`、`HARNESS_NETWORK_*` 和 `HARNESS_S2_*` 调整。

检查环境：

```powershell
python -B -m research_harness doctor --format json
```

### 3. 最快链路验证

```powershell
$runId = "review-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss")

python -B -m research_harness research review canary `
  long-context-sparse-models `
  --run-id $runId `
  --thread $runId `
  --allow-network `
  --allow-single-model-fallback `
  --stop-after synthesis `
  --format json
```

Smoke 只验证完整链路，不代表领域综述已经充分。

## 正式调研工作流

### 阶段 A：5 篇论文冷启动

```powershell
$seedRun = "seed5-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$seedManifest = "research\long-context-sparse-models\seed-papers.yaml"

python -B -m research_harness research review start `
  long-context-sparse-models `
  --run-id $seedRun `
  --thread $seedRun `
  --profile seed5 `
  --seed-manifest $seedManifest `
  --allow-network `
  --format json
```

主要产物位于：

```text
research/long-context-sparse-models/reviews/<seed-run-id>/
├── review.md
├── source-manifest.jsonl
├── source-skims.jsonl
├── evidence-cards.jsonl
├── technology-map.yaml
├── coverage-matrix.yaml
├── research-gaps.yaml
├── nonconsensus-assessments.yaml
├── trajectory-summary.jsonl
└── promotion-manifest.yaml
```

### 阶段 B：审核并晋升 Seed 论文

先阅读报告、证据卡和晋升清单：

```powershell
$promotion = "research\long-context-sparse-models\reviews\$seedRun\promotion-manifest.yaml"
Get-Content $promotion
notepad $promotion
```

只对确认的条目设置：

```yaml
approved: true
status: approved
```

执行 Full Ingest、验证和 staging：

```powershell
python -B -m research_harness research review promote `
  long-context-sparse-models `
  --thread $seedRun `
  --manifest $promotion `
  --execute `
  --allow-network `
  --format json
```

预览正式 Wiki 变更：

```powershell
python -B -m research_harness research publish-staged `
  long-context-sparse-models `
  --run-id $seedRun `
  --target formal `
  --max-papers 5 `
  --preview
```

确认预览后去掉 `--preview`，执行正式原子发布。

### 阶段 C：至少 50 篇论文的领域综述

```powershell
$reviewRun = "literature50-" + (Get-Date -Format "yyyyMMdd-HHmmss")

python -B -m research_harness research review start `
  long-context-sparse-models `
  --run-id $reviewRun `
  --thread $reviewRun `
  --profile literature50 `
  --seed-manifest "research\long-context-sparse-models\seed-papers.yaml" `
  --allow-network `
  --format json
```

该 profile 最多执行 4 轮检索。每轮根据最高优先级研究缺口补搜，最终报告会明确列出已支持结论、非共识、证据不足和仍未解决的问题。

## 状态、恢复与重新综合

```powershell
# 查看状态
python -B -m research_harness research review status `
  long-context-sparse-models `
  --thread $reviewRun `
  --format json

# 从已有真源重新规划并继续
python -B -m research_harness research review resume `
  long-context-sparse-models `
  --thread $reviewRun `
  --mode replan `
  --format json

# 用当前 Evidence Pool 重新生成报告
python -B -m research_harness research review synthesize `
  long-context-sparse-models `
  --thread $reviewRun `
  --format json
```

长时间运行时，终端会用单行心跳显示阶段、完成数、当前来源和耗时。持久化进度位于：

```text
.harness/review-runs/<run-id>/state/progress.json
```

`Ctrl+C` 会保留 checkpoint。再次使用同一个 `--thread` 执行 `resume`，已完成的来源、Skim、材料和 EvidenceCard 会被复用。已经生成报告但仍有入选论文深读失败时，`resume --mode replan` 只补这些论文，再更新 assessment 和报告。

## 证据与非共识规则

| 结果 | 判定 |
|---|---|
| `supported-consensus` | 至少两篇独立论文在可比条件下支持同一结论 |
| `contested` | 至少两篇独立论文在可比条件下得到相反结论 |
| `insufficient-evidence` | 来源数量、实验条件或 locator 不足以形成跨论文判断 |

同一论文中的不同配置只形成该论文内部比较。跨论文判断会检查 model、method、task、benchmark、context、metric 和实验设定。无法直接比较的结果会保留各自观察及其适用边界。

每张 EvidenceCard 至少保存：

- 原子结论或实验结果；
- 作者陈述与 Harness 解释的区分；
- source ID、URL、版本与内容 hash；
- model、method、task、benchmark、context、metric 和 value；
- PDF 页码、表格、图、章节或官方网页定位；
- `located / cross-checked / verified` 状态。

## 架构与模块

| 模块 | 技术 | 职责 |
|---|---|---|
| `research_harness/review_control.py` | LangGraph | Fast Research Loop、checkpoint、pivot 与 synthesis |
| `research_harness/review_providers.py` | DeepXiv SDK、Tavily SDK、GitHub/S2 REST、pypdf | 多源发现、元数据、材料获取 |
| `research_harness/review_semantics.py` | LangChain + OpenAI-compatible model | Skim、深读、证据抽取、理解更新 |
| `research_harness/review_logic.py` | Python + Pydantic | 漏斗、去重、角色配额、coverage、gap 与 readiness |
| `research_harness/review_storage.py` | JSONL/YAML/Markdown | 可审阅研究产物与运行真源 |
| `research_harness/review_promotion.py` | Pydantic + Wiki Engine | 人工批准后的 Durable Evidence Loop |
| `tools/wiki/` | Markdown/YAML parser、resolver、validator、writer | 双向链接、索引、schema 和原子发布 |
| `research_harness/persistence.py` | SQLite + LangGraph Checkpointer | thread 状态与恢复 |
| `skills/*/SKILL.md` | 显式 Skill Registry | 可复用研究操作规程 |

系统把工作分成两类：

- 确定性代码：身份规范化、去重、预算、Schema、引用完整性、独立来源、实验可比性、coverage、gap、saturation、promotion 排名和 Error Book recurrence；
- 模型：检索语义规划、论文理解、候选关系、证据解释、反证判断和结构化综合。

## Skill

当前 Skill：

```text
search-paper       source-skim          evidence-extract
project-audit      review-synthesize    ingest-paper
verify-evidence    revise-evidence      analyze-claims
wiki-link
```

查看 Registry：

```powershell
python -B -m research_harness skills list
python -B -m research_harness skills show evidence-extract
```

Skill 规定如何操作；Markdown/YAML Wiki 保存持续积累的领域知识。Outer Loop 通过显式表把阶段绑定到 Skill，不使用智能 Router。

## 存储边界

```text
.harness/                                  下载、缓存、checkpoint、局部错误；Git 忽略
research/<research-id>/reviews/<run-id>/   可审阅综述与结构化证据包
wiki/                                      正式 Markdown 知识真源
error_book/                                跨运行重复错误与优化建议
skills/                                    可复用操作规程
```

SQLite 默认位于当前仓库的 `.harness/research-harness.sqlite3`。可通过 `HARNESS_DB_PATH` 指向任意可写位置；使用持久化文件以支持中断恢复。

## 测试

快速离线验证：

```powershell
python -m unittest discover -s research_harness/tests -p "test_*.py"
python -m unittest discover -s tools/wiki/tests -p "test_*.py"
python -m unittest discover -s skills/search-paper/scripts/tests -p "test_*.py"
git diff --check
```

联网任务由使用者显式执行。建议按 `doctor → smoke → seed5 → literature50 → promotion` 的顺序验收。

## 文档

- [科研综述双环与完整命令](docs/REVIEW_FIRST_HARNESS.md)
- [系统架构](docs/SYSTEM_ARCHITECTURE.md)
- [全部参数与计算指标](docs/PARAMETERS_AND_METRICS.md)
- [Canary 与运行级验收](docs/CANARY_AND_EVALUATION.md)
- [OpenAI-compatible 模型接入](docs/OPENAI_COMPATIBLE_MODEL.md)
- [DoneCriteria 激活清单](docs/DONE_CRITERIA_ACTIVATION_CHECKLIST.md)
- [当前研究范围](research/long-context-sparse-models/scope.md)

## 当前阶段

Harness 已具备 `seed5` 冷启动、`literature50` 正式调研、Gap 驱动补搜、EvidenceCard 综合和人工 Wiki 晋升链路。Error Book 已实现运行内记录与跨运行 recurrence 聚合，后续可继续丰富错误分类和 Skill 优化评审界面。
