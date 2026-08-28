# LLM-Wiki Research Harness v0.1.0-alpha.2

这是面向异机安装和联网 Canary 验证的预发布版本，不是正式研究结论版本。
`done-criteria.yaml` 仍为 `draft`，系统不会把当前语料规模误判为“调研完成”。

## 本版重点：本地 OpenAI-compatible 模型

Harness 不再依赖 LangChain 根据模型名猜测 provider，而是统一显式构造：

```text
ChatOpenAI(
  model=<served model ID>,
  base_url=<explicit endpoint>,
  api_key=<OPENAI_API_KEY>
)
```

Search planning、candidate screening、paper ingest、evidence verification、
non-consensus claim analysis 和通用 LangGraph agent/tool loop 使用同一个连接
contract。

本地 endpoint 可以使用服务端实际暴露的任意模型 ID。DeepSeek 官方 endpoint
仍只允许精确的 `deepseek-v4-flash`。

## 配置与安全变化

- `HARNESS_MODEL` 必须使用 `openai:<served-model-id>`；
- 新增 `HARNESS_MODEL_BASE_URL`，模型启用时必须显式配置 endpoint；
- 不会隐式回退到 OpenAI 公网默认地址；
- 兼容读取旧变量 `OPENAI_API_BASE` 和 `OPENAI_BASE_URL`；
- endpoint 仅允许绝对 HTTP(S) URL；
- URL 中禁止包含用户名、密码、query 或 fragment；
- localhost 模型调用也必须显式传 `--allow-network`；
- `doctor` 新增 model、base URL、endpoint host 和配置完整性检查；
- API key 只从进程环境读取，不进入 Wiki、semantic artifact 或 checkpoint。

## 恢复与制品变化

Semantic artifact schema 从 `0.1` 升为 `0.2`（manifest schema 仍为 `0.1`），现在
记录非秘密的 served model ID 和规范化 base URL。Checkpoint 增加 model runtime
fingerprint。

`--mode checkpoint` 精确恢复要求模型和 endpoint 与中断前一致。alpha.1 的旧
pending checkpoint 没有该 fingerprint，因此升级后应使用：

```text
--mode replan
```

重新检查 Markdown/YAML 真源再继续。

## Canary 变化

Canary 隔离子进程会显式继承 model ID、model base URL、workspace 和隔离 SQLite
路径。API key 仍只通过进程环境继承，不写入 Canary request JSON。

## 已验证

Windows、Conda base、Python 3.13.5 离线测试：

- Research Harness：71 tests passed；
- Wiki Engine：22 tests passed；
- Search scripts：16 tests passed；
- 总计：109 tests passed。

这些测试不读取真实 Token，也不调用 DeepXiv 或模型 endpoint。

## 远端需要填写什么

| 内容 | 示例 | 说明 |
|---|---|---|
| Python 路径 | `D:\anaconda3\python.exe` | 按远端实际 Conda/Python 路径修改 |
| 模型地址 | `http://127.0.0.1:8000/v1` | 只有模型在同机运行时才使用 `127.0.0.1` |
| 模型 ID | `qwen2.5-32b-instruct` | 必须与 endpoint `/v1/models` 返回值完全一致 |
| 模型 Key | 模型服务提供的 Key | 无鉴权服务也需要非空 sentinel |
| DeepXiv Token | 注册获得的 Token | 仅联网论文检索需要 |
| SQLite 路径 | 通常不填 | 默认位于当前 D 盘项目的 `.harness/` |

不要把真实 Key 或 Token 写入 `.env.example`、README、YAML、命令参数或测试输出。

## 远端最短验证流程

解压并进入项目：

```powershell
Set-Location "D:\releases\auto-paper-research-v0.1.0-alpha.2"
D:\anaconda3\python.exe -m pip install -r requirements-harness.txt
```

填写模型 endpoint 和 key：

```powershell
$env:HARNESS_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"

$secureModelKey = Read-Host -Prompt "Local model API key" -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new("", $secureModelKey).Password
Remove-Variable secureModelKey
```

如果本地服务完全不鉴权，可以改用非秘密 sentinel：

```powershell
$env:OPENAI_API_KEY = "local-not-secret"
```

查询服务端真实模型 ID：

```powershell
$headers = @{ Authorization = "Bearer $env:OPENAI_API_KEY" }
$models = Invoke-RestMethod `
  -Method Get `
  -Uri "$($env:HARNESS_MODEL_BASE_URL.TrimEnd('/'))/models" `
  -Headers $headers
$models.data | Select-Object id
```

把下面内容替换为上一步返回的精确 ID，并填写 DeepXiv Token：

```powershell
$env:HARNESS_MODEL = "openai:YOUR_EXACT_MODEL_ID"

$secureDeepXivToken = Read-Host -Prompt "DeepXiv Token" -AsSecureString
$env:DEEPXIV_TOKEN = [System.Net.NetworkCredential]::new("", $secureDeepXivToken).Password
Remove-Variable secureDeepXivToken
```

执行离线配置检查；该命令不会请求模型：

```powershell
D:\anaconda3\python.exe -B -m research_harness doctor --format json
```

至少确认以下字段：

```json
{
  "model": "openai:YOUR_EXACT_MODEL_ID",
  "model_base_url": "http://127.0.0.1:8000/v1",
  "openai_key_configured": true,
  "model_configuration_ready": true,
  "deepxiv_token_configured": true
}
```

执行最小模型实连：

```powershell
D:\anaconda3\python.exe -B -m research_harness run `
  "只回复 LOCAL_MODEL_OK，不调用任何工具。" `
  --thread alpha2-model-smoke `
  --allow-network
```

随后执行一次有硬边界的检索 Canary：

```powershell
$runId = "alpha2-retrieval-" + (Get-Date -Format "yyyyMMdd-HHmmss")

D:\anaconda3\python.exe -B -m research_harness `
  research canary long-context-sparse-models `
  --run-id $runId `
  --allow-network `
  --stop-after retrieval `
  --max-planned-queries 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --max-actions 1 `
  --deadline-seconds 300 `
  --provider-max-retries 0 `
  --format json
```

检索通过后再运行单篇 ingest，不要直接启动完整 loop：

```powershell
$runId = "alpha2-ingest-" + (Get-Date -Format "yyyyMMdd-HHmmss")

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

## 本地服务能力要求

当前 semantic pipeline 要求服务支持：

1. `/v1/chat/completions`；
2. OpenAI-compatible `response_format={"type":"json_object"}`；
3. 返回符合 schema 的合法 JSON；
4. 足够的上下文窗口；
5. 通用 `run/chat` 如需使用工具，还必须支持 OpenAI tool calling。

`doctor` 只检查配置，不代表 endpoint 已经完成实连验证。

## 发布包

发布附件由 tag 通过 `git archive` 构建，包含 Harness、Skills、Wiki Engine、
Markdown/YAML 真源、scope、DoneCriteria、文档、离线测试、LongLoRA 种子论文、
`.env.example` 和锁定依赖。

发布包不包含 API key、Token、`.env`、`.git`、`.harness`、SQLite/WAL、Canary
workspace、运行日志、模型原始输出、缓存、临时目录或运行时下载的论文。

## 已知限制

- OpenAI-compatible 路径不等于服务一定支持 JSON mode 或 tool calling；
- `--allow-network` 当前同时授权模型 endpoint、DeepXiv 和受控论文下载；
- claim/facet provenance 和重复实验显示标题仍需继续完善；
- citation expansion、最终综述 synthesis、PPT 和智能 Skill Router 尚未实现；
- 正式完整循环前应先通过分阶段 Canary。
