<!-- ppt-master-schema: design-spec/v1 -->
# Sparse Long-Context Research Harness Pilot - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | 稀疏化模型在长上下文领域性能与瓶颈：Research Harness 首期汇报 |
| Canvas Format | PPT 16:9（1280 × 720） |
| Page Count | 16 |
| Primary Language | zh-CN |
| Target Audience | 课题负责人、研究与工程团队，以及需要审阅该方案的导师或技术负责人 |
| Communication Intent | 以约 3:2 的篇幅先展示稀疏长上下文调研结果、非共识线索与证据缺口，再说明 Harness、Skill、LoopEngineer 和 Error Book 的工程实现及下一轮计划。 |
| Desired Audience Outcome | 受众能够区分已验证发现、待补证据和系统尚未完成的能力，并认可按技术谱系、工程指标与非共识假设继续扩展的下一轮计划。 |
| Core Message / Ask / Action | 首期价值不在于一次生成综述，而在于建立可追溯的研究循环：Skill 约束、确定性脚本执行、结构化 Wiki 存证、独立验证和 Error Book 反馈共同收敛。 |
| Delivery Context | 主讲的 12–15 分钟技术/课题评审；会后作为可独立阅读的项目记录。 |
| Artifact Afterlife | 首期 Harness 架构与试运行基线，用于后续迭代、评审和交接。 |
| Reading Mode | balanced |
| Content Strategy | 调研结果与工程实现约为 3:2；可重组现有材料以形成清晰叙事，但不扩展未经证据验证的研究结论。 |
| Design Style | conclusion-first pyramid narrative with vintage-poster geometry and evidence-coded research colors |
| AI Image Acquisition Path | not applicable |
| Generation Mode | continuous |
| Spec Refinement | disabled |
| Speaker Notes | enabled — final Stage-2 proactive policy |
| Custom Animations | disabled — final Stage-2 proactive policy |
| Narration Audio | disabled — final Stage-2 proactive policy |
| Created Date | 2026-08-31 |

## II. Canvas Specification

| Property | Value |
| --- | --- |
| Format | PPT 16:9 |
| Dimensions | 1280 × 720 |
| viewBox | `0 0 1280 720` |
| Margins | 64 px horizontal; 48 px vertical |
| Content Area | x=64–1216; y=48–672 |

## III. Visual Theme

### Theme Style

- **Mode**: pyramid
- **Visual style**: vintage-poster
- **Theme**: 研究蓝底层系统、证据橙反例、青绿已验证状态；以中世纪海报式圆盘、拱形、水平带和轻量半调网点建立识别度。
- **Tone**: 简洁、可信、有工程复盘感；结论先行，所有边界显式。

### Color Scheme

| Role | HEX | Purpose |
| --- | --- | --- |
| Background | #F8FAFC | 主画布与大面积留白 |
| Secondary background | #EAF0F6 | 次级证据区、表格带与流程背景 |
| Primary | #123B5D | 标题、系统主路径与大色块 |
| Accent | #E76F2E | 反例、风险、故障和未闭环项 |
| Secondary accent | #2A9D8F | 已验证、通过和正向进展 |
| Body text | #17212B | 正文与关键标签 |
| Secondary text | #52616F | 注释、限定语和来源 |
| Divider | #B9C8D4 | 细规则、网格和轻边界 |

## IV. Typography System

### Font Plan

| Role | Character (Reference) | Primary | English if non-English | Fallback tail |
| --- | --- | --- | --- | --- |
| Title | editorial serif accent paired with a compact Chinese poster title | Microsoft YaHei | Georgia | serif |
| Body | clean neutral sans for technical readability | Microsoft YaHei | Aptos | Arial, sans-serif |

- **Title stack**: `"Microsoft YaHei", Georgia, serif`
- **Body stack**: `"Microsoft YaHei", Aptos, Arial, sans-serif`

### Font Size Hierarchy

| Purpose | Anchor Size (px) |
| --- | ---: |
| Body | 24 |
| Title | 42 |
| Subtitle | 32 |
| Annotation | 18 |
| Footer | 14 |

## V. Layout Principles

### Deck-wide Direction

- **Hierarchy direction**: assertion title first, one hero visual or number second, then two to four evidence blocks, ending with a source or boundary line.
- **Composition tendency**: a few large vintage-poster planes, partial discs, arches and horizon bands; data pages stay flat and aligned while retaining one poster-scale focal element.
- **Cross-page continuity**: recurring navy horizon band, orange evidence marker, teal verified marker, subtle halftone field and lower-right page number; compositions vary between anchor, dense and breathing pages.
- **Spacing posture**: variable by page rhythm; dense technical pages use a firm grid, anchor pages preserve large negative space.
- **Spacing anchors**: page margin 64 px; block gap 24 px; column gutter 32 px; corner radius 12 px; body leading 34 px.

## VI. Icon Usage Specification

- **Primary bundled library**: tabler-outline
- **Stroke Width**: 2

| Icon Path | Suitable Scenarios |
| --- | --- |
| tabler-outline/search | 检索与候选发现 |
| tabler-outline/file-text | 论文、PDF 与结构化页面 |
| tabler-outline/database | SQLite 与持久状态 |
| tabler-outline/link | 双向链接与证据关系 |
| tabler-outline/shield-check | 验证、支持状态与隔离保证 |
| tabler-outline/refresh | LoopEngineer 循环与重新观察 |
| tabler-outline/alert-triangle | 故障、风险与 Error Book |
| tabler-outline/git-branch | 技术谱系与方法分支 |
| tabler-outline/book | Wiki 与综述产物 |
| tabler-outline/cpu | kernel、硬件与工程指标 |
| tabler-outline/chart-bar | 实验指标与覆盖度 |
| tabler-outline/code | 确定性脚本与开源实现 |

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Layout pattern | Crop Policy | Acquire Via | Status | Reference | text_policy | page_role |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## IX. Content Outline

### Part 1: 技术谱系、核心结果与非共识证据

#### Slide 01 - 封面

- **Audience move**: 不知道本次汇报范围 → 知道主题、方法和首期定位
- **Relationships**: 主标题、研究主题和 Harness Pilot 身份属于同一首期任务；“性能、瓶颈、证据”并列界定范围
- **Layout**: 海报式大标题占左侧；右侧以局部橙色太阳圆盘、蓝色拱形和三枚主题标签形成视觉锚点
- **Title**: 稀疏化模型在长上下文领域的性能与瓶颈
- **Core message**: 这是一套可审计的调研 Harness 首期汇报，而不是一次性自动生成的综述。
- **Content**: Research Harness Pilot · Skill × Wiki × LoopEngineer / 初步调研与系统复盘 / 2026.08

#### Slide 02 - 初步调研的三个判断

- **Audience move**: 尚不清楚首期得到了什么 → 先获得三条证据强度明确的研究判断
- **Relationships**: 拓扑影响质量、工程收益缺证据、真实任务外推不足三者并列；第一条由论文内比较支持，后两条仍是缺口
- **Layout**: 三个大判断块占据主体；第一个青绿标记 verified，后两个以橙色标记 insufficient-evidence
- **Title**: 初步调研得到 3 个判断，但只有 1 个已被论文内受控证据支持
- **Core message**: 当前最可靠的发现是“具体拓扑影响质量”；工程加速和真实任务能力仍不能下结论。
- **Content**: ① LongMixed 与 SCCA variants 表明拓扑组合会改变 8K PPL · ② 理论线性复杂度尚未转化为可比较 wall-clock 证据 · ③ PG19/Proof-pile perplexity 不能代表 QA、检索和推理能力

#### Slide 03 - 技术谱系按瓶颈拆分

- **Audience move**: 把所有“稀疏化”放在同一排行榜 → 按生效阶段和系统瓶颈拆成四条主线
- **Relationships**: training、prefill、decode 和 kernel 四个阶段并列；每个阶段连接不同稀疏对象、指标和失效条件
- **Layout**: 四条水平技术轨道；每条由阶段、稀疏对象、核心指标和当前状态组成
- **Title**: 稀疏长上下文不是一类问题，而是 4 条不同技术主线
- **Core message**: attention edge、KV/token 和 kernel sparsity 解决的不是同一个系统瓶颈。
- **Content**: 静态 context-extension training · 动态 sparse prefill · KV/token sparsification for decode · kernel-aware sparse execution

#### Slide 04 - 当前方法地图

- **Audience move**: 只知道几个论文名称 → 看见方法之间在位移、head 分配和拓扑组合上的结构关系
- **Relationships**: S2 是 LongLoRA baseline；SCCA fixed 与 flow 改变 K/V 跨块传播；SDA 提供扩张位置；LongMixed 组合 fixed 与 SDA heads
- **Layout**: 一棵从 S2 向 SCCA fixed、flow、SDA 和 LongMixed 展开的技术谱系；verified 与 draft 状态分色
- **Title**: 当前证据集中在静态训练期分支，动态 prefill 与 decode 仍缺样本
- **Core message**: 首期已验证 SCCA 分支，LongLoRA 仍是 draft；其余三条主线尚未进入独立验证闭环。
- **Content**: S2：shifted local groups（draft） · SCCA fixed：固定 K/V 跨块（verified） · SCCA flow：分头位移（verified） · SDA-2/4：扩张位置（definition verified） · LongMixed：fixed + SDA head mixture（verified）

#### Slide 05 - 结构变量

- **Audience move**: 只用“局部/全局/稀疏率”描述方法 → 知道后续比较必须记录的拓扑变量
- **Relationships**: 张量位移、schedule、head 分配、pattern mixing 和退化 variant 共同决定稀疏拓扑；任何单一标签都不能替代它们
- **Layout**: 中心为 attention topology；五个变量像海报射线向外展开，每条带一个具体问题
- **Title**: 决定质量的是具体拓扑变量，不只是“更全局”或“更稀疏”
- **Core message**: 后续 Wiki schema 必须能表达 Q/K/V 如何移动、哪些 heads 使用何种 pattern，以及混合比例。
- **Content**: Q 与 K/V 哪些张量位移 · fixed / per-head / grouped schedule · local、cross-chunk、dilated head 占比 · pattern 如何组合 · 同设置是否存在质量退化 variant

#### Slide 06 - 论文内受控结果

- **Audience move**: 只知道 SCCA 是稀疏方法 → 看见同一 8K 设置下不同拓扑产生相反质量结果
- **Relationships**: S2 是 baseline；SCCA fixed、SCCA flow、LongMixed 与它构成同条件对比；perplexity 越低越好
- **Layout**: 四个方法块沿同一基线排列；LongMixed 用青绿突出，SCCA flow 用橙色标出反向结果
- **Title**: 相同 8K 条件下，LongMixed 最好；SCCA flow 反而略差
- **Core message**: 稀疏注意力的拓扑与 head 组合会显著改变质量，不能只看名义复杂度或感受野。
- **Content**: PG19 PPL：S2 9.41 baseline · SCCA fixed 9.17（-2.6%） · SCCA flow 9.47（+0.6%，更差） · LongMixed 8.73（-7.2%） / 同设置：LLaMA2-7B、PI + LoRA、RedPajama 子集 / Source：SCCA PDF p.5–6, Table 2；Harness verify-evidence

#### Slide 07 - 非共识判断与证据边界

- **Audience move**: 把论文内结果推广为普遍规律 → 只接受与证据强度匹配的判断
- **Relationships**: “拓扑重要”由论文内受控结果支持；跨论文、工程性能和真实任务外推三个判断仍缺证据
- **Layout**: 中央主判断；四个围绕状态章分别标记 verified 与 insufficient-evidence
- **Title**: 非共识线索：更大全局感受野不足以保证更好质量
- **Core message**: 当前能否定一个过强直觉，但不能宣称跨模型共识、真实任务优势或工程加速。
- **Content**: 论文内数值：verified · 跨论文共识：insufficient-evidence · latency/throughput/memory：insufficient-evidence · QA/retrieval/reasoning：insufficient-evidence

### Part 2: Benchmark、工程瓶颈与开源缺口

#### Slide 08 - Benchmark 与指标地图

- **Audience move**: 把 perplexity 当成长上下文能力的完整代理 → 区分语言建模、真实任务和工程指标
- **Relationships**: PG19/Proof-pile PPL 属于质量轴；QA、retrieval、reasoning、summary、code 属于任务轴；latency、TTFT、throughput、memory 属于工程轴
- **Layout**: 三层指标地图：已验证层、待验证任务层、待验证工程层；每层采用独立色带
- **Title**: 当前 verified 指标只有 perplexity，不能代表完整长文任务能力
- **Core message**: quality、task capability 与 engineering efficiency 是三个独立证据轴，不能互相替代。
- **Content**: 已验证：PG19 validation、Proof-pile test PPL（1K–8K） · 待补任务：LongBench/RULER/检索、QA、多跳推理、摘要、代码 · 待补工程：TTFT、latency、throughput、peak memory、KV memory

#### Slide 09 - 工程瓶颈研究结论

- **Audience move**: 从理论线性复杂度直接推断加速 → 看见 sparse kernel、访存和阶段差异带来的失效条件
- **Relationships**: 理论 FLOPs 与 wall-clock 之间受不规则访存、索引、kernel launch 和硬件利用率影响；prefill compute 与 decode KV memory 是不同瓶颈
- **Layout**: 左侧“理论承诺”，中间四个系统摩擦点，右侧“必须实测的指标”；底部对比 prefill 与 decode
- **Title**: 理论复杂度只有穿过 kernel 与硬件，才可能成为真实加速
- **Core message**: SCCA 只有复杂度描述和硬件设置，没有 latency、throughput 或 peak-memory 对照。
- **Content**: irregular memory access · index/select overhead · kernel launch · hardware utilization · mask-after-dense 风险 / Prefill：TTFT、attention compute / Decode：KV memory、token selection / 需要同硬件 dense/FlashAttention baseline

#### Slide 10 - 开源项目与待解决问题

- **Audience move**: 把代码仓库链接当作可复现证据 → 理解开源项目必须通过六项工程审计
- **Relationships**: official owner、license、commit、entrypoint、kernel path 和 reproducible command 共同构成开源证据链；缺一项就不能支持可复现结论
- **Layout**: 六枚工程审计章围绕“Open-source ≠ reproducible”；下方对应 Q03/Q04/Q06/Q07 四个下一样本
- **Title**: 开源项目不是一个 URL，而是一条可复现证据链
- **Core message**: 当前 SCCA/LongLoRA 的仓库归属、commit、kernel 与复现命令尚未纳入 verified Wiki。
- **Content**: owner · license · pinned commit · runnable entrypoint · real sparse kernel · hardware-fixed command / 下一样本：Q03 dynamic prefill · Q04 KV/token · Q06 kernel-aware · Q07 failure/strong baseline

### Part 3: Harness、LoopEngineer 与工程复盘（6 页）

#### Slide 11 - 总体架构与状态边界

- **Audience move**: 只看到一个 Agent → 看见领域真源、运行状态与研究控制循环的分层
- **Relationships**: 用户与 CLI 进入 Harness；Inner Loop 调用工具；工具读写 Wiki、DeepXiv、search-run 与 SQLite；Outer Loop重新观察真源并决定下一动作
- **Layout**: 中央 Harness 核心；上方用户入口，下方 Markdown/YAML、D 盘 SQLite 与 Error Book；左右分别为 Inner 与 Outer Loop
- **Title**: Harness 把领域真源、运行状态与研究控制分开
- **Core message**: Wiki 保存科学知识，SQLite 保存可恢复状态，Outer Loop 只依据可计算真源做决策。
- **Content**: LangChain/LangGraph orchestration · Markdown/YAML source of truth · DeepXiv SDK · D 盘 SQLite checkpoint + grounded memory · gap-directed outer control

#### Slide 12 - Skill 与脚本的分工

- **Audience move**: 把 Skill 当成提示词文件 → 理解 Skill 是操作协议，脚本是确定性执行边界
- **Relationships**: 五个 Skill 对应可复用动作；确定性脚本承接可计算工作；LLM 只处理语义抽取、歧义判断和综合
- **Layout**: 三列责任面：Skill / Python tools / LLM；底部用一条边界线强调“能计算的，不交给模型猜”
- **Title**: Skill 定义动作协议，脚本守住确定性边界
- **Core message**: 重复、可验证的工作必须沉淀为函数、schema 和校验器。
- **Content**: Skills：search-paper / ingest-paper / verify-evidence / revise-evidence / analyze-claims · Scripts：ID、schema、去重、backlink、coverage、Done、预算、脱敏 · LLM：论文语义、claim 分类、歧义与 synthesis

#### Slide 13 - LoopEngineer 循环

- **Audience move**: 把失败理解成“再调用一次模型” → 理解失败必须转化为规则、脚本或 Skill 改动
- **Relationships**: Observe → classify → change → rerun → measure 形成闭环；无进展与重复失败反向影响下一决策
- **Layout**: 五段环形循环围绕中心“Research truth”；旁边放一次 schema repair 的真实例子
- **Title**: 每个失败都要改变下一轮，而不是只重试模型
- **Core message**: LoopEngineer 的单位是“可观察问题 → 可执行改进 → 可测结果”。
- **Content**: 观察 artifact 与指标 · 归类 schema/evidence/coverage/alias · 修改脚本、Skill、schema 或 query · 隔离重跑 · 比较 progress 与 recurrence

#### Slide 14 - Canary 纵切面

- **Audience move**: 认为完整研究才能判断系统好坏 → 获得一次真实联网小规模运行的量化证据
- **Relationships**: search → screening → ingest → verify 按顺序执行；一次 schema repair 后通过；隔离 workspace 阻止正式 Wiki 污染
- **Layout**: 386 秒作为海报级主数字；流水线周围显示 1 query、5 candidates、3 selected、1 paper、40 entities、40/40 supported
- **Title**: 一次 386 秒 Canary 打通 search → ingest → verify
- **Core message**: 小规模运行已证明检索、结构化 Wiki 与独立验证可以联动，并保持正式真源不变。
- **Content**: 1 query · 5 candidates · 3 selected · 1 paper · 40 entities created · 40/40 supported · 1 schema repair · 0 unresolved / formal source truth unchanged

#### Slide 15 - 问题与 Error Book 状态

- **Audience move**: 误以为流程通过或 Error Book 已完成自演化 → 看见四类问题和当前仅记录层的真实状态
- **Relationships**: schema、coverage、experiment granularity、alias 四类问题进入 Error Book；JSONL append 已实现，但聚合、复发检测、规则建议、测试绑定和 Skill 更新仍缺失
- **Layout**: 左侧四张问题票据，右侧从 recorded 到 self-improving 的五级阶梯；只有第一级为青绿
- **Title**: Error Book 已能记录问题，但还没有形成自动优化闭环
- **Core message**: 当前 Error Book 是可审计日志，不是自动修复器；工程完成度必须如实标注。
- **Content**: claim scope 空 → schema repair · facet tag 与 metric=0 冲突 · 1 表拆成 24 records 导致计数膨胀 · method/concept alias collision / 已有：README + errors.jsonl + 3 recurrence keys / 未有：aggregator、recurrence/severity、rule proposer、test binding、Skill updater

#### Slide 16 - 下一轮行动与交付

- **Audience move**: 只知道系统还有缺口 → 接受四个定向样本和两条工程闭环任务
- **Relationships**: Q03、Q04、Q06、Q07 并列补齐研究证据；Error Book aggregator 与 final synthesis executor 补齐工程闭环；两类工作完成后汇合为正式综述
- **Layout**: 上方 3:2 双轨路线：研究轨四个行动块、工程轨两个行动块；底部汇合到 cross-family assessment、report 与 presentation
- **Title**: 下一轮先补 4 类证据，再把 Error Book 和自动总结闭环
- **Core message**: 研究侧补动态 prefill、KV/token、kernel 和失败基线；工程侧完成 Error Book 聚合与 evidence-grounded synthesis。
- **Content**: Research：Q03 TTFT/latency · Q04 decode memory · Q06 same-hardware dense baseline · Q07 counter-evidence / Engineering：Error Book aggregator + recurrence policy · final report/PPT synthesis executor / Gate：独立配置与 measurement 分开计数；只有实测指标提升 engineering facet

## X. Speaker Notes Requirements

- **Generation**: enabled
- **Filename**: match each SVG filename under `notes/`
- **Content**: 每页先讲结论，再补一到三个来源事实；明确区分 verified、draft、missing 与 insufficient-evidence，不朗读全部页面文字。
- **Total duration**: 12–15 minutes
- **Notes style**: concise, conversational, technically precise
- **Presentation purpose**: 以前 9 页调研结果、后 6 页工程实现的约 3:2 比例，展示技术谱系、非共识证据与缺口，并说明 Harness、LoopEngineer 和 Error Book 的当前实现及下一轮计划。
