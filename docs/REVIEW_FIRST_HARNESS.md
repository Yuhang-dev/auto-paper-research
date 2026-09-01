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
| `seed5` | 固定 5 篇论文 | 5 | 5 | 5 |
| `standard` | 50 | 20 | 10 | 最多 6 |
| `literature50` | 110 | 60，其中至少 50 篇论文 | 15，其中至少 13 篇论文 | 最多 6 |

`seed5` 是正式调研前的冷启动入口。它读取
`research/<research-id>/seed-papers.yaml` 中人工选定的精确 arXiv 身份，跳过 provider
宽检索，让 5 篇论文全部完成 Skim、PDF Deep Read 和 EvidenceCard 抽取。随后仍需
人工批准 Promotion Manifest，再执行 Full Ingest、独立证据复核、staging 和正式
Wiki 发布。相同 manifest 传给 `literature50` 后，这 5 篇会被确定性优先纳入 Skim
和 Deep Read，成为第一轮理解基线；模型上下文只接收本轮选中的材料与证据卡。

标准来源软配额为论文 30、GitHub/项目 10、一般 Web 10。论文发现由 DeepXiv
承担；Semantic Scholar 不参与宽检索，也不增加来源数量。它只在漏斗已经选出
Deep Read 论文后，按 arXiv ID 或 DOI 为这些少量论文补充 citation count、venue
和外部 ID。从第二轮开始，未填满的全局来源预算可以被仍有结果的检索 provider
重新利用；最终总来源始终受 `max_sources` 约束。

Standard 的 12 个 Query 按剩余轮次均匀保留预算，默认三轮各最多 4 个，避免前两轮
耗尽 Query 额度而跳过最后一次 Gap 驱动补搜。

`literature50` 用 100 / 5 / 5 的论文、项目、Web 发现软配额，并将 24 个 Query
按四轮分配为 `6 → 6 → 6 → 6`。每轮确定性保留 4 个 DeepXiv 论文 Query，形成最多
100 个去重论文候选，再完成至少 50 篇论文的标题/摘要级 Skim。最终结论只引用进入
Deep Read 后生成的 EvidenceCard；报告附录列出完整的 50+ 论文 Skim 范围。

来源软配额只服务于发现和 Skim 多样性，不再直接决定 Deep Read。Deep Read
优先满足原始论文下限：Smoke 为 2，standard 为 6；剩余名额再按证据价值补充
官方项目和一手 Web 资料。标题明确为 survey 的论文会降权，但仍可用于技术谱系。
ResearchGate、Academia.edu、Scribd 等聚合镜像只保留为导航线索，不能进入证据抽取。

Standard 的 20 个 Skim 采用 source role 软目标：survey 2、primary study 6、
benchmark 2、reproduction 1、project 2，其余按综合得分补齐。10 个 Deep Read 中
survey 最多 2、非论文来源最多 2，并优先保证 6 个 primary/benchmark/reproduction
论文。来源不足时按稳定排名补位，不中断运行。

Skim 上限是整个 run 的累计预算，不是每轮重新选择 20 个。三轮 Standard 默认依次
开放到 `7 → 14 → 20`，从而为后续 gap-driven 来源保留阅读名额。旧 run 中已经超过
20 的 Skim 会继续保留用于审计，但恢复时不再增加。

`literature50` 的 60 个 Skim 采用软目标：survey 4、primary study 28、benchmark 8、
reproduction 5、project 3，其余按综合得分补齐。确定性选择器优先达到 50 篇论文
下限。15 个 Deep Read 优先包含 12 个 primary/benchmark/reproduction 研究，整体
至少保留 13 篇论文。

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
默认最多 3 轮。每轮 assessment 后，确定性 Gap Analyzer 根据缺失 facet、单来源
结论、不可比实验、方法工程证据、临时孤岛节点和资料时效性生成下一轮检索目标。
连续两轮都没有新方法路线、Citation-ready EvidenceCard、新独立来源、新 covered
facet、被解决的 blocking uncertainty、独立反证或新确认关系时，判为基本饱和。
达到预算但仍有缺口时仍生成报告，并把缺口列为 unresolved。

Assessment 主调用遗漏某个稳定 `uncertainty_id` 时，运行时只针对缺失项执行一次
Evidence Pool 重试。这个补全不重读 Skim、不重复处理已完成项；缺少跨论文可比证据
的假设继续标记为 `insufficient-evidence`。

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
| `SourceRelationCandidate` | 待确认的 alias/variant/extends/implements 等临时关系 |
| `ReviewCoverageMatrix` | required facet 的独立证据来源和卡片覆盖 |
| `ReviewGap` | 确定性生成的检索盲点、固定优先级和下一步查询 |
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
  选中的论文调用一次 Paper Batch API，不执行 bulk discovery，不参与 Skim，
  也不作为正式证据；
- arXiv：受控 PDF 下载和选择性文本提取；
- Tavily：官方项目页、静态 Web 和反证发现；
- GitHub REST：仓库元数据、README、license、版本和活跃度。

论文按 DOI/arXiv ID、项目按 `owner/repo`、网页按规范 URL 去重，同时保留每次
query、provider、rank 和发现时间。标题/作者相似只形成 `possible-same-work`
候选关系，不自动合并。单个 provider 失败只形成该来源的错误事件，不会终止整轮。

模型分工：

```powershell
Copy-Item .env.example .env.local
notepad .env.local
python -B -m research_harness doctor
```

`.env.local` 保存两级模型和 Provider 凭证，程序启动时自动加载。当前 shell 中同名
变量覆盖文件值；该文件由 Git 忽略。

`S2_API_KEY` 也可作为兼容别名，但项目文档统一使用
`SEMANTIC_SCHOLAR_API_KEY`。Key 加载到当前进程，不进入 run config、artifact、
SQLite 或 Git。Semantic Scholar 请求只发生在 Deep Read 选择之后，一次批量补全
本轮入选论文的有限元数据。同一 key 的请求默认至少间隔 1.1 秒；429 优先遵循
`Retry-After`，否则至少等待 30 秒，默认最多尝试 6 次。尝试耗尽后进入 300 秒冷却，
冷却结束或稍后恢复同一 run 时重新探测。S2 元数据补全失败会被记录，同时 PDF 获取、
证据抽取和整轮调研继续运行。
S2 返回的 citation count 等元数据仅用于
导航和审计，不能替代带 locator 的 `EvidenceCard`。

Fast 模型处理规划、screening 和 Skim；Reasoning 模型处理深读、EvidenceCard、
反证判断和 synthesis。旧的 `HARNESS_MODEL / HARNESS_MODEL_BASE_URL /
OPENAI_API_KEY` 可作为 Fast 配置。标准运行缺少 Reasoning 配置会预检失败，只有
显式 `--allow-single-model-fallback` 才退化。所有 OpenAI-compatible endpoint 都使用
服务端实际接受的精确模型 ID；Harness 不按 DeepSeek 等 endpoint 域名写死模型名。

`run-config.yaml` 只记录两级模型 ID、base URL、是否实际发生单模型退化和非秘密
fingerprint；API key 从不进入 config、artifact 或 SQLite checkpoint。

超时和退避参数集中放在 `.env.local`：

```dotenv
HARNESS_FAST_MODEL_TIMEOUT_SECONDS=180
HARNESS_FAST_MODEL_MAX_RETRIES=2
HARNESS_REASONING_MODEL_TIMEOUT_SECONDS=300
HARNESS_REASONING_MODEL_MAX_RETRIES=2

HARNESS_NETWORK_MAX_ATTEMPTS=3
HARNESS_NETWORK_CONNECT_TIMEOUT_SECONDS=20
HARNESS_NETWORK_READ_TIMEOUT_SECONDS=180
HARNESS_NETWORK_BACKOFF_SECONDS=2
HARNESS_NETWORK_MAX_BACKOFF_SECONDS=30

HARNESS_S2_MAX_ATTEMPTS=6
HARNESS_S2_CONNECT_TIMEOUT_SECONDS=20
HARNESS_S2_READ_TIMEOUT_SECONDS=90
HARNESS_S2_REQUEST_INTERVAL_SECONDS=1.1
HARNESS_S2_RATE_LIMIT_BACKOFF_SECONDS=30
HARNESS_S2_COOLDOWN_SECONDS=300
```

`HARNESS_NETWORK_*` 同时用于 provider 查询、静态网页、GitHub 元数据和 arXiv PDF；
PDF 下载会在 `arxiv.org` 与 `export.arxiv.org` 的批准地址之间重试。模型调用使用独立
预算，避免把慢推理和网络下载混为同一种 timeout。

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
- survey 的定量转述只用于导航，量化 EvidenceCard 必须回到原始研究；
- 数字必须带实验条件；
- locator 和 content hash 必须存在；
- 报告不得引用未知 card ID；
- comparison、consensus、contradiction 至少关联两个独立来源；
- 同一论文里的 SCCA、S2、LongMixed 等配置不算跨论文证据；
- `independent_source_ids` 由程序根据 EvidenceCard 反向计算，不采用模型自由填写值；
- 一条 Assessment 不合法时只降级或隔离该条，同批 Claim 和 uncertainty 继续合并；
- 不可比或来源不足时降级为单篇观察或 `insufficient-evidence`；
- 只有 Evidence Pool 建立后重新执行的 Assessment 才计入
  `nonconsensus_review_complete`，Skim-only 结果只驱动补搜。每个范围内候选假设使用
  稳定 `uncertainty_id` 绑定 Assessment；完成检查可以得到
  `supported-consensus`、`contested` 或 `insufficient-evidence`。
- 宽泛的研究主问题只用于首轮定向；已有 EvidenceCard 后由具体 coverage/gap 接管，
  不再作为永久 blocking uncertainty。

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

归一化、去重、source role 配额、来源权威性门槛、论文优先 Deep Read、Skim
数字清理、EvidenceCard schema、Coverage Matrix、Gap priority、独立来源校验、
技术地图、readiness、saturation、promotion ranking 和 citation completeness
都由 Python 确定性实现。

错误先写入本次运行的 `.harness/review-runs/<run-id>/state/errors.jsonl`。只有同一
`recurrence_key` 在至少两个独立 run 中出现，才进入
`error_book/_generated/review-recurrences.yaml` 并生成修改建议。系统不会自动改 Skill。

## 8. CLI

5 篇冷启动、晋升与正式 Wiki 发布：

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

先检查：

```powershell
$promotion = "research\long-context-sparse-models\reviews\$seedRun\promotion-manifest.yaml"
Get-Content $promotion
```

把 5 个确认项的 `approved` 改为 `true`、`status` 改为 `approved` 后执行：

```powershell
python -B -m research_harness research review promote `
  long-context-sparse-models `
  --thread $seedRun `
  --manifest $promotion `
  --execute `
  --allow-network `
  --format json

python -B -m research_harness research publish-staged `
  long-context-sparse-models `
  --run-id $seedRun `
  --target formal `
  --preview

# Preview 通过后，去掉 --preview 正式发布。
```

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

至少 50 篇论文的正式综述：

```powershell
$runId = "literature50-" + (Get-Date -Format "yyyyMMdd-HHmmss")

python -B -m research_harness research review start `
  long-context-sparse-models `
  --run-id $runId `
  --thread $runId `
  --profile literature50 `
  --seed-manifest "research\long-context-sparse-models\seed-papers.yaml" `
  --allow-network `
  --format json
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

### 8.1 运行进度与心跳

`start`、`canary`、`resume` 和 `synthesize` 共用同一个进度器。交互式终端默认每秒
覆盖同一行：

```text
[deep-read] 3/10 · Evidence extraction: <paper title> · 01:42...
```

点号循环表示进程仍在运行；计数或标题变化表示任务取得了结构化进展。阶段切换时保留
一条完成记录，随后在新的一行继续。进度写到 stderr，`--format json` 的 stdout
仍可安全重定向或交给脚本解析。

持久化心跳位于：

```text
.harness/review-runs/<run-id>/state/progress.json
```

它包含 `status`、`stage`、`detail`、完成数、总数、总耗时、阶段耗时、
`heartbeat_at`、`last_progress_at` 和 `seconds_since_progress`。因此可以区分“进程仍
存活”和“最近一次结构化进展”。另一个终端执行 `research review status` 时，返回值
中的 `progress` 就是该快照。

显示模式通过 `.env.local` 设置：

```text
HARNESS_PROGRESS=auto   # TTY 单行刷新；重定向时每 30 秒写一条 stderr 日志
HARNESS_PROGRESS=live  # 强制单行刷新
HARNESS_PROGRESS=plain # 阶段变化或每 30 秒打印一行
HARNESS_PROGRESS=off   # 关闭终端输出，仍更新 progress.json
```

单行刷新比累积一长串 `...` 更容易阅读，也不会让长任务刷满终端。PDF 解析器等第三方
库偶尔写入 stderr 时，下一次心跳会重新绘制当前状态。

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
├── coverage-matrix.yaml
├── research-gaps.yaml
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
