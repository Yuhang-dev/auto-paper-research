# 面向科研综述的双环 Harness

## 1. 目标与边界

系统现在有两条职责不同的路径：

```text
Fast Research Loop                         Durable Evidence Loop

问题 → 多源检索 → Skim → 定向深读          人工批准 PromotionManifest
    → Evidence Pool → 综述                         ↓
                                                Full Ingest
                                                → Verify
                                                → staged-for-wiki
                                                → 显式 publish
```

Fast Loop 的完成物是一份可引用的科研综述和结构化证据包，不以 Wiki
实体数量判断好坏。Durable Loop 只处理最终影响报告结论、且经过人工批准的少量论文。
Skim 只用于筛选和导航，不能作为报告证据。

现有 `research run / canary / publish-staged` 保持兼容，旧 Wiki 和 schema
不需要迁移。

## 2. 默认漏斗

| Profile | 去重来源 | Skim | Deep Read | Wiki 候选 |
|---|---:|---:|---:|---:|
| `smoke` | 8 | 4 | 2 | 0 |
| `standard` | 50 | 20 | 10 | 最多 6 |

标准来源软配额为论文 30、GitHub/项目 10、一般 Web 10。论文发现由 DeepXiv
承担；Semantic Scholar 不参与宽检索，也不增加来源数量。它只在漏斗已经选出
Deep Read 论文后，按 arXiv ID 或 DOI 为这些少量论文补充 citation count、venue
和外部 ID。从第二轮开始，未填满的全局来源预算可以被仍有结果的检索 provider
重新利用；最终总来源始终受 `max_sources` 约束。

来源软配额只服务于发现和 Skim 多样性，不再直接决定 Deep Read。Deep Read
优先满足原始论文下限：Smoke 为 2，standard 为 6；剩余名额再按证据价值补充
官方项目和一手 Web 资料。标题明确为 survey 的论文会降权，但仍可用于技术谱系。
ResearchGate、Academia.edu、Scribd 等聚合镜像只保留为导航线索，不能进入证据抽取。

## 3. Fast Research Loop

独立 LangGraph 位于 `research_harness/review_control.py`：

```text
frame
  → retrieve
  → screen
  → skim
  → reason
  → deep_read
  → assess
  ├─ uncertainty-driven pivot → retrieve
  └─ ready / budget reached  → synthesize
```

每轮检索围绕最高优先级的 `ResearchUncertainty`，而不是为了增加论文数量。
默认最多 3 轮。连续两轮都没有新方法路线、Citation-ready EvidenceCard、被解决的
blocking uncertainty 或独立反证时，判为基本饱和。达到预算但仍有缺口时仍生成报告，
并把缺口列为 unresolved。

并发边界：网络 4、Skim 2、深读/证据抽取 2；SQLite 和 artifact 写入保持串行。
批次产物按稳定 source ID 排序。每个图节点形成 checkpoint，单个已经完成的 Skim、
material 和 EvidenceCard 也落盘，因此中断后不会重做已完成项。

## 4. 数据契约

`research_harness/review_models.py` 定义：

| Contract | 含义 |
|---|---|
| `ReviewRunConfig` | 范围、漏斗、并发、模型退化和停止阶段 |
| `SourceRecord` | 论文、项目、官方网页的统一发现记录 |
| `SourceSkim` | 明确为 provisional、不可引用的轻量阅读结果 |
| `ResearchUncertainty` | 未解决问题、优先级、blocking 和下一步查询 |
| `UnderstandingClaim` | 暂定理解、支持/反对卡片和替代解释 |
| `EvidenceCard` | 带 hash、实验条件和 locator 的原子证据 |
| `ReviewReadiness` | 基于证据、facet、blocking gap 和 saturation 的综合条件 |
| `PromotionManifest` | 人工批准进入重型 Durable Loop 的来源 |

LangGraph checkpoint 只保存 ID、计数、hash、路径和决策历史。PDF 文本、Skim、
EvidenceCard 等大对象保存在运行目录，不在每轮重复注入模型上下文。

## 5. Provider 与模型

统一 provider 在 `review_providers.py`：

- DeepXiv：论文发现和元数据；
- Semantic Scholar Academic Graph：可选的入选论文元数据补全；只对 Deep Read
  选中的论文调用单篇 Paper Details API，不执行 bulk discovery，不参与 Skim，
  也不作为正式证据；
- arXiv：受控 PDF 下载和选择性文本提取；
- Tavily：官方项目页、静态 Web 和反证发现；
- GitHub REST：仓库元数据、README、license、版本和活跃度。

论文按 DOI/arXiv ID、项目按 `owner/repo`、网页按规范 URL 去重，同时保留每次
query、provider、rank 和发现时间。单个 provider 失败只形成该来源的错误事件，
不会终止整轮。

模型分工：

```powershell
$env:HARNESS_FAST_MODEL = "openai:<fast-model-id>"
$env:HARNESS_FAST_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
$env:HARNESS_FAST_API_KEY = Read-Host -Prompt "Fast model key" -MaskInput

$env:HARNESS_REASONING_MODEL = "openai:<reasoning-model-id>"
$env:HARNESS_REASONING_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
$env:HARNESS_REASONING_API_KEY = Read-Host -Prompt "Reasoning model key" -MaskInput

$env:DEEPXIV_TOKEN = Read-Host -Prompt "DeepXiv token" -MaskInput
$env:SEMANTIC_SCHOLAR_API_KEY = Read-Host -Prompt "Semantic Scholar key (optional)" -MaskInput
$env:TAVILY_API_KEY = Read-Host -Prompt "Tavily key" -MaskInput
$env:GITHUB_TOKEN = Read-Host -Prompt "GitHub token (optional)" -MaskInput
```

`S2_API_KEY` 也可作为兼容别名，但项目文档统一使用
`SEMANTIC_SCHOLAR_API_KEY`。Key 仅从当前进程环境读取，不写入 run config、
artifact、SQLite 或 Git。Semantic Scholar 请求在 provider 内串行并遵守默认每秒一次
的 key 配额；请求只发生在 Deep Read 选择之后，且一次只取一篇论文的有限元数据。
补全失败只记录运行错误，不会阻断 PDF 获取、证据抽取或整轮调研。S2 返回的 citation
count 等元数据仅用于导航和审计，不能替代带 locator 的 `EvidenceCard`。

Fast 模型处理规划、screening 和 Skim；Reasoning 模型处理深读、EvidenceCard、
反证判断和 synthesis。旧的 `HARNESS_MODEL / HARNESS_MODEL_BASE_URL /
OPENAI_API_KEY` 可作为 Fast 配置。标准运行缺少 Reasoning 配置会预检失败，只有
显式 `--allow-single-model-fallback` 才退化。所有 OpenAI-compatible endpoint 都使用
服务端实际接受的精确模型 ID；Harness 不按 DeepSeek 等 endpoint 域名写死模型名。

`run-config.yaml` 只记录两级模型 ID、base URL、是否实际发生单模型退化和非秘密
fingerprint；API key 从不进入 config、artifact 或 SQLite checkpoint。

## 6. 证据和非共识语义

正式报告只消费 `EvidenceCard`。每张卡片保存 source ID、URL、版本/hash、原子陈述、
作者陈述与 Agent 解释的区分、实验条件以及页码/表格/图/章节/官方网页定位。

确定性校验要求：

- Skim 中未经全文确认的数字会在进入 reasoning 前被移除；
- 聚合镜像和来源身份未知的 Web 页面不能产生 EvidenceCard；
- 单篇 PDF、repository README 和静态官方 Web 页分别最多保留 8、6、4 张卡片；
- EvidenceExtraction schema 校验失败时执行一次有界修复，修复过程保留原输出中的
  事实和 locator，并移除格式无效或不完整的条目；
- 静态官方 Web 页支持页面级作者陈述或文档事实，
  不能代替 PDF 的定量实验结果；
- 数字必须带实验条件；
- locator 和 content hash 必须存在；
- 报告不得引用未知 card ID；
- comparison、consensus、contradiction 至少关联两个独立来源；
- 同一论文里的 SCCA、S2、LongMixed 等配置不算跨论文证据；
- 不可比或来源不足时降级为单篇观察或 `insufficient-evidence`。

最终 Markdown 不由模型自由写文件。模型先产生 `ReviewSynthesisDraft`，程序验证
EvidenceCard 引用后，再用确定性 renderer 生成十节报告和证据索引。

## 7. Skills 与 LoopEngineer

Fast Loop 使用显式绑定，不存在智能 Skill Router：

| 阶段 | Skill |
|---|---|
| query planning | `search-paper` |
| paper/web skim | `source-skim` |
| project inspection | `project-audit` |
| deep evidence | `evidence-extract` / `project-audit` |
| reasoning/synthesis | `review-synthesize` |
| promotion | `ingest-paper`，之后 `verify-evidence / wiki-link` |

归一化、去重、来源权威性门槛、论文优先 Deep Read、Skim 数字清理、
EvidenceCard schema、独立来源校验、技术地图、readiness、saturation 和 citation
completeness 都由 Python 确定性实现。

错误先写入本次运行的 `.harness/review-runs/<run-id>/state/errors.jsonl`。只有同一
`recurrence_key` 在至少两个独立 run 中出现，才进入
`error_book/_generated/review-recurrences.yaml` 并生成修改建议。系统不会自动改 Skill。

## 8. CLI

联网 smoke：

```powershell
$runId = "review-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss")

python -B -m research_harness research review canary `
  long-context-sparse-models `
  --run-id $runId `
  --thread "review-smoke-v1" `
  --allow-network `
  --allow-single-model-fallback `
  --stop-after synthesis `
  --format json
```

标准综述：

```powershell
python -B -m research_harness research review start `
  long-context-sparse-models `
  --thread "sparse-review-standard-v1" `
  --profile standard `
  --allow-network
```

状态、恢复和重新综合：

```powershell
python -B -m research_harness research review status `
  long-context-sparse-models --thread "sparse-review-standard-v1"

python -B -m research_harness research review resume `
  long-context-sparse-models --thread "sparse-review-standard-v1"

python -B -m research_harness research review synthesize `
  long-context-sparse-models --thread "sparse-review-standard-v1"
```

`resume` 默认使用 `replan`：重新读取 run artifacts 后继续，并复用已完成的
source/skim/material/card。只有需要精确执行 LangGraph pending node 时才显式传
`--mode checkpoint`。

人工打开并编辑报告生成的 `promotion-manifest.yaml`，只把确认项设为
`approved: true`、`status: approved`，然后：

```powershell
python -B -m research_harness research review promote `
  long-context-sparse-models `
  --thread "sparse-review-standard-v1" `
  --execute

python -B -m research_harness research publish-staged `
  long-context-sparse-models `
  --run-id <review-run-id> `
  --target formal `
  --preview

# 审核 preview 后移除 --preview，才会修改正式 Wiki。
```

执行 promotion 时会先用 `verify-evidence` 对 manifest 中批准的 EvidenceCard
逐条做独立来源复核，要求 verdict 为 supported 且返回的 PDF 页属于保留 excerpt；
随后执行 Full Ingest，并对生成页面做 shadow Wiki schema/link validation。两道门均
通过后才写入 `staged-for-wiki` 队列。此过程仍不修改正式 Wiki。
这里验证的是“本次晋升所依据的报告关键证据”；正式发布后的每个 Wiki entity
仍遵守原有 draft/needs-review/verified 生命周期，只有通过 Durable Loop 的实体级
验证后才计入 durable evidence coverage。

## 9. 输出

原始下载、材料、run-local errors 和 checkpoint 关联文件：

```text
.harness/review-runs/<run-id>/
```

标准运行的审阅包：

```text
research/<research-id>/reviews/<run-id>/
├── review.md
├── source-manifest.jsonl
├── source-skims.jsonl
├── evidence-cards.jsonl
├── technology-map.yaml
├── nonconsensus-assessments.yaml
├── trajectory-summary.jsonl
└── promotion-manifest.yaml
```

Canary 的相同产物留在 `.harness/review-runs/<run-id>/deliverables/`，不会进入正式
research 目录，也不会写 Wiki。

## 10. 快速离线验收

默认测试不访问 provider 或模型：

```powershell
python -B -m unittest research_harness.tests.test_review_loop -v
```

该测试覆盖去重、Skim 证据边界、非共识独立来源、引用完整性、并发稳定结果、
Fast Loop 不写 Wiki、checkpoint resume、promotion 预览和跨运行 Error Book。
