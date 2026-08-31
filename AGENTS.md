# Project Agent Memory

本文件是本仓库的长期协作约定。后续 Agent 在开始工作前应先阅读并遵守。

## 沟通与工作方式

- 默认使用中文，先给结论，再给必要的依据、命令或下一步。
- 不要防御性写作，不要防御性思考。不要为了规避假想问题而增加用户没有要求的限制、门槛、免责声明或复杂架构。
- 禁止使用否定的否定（双重或多重否定）。直接说“可以”“不可以”“已完成”“缺少什么”。
- 遇到真实风险时给出具体证据和最小必要约束，不要把个人偏好写成系统硬限制。
- 用户要求实现时直接实现；用户要求“先看、先计划、不要改”时保持只读。
- 用户要求命令时，优先给可直接复制的 PowerShell 命令，不代替用户运行耗时联网任务。
- 测试默认采用最小、快速、离线验证。除非用户明确要求，不运行完整联网调研或长时间测试。

## 项目目标

本项目用于构建“稀疏化模型在长上下文领域性能与瓶颈”的科研综述 Harness，重点包括：

- 长文任务与 benchmark；
- 稀疏化模型结构、技术谱系和代表论文；
- 性能、质量、latency、throughput、memory、KV cache 与 kernel 瓶颈；
- 开源项目和工程成熟度；
- 跨论文、可比较条件下的非共识假设与结论；
- 可复用论文、EvidenceCard、技术地图、trajectory 和 Error Book。

## 架构边界

- Fast Research Loop 负责检索、筛选、Skim、Deep Read、EvidenceCard 和综述生成。
- Durable Evidence Loop 负责人工批准后的 Full Ingest、Verify、staged-for-wiki 和正式 Wiki 发布。
- Skim 只用于导航和筛选，不能作为正式定量结论的证据。
- 正式结论必须能追溯到 EvidenceCard、原始来源和 locator。
- 同一论文中的不同配置不能被描述为跨论文非共识。
- Fast Loop 不写正式 Wiki；只有显式 promotion 和 publish 才进入正式 Wiki。
- Markdown/YAML 是领域知识真源；SQLite 保存 checkpoint 和显式运行记忆。

## Skill 与 LoopEngineer

- Skill 是可复用操作流程，不是一篇论文一个 Skill。
- Skill 通过显式映射使用，不引入不必要的智能 Router。
- 身份规范化、去重、预算、schema 校验、引用完整性、可比性、readiness、saturation 和 recurrence 聚合优先使用确定性代码。
- 模型负责需要语义判断的环节，不能把所有任务都交给模型。
- Error Book 记录真实重复错误并提出 Skill/脚本优化建议；系统不得自动修改 Skill。

## 配置约定

- OpenAI-compatible 模型写作 `openai:<served-model-id>`，并显式配置 base URL。
- 不按供应商域名硬编码模型白名单；模型是否存在及能力是否满足由服务端和能力探测决定。
- SQLite 可以放在任意用户选择的可写盘符；仍使用持久化文件，不使用 `:memory:` checkpoint。
- 密钥只从当前进程环境读取，不写入代码、Markdown、YAML、SQLite、artifact、日志或 Git。
- Windows 环境通常使用 Conda 环境 `paper-harness`；激活后直接运行 `python`，不要依赖未定义的 `$harnessPython`。

## Git 与交付

- 保留用户已有改动，不覆盖无关文件。
- 不把 Agent/Codex 添加为 co-author，也不添加 `Co-authored-by` trailer。
- 提交前运行与变更成比例的快速测试和 `git diff --check`。
- 只有用户要求上传时才 push；推送后给出 commit hash、分支和可复制的 pull 命令。
