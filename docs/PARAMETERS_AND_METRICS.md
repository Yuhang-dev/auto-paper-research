# 程序参数与指标总表

> 适用版本：Harness / DoneCriteria schema `0.2`，Search Run schema `0.1`
>
> 源码审计基线：Canary / semantic-artifact 工作树（2026-08-28）

本文回答三个问题：

1. 哪些值可以通过环境变量、命令行或 YAML 调整；
2. 哪些值由程序从 Markdown Wiki、Search Run YAML 和 SQLite 状态中计算，不能当作配置填写；
3. 哪些值虽然能改源码，但当前没有对外配置接口。

## 1. 分类约定

| 标记 | 类别 | 是否建议修改 | 含义 |
|---|---|---:|---|
| A | 对外可调参数 | 是 | 有正式环境变量、CLI 或 YAML 入口 |
| R | 单次运行输入 | 按任务填写 | 研究 ID、thread、query、过滤条件等 |
| I | 内部代码参数 | 一般不建议 | 构造器默认值或硬编码常量，没有稳定 CLI/YAML 接口 |
| C | 计算指标 | 否 | 从真源确定性重算；手工填写会被覆盖或造成不一致 |
| F | 固定协议 / schema | 除非升级版本 | ID、状态枚举、字段约束和安全边界 |

最重要的边界是：

```text
done-criteria.yaml              = 可调阈值和预算
search-run 的 scope / queries   = 可调检索计划
search-run coverage.metrics     = 计算结果
ResearchSnapshot                = 计算结果
ProgressMeasurement             = 计算结果
DoneCheck                       = 计算结果
```

不要手工把计算指标改成“看起来达标”。Search Run 指标应通过
`validate_search_run.py --fix-metrics` 重算，Outer Loop 指标应通过
`research inspect` / `research evaluate` 重算。

## 2. 配置优先级与配置真源

### 2.1 优先级

Harness 的三个全局配置按以下优先级解析：

```text
CLI --db / --model / --workspace
  > 对应 HARNESS_* 环境变量
  > 程序默认值
```

DoneCriteria 按以下优先级解析：

```text
--criteria 指定的 YAML
  > research/<research-id>/done-criteria.yaml
```

程序启动时自动加载仓库根目录的 `.env.local`。当前 shell 中的同名环境变量覆盖
文件值；`.env.local` 由 Git 忽略。

### 2.2 主要配置真源

| 配置范围 | 真源 |
|---|---|
| Harness 进程配置 | `.env.local`、环境变量、`research_harness/config.py`、全局 CLI |
| Outer Loop 完成门槛 | `research/<research-id>/done-criteria.yaml` |
| 单次搜索计划 | `research/<research-id>/search-runs/*.yaml` |
| Wiki 数据协议 | `wiki/_meta/schema.yaml` |
| 搜索协议 | `skills/search-paper/references/search-output-schema.md` |
| 计算公式 | `research_harness/research_evaluation.py`、`skills/search-paper/scripts/search_common.py` |
| 内部执行限制 | `research_harness/*.py`、`research_harness/tools.py` |

## 3. Harness 环境变量和全局配置

### 3.1 正式 Harness 配置

| 参数 | 类别 | 默认值 | 允许范围 / 约束 | 作用 |
|---|---|---:|---|---|
| `HARNESS_DB_PATH` / `--db` | A | `.harness/research-harness.sqlite3` | 任意可写盘符上的持久化文件；扩展名 `.db`、`.sqlite` 或 `.sqlite3`；拒绝 `:memory:` | LangGraph checkpoint 与跨 thread memory 的 SQLite 文件 |
| `HARNESS_MODEL` / `--model` | A | 无 | 必须为 `openai:<served-model-name>` | 语义规划、筛选、论文抽取、验证、非共识分析使用的模型 |
| `HARNESS_MODEL_BASE_URL` / `--model-base-url` | A | 无 | 绝对 HTTP(S) OpenAI-compatible API root；配置模型时必填 | 显式模型 endpoint，不允许隐式回退到 OpenAI 公网默认地址 |
| `HARNESS_WORKSPACE_ID` / `--workspace` | A | `long-context-sparse-models` | 非空，最多 120 字符 | 跨 thread memory namespace |
| `HARNESS_CONTEXT_TOKENS` | A | `6000` | `512..200000` 整数 | Inner Agent Loop 每次发送给模型的最近消息 token 预算 |
| `HARNESS_MAX_TOOL_ITERATIONS` | A | `6` | `1..30` 整数 | Inner Agent Loop 每轮最多经历多少次 tool-observe 循环；不控制 Outer Loop |
| `HARNESS_TOOL_OUTPUT_CHARS` | A | `12000` | `1000..100000` 整数 | 每个 LangChain tool 返回给模型的 JSON 字符上限 |

Harness 只使用 OpenAI-compatible adapter，但任意 endpoint 都可以暴露明确的
model ID。裸模型名和其他 provider 前缀会被拒绝；具体模型是否可用由服务端判断，
不再按 endpoint 域名写死模型名。测试可通过构造器注入进程内 fake model，而不配置
socket endpoint。

推荐配置：

```powershell
$env:HARNESS_MODEL = "openai:<served-model-id>"
$env:HARNESS_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
$env:HARNESS_DB_PATH = "C:/wiki-papersearch/.harness/research-harness.sqlite3"
```

### 3.2 凭证、适配器和运行环境

| 参数 | 类别 | 默认值 | 作用与注意事项 |
|---|---|---:|---|
| `HARNESS_MODEL_BASE_URL` | A | 无 | 模型 endpoint 的项目级规范变量；优先于兼容变量 |
| `OPENAI_API_BASE` | A | 无 | 旧 LangChain-compatible endpoint 变量；仅在未设置规范变量时读取 |
| `OPENAI_BASE_URL` | A | 无 | 旧 OpenAI SDK-compatible endpoint 变量；与同时存在的 `OPENAI_API_BASE` 不一致时拒绝启动 |
| `OPENAI_API_KEY` | A | 无 | OpenAI-compatible endpoint 的凭证；本地无鉴权服务也需非空 sentinel |
| `DEEPXIV_TOKEN` | A | 无 | DeepXiv SDK 凭证；没有时论文发现 provider 不可用 |
| `SEMANTIC_SCHOLAR_API_KEY` | A | 无 | 可选的 Semantic Scholar 凭证；仅补充入选论文元数据，不参与第一轮批量发现 |
| `S2_API_KEY` | F | 无 | `SEMANTIC_SCHOLAR_API_KEY` 的兼容别名；规范变量优先 |
| `TAVILY_API_KEY` | A | 无 | Review Fast Loop 的 Web/官方项目页检索凭证；standard profile 必需 |
| `GITHUB_TOKEN` | A | 无 | GitHub REST 凭证；可选，但 standard 正式运行建议配置以避免匿名限流 |
| `LANGGRAPH_STRICT_MSGPACK` | A/F | `true` | 持久化兼容保护；`persistence.py` 只在未设置时写入 `true` |
| `TIKTOKEN_CACHE_DIR` | A | `.harness/tiktoken-cache`（子进程 fallback） | 将 tokenizer 缓存保留在当前项目目录 |
| `PDFTOTEXT_PATH` | A | 自动发现 | `pypdf` 不可用时的 `pdftotext` 可执行文件路径 |
| `PYTHONUTF8` | I | 子进程中强制 `1` | DeepXiv 子进程 UTF-8 输出保护 |
| `DEEPSEEK_API_KEY` | F | 不使用 | 只会在异常消息中被脱敏；不是当前模型适配器的凭证变量 |

Key 不得写入 `.env`、Search Run YAML、Wiki、SQLite memory、命令参数或日志。

### 3.3 Review-first 双模型与漏斗参数

| 参数 | 类别 | 默认 / 回退 | 作用 |
|---|---|---|---|
| `HARNESS_FAST_MODEL` | A | `HARNESS_MODEL` | query planning、screening、Skim 的 `openai:<id>` |
| `HARNESS_FAST_MODEL_BASE_URL` | A | `HARNESS_MODEL_BASE_URL` | Fast OpenAI-compatible endpoint |
| `HARNESS_FAST_API_KEY` | A | `OPENAI_API_KEY` | Fast endpoint 凭证 |
| `HARNESS_REASONING_MODEL` | A | 无 | Deep Read、EvidenceCard、reasoning、synthesis 模型 |
| `HARNESS_REASONING_MODEL_BASE_URL` | A | 无 | Reasoning endpoint |
| `HARNESS_REASONING_API_KEY` | A | 无 | Reasoning endpoint 凭证 |
| `--allow-single-model-fallback` | A | `false` | 明确允许 Fast 模型兼任 Reasoning；seed5/standard/literature50 缺少 Reasoning 配置且未传此项会失败 |
| `--profile` | A | `standard` | `smoke`、`seed5`、`standard` 或 `literature50` |
| `--seed-manifest` | A | `seed5` 自动使用主题目录的 `seed-papers.yaml` | 精确 arXiv 身份冷启动；传给正式 profile 时优先进入 Skim/Deep Read |
| `--stop-after` | A | `synthesis` | 在指定 Review stage 后有界停止 |
| `--thread` | R | 必填 | SQLite checkpoint 身份 |
| `--run-id` | R | 自动生成 | artifact 身份；必须为安全 ASCII 文件名 |
| `HARNESS_PROGRESS` | A | `auto` | Review CLI 进度显示模式：`auto`、`live`、`plain`、`off`；所有模式都持续更新运行目录中的 `state/progress.json` |

V1 profile 预算固定在 `ReviewRunConfig.for_profile()`，暂未暴露逐项 CLI 覆盖：

| 参数 | 类别 | smoke | seed5 | standard | literature50 |
|---|---|---:|---:|---:|---:|
| `max_sources` | I | 8 | 5 | 50 | 110 |
| `max_skims` | I | 4 | 5 | 20；三轮 7 / 14 / 20 | 60；四轮 15 / 30 / 45 / 60 |
| `minimum_paper_skims` | I | 0 | 5 | 0 | 50 |
| `max_deep_reads` | I | 2 | 5 | 10 | 15 |
| `minimum_deep_read_papers` | I | 2 | 5 | 6 | 13 |
| `minimum_core_study_deep_reads` | I | 0 | 5 | 6 | 12 |
| `max_survey_deep_reads` | I | 2 | 2 | 2 | 2 |
| `max_nonpaper_deep_reads` | I | 2 | 0 | 2 | 2 |
| source role soft targets | I | 无 | 无 | survey 2 / primary 6 / benchmark 2 / reproduction 1 / project 2 | survey 4 / primary 28 / benchmark 8 / reproduction 5 / project 3 |
| `max_promotions` | I | 0 | 5 | 6 | 6 |
| paper / project / web soft quota | I | 5 / 1 / 2 | 5 / 0 / 0 | 30 / 10 / 10 | 100 / 5 / 5 |
| `max_search_rounds` | I | 1 | 1 | 3 | 4 |
| `max_queries` | I | 3 | 1（seed-only 时不调用 provider） | 12 | 24 |
| `minimum_evidenced_claims` | I | 1 | 1 | 1 | 8 |
| `target_core_findings` | I | 1 | 5 | 6 | 8 |
| network / skim / deep-read concurrency | I | 4 / 2 / 2 | 4 / 2 / 2 | 4 / 2 / 2 | 4 / 2 / 2 |
| EvidenceCard per source: PDF / repository / Web | I | 8 / 6 / 4 | 8 / 6 / 4 | 8 / 6 / 4 | 8 / 6 / 4 |
| EvidenceExtraction schema repair attempts | I | 1 | 1 | 1 | 1 |

Evidence extraction limits each source to a compact set of high-value cards. A schema-invalid
model response receives one bounded repair pass that may remove invalid or incomplete entries
while preserving the facts and locators already present in the original response.

Review readiness 是计算结果，不可通过 YAML 直接填写：

| 指标 | 类别 | 计算语义 |
|---|---|---|
| `facet_statuses` | C | 每个 facet 按独立 EvidenceCard 来源数计算 missing/partial/covered |
| `citation_ready_cards` | C | 合法且有 locator/hash 的 EvidenceCard 数 |
| `evidenced_claims` | C | 至少关联一个支持或反对卡片的 UnderstandingClaim 数 |
| `independent_sources` | C | Evidence Pool 中唯一 source ID 数 |
| `paper_skims` | C | 已完成 SourceSkim 且规范化来源类型为 paper 的唯一来源数 |
| `minimum_paper_skims` | I/C | 当前 profile 的论文广度下限；literature50 为 50 |
| `deep_read_papers` | C | 已完成正文材料获取与 EvidenceCard 抽取的 paper 来源数 |
| `minimum_deep_read_papers` | I/C | 当前 profile 的论文深读下限；literature50 为 13 |
| `unresolved_blocking_ids` | C | 仍为 open 且 blocking 的 uncertainty |
| `nonconsensus_review_complete` | C | 每个范围内候选假设的稳定 `uncertainty_id` 都已有 Evidence Pool 阶段 Assessment；Skim-only assessment 不计入 |
| `saturated` | C | 连续两轮无新路线、卡片、独立来源、covered facet、blocking resolution、独立反证和确认关系 |
| `ready` | C | facet/evidence/uncertainty/nonconsensus/saturation 的组合判断 |

Review profile 达到预算但 `ready=false` 时仍生成有界综述，并把缺口写入报告；不会
伪造完成状态。旧 `DoneCriteria` 继续服务 Durable Evidence Loop，不控制 Fast Loop。

Fast Review Gap Analyzer 使用固定内部优先级；report-critical gap 额外加 `0.05`，
上限为 `1.00`：

| Review gap | 基础 priority |
|---|---:|
| blocking uncertainty | 1.00 |
| missing required facet | 0.90 |
| single-source claim | 0.80 |
| incomparable evidence | 0.75 |
| missing method engineering/failure evidence | 0.70 |
| orphan provisional concept | 0.60 |
| stale evidence | 0.50 |

这些常量属于实现参数。`research-gaps.yaml` 保存每轮实际生成的 gap、来源、目标
facet/source role 和推荐 Query。

### 3.4 SQLite 固定参数

以下是内部实现参数，不是正式调参接口：

| 参数 | 类别 | 当前值 | 说明 |
|---|---|---:|---|
| SQLite connection timeout | I | `30.0 s` | 数据库锁等待 |
| `journal_mode` | I | `WAL` | checkpoint/store 并发读写 |
| `synchronous` | I | `NORMAL` | WAL 持久化策略 |
| `foreign_keys` | I | `ON` | SQLite 外键检查 |
| `busy_timeout` | I | `30000 ms` | PRAGMA 锁等待 |
| connection count | I | `2` | checkpointer 和 store 各自持有一个 connection |

## 4. Outer Loop：所有可调 DoneCriteria

当前文件：`research/long-context-sparse-models/done-criteria.yaml`。

### 4.1 开关和 Coverage 门槛

| 参数 | 类别 | 当前值 | 合法值 | 程序比较对象 |
|---|---|---:|---|---|
| `schema_version` | F | `"0.2"` | 只能是 `"0.2"` | Pydantic contract 版本 |
| `status` | A | `draft` | `draft` / `active` | 只有 `active` 才允许 `complete=true` |
| `facet_requirements` | A | 见下表 | key 非空；值为 `partial` / `covered` | **Wiki evidence facet coverage**；candidate coverage 不能满足 Done |
| `minimum_method_families` | A | `5` | `>=0` 整数 | `len(snapshot.taxonomy.method_families)` |
| `minimum_core_candidates` | A | `40` | `>=0` 整数 | Search Run 去重后的 `core_candidates` |
| `minimum_ingested_papers` | A | `20` | `>=0` 整数 | Wiki 中 `type: paper` 的唯一实体数 |

当前 facet 门槛：

| Facet | 当前要求 |
|---|---|
| `technical-taxonomy` | `covered` |
| `static-vs-dynamic` | `covered` |
| `prefill-vs-decode` | `covered` |
| `synthetic-vs-real-tasks` | `covered` |
| `quality-metrics` | `covered` |
| `latency-throughput` | `covered` |
| `memory-and-kv-cache` | `covered` |
| `kernels-and-hardware` | `partial` |
| `open-source-implementations` | `partial` |
| `limitations-and-counter-evidence` | `covered` |

Facet 的计算语义：

```text
covered = 至少一个带该 facet 的 verified paper / experiment / claim
partial = 没有 verified，但至少一个 draft 或 needs-review 实体
missing = 两者都没有
```

因此 `covered` 当前是状态门槛，不是数量门槛；如果需要每个 facet 至少 N 条
独立证据，当前 schema 还没有对应的 `facet_count_requirements`。

### 4.2 Quality 与证据门槛

| 参数 | 类别 | 当前值 | 合法值 | 程序比较对象 |
|---|---|---:|---|---|
| `minimum_verified_papers` | A | `10` | `>=0` 整数 | Wiki 中 `paper.status == verified` |
| `minimum_experiments` | A | `50` | `>=0` 整数 | Wiki 唯一 experiment 数；注意不是 verified experiment 数 |
| `minimum_verified_claims` | A | `30` | `>=0` 整数 | Wiki 中 `claim.status == verified` |
| `minimum_evidence_locator_ratio` | A | `0.90` | `0..1` | 有 `evidence.locator` 的 experiment / 全部 experiment |
| `maximum_schema_errors` | A | `0` | `>=0` 整数 | Wiki validator 的 `ERROR` 数 |
| `require_nonconsensus_review` | A | `true` | boolean | 是否启用非共识评估门槛 |
| `minimum_verified_nonconsensus_assessments` | A | `3` | `>=0`；review=true 时必须 `>=1` | 同时满足页面 `status: verified` 与 assessment `verified: true` 的数量 |
| `context_bucket_requirements` | A | `8K-32K: 3`、`32K-64K: 3`、`>=64K: 3` | 名称非空、数量 `>=0` | 各 context bucket 的结构化 experiment 数 |
| `engineering_metric_requirements` | A | `latency: 3`、`memory: 3` | 名称非空、数量 `>=0` | 各工程 metric 类别的结构化 experiment 数 |
| `require_no_open_blocking_gaps` | A | `true` | boolean | 是否要求 open blocking gap 数为 0 |

注意：`minimum_experiments`、context bucket 和 engineering metric 当前按“结构化
experiment”计数，不要求 experiment 自身 `verified`。这和
`minimum_verified_claims` 的语义不同。

### 4.3 Search saturation

| 参数 | 类别 | 当前值 | 合法值 | 含义 |
|---|---|---:|---|---|
| `minimum_completed_search_rounds` | A | `2` | `>=1` | 至少需要多少条 `SearchYield` |
| `saturation_window` | A | `2` | `>=1` 且不得大于上项 | 检查最后多少轮 |
| `saturation_novelty_threshold` | A | `0.0` | `>=0` | 每轮允许的最大 `new_core_papers`；这是篇数，不是 `[0,1]` 归一化分数 |

Saturation 公式：

```text
len(search_yields) >= minimum_completed_search_rounds
AND
最后 saturation_window 条 yield 的 new_core_papers
    全部 <= saturation_novelty_threshold
```

`SearchYield` 来自每个 Search Run 的 `coverage.metrics.new_core_by_round`。程序同时记录
`valid_discovery_round`、`invalid_reasons`、`query_statuses` 和
`screening_complete`。只有满足以下条件的 yield 才计入 saturation：至少一个
`succeeded/empty` provider query；全部 query 已终止且无失败/凭证阻塞；候选已全部
完成合法 relevance screening。失败或未筛选轮仍保留审计记录，但不能制造虚假的
零 novelty 饱和。

### 4.4 硬预算与停滞预算

| 参数 | 类别 | 当前值 | 合法值 | 命中条件 |
|---|---|---:|---|---|
| `max_research_iterations` | A | `30` | `>=1` | 已实际尝试的 action 数达到上限 |
| `max_search_runs` | A | `20` | `>=1` | `search-runs/*.yaml` 文件数达到上限 |
| `max_ingested_papers` | A | `100` | `>=1` | Wiki paper 数达到上限 |
| `max_tool_calls` | A | `500` | `>=1` | Outer action 累计 model/provider 调用计数达到上限 |
| `max_no_progress_rounds` | A | `3` | `>=1` | 同一个 `(gap_id, recommended_action)` 连续无进展次数达到上限 |

`max_no_progress_rounds` 不是全局“连续三轮无进展”。它按 gap/action pair 单独
计数；某个 pair 耗尽后，Controller 仍可选择其他未耗尽 pair。只有所有可执行的
open pair 都耗尽时才得到 `stop_reason: stalled`。

`research evaluate --no-progress-rounds` 仍为 checkpoint/CLI 兼容而保留，但
`check_done()` 当前会忽略这个全局输入；真正生效的是
`attempts_by_gap_action[*].no_progress`。

### 4.5 完成与停止公式

```text
complete =
    criteria.status == active
AND coverage_passed
AND quality_passed
AND saturation_passed
AND blocking_gaps_passed
```

停止原因按优先顺序计算：

1. 全部门槛通过：`completed`；
2. 任一硬预算命中：`budget_exhausted`；
3. 已知 query 全部 blocked/failed 且没有待 ingest candidate：`blocked`；
4. 所有受支持 gap/action pair 都因无进展耗尽：`stalled`；
5. open gap 没有 executor：`blocked`。

`status: draft` 只禁止自动完成，不会禁止预算触发停止。

## 5. Search Run：可调检索参数

Search Run 模板：`skills/search-paper/assets/search-run-template.yaml`。

### 5.1 Run 和 scope

| 参数 | 类别 | 默认 / 当前 | 约束或实现状态 |
|---|---|---:|---|
| `run.id` | R/F | 自动生成 | 必须唯一；自动计划格式为 `<research-id>-<UTC>-rNN` |
| `run.topic_slug` | R/F | 研究 ID | initializer 要求 lowercase kebab-case |
| `run.question` | R | 必填 | 本轮主问题 |
| `run.status` | C/F | `planned` | 由执行流迁移；不要用来伪造完成 |
| `run.round` | R/C | `1` | 正整数；自动计划按历史最大 round + 1 |
| `run.provider.source` | A | `arxiv` | `arxiv` / `biorxiv` / `medrxiv`；自动 PDF acquisition V0 仅支持 arXiv |
| `run.budget.max_queries` | A | `8` | initializer 为 `1..100`；validator 强制正数且 query 数不得超过它 |
| `run.budget.max_candidates` | A | `null` | 非 null 时 validator 强制总 candidate 数不超过它；Canary 会设置为新增 candidate 上限 |
| `run.budget.max_provider_query_calls` | A | `null` | Canary/DeepXiv executor 的 `Reader.search` 调用上限 |
| `run.budget.max_new_unique_candidates` | A | `null` | 本次执行允许接受的新增 stable candidate ID 上限；重复记录仍可合并 provenance |
| `run.budget.provider_max_retries` | A | `null` | Provider SDK retry；Canary 默认 `0` |
| `run.budget.max_rounds` | A（记录性） | `3` | initializer 为 `1..20`；当前不会代替 DoneCriteria 的 `max_search_runs` 硬停机 |
| `run.stop_reason` | C/R | `null` | 非 running 状态结束时记录原因 |
| `scope.included_concepts` | R | `[]` | 明确纳入边界 |
| `scope.excluded_concepts` | R | `[]` | 明确排除边界 |
| `scope.required_facets` | R | `[]` | Search candidate coverage 的维度；应与研究 scope / DoneCriteria 对齐 |
| `scope.years.from/to` | R | `null` | 研究范围记录；不会自动转换为 query filter |
| `scope.venues/categories/sources` | R | 空列表 / arXiv | 研究范围记录 |
| `scope.assumptions` | R | `[]` | 显式假设 |
| `scope.unresolved_questions` | R | `[]` | 未决 scope 问题；进入 Snapshot |

### 5.2 Query 与 DeepXiv filters

| 参数 | 类别 | 默认 | 约束 / 传给 SDK 的字段 |
|---|---|---:|---|
| `id` | R/F | `Q01` 或 `RNNQNN` | 字母开头，只含字母、数字、`_ . -` |
| `round` | R | `1` | 正整数 |
| `family` | R | `unclassified` | 自动规划时 1..80 字符并规范为 kebab-case |
| `text` | R | 必填 | 2..500 字符（自动规划）；validator 上限 500 |
| `purpose` | R | `null` | 自动规划时 2..500 字符 |
| `target_facets` | R | `[]` | 自动规划时 1..6 个，且必须属于 required facets |
| `derived_from` | R | `null` | 最多 64 字符；记录 query 演化 |
| `filters.source` | A/R | `arxiv` | `arxiv` / `biorxiv` / `medrxiv` |
| `filters.categories` | A/R | `[]` | SDK `categories` |
| `filters.authors` | A/R | `[]` | SDK `authors` |
| `filters.orgs` | A/R | `[]` | SDK `orgs` |
| `filters.venues` | A/R | `[]` | SDK 参数名 `venue` |
| `filters.venue_year` | A/R | `null` | SDK `venue_year` |
| `filters.min_citations` | A/R | `null` | `>=0`；SDK 参数名 `min_citation` |
| `filters.date_search_type` | A/R | `null` | Provider-specific string |
| `filters.date_str` | A/R | `null` | Provider-specific string |
| `filters.date_from` / `date_to` | A/R | `null` | Provider-specific date boundary |
| `filters.use_fine_rerank` | A/R | `false` | boolean |
| `filters.size` | A/R | `20` | `1..100`；单 query 返回量 |
| `filters.offset` | A/R | `0` | `0..10000` |
| `execution.*` | C | planned/null | 执行状态、时间、计数、raw path 和 error ID，由脚本写入 |

DeepXiv filters 是限制性交集。`scope.years` 等 scope 字段只是研究记录，不会自动
注入 query；真正传给 SDK 的是每个 query 的 `filters`。

### 5.3 Search planner / screener 的结构化限制

| 参数 | 类别 | 当前值 | 说明 |
|---|---|---:|---|
| 单次自动规划 query 数 | I | `1..4` | `SearchPlanDraft.queries` |
| planner 参考的历史 query 数 | I | 最后 `80` 条 | 避免 prompt 无界增长 |
| screening batch size | I | `12` | 合法范围 `1..16` |
| 每个 Search Run 最多 selected-for-ingest | A/I | `3` | Canary 可用 `--max-selected-candidates` 调整，合法范围 `1..20`；按模型显式选择、总分、candidate ID 排序 |
| 每批 screening 返回数 | F | `1..16` | 每个 supplied candidate 必须恰好判断一次 |
| 五个 relevance score | C/语义判断 | 每项 `0..2` | `sparsity_alignment`、`long_context_alignment`、`evidence_value`、`engineering_value`、`challenge_value` |
| score total | C | `0..10` | 五项直接求和；没有机械 core 阈值 |

只有 `label == core` 且不存在已有 Wiki paper 的 candidate 才能进入自动 ingest
候选。标签优先遵守语义理由，不允许只凭总分机械决定。

## 6. Search Run 的全部计算指标

这些字段位于 `coverage.metrics`，类别全部是 C。

| 指标 | 计算方式 |
|---|---|
| `executed_queries` | execution status 属于 `succeeded / empty / failed` 的 query 数 |
| `raw_retrieved_hits` | 所有非负 `execution.retrieved_count` 之和 |
| `unique_candidates` | 去重后 `candidates` 列表长度 |
| `duplicate_rate` | `round(max(raw_hits - unique_candidates, 0) / raw_hits, 6)`；raw hits 为 0 时为 0 |
| `relevance_counts.core` | label 为 `core` 的 candidate 数 |
| `relevance_counts.adjacent` | label 为 `adjacent` 的 candidate 数 |
| `relevance_counts.background` | label 为 `background` 的 candidate 数 |
| `relevance_counts.exclude` | label 为 `exclude` 的 candidate 数 |
| `relevance_counts.untriaged` | 没有合法 label 的 candidate 数 |
| `missing_metadata_count` | title、authors、year 任一缺失的 candidate 数 |
| `new_core_by_round` | 每个 core candidate 按首次发现它的 query round 计数 |

精确 stable ID 重复会合并；相同 DOI 也会合并；同标题同年份但 ID 不同只会建立
`possible_version_of`，不会自动合并。

## 7. ResearchSnapshot：所有计算指标

`research inspect` 不调用 LLM，也不写真源。它从全部 Search Run、Markdown Wiki 和
Wiki validator 构建下列只读快照。

### 7.1 CorpusSnapshot

| 指标 | 计算方式 |
|---|---|
| `search_run_count` | `search-runs/*.yaml|yml` 文件数 |
| `search_runs_by_status` | 按 `run.status` 计数 |
| `query_count` | 全部 query 数 |
| `query_statuses` | 按 execution status 计数 |
| `pending_queries` | status 为 `planned` 的 query 数 |
| `blocked_queries` | status 为 `blocked-credential` 或 `failed` 的 query 数 |
| `planned_query_ids` | planned query ID 排序集合 |
| `blocked_query_ids` | blocked/failed query ID 排序集合 |
| `unique_candidates` | 跨全部 run 按 `candidate_id` 去重后的数量 |
| `candidates_by_relevance` | `core/adjacent/background/exclude/untriaged/conflict` 计数 |
| `core_candidates` | 跨 run 唯一 candidate 中唯一 label 为 core 的数量 |
| `selected_for_ingest` | 跨 run 唯一 candidate 中至少一次处于 selected-for-ingest 的数量 |
| `staged_for_wiki` | 跨 run 唯一 candidate 中至少一次处于 staged-for-wiki 的数量；尚未计入 Wiki paper |
| `ingested_papers` | Wiki 唯一 `paper` 实体数 |
| `verified_papers` | Wiki paper 中 `status: verified` 的数量 |
| `search_run_paths` | 被检查的 run 相对路径 |
| `search_yields` | 每轮 `new_core_papers` 序列 |
| `declared_search_gaps` | `coverage.gaps` 去重排序集合 |

同一个 candidate 如果在不同 run 中出现不同 relevance label，会计入 `conflict`，
不会同时计入 core。

Candidate facet 状态是路由启发式，不是证据门槛。当前确定性聚合规则为：同一 facet
至少 2 个 `core` candidate → `covered`；至少 1 个
`core/adjacent/background` candidate → `partial`；否则 `missing`。它只决定后续优先
search、ingest 还是 verify，不能满足正式 Done 的 evidence facet requirement。

### 7.2 TaxonomySnapshot

| 指标 | 计算方式 |
|---|---|
| `required_facets` | 全部 run `scope.required_facets` 的并集 |
| `candidate_facet_coverage` | run 中每个 facet 的最高状态：`covered > partial > missing` |
| `candidate_facet_counts` | facet 关联的唯一 candidate ID 数 |
| `evidence_facet_coverage` | verified entity → covered；draft/needs-review → partial；否则 missing |
| `evidence_facet_counts` | facet 关联的 verified paper/experiment/claim 唯一数 |
| `facet_next_queries` | run 中 `next_query` 提示的去重集合 |
| `method_entities` | Wiki method + `concept.kind == method` 的唯一实体数 |
| `method_families` | 从 `method_family`、`family`、`taxonomy.family`、`sparsity.object` 依次解析并计数 |
| `unclassified_methods` | 无法解析 family 的 method 实体数 |
| `unresolved_scope_questions` | scope 未决问题并集 |

### 7.3 EvidenceSnapshot

| 指标 | 计算方式 |
|---|---|
| `experiments_total` | Wiki 唯一 experiment 数 |
| `verified_experiments` | experiment `status == verified` 数 |
| `experiments_with_evidence_locator` | `evidence` 是 mapping 且 `evidence.locator` 非空的 experiment 数 |
| `evidence_locator_ratio` | 上项 / experiments_total，保留 6 位；无 experiment 时为 0 |
| `claims_total` | Wiki 唯一 claim 数 |
| `verified_claims` | claim `status == verified` 数 |
| `claims_with_evidence` | 至少有一个 inbound `supports` 或 `contradicts` edge 的 claim 数 |
| `claims_by_assessment` | 按 claim `assessment` 计数；缺失计 `unknown` |
| `contested_claims` | assessment 为 contested，或存在 inbound contradicts edge 的 claim 并集大小 |
| `nonconsensus_assessments` | Wiki assessment 实体总数，包括不合法 draft |
| `verified_nonconsensus_assessments` | 合法 assessment contract 且页面 status verified、字段 verified=true 的数量 |
| `assessments_by_result` | 合法 assessment 按三种 result 计数 |
| `benchmarks_total` | Wiki benchmark 数 |
| `benchmark_ids` | benchmark canonical ID 排序列表 |
| `models_total` | Wiki model 数 |
| `model_families` | model `family` casefold 后计数；缺失为 unclassified |
| `context_length_buckets` | experiment 按固定区间计数，见下一节 |
| `engineering_metrics` | experiment metric 名称/类型按固定关键词分类计数，见下一节 |
| `revision_candidates` / `revision_candidate_ids` | `needs-review` method/claim 中具有可操作 verifier 矛盾或 locator 反馈、且修订次数少于 2 的数量与 ID |
| `revision_exhausted` / `revision_exhausted_ids` | 已达到两轮修订上限、仍为 `needs-review` 的 method/claim 数量与 ID |

### 7.4 QualitySnapshot

| 指标 | 计算方式 |
|---|---|
| `schema_errors` | Wiki validator severity=`ERROR` 数 |
| `schema_warnings` | severity=`WARNING` 数 |
| `diagnostic_codes` | 按 diagnostic code 计数 |
| `duplicate_entity_ids` | canonical ID 重复数 |
| `unresolved_wikilinks` | 无法解析目标的 Markdown wikilink 数 |

### 7.5 Hash 与身份指标

| 指标 | 计算方式 |
|---|---|
| `wiki_source_hash` | Wiki Markdown 真源的确定性 hash |
| `snapshot_id` | 对 `wiki_source_hash` 及每个 Search Run 的 path + SHA-256 进行规范 JSON 后再 SHA-256 |
| `research_id` | 运行输入；必须是 1..120 位 lowercase 字母、数字、连字符 |

## 8. 固定分类规则：边界不可通过 YAML 调整

### 8.1 Context bucket

| Bucket | 固定计算区间 | 类别 |
|---|---:|---|
| `<8K` | `< 8192` | I/C |
| `8K-32K` | `8192..32767` | I/C |
| `32K-64K` | `32768..65535` | I/C |
| `>=64K` | `>=65536` | I/C |

`context_bucket_requirements` 可以调整“每桶要求多少条”，但桶边界不能通过 YAML
调整。

### 8.2 Engineering metric 分类

程序把 experiment 的 `metric.name` 与 `metric.type` 拼接、casefold 后按关键词分类：

| 类别 | 固定关键词 |
|---|---|
| `latency` | `latency`、`wall-clock`、`time`、`hour` |
| `throughput` | `throughput`、`token/s`、`tokens/s` |
| `memory` | `memory`、`vram`、`gpu ram` |
| `flops` | `flop`、`compute`、`computation` |

`engineering_metric_requirements` 可以调整类别和要求数量，但新增一个程序不认识的
类别只会一直得到 0；新增分类规则需要修改 `_metric_categories()`。

## 9. ProgressMeasurement：计算项与权重

`measure_progress(before, after)` 只比较结构化快照。负 delta 通常不扣分；
`selected_for_ingest` 或 `revision_candidates` 减少会被视为成功消费 handoff/反馈，仍加正分。

| Delta | 固定权重 | 类别 |
|---|---:|---|
| `unique_candidates` | `0.5` | I/C |
| `core_candidates` | `1.0` | I/C |
| `selected_for_ingest` 正增长 | `0.5` | I/C |
| `selected_for_ingest` 负增长的绝对值 | `0.5` | I/C |
| `ingested_papers` | `1.0` | I/C |
| `verified_papers` | `2.0` | I/C |
| `method_families` | `3.0` | I/C |
| `evidence_facets_covered` | `2.0` | I/C |
| `experiments` | `1.0` | I/C |
| `verified_claims` | `2.0` | I/C |
| `contested_claims` | `3.0` | I/C |
| `nonconsensus_assessments` | `1.5` | I/C |
| `verified_nonconsensus_assessments` | `3.0` | I/C |
| `evidence_locators` | `1.0` | I/C |
| `benchmarks` | `1.0` | I/C |
| `revision_candidates` 负增长的绝对值 | `1.5` | I/C |

公式：

```text
progress_score = Σ max(delta_i, 0) × weight_i
               + max(-delta_selected_for_ingest, 0) × 0.5

made_progress = progress_score > 0
```

全部 ProgressMeasurement 字段：

| 字段 | 计算方式 |
|---|---|
| `baseline` | before 不存在时为 true |
| `action_attempted` | 来自本轮 ActionResult |
| `changed` | before.snapshot_id != after.snapshot_id |
| `deltas` | 上表各计数的 after - before |
| `progress_score` | 上述加权公式 |
| `made_progress` | score > 0 |
| `no_progress_rounds` | attempted 且无进展则 +1；有进展归零；未尝试保持不变 |
| `changed_sources` | Wiki hash 变化记 `wiki`；Search Run 关键状态变化记 `search-runs` |

权重目前是源码常量，不受 DoneCriteria 控制。`progress_score` 也不用于 search
saturation；saturation 单独使用 `new_core_papers`。

## 10. Gap 优先级与决策常量

Gap 的优先级是当前确定性 routing 的内部常量，不是 YAML 参数。

| Gap | 固定 priority | 推荐 action |
|---|---:|---|
| Wiki schema error | `1.00` | `verify` |
| 已规划 query 未执行 | `0.99` | `search` |
| selected candidate 待 ingest | `0.97` | `ingest` |
| facet evidence missing | `0.95` | search / ingest（按 candidate 状态） |
| core candidate 不足 | `0.94` | `search` |
| evidence locator ratio 不足 | `0.93` | `verify` |
| `>=64K` context 不足 | `0.92` | `search` |
| experiment 不足 | `0.91` | ingest / verify |
| method family 不足 | `0.90` | ingest / search |
| engineering metric 不足 | `0.90` | `search` |
| ingested paper 不足 | `0.89` | ingest / search |
| verified claim 不足 | `0.88` | `verify` |
| non-consensus assessment 不足 | `0.86` | analyze / verify |
| 其他 context bucket 不足 | `0.84` | `search` |
| facet evidence partial | `0.82` | `verify` |
| verified paper 不足 | `0.82` | `verify` |
| benchmark entity 为 0 | `0.81` | `ingest` |
| Wiki schema warning | `0.62` | `verify` |

排序规则是 `priority` 降序，再按稳定 gap ID 排序。决策的
`expected_information_gain` 为 priority clamp 到 `0.05..0.95`；完成时为 0，停机后
synthesize 通常为 0.1，没有可执行 pair 时为 0.05。

### 10.1 Gap、Decision 与 ActionResult 字段

这些字段是控制层产生和持久化的数据，不是人工调参入口。

| Contract | 字段 | 生成方式 |
|---|---|---|
| `ResearchGap` | `id` | `SHA-256("<gap-type>:<key>")` 前 12 位，加 `gap-` 前缀 |
| `ResearchGap` | `key`、`type`、`question` | 由确定性 gap rule 生成 |
| `ResearchGap` | `priority` | 上表固定常量，范围 `0..1` |
| `ResearchGap` | `reasons`、`evidence`、`search_focus` | 从当前 Snapshot 与 criteria 生成 |
| `ResearchGap` | `recommended_action` | 由 candidate/evidence 状态和 gap 类型路由 |
| `ResearchGap` | `blocking` | 由 gap rule 决定；facet、non-consensus、context、engineering、schema error 等可为 true |
| `ResearchGap` | `status` | `open / unresolved / resolved`；当前新 gap 默认为 open |
| `ResearchDecision` | `action`、`target_gap_id`、`reason` | 从最高优先级、未耗尽且有 executor 的 open gap 产生 |
| `ResearchDecision` | `expected_information_gain` | priority clamp 或 stop/finish 固定值 |
| `ResearchDecision` | `source` | 当前 `deterministic-v0`；contract 也允许 `structured-llm` |
| `ResearchActionResult` | `action_id` | `action-NNNN` 单调序号 |
| `ResearchActionResult` | `action`、`target_gap_id` | 保留 Decision 的 action 与目标 |
| `ResearchActionResult` | `status`、`outcome`、`attempted` | executor 根据前置条件、调用结果和是否有新信息生成 |
| `ResearchActionResult` | `tool_calls` | 本 action 实际 provider/model/acquisition 调用记账 |
| `ResearchActionResult` | `changed_sources` | 本 action 修改的 Wiki、Search Run、raw result 或 PDF 路径 |
| `ResearchActionResult` | `summary`、`error_codes`、`metrics` | 结构化执行摘要、稳定错误码和 action-specific 计数 |

## 11. DoneCheck、状态计数和动作指标

### 11.1 DoneCheck（全部为 C）

| 字段 | 含义 |
|---|---|
| `complete` | active 且 coverage/quality/saturation/blocking 全通过 |
| `coverage_passed` | facet、method family、core candidate、ingested paper 全通过 |
| `quality_passed` | verified paper、experiment、verified claim、locator、schema、non-consensus、context、engineering 全通过 |
| `saturation_passed` | SearchYield 窗口通过 |
| `blocking_gaps_passed` | 未要求清零，或没有 open blocking gap |
| `blocking_gap_ids` | 当前 open blocking gap 的稳定 ID |
| `budget_exhausted` | 任一硬预算命中 |
| `stop_reason` | `completed / budget_exhausted / blocked / stalled / human_review_required` 或 null |
| `failures` | 所有未通过门槛的人类可读说明 |
| `budget_hits` | 命中的预算字段名 |

### 11.2 Inner Agent Loop 状态（全部为 C）

| 字段 | 计算方式 |
|---|---|
| `iteration` | 每经过一次 ToolNode observe +1 |
| `tool_failures` | tool JSON 中 `ok:false` 的累计数 |
| `stop_reason` | 达到 inner limit 时为 `max-tool-iterations` |
| `context_message_count` | trim 后发给模型的消息数 |
| `recalled_memory_count` | 本轮注入的 memory 条数，固定最多 8 |
| `last_tools` | 最近一次 agent action 调用的 tool 名称 |

Inner LangGraph `recursion_limit` 自动计算为：

```text
HARNESS_MAX_TOOL_ITERATIONS * 4 + 8
```

### 11.3 Outer Controller 状态（全部为 C）

| 字段 | 计算方式 |
|---|---|
| `control_passes` | inspect/evaluate 控制轮数 |
| `research_iterations` | `ActionResult.attempted == true` 时 +1 |
| `tool_calls` | ActionResult.tool_calls 累加 |
| `action_sequence` | 每次 execute 编号 +1 |
| `attempts_by_gap_action.attempts` | 每个 pair 的实际尝试次数 |
| `attempts_by_gap_action.no_progress` | 每个 pair 连续无结构化进展次数 |
| `attempts_by_gap_action.tool_failures` | outcome=`tool_failure` 次数 |
| `attempts_by_gap_action.negative_results` | outcome=`negative_research_result` 次数 |
| `decision_history` / `action_history` | checkpoint 中的确定性历史 |
| `model_runtime_fingerprint` | 对 adapter、served model ID 和规范化 base URL 计算 SHA-256；精确恢复时必须一致，不包含 key |

Outer autonomous graph 的 `recursion_limit` 自动计算为：

```text
max_research_iterations * 8 + 32
```

### 11.4 ActionResult metrics（全部为 C）

| Action | 可能出现的 metrics |
|---|---|
| `search` | `queries_planned`、`queries_selected`、`queries_attempted`、`queries_succeeded`、`queries_failed`、`empty_results`、`new_candidates`、`candidates_triaged`、`candidates_selected_for_ingest`、`candidates_excluded` |
| `ingest` | `candidates_selected`、`entities_created`、`entities_reused`、`pages_changed`、`candidate_states_updated`、`pdf_pages`、`paper_sources_attempted`、`paper_sources_acquired`、`ingest_model_calls`、`schema_repair_attempted`、`schema_repair_applied`、`structured_output_invalid_attempts` |
| `verify` | `verification_targets`、`entities_verified`、`entities_unresolved`、`pages_changed` |
| `revise_evidence` | `revision_targets`、`fields_revised`、`pages_changed` |
| `analyze_claims` | `assessments_created`、`pages_changed` |

`tool_calls` 的口径是 action 内的实际 provider/model/acquisition 调用记账，不等同于
Inner Agent Loop 的 ToolNode 次数。例如 search 为 provider calls + planner model
calls + screening model calls。

## 12. CLI 参数总表

### 12.1 Harness CLI

所有 Harness 子命令都可先带全局 `--db`、`--model`、`--workspace`。

| 命令 | 参数 | 类别 | 默认 / 说明 |
|---|---|---|---|
| `doctor` | `--format text|json` | R | `text` |
| `run` | `task`、`--thread`、`--allow-network`、`--format` | R | task/thread 必填；network 默认 false |
| `chat` | `--thread`、`--allow-network` | R | 交互模式 |
| `state` | `--thread`、`--message-limit`、`--format` | R | limit 默认 20，仅影响显示 |
| `memories` | `--query`、`--limit`、`--format` | R | query 默认空，limit 默认 20 |
| `tools` | `--format` | R | 只列工具 |
| `skills list` | `--format` | R | 只读 |
| `skills show` | `name`、`--format` | R | 只读 |
| `skills read` | `name`、`resource`、`--format` | R | 只读、资源必须在 registry 内 |
| `research inspect` | `research_id`、`--format` | R | 只读快照 |
| `research evaluate` | `research_id`、`--criteria`、`--iteration`、`--tool-calls`、`--no-progress-rounds`、`--format` | R | 三个计数默认 0；no-progress 为兼容输入 |
| `research step` | `research_id`、`--criteria`、`--thread`、`--format` | R | 单次非变更控制 pass；thread 缺省为 `research:<id>` |
| `research run` | `research_id`、`--criteria`、`--thread`、`--allow-network`、`--format` | R | 自动循环；thread 缺省为 `research:<id>` |
| `research resume` | `research_id`、`--criteria`、`--thread`、`--mode`、`--allow-network`、`--format` | R | thread 必填；mode 默认 `replan` |

`resume --mode checkpoint` 会精确恢复 pending node，因此 `--allow-network` 必须与
checkpoint 保存的授权状态一致；默认 `replan` 会重新读取 Markdown/YAML 真源。

Thread ID 必须是 1..200 位 ASCII 字母、数字、`.`、`:`、`_`、`-`，且首字符为
字母或数字。

### 12.2 Wiki CLI

全局参数：`--wiki-root`、`--meta-root`；相对路径按仓库根解析。

| 命令 | 参数 | 默认 / 约束 |
|---|---|---|
| `index` | `--format text|json` | 生成可重建 JSON index |
| `validate` | `--strict`、`--format` | strict 时 warning 也导致失败 |
| `search` | `text`、`--type`、`--status`、`--year`、`--format` | 文本搜索过滤 |
| `show` | `reference`、`--body`、`--format` | reference 可为 ID/title/alias/legacy path |
| `backlinks` | `reference`、`--format` | 入链 |
| `neighbors` | `reference`、`--format` | 一跳关系 |
| `related` | `reference`、`--depth`、`--format` | depth 默认 2 |
| `query` | `--type`、`--status`、`--benchmark`、`--method`、`--model`、`--min-context`、`--max-context`、`--sparsity-target`、`--min-sparsity`、`--max-sparsity`、`--format` | 结构化实体过滤 |
| `stats` | `--format` | 计算 Wiki 统计 |

### 12.3 Search / validation 脚本 CLI

| 脚本 | 参数 | 默认 / 约束 |
|---|---|---|
| `new_search_run.py` | `--topic-slug`、`--question`、可重复 `--query`、`--source`、`--max-queries`、`--max-rounds`、`--output` | source=`arxiv`，max queries=8，max rounds=3；输出必须在仓库内 |
| `deepxiv_search.py` | `--run`、可重复 `--query-id`、`--retry-failed`、`--dry-run`、`--timeout`、`--max-retries`、`--retry-delay`、`--raw-dir`、`--fail-fast` | timeout=60s；retries=3，合法 0..10；delay=1.0s 且 >=0 |
| `validate_search_run.py` | `run`、`--fix-metrics`、`--strict`、`--json` | fix-metrics 会原子重写计算指标 |
| `validate_ingest_draft.py` | `draft`、`--format text|json` | text |
| `validate_verification_draft.py` | `draft`、`--kind paper|assessment`、`--json` | kind 必填 |
| `validate_revision_draft.py` | `draft`、`--json` | 只校验受限修订 Draft，不发布 |
| `validate_assessment_draft.py` | `draft`、`--json` | 只校验，不发布 |

## 13. Agent tools 的运行参数

这些参数由模型在 Inner Loop 调用 tool 时填写；括号内是程序 clamp 后的范围。

| Tool | 参数 | 默认 / 范围 |
|---|---|---|
| `wiki_search` | query、entity_type、status、year、limit | limit=10，clamp `1..25` |
| `wiki_show` | reference、include_body | include_body=false |
| `wiki_related` | reference、depth、limit | depth=2 且必须 `1..4`；limit=25，clamp `1..50` |
| `wiki_experiment_query` | benchmark、method、model、min/max context、sparsity target、min/max sparsity、status、limit | limit=25，clamp `1..50` |
| `wiki_validate` | max_diagnostics | 30，clamp `1..100` |
| `wiki_stats` | 无 | 只读 |
| `search_run_status` | run_path、candidate_limit | 20，clamp `1..50` |
| `deepxiv_search_run` | run_path、query_ids、dry_run | dry_run=true；非 dry-run 必须 allow_network |
| `remember_research_memory` | text、topic、kind、evidence_ids | text 1..4000；topic <=200；evidence ID 最多 20 |
| `recall_research_memory` | query、limit | limit=8，clamp `1..20`；最多扫描最近 200 条 |

Memory `kind` 固定为：`observation / decision / preference / open-question`。相同
text/topic/kind/evidence IDs 会得到相同 hash key，并增加 `confirmations`，而不是重复
插入。

## 14. 内部执行上限

这些值可以通过依赖注入或改源码调整，但目前没有稳定环境变量/CLI/YAML 入口。

| 参数 | 类别 | 当前值 | 约束 / 作用 |
|---|---|---:|---|
| Action executor timeout | I | `300 s` | 必须 >0；DeepXiv subprocess 总超时 |
| SearchRuntime validator timeout | I | `120 s` | 必须 >0；executor 创建时取 `min(action_timeout, 180)` |
| PDF acquisition timeout | I | `90 s` | executor 默认创建时取 `min(action_timeout, 120)` |
| 最大 PDF 字节 | I/F | `200 MiB` | 下载与 ingest 都检查；必须以 `%PDF-` 开头 |
| 自动 PDF destination | I | `sources/papers/` | 必须在仓库内；V0 仅允许 approved arxiv.org HTTPS host |
| PDF text extraction timeout | I | `90 s` | 仅 pdftotext backend 使用 |
| ingest excerpt pages | I | `16` | 最少 4；必含前 3 页和最后 2 页，再按关键词选页 |
| ingest excerpt chars | I | `70000` | 最少 4000 |
| Wiki catalog prompt chars | I | `24000` | 超出截断 |
| ingest schema repair attempts | I/F | `1` | 初次结构化输出校验失败后只允许一次；repair prompt 不再携带 PDF，只能依据原输出修结构，仍失败即停止 |
| verification entity body chars | I | `6000` | 每个实体 prompt 片段 |
| verification excerpt chars | I | `90000` | PDF 验证 prompt 上限 |
| verification decisions | F | 最多 `80` | 每个 supplied entity 必须恰好一个 decision |
| verification pages/decision | F | 最多 `24` | 页码必须在 PDF 范围内 |
| evidence revision attempts/entity | F | 最多 `2` | 达到上限后不再自动修订，形成 blocking human-review gap |
| evidence revision fields | F | reason/type allow-list | locator 仅 evidence；method contradiction 仅 definition/evidence；claim contradiction 仅 statement/scope/evidence |
| evidence revision excerpt chars | I | `90000` | 复用 page-aware verification excerpt，所有 source_pages 必须位于其中 |
| non-consensus entity body chars | I | `4000` | 每个实体 prompt 片段 |
| non-consensus max claims | I | `24` | 必须 >0 |
| non-consensus max experiments | I | `40` | 必须 >0 |
| Skill document bytes | I/F | `512000` | 超过即拒绝注册 |
| Skill resource chars | I | 默认 `100000` | 单次允许 `1..1000000` |
| DeepXiv tool wrapper timeout | I | `300 s` | Inner tool 调脚本的总超时 |

## 15. 固定状态与数据协议

以下值不是调优指标，而是 schema 合法值。

| 协议 | 合法值 |
|---|---|
| Wiki lifecycle | `candidate / draft / needs-review / verified / deprecated` |
| claim assessment | `open / supported / contested / refuted` |
| non-consensus result | `supported-consensus / contested / insufficient-evidence` |
| verification verdict | `supported / contradicted / insufficient` |
| condition alignment | `aligned / partially-aligned / mismatched / unknown` |
| facet status | `missing / partial / covered / not-required` |
| Search Run status | `planned / running / partial / complete / blocked-credential / blocked-provider / needs-review` |
| Query status | `planned / succeeded / empty / failed / skipped-duplicate / blocked-credential` |
| Candidate relevance | `core / adjacent / background / exclude`；未筛选时为 null |
| Candidate review state | `metadata-only / abstract-screened / selected-for-ingest / ingested / excluded / needs-review` |
| Research action | `search / ingest / revise_evidence / analyze_claims / expand_citations / verify / synthesize / finish` |
| Action status | `success / partial / failed / blocked` |
| Action outcome | `positive / negative_research_result / tool_failure / precondition_blocked / unsupported` |

当前 executor 实际支持 `search / ingest / verify / revise_evidence / analyze_claims`。
`expand_citations / synthesize / finish` 存在于控制协议中，但不是同级可变更 executor。

Wiki 页面字段、relation 与 verified gate 详见 `wiki/_meta/schema.yaml`。这些是领域数据
协议，不应误当作运行参数；修改它们需要 schema version 和迁移策略。

## 16. Wiki 自身的计算统计

`python -m tools.wiki stats` 计算下列指标：

| 指标 | 计算方式 |
|---|---|
| `source_hash` | Wiki Markdown 真源 hash |
| `page_files` | 解析到的 Markdown entity 文件数 |
| `unique_entities` | canonical ID 去重后的实体数 |
| `duplicate_ids` | 重复 canonical ID 数 |
| `entities_by_type` | 按 type 计数 |
| `entities_by_status` | 按 lifecycle status 计数 |
| `entities_by_schema` | 按 schema version 计数 |
| `structured_edges` | frontmatter relation edge 数 |
| `edges_by_relation` | 按 relation type 计数 |
| `navigational_links` | Markdown wikilink 数 |
| `unresolved_navigational_links` | 无法解析目标的 wikilink 数 |
| `diagnostics_by_severity` | ERROR / WARNING / INFO 数 |

## 17. 调参建议

### 17.1 正式调研

优先只调整：

1. `done-criteria.yaml` 的门槛与预算；
2. Search Run 的 scope、queries 和 filters；
3. Harness 的模型上下文、inner tool iteration 与 tool output 上限。

不要为了“更快完成”修改计算指标、progress 权重、Gap priority 或 Wiki 状态。

### 17.2 联网 Canary（推荐）

不要复制并放宽正式 DoneCriteria。Canary 使用独立的
`.harness/canary/<run-id>/workspace`、SQLite、Wiki、Search Run 和 semantic artifact，
按 observation boundary 停止：

```powershell
D:\anaconda3\python.exe -B -m research_harness `
  --model "openai:<served-model-id>" `
  --model-base-url "http://127.0.0.1:8000/v1" `
  research canary long-context-sparse-models `
  --run-id retrieval-v1 `
  --allow-network `
  --stop-after retrieval `
  --max-planned-queries 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --max-actions 1 `
  --deadline-seconds 120 `
  --provider-max-retries 0
```

Canary 参数均为 A：

| 参数 | 默认值 | 约束 / 语义 |
|---|---:|---|
| `stop_after` | `screening` | `retrieval / screening / ingest / verification / revision / reverification / analysis` |
| `max_planned_queries` | `1` | `1..8`；query planner 和既有 run 选择都受限 |
| `max_provider_query_calls` | `1` | `1..8`；指 `Reader.search` 次数，不声称约束 SDK 内部 HTTP |
| `max_new_unique_candidates` | `5` | `1..100`；按 provider rank + candidate ID 接受；重复项可继续合并 provenance |
| `max_papers_ingested` | `1` | `1..5`；按隔离 workspace 内新增 paper 计算 |
| `max_actions` | `3` | `1..20`；必须足够到达指定 stage |
| `deadline_seconds` | `300` | `30..3600`；父进程硬超时并终止 worker tree |
| `provider_max_retries` | `0` | `0..10`；Canary 默认不重试，便于解释调用上限 |

`retrieval` 和 `screening` 不是新的 `ResearchAction`；它们是 `search` action 内的两个
观察边界。`stop_after=retrieval` 会在 `_execute_search()` 内跳过 screening。
各 stage 的最低 `max_actions` 依次为 `1 / 1 / 2 / 3 / 4 / 5 / 6`；没有可操作修订
反馈时 revision/reverification 会跳过实际 action，但仍完成对应观察边界。

旧的“放宽 done-criteria 后跑完整 Outer Loop”只保留为 legacy 诊断方式，不再是联网
验收首选，因为它会混淆“运行健康”与“正式研究完成”。

### 17.3 语义制品、trajectory 与人工标注

Search plan、candidate screening、paper ingest、evidence verification、evidence revision 和
non-consensus analysis 的结构化模型输出，在 Pydantic/语义合约验证后、确定性编译前
写为 content-addressed immutable JSON。Skill 文件 hash、schema/reference hash、模型
名、snapshot ID、Wiki source hash、PDF/Search Run hash 与输入 ID 一并记录。后续发布
路径只写入独立可变 `semantic-manifest.json`，不会修改语义制品本体。

Canary 的 `trajectory.jsonl` 从其 SQLite LangGraph checkpoint 导出，不是第二套运行
真源。正式 Outer Loop 也可导出：

```powershell
D:\anaconda3\python.exe -B -m research_harness research export-trajectory `
  long-context-sparse-models --thread outer-v1
```

相邻的 `human-annotations.yaml` 是 sidecar；标注以 `target_id + source_sha256` 绑定，
不得直接覆盖模型原始输出。标注分为 control action、candidate、ingest entity 和
verification 四类；`annotation_freshness()` 会把 source hash 已变化的标注判为 `stale`。
评估筛选/验证时应优先查看 false-promotion rate，而不是只看 raw recall。

## 18. 已知实现语义与后续可配置化候选

| 项目 | 当前状态 | 建议 |
|---|---|---|
| Search Run candidate budget | Canary 与 DeepXiv executor 已硬执行，普通 null budget 不限制 | 正式 run 如需同样边界，显式填写 budget 或使用 Canary CLI |
| Search Run `max_rounds` | initializer 校验、自动 run 继承，但不负责 Outer Loop 硬停 | 正式停止继续使用 `max_search_runs`，或未来增加统一 round budget |
| facet 数量门槛 | 只有 partial/covered 状态门槛 | 需要时增加 `facet_count_requirements` |
| experiment quality 门槛 | `minimum_experiments` 按全部 experiment 数 | 若审核要求更严，增加 `minimum_verified_experiments` |
| context/engineering counts | 当前按全部 experiment 数 | 可升级为 verified-only 计数，并明确 schema version |
| `verified_experiments` | 已计算，但当前 DoneCriteria 不读取 | 增加门槛前先确认 verification throughput |
| benchmark / model coverage | benchmark 仅在总数为 0 时产生 gap；model 目前没有 Done gate | 需要时新增显式数量或 family coverage criteria |
| saturation 的“completed round” | 只接受成功/empty、无失败/阻塞且完成 screening 的 valid discovery round | 激活正式 Done 前仍需真实运行校准 novelty threshold |
| progress 权重 | 源码固定 | 跑出经验分布后再决定是否外置，避免过早调参 |
| Gap priorities | 源码固定 | 先保留确定性；只有出现稳定错误路由再外置 |
| 工程 metric 关键词 | 源码固定 | 出现稳定漏分类后扩展 alias 表和测试 |

---

相关文档：

- `docs/SYSTEM_ARCHITECTURE.md`
- `research/long-context-sparse-models/done-criteria.yaml`
- `skills/search-paper/references/search-output-schema.md`
- `wiki/_meta/schema.yaml`
