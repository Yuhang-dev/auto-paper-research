# LLM-Wiki Research Harness v0.1.0-alpha.1

这是首个供异机拉取、安装和联网 Canary 验证的预发布版本，不是正式研究结论版本。
`done-criteria.yaml` 仍为 `draft`，系统不会把当前语料规模误判为“调研完成”。

> 本文保留 `v0.1.0-alpha.1` 当时的 DeepSeek-only 配置。当前工作树已支持显式的
> 本地 OpenAI-compatible endpoint；新配置请以
> `docs/OPENAI_COMPATIBLE_MODEL.md` 为准。

## 发布包保留内容

- `research_harness/`：LangGraph inner/outer loop、SQLite checkpoint/memory、Canary、轨迹和确定性控制层；
- `skills/`：search、ingest、verify、claim analysis 的 Skill、references、assets、scripts 与离线测试；
- `tools/wiki/`、`wiki/`：Wiki Engine、schema、关系类型和 Markdown 真源；
- `research/`：主题 scope、DoneCriteria 和第一轮检索计划；
- `docs/`、`error_book/`：架构、参数审计、评估说明和循环优化入口；
- 全部测试；
- `sources/papers/longlora-iclr-2024.pdf`：保留这一份已跟踪的 1.2 MB 种子论文，使现有 Wiki locator 可在离线环境复核；
- `.env.example` 和锁定依赖，但不包含任何真实 Token。

## 发布包排除内容

- `.git/`、`.vscode/`；
- `.env` 及其他凭据文件；
- `.harness/`、LangGraph checkpoints、SQLite/WAL/SHM；
- `tmp/`、`dist/`、Python/test/type-check 缓存；
- `wiki/_generated/` 与 `error_book/_generated/` 等可重建索引；
- 本地 Canary workspace、模型原始输出和运行日志；
- 除已跟踪 LongLoRA 种子论文外，运行时下载的论文 PDF。

压缩包由 `git archive` 从发布 tag 构建，因此只包含已提交文件；发布同时提供
`.zip.sha256`。构建规则由 `.gitattributes` 和 `tools/build_release.ps1` 固化。

## 已验证

在 Windows、Conda base、Python 3.13 环境完成以下离线验证：

- Research Harness：67 tests passed；
- Wiki Engine：22 tests passed；
- Search scripts：16 tests passed；
- 总计：105 tests passed；
- 不读取真实 Token，不调用 DeepXiv 或模型服务。

用户提供的隔离 ingest Canary 结果也已通过：1 次 DeepXiv query、5 个新候选、1 篇
论文摄取、14 个 Wiki entity 写入，正式 source of truth 保持不变。该 Canary 的
`.harness` 工作区按发布策略不进入仓库或压缩包。

## D 盘快速安装与离线检查

```powershell
$releaseRoot = "D:\auto-paper-research-v0.1.0-alpha.1"
Set-Location $releaseRoot

D:\anaconda3\python.exe -m pip install -r requirements-harness.txt
D:\anaconda3\python.exe -B -m research_harness doctor
D:\anaconda3\python.exe -B -m unittest discover -s research_harness\tests -v
D:\anaconda3\python.exe -B -m unittest discover -s tools\wiki\tests -v
D:\anaconda3\python.exe -B -m unittest discover -s skills\search-paper\scripts\tests -v
```

默认 SQLite 位于当前解压目录的 `.harness/`，无需设置 `HARNESS_DB_PATH`。若要覆盖，
必须把它设为当前 D 盘 release 目录下的 `.db`、`.sqlite` 或 `.sqlite3` 路径，不能照抄
其他 clone 的绝对路径。

`requirements-harness.txt` 包含 `deepxiv-sdk==1.0.0`，不需要配置 DeepXiv MCP；
`pypdf` 提供不依赖本机 TeX Live 的 PDF 文本抽取后端。

## 最小联网 Canary

以下遮罩输入命令需要 PowerShell 7。只在当前 PowerShell 会话输入凭据，不要写入
仓库、YAML、SQLite memory 或命令参数：

```powershell
$env:DEEPXIV_TOKEN = Read-Host -Prompt "DeepXiv Token" -MaskInput
$env:OPENAI_API_KEY = Read-Host -Prompt "DeepSeek API Key" -MaskInput
$env:OPENAI_API_BASE = "https://api.deepseek.com"
$env:HARNESS_MODEL = "openai:deepseek-v4-flash"

$runId = "release-retrieval-" + (Get-Date -Format "yyyyMMdd-HHmmss")
D:\anaconda3\python.exe -B -m research_harness `
  research canary long-context-sparse-models `
  --run-id $runId `
  --allow-network `
  --stop-after retrieval `
  --max-actions 1 `
  --max-provider-query-calls 1 `
  --max-new-unique-candidates 5 `
  --deadline-seconds 180 `
  --provider-max-retries 0 `
  --format json
```

确认 retrieval 后，用新的 `run-id` 做单篇 ingest Canary。真实单篇摄取可能超过
3 分钟，因此这里保留 900 秒硬 deadline：

```powershell
$runId = "release-ingest-" + (Get-Date -Format "yyyyMMdd-HHmmss")
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

不要一开始运行无边界的完整 research loop。进入 verification 前应先审查 ingest
生成的 Wiki 页面与 semantic artifact。

## Alpha 已知限制

1. `author-stated`、`experiment-supported` 与 `agent-analysis` claim 的验证语义仍需进一步拆分；当前自动验证的可靠路径主要覆盖带 experiment edge 的 claim。
2. Candidate 的 assessed facet 与 query target facet 尚未完全分离；candidate coverage 只能用于路由，不能视为 evidence coverage。Done gate 已只读取 Wiki evidence coverage。
3. 同论文、benchmark、context 下的多个 experiment 可能产生相同展示标题并触发 alias warning；实体 ID 仍保持不同。
4. 模型筛选尚未固定 temperature/seed，同一候选集的 label/selection 可能波动；Canary 应保留 semantic artifact 并比较重复运行。
5. `select_for_ingest` 当前更接近排序建议而非绝对 eligibility gate。
6. Citation expansion、最终综述 synthesis、PPT 生成和通用智能 Skill Router 尚未实现。

远端测试应以这些限制为观察目标；不要在修复并完成 verification Canary 之前把本版本标为 stable。
