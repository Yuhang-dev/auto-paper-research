# Pilot 技术地图：稀疏化模型在长上下文领域的性能与瓶颈

## 用途与边界

这是首期 Harness 的小规模纵切面，不是完整综述。它用 LongLoRA 与 SCCA 分支验证
“论文 → 方法 → 实验 → 结论 → 技术地图 → 缺口 → 下一轮检索”的联动结构。

当前只有 SCCA 论文完成结构化独立验证；LongLoRA 页面仍为 `draft`，只用于定位技术
谱系和提出下一轮问题。所有缺少直接证据的格子都保持 `missing` 或
`insufficient-evidence`。

## 当前技术谱系

| 家族 | 稀疏对象 | 模式 | 生效阶段 | 当前代表方法 | 当前证据状态 |
|---|---|---|---|---|---|
| 训练期局部分块稀疏 | attention edges / blocks | static、regular | context-extension training | LongLoRA S2 | draft；有本地 PDF，尚未独立结构化验证 |
| 固定跨块稀疏 | K/V chunks | static、regular | context-extension training | SCCA fixed | verified at 1K–8K perplexity |
| 分头跨块稀疏 | K/V chunks by head | static schedule、regular | context-extension training | SCCA flow | verified；PG19 8K 略差于 S2 |
| 扩张稀疏 | global dilated token positions | static、regular | context-extension training | SDA-2 / SDA-4 | method definition verified；缺少单独消融页 |
| 混合稀疏拓扑 | cross-chunk + dilated heads | static hybrid | context-extension training | LongMixed | verified；PG19 1K–8K 均优于 S2 |
| 动态稀疏 prefill | input-dependent attention selection | dynamic、可能 irregular | prefill | 未摄取 | missing；下一轮 Q03 |
| KV/token 稀疏 | KV entries / tokens | heuristic 或 learned | decode / KV management | 未摄取 | boundary family；下一轮 Q04 |
| Kernel-aware 稀疏推理 | blocks / tiles / kernels | hardware-aligned | prefill 或 decode | 未摄取 | missing；下一轮 Q06 |
| 失败分析与强 dense baseline | comparison conditions | 不适用 | evaluation | 未摄取 | missing；下一轮 Q07 |

## 核心问题与当前答案

### 1. 稀疏连接减少后，跨块信息如何传播？

S2 通过半数 heads 的 shifted grouping 连接相邻局部组；SCCA fixed 只移动部分 K/V；
SCCA flow 给不同 heads 不同位移；LongMixed 再加入 dilated patterns。当前证据说明
“存在跨块连接”并不足够，具体位移和 heads 的组合方式会改变 perplexity。

### 2. 理论复杂度是否转化为真实工程收益？

当前答案是 `insufficient-evidence`。SCCA 提供线性复杂度描述和硬件/训练设置，却没有
latency、throughput 或 peak-memory 对照。LongLoRA draft 中存在训练时间与显存数据，
但尚未进入当前结构化验证闭环，也不能替代不同方法的同条件 kernel comparison。

### 3. 长上下文质量由什么指标代表？

当前 verified evidence 仅覆盖 PG19 和 Proof-pile perplexity。它可以回答语言建模质量，
不能回答长文问答、摘要、多跳推理或代码任务能力。Passkey、LongBench、RULER 等任务
仍需独立 benchmark 实体和实验记录。

### 4. 稀疏方法的收益来自结构本身还是训练/位置扩展？

当前无法完全解耦。S2、SCCA 和 LongMixed 的实验都与 Position Interpolation、LoRA、
RedPajama 训练共同出现；论文还报告小上下文 perplexity 退化并将其归因于 Position
Interpolation。后续应优先寻找保持训练数据与位置方法不变、只替换 attention pattern
的消融。

### 5. Prefill 与 decode 是否是同一个稀疏化问题？

不是。当前分支主要减少 context-extension training 或 attention compute；KV/token
sparsification 主要处理 decode memory 和 KV 管理。Harness 将其保留为相邻家族，避免
把不同瓶颈的结果直接排名。

## 当前实验与指标地图

| 维度 | 已覆盖 | 未覆盖 |
|---|---|---|
| 模型 | LLaMA2-7B | 其他模型族、不同规模的同条件比较 |
| Context | 1K、2K、4K、8K | 32K、64K、100K 的 verified controlled evidence |
| 质量 | PG19 / Proof-pile perplexity | QA、retrieval、reasoning、summarization、code |
| 工程 | 训练硬件和软件设置 | latency、TTFT、throughput、peak memory、KV memory |
| Baseline | S2，论文内同设置 | 优化后的 dense/FlashAttention、动态稀疏强基线 |
| 实现 | 论文方法描述 | 仓库归属、commit、kernel、复现命令和硬件可迁移性 |

## 已得到的非共识线索

当前最有用的结果见
[Pilot Result 01](pilot-result-scca-pattern-composition.md)：在相同 8K 设置下，
LongMixed 明显优于 S2，而 SCCA flow 在 PG19 上略差于 S2。这反对了“感受野越全局，
质量越好”的简单叙事。

该线索的状态是：

- 论文内数值：`verified`；
- 跨论文共识：`insufficient-evidence`；
- 工程性能解释：`insufficient-evidence`；
- 真实长文任务外推：`insufficient-evidence`。

## 工程实现与复现状态

| 项目 | 当前状态 | 下一步确定性检查 |
|---|---|---|
| DeepSpeed / LoRA / PI 设置 | 论文定位已核对 | 归一化为 training-stack 字段 |
| GPU 与量化设置 | SCCA 报告 8×32GB V100、4-bit evaluation | 不把“报告硬件”计作“硬件性能证据” |
| 开源仓库 | 尚未核验 | 检查官方所有者、license、commit、入口脚本 |
| Sparse kernel | 尚未抽取 | 检查是否真正执行稀疏 kernel，而非 mask 后 dense kernel |
| 可复现命令 | 尚未记录 | 固定模型、长度、batch、dtype、硬件和 warmup |

## Harness 纵切面验收

| 环节 | 状态 | 证据 |
|---|---|---|
| Skill 驱动检索与筛选 | passed | 1 query、5 candidates、3 selected |
| PDF 获取与 ingest | passed with repair | 一次 schema repair 后生成结构化 Wiki |
| Wiki 双向实体 | passed | paper/method/claim/experiment/model/benchmark 已链接 |
| 独立证据核验 | passed | SCCA bundle 40/40 supported |
| 非共识发现 | partial | 有论文内反例；尚无跨论文 assessment |
| 技术地图 | pilot | 当前文件；仅覆盖 training-time static branch |
| 工程瓶颈 | partial | 缺少 wall-clock 与 kernel evidence |
| Error Book 反馈 | recorded | 三个可重复口径问题已记录 |
| 自动最终综述 | missing | synthesis executor 尚未实现，本次报告为人工受控 synthesis |

## 下一轮 Loop

下一轮不应继续泛搜同类 context-extension 论文，而应按缺口定向执行：

1. `Q03`：补一篇动态 sparse prefill 方法，要求有 TTFT/latency；
2. `Q04`：补一篇 KV/token sparsification 方法，单独记录 decode memory；
3. `Q06`：补一篇 kernel-aware 工作，要求同硬件 dense baseline；
4. `Q07`：补一篇失败分析或强 baseline 工作；
5. 每篇只把“独立实验配置”和“表格 measurement”分别计数；
6. 只有实际指标、实现或复现证据才能提升相应 engineering facet。

完成这四个定向样本后，才能进行第一轮跨家族比较和 non-consensus assessment。
