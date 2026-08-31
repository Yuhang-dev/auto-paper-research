# Pilot Result 01：稀疏注意力的拓扑组合比“更大全局感受野”更能预测 8K 语言建模质量

## 结论

在 SCCA 论文报告的同一受控设置中（LLaMA2-7B、Position
Interpolation + LoRA、同一 RedPajama 子集、相同训练轮数与评估方式），不同稀疏注意力
拓扑的效果并不一致：

- `LongMixed` 在 PG19 8K 上相对 S2 将 perplexity 从 9.41 降到 8.73，
  相对降低约 **7.2%**；
- `SCCA fixed` 在同一条件下降到 9.17，相对降低约 **2.6%**；
- `SCCA flow` 反而升到 9.47，相对恶化约 **0.6%**。

因此，这个小样本支持的不是“跨块传播越强，长上下文效果就越好”，而是：

> **稀疏注意力的具体拓扑与头部组合方式会显著影响质量；名义上的线性复杂度或更大全局感受野，本身不足以保证更好的长上下文性能。**

这是一个单论文、论文内受控比较结果，不是对所有模型、上下文长度或任务的普遍定律。

## 研究问题

在相同模型、训练数据、位置插值和上下文长度下，扩大跨块信息流是否会稳定优于
LongLoRA 的 Shifted Sparse Attention（S2）？

## 关键证据

下表来自 SCCA 论文 Table 2。Perplexity 越低越好；括号内为相对 S2 的变化，计算为
`(method - S2) / S2`。

| 8K attention pattern | PG19 validation | 相对 S2 | Proof-pile test | 相对 S2 |
|---|---:|---:|---:|---:|
| S2 | 9.41 | baseline | 2.96 | baseline |
| SCCA fixed | 9.17 | -2.6% | 2.88 | -2.7% |
| SCCA flow | 9.47 | **+0.6%（更差）** | 2.91 | -1.7% |
| LongMixed | **8.73** | **-7.2%** | 2.90 | -2.0% |

LongMixed 在 PG19 的全部四个评估长度上也都优于 S2：

| Context | S2 | LongMixed | 相对变化 |
|---:|---:|---:|---:|
| 1,024 | 11.71 | 10.49 | -10.4% |
| 2,048 | 10.73 | 9.65 | -10.1% |
| 4,096 | 9.98 | 9.10 | -8.8% |
| 8,192 | 9.41 | 8.73 | -7.2% |

证据定位：SCCA PDF p. 5 的实验设置及 p. 6 Table 2。Harness 的独立
`verify-evidence` 调用已逐项核对上述数值、模型、数据集、上下文长度和 baseline。

## 为什么这个结果有用

### 1. 它否定了一个过强的工程直觉

SCCA flow 通过不同 attention heads 的不同 K/V 位移扩大感受野，论文同时描述其具有
线性复杂度。但在 PG19 8K 上，它仍略差于 S2。由此不能仅凭理论感受野或复杂度判断
一个稀疏模式的质量收益。

### 2. 它把“稀疏化”进一步拆成可比较的结构变量

这组结果提示后续技术地图不能只写“局部、跨块、扩张”三个标签，还应记录：

- Q 与 K/V 中哪些张量发生位移；
- 位移是固定、分头还是分组；
- 每种 pattern 分配多少 attention heads；
- 局部、跨块与 dilated pattern 如何混合；
- 同一设置下是否存在质量退化的 variant。

LongMixed 的优势来自组合 SCCA fixed、SDA-2 和 SDA-4，而不是单一“更全局”的模式。

### 3. 它暴露了质量结论与工程结论之间的证据断层

SCCA 论文报告了使用 8 张 32GB V100、DeepSpeed Stage 3 和 4-bit evaluation，但没有
给出可比较的 latency、throughput 或 peak-memory 数字。因此本次证据只能支持
perplexity 质量比较，不能支持“工程上更快”或“显存更省”的结论。

## 与 LongLoRA 的关系

SCCA 论文把 LongLoRA 的 S2 作为同设置 baseline，因此上面的 Table 2 比较是有效的
论文内比较。项目中已有的 LongLoRA 页面还记录了另一组不同尺度的结果：LongLoRA
可将 Llama2 模型扩展到 32K–100K，并在 65,536 上报告训练时间和显存数据。

这两组结果不能直接横向排名：

- SCCA pilot 的受控比较止于 8K，模型为 LLaMA2-7B；
- LongLoRA 的更长上下文实验使用不同训练与硬件条件；
- SCCA 没有报告 LongBench、RULER 或 passkey 等真实长文能力测试；
- LongLoRA 的 100K 结果主要证明 trainability 与 PG19 perplexity，不能自动外推为
  100K 下的通用任务能力。

## 不能从本次 Pilot 推出的结论

- 不能声称 LongMixed 在 32K、64K 或 100K 仍优于 S2；
- 不能声称 SCCA 在长文问答、检索或多跳推理上更强；
- 不能声称任何 SCCA variant 具有实测 latency、throughput 或 memory 优势；
- 不能把单个模型族和单篇论文的结果解释为跨模型共识；
- 不能把 24 个表格数据点当成 24 个独立论文实验来证明文献覆盖充分。

## Harness 小规模验收记录

本报告复用隔离 Canary `reverification-20260830-235346` 的可审计产物：

- DeepXiv 检索：1 个 query，5 个唯一候选，3 个进入 ingest 队列；
- Paper ingest：摄取 arXiv `2312.07305`，首次结构化输出失败后完成一次 schema repair；
- Wiki 编译：生成 1 paper、6 methods、6 claims、24 experiment records、
  2 benchmarks、1 model；
- Evidence verification：40 个实体全部得到 `supported`，0 个进入 `needs-review`；
- 所有 Canary isolation 和调用上限 invariants 为 `true`；
- 因首次验证没有产生可操作反馈，revision/reverification action 被安全跳过；
- Canary workspace 未修改正式 Wiki 真源。

## 证据与复核入口

- [Canary report](../../.harness/canary/reverification-20260830-235346/report.json)
- [SCCA verified paper page](../../.harness/canary/reverification-20260830-235346/workspace/wiki/papers/scca-shifted-cross-chunk-attention-for-long-contextual-semantic-expansion.md)
- [Immutable verification artifact](../../.harness/canary/reverification-20260830-235346/artifacts/semantic/evidence-verification/semantic-evidence-verification-8324af8267f4e2a6b347.json)
- [SCCA source PDF](../../.harness/canary/reverification-20260830-235346/workspace/sources/papers/arxiv-2312.07305.pdf)
- [LongLoRA draft page](../../wiki/papers/longlora.md)

## 下一步最小验证

下一轮只需要补两类论文各一篇，就能检验这个 Pilot 结论是否可迁移：

1. 一篇在 32K 以上报告真实长文任务结果的稀疏 attention 论文；
2. 一篇同时报告 latency/throughput 与 peak-memory 的 kernel-aware 稀疏推理论文。

比较时应把“质量收益”“实测工程收益”和“理论复杂度”作为三个独立证据轴，不再用
其中任意一个替代另外两个。
