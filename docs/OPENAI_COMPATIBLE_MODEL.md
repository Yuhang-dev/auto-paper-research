# 本地 OpenAI-compatible 模型接入

Harness 不再把模型连接隐式交给 LangChain 环境解析。所有语义阶段统一构造：

```text
ChatOpenAI(
  model=<served model ID>,
  base_url=<explicit endpoint>,
  api_key=<OPENAI_API_KEY>
)
```

Search planning、candidate screening、paper ingest、evidence verification、claim
analysis 和通用 agent loop 使用同一个连接 contract。

## 本机配置

先从服务端确认实际 model ID，然后复制模板：

```powershell
Copy-Item .env.example .env.local
notepad .env.local
```

在 `.env.local` 中填写服务端接受的模型 ID、Base URL 和 API key。如果本地服务不校验
认证，OpenAI client 仍要求非空 key，可以填写一个非秘密 sentinel：

```text
OPENAI_API_KEY=local-not-secret
```

`.env.local` 由 Git 忽略。运行产物、YAML、Wiki、SQLite 和日志不保存 key。

也可以把非敏感模型配置作为全局 CLI 参数放在子命令之前：

```powershell
D:\anaconda3\python.exe -B -m research_harness `
  --model "openai:<served-model-id>" `
  --model-base-url "http://127.0.0.1:8000/v1" `
  doctor
```

Key 只能通过 `OPENAI_API_KEY` 传入，不提供 CLI key 参数。

`doctor` 只检查配置、依赖、Wiki 和 SQLite，不向模型 endpoint 发请求。输出中的
`model_configuration_ready: true` 表示 model、base URL 和非空 key 均已就绪；它不
等于服务端已经通过实连测试。

语义 artifact 会记录非秘密的 model 与规范化 base URL。精确 checkpoint 恢复也会
校验这两个值；若切换了本地服务或 served model，请使用 `--mode replan`，不要从旧
pending node 精确续跑。API key 不进入 artifact 或 checkpoint。

## 配置优先级

模型 endpoint 按以下顺序解析：

```text
--model-base-url
  > HARNESS_MODEL_BASE_URL
  > OPENAI_API_BASE
  > OPENAI_BASE_URL
```

后两个变量用于兼容旧配置；若二者同时存在且 URL 不同，Harness 会拒绝启动。
配置模型时 endpoint 必须显式存在，不会回退到 OpenAI 公网默认地址。

URL 只允许绝对 `http://` 或 `https://`，禁止把用户名、密码、query 或 fragment
写进 URL。

## 运行授权

localhost 也属于 socket-backed 调用，因此必须显式授权：

```powershell
$env:DEEPXIV_TOKEN = Read-Host -Prompt "DeepXiv Token" -MaskInput
D:\anaconda3\python.exe -B -m research_harness research canary `
  long-context-sparse-models `
  --run-id local-model-smoke `
  --allow-network `
  --stop-after screening `
  --max-actions 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 3 `
  --deadline-seconds 300 `
  --provider-max-retries 0
```

`--allow-network` 当前同时授权模型 endpoint、DeepXiv 和受控论文下载；Canary 的
provider/action/deadline 上限仍然有效。

## 本地服务能力要求

仅仅支持 OpenAI 路径和字段名还不够。当前 Harness 需要：

1. `/v1/chat/completions`；
2. `response_format={"type":"json_object"}`，语义 pipeline 使用 JSON mode；
3. 返回合法 JSON，能够满足给定 Pydantic schema；
4. 通用 `run/chat` 若要调用 Wiki/DeepXiv tools，还需要 OpenAI tool calling；
5. 足够的上下文窗口容纳论文 excerpt、Wiki catalog 和输出 schema。

不支持 tool calling 的本地模型仍可尝试独立的 search/ingest/verify semantic
pipeline，但不能可靠运行通用 agent tool loop。模型服务返回的供应商私有字段不属于
Harness contract。

## DeepSeek 兼容配置

DeepSeek 仍通过同一个 OpenAI-compatible client 使用：

```powershell
$env:HARNESS_MODEL = "openai:<DeepSeek 服务端接受的模型 ID>"
$env:HARNESS_MODEL_BASE_URL = "https://api.deepseek.com"
$env:OPENAI_API_KEY = Read-Host -Prompt "DeepSeek API Key" -MaskInput
```

配置层不会根据 endpoint 域名限制模型名。Harness 只校验 `openai:` adapter、非空
served model ID 和显式 HTTP(S) base URL；模型是否存在及其结构化输出、tool calling
能力由服务端响应和能力探测确定。
