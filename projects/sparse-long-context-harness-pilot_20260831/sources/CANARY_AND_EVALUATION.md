# Bounded Canary 与评估制品

## 目标

Canary 用来回答“真实 provider、模型、Skill、确定性编译器和 checkpoint 链路是否健康”，
不回答“综述是否完成”。它不会放宽或激活正式 `done-criteria.yaml`，也不会修改正式
Markdown Wiki、Search Run 或主 SQLite。

每次运行位于：

```text
.harness/canary/<run-id>/
├── canary.sqlite3
├── report.json
├── trajectory.jsonl
├── human-annotations.yaml
├── artifacts/
│   ├── semantic-manifest.json
│   └── semantic/<kind>/semantic-*.json
└── workspace/
    ├── wiki/
    ├── research/<research-id>/
    └── sources/papers/
```

`.harness/` 已被 Git 忽略。运行前后会计算正式 Wiki 与目标 research 目录的树 hash；
`formal_source_truth_unchanged` 必须为 true。

## 阶段语义

```text
search action
  ├── retrieval observation boundary
  └── screening observation boundary
          ↓
       ingest
          ↓
     verification
          ↓
   optional revision
          ↓
    reverification
          ↓
       analysis
```

`retrieval` 与 `screening` 不是新增 action。`--stop-after retrieval` 在
`DeterministicActionExecutor._execute_search()` 内直接跳过 candidate screening。

## 推荐的递进测试

先在当前 PowerShell 会话设置凭证，不要把 Key 写入命令、仓库或 `.env`：

```powershell
$env:DEEPXIV_TOKEN = Read-Host -Prompt "DeepXiv Token" -MaskInput
$env:OPENAI_API_KEY = Read-Host -Prompt "Local model API key" -MaskInput
$env:HARNESS_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
$env:HARNESS_MODEL = "openai:<served-model-id>"
```

本地 endpoint 也必须传 `--allow-network`。服务能力要求和 DeepSeek 兼容配置见
`docs/OPENAI_COMPATIBLE_MODEL.md`。

第一步只验证真实检索：

```powershell
D:\anaconda3\python.exe -B -m research_harness `
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

第二步包含一次结构化 screening：

```powershell
D:\anaconda3\python.exe -B -m research_harness `
  research canary long-context-sparse-models `
  --run-id screening-v1 `
  --allow-network `
  --stop-after screening `
  --max-planned-queries 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --max-actions 1 `
  --deadline-seconds 180 `
  --provider-max-retries 0
```

只有前两步的 report、raw result、semantic artifact 和人工抽查正常后，才逐步改为
`ingest`、`verification`、`revision`、`reverification`、`analysis`。对应
`max_actions` 至少为 2、3、4、5、6。

第三步只摄取一篇论文，并给首次完整 PDF 抽取和一次可能的 schema repair 留出明确
deadline：

```powershell
$runId = "ingest-" + (Get-Date -Format "yyyyMMdd-HHmmss")
D:\anaconda3\python.exe -B -m research_harness `
  research canary long-context-sparse-models `
  --run-id $runId `
  --allow-network `
  --stop-after ingest `
  --max-planned-queries 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --max-papers-ingested 1 `
  --max-actions 2 `
  --deadline-seconds 900 `
  --provider-max-retries 0 `
  --format json
```

`900` 秒是 Canary 进程的硬上限，不是期望耗时；schema 首次成功时不会发生第二次
模型调用。失败报告中的 `semantic_artifact_ids` 可直接定位原始无效输出和字段错误。

验证阶段出现可操作的 method/claim 矛盾或 locator 缺陷后，可用新 run 一次测试
修订与独立复验：

```powershell
$runId = "reverification-" + (Get-Date -Format "yyyyMMdd-HHmmss")
D:\anaconda3\python.exe -B -m research_harness `
  research canary long-context-sparse-models `
  --run-id $runId `
  --allow-network `
  --stop-after reverification `
  --max-planned-queries 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --max-papers-ingested 1 `
  --max-actions 5 `
  --deadline-seconds 1800 `
  --provider-max-retries 0 `
  --format json
```

若 initial verification 没有产生符合条件的反馈，`revision` 和 `reverification` 会
作为“无候选”边界安全跳过，不额外调用模型；这仍是通过结果。

## 硬边界

- `max_provider_query_calls` 统计 `Reader.search` 调用，不宣称限制 SDK 内部 HTTP；
- Canary 强制 `provider_max_retries=0` 的默认值，使一次 search 调用近似一次 provider
  attempt；
- provider 请求的 `size` 不超过剩余新增候选容量；
- exact ID / DOI duplicate 可以合并新的 discovery provenance；
- 新 identity 按 provider rank、candidate ID 的确定性顺序接受，超过容量即丢弃；
- 父 CLI 以独立 worker 运行 Canary，到达 deadline 后终止 worker process tree；
- 即使 worker 被强制终止，所有写入也只发生在隔离 workspace。

## 如何判定好坏

不需要等完整综述跑完。每个 boundary 都有局部验收：

| Boundary | 最小验收证据 |
|---|---|
| retrieval | provider call/new candidate 上限通过；raw response 可审计；provenance 正确 |
| screening | 所有输入 candidate 恰好返回一次；无越界 ID；抽查 false promotion |
| ingest | PDF hash/页码存在；语义制品先于 Wiki 编译；schema 无 ERROR；若结构化输出失败，报告精确字段路径且无效输出以 `schema_valid: false` 留存；最多一次 repair |
| verification | source locator、数值和条件 gate 生效；unsupported 不会升 verified |
| revision | 只改 reason/type allow-list；旧 verification 进入 revision_history；状态回到 draft |
| reverification | 由独立 verify 调用决定是否晋升；revision 本身不能标记 verified |
| analysis | 不强制 contested；条件不齐时允许 insufficient-evidence；结果仍 needs-review |

`report.json` 中所有 invariants 应为 true。运行结果为 `blocked` 时，首先看
`error_codes`；这通常表示凭证、模型、PDF 或前置证据不足，不等于程序崩溃。

## 评估真源

- 运行控制真源：Canary SQLite checkpoint；
- 领域数据真源：隔离 Markdown/YAML；
- 模型判断原始记录：immutable semantic artifact，并记录非秘密的 model/base URL；
- artifact 到发布页面的关联：mutable semantic manifest；
- 人工 adjudication：sidecar annotation；按 control action、candidate、ingest entity、
  verification 分别记录 action/relevance/extraction/locator/verdict/promotion 判断；
- `trajectory.jsonl`：checkpoint 的可重建导出，不参与控制决策。

标注通过 `target_id + source_sha256` 绑定被审核对象。评估时用当前 source hash 调用
`annotation_freshness()`；hash 不同即为 `stale`，不能继续计入校准指标。
