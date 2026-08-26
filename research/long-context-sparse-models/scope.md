---
id: research-scope:long-context-sparse-models
type: research-scope
status: discovery-v0
title: 稀疏化模型在长上下文领域的性能与瓶颈
---

# 稀疏化模型在长上下文领域的性能与瓶颈

## Primary question

稀疏化模型在长上下文任务中的性能收益、工程瓶颈与失效条件是什么？

## Core scope

首轮将以下对象视为核心候选：

- 在 Transformer/LLM 长上下文中显式减少 attention 连接或计算的结构；
- 静态、规则化、块稀疏、局部-全局、分层与动态稀疏 attention；
- 直接报告长上下文质量、效率、显存或系统实现证据的方法；
- 对稀疏长上下文方法给出强基线、复现、失败分析或反例的工作。

## Boundary scope

以下对象先作为相邻方向单独标记，不与 attention 稀疏混为一类：

- token selection、token eviction 与 KV-cache sparsification；
- layer/head/activation/weight sparsity；
- MoE routing sparsity；
- RAG、上下文压缩、recurrent memory 与 state-space model。

只有当它们直接回答长上下文性能或瓶颈问题时，才进入主要证据比较。

## Exclusions

- 没有长上下文设置或证据的通用剪枝论文；
- 没有长上下文设置或证据的通用 MoE 论文；
- 仅有标题或摘要线索、但无法取得可核验正文的结论性使用；
- 与语言模型无关且没有可迁移系统证据的稀疏视觉/生物序列工作；
- 仅以供应商检索分数或引用数代替相关性和证据强度。

## Evidence axes

技术谱系按以下轴组织：

- 稀疏对象：attention edge、block、token、KV、head/layer、weight、expert；
- 模式：static/dynamic、regular/irregular、heuristic/learned；
- 阶段：training、context extension、prefill、decode；
- 任务：synthetic retrieval、long-document QA、summarization、reasoning、code；
- 质量：accuracy、F1、ROUGE、perplexity、retrieval success；
- 效率：latency、throughput、FLOPs、attended-token ratio；
- 资源：peak memory、KV memory、training cost；
- 工程：kernel、hardware utilization、framework、open-source reproducibility；
- 风险：length extrapolation、distribution shift、benchmark sensitivity、失效条件。

## Candidate non-consensus hypotheses

以下均为待证伪/待验证假设，不是综述结论：

1. 理论 attention FLOPs 的下降，可能不会转化为 wall-clock 加速，因为不规则访存、索引与 kernel launch 开销占主导。
2. Needle/passkey 等合成检索上的优势，可能不能预测真实长文问答、摘要与多跳推理表现。
3. 动态稀疏的选择成本会随上下文和硬件改变，某些长度区间可能不如优化后的 dense attention。
4. KV/token 稀疏主要缓解 decode memory，而结构化 attention 稀疏常主要缓解 prefill compute；把两者合并比较会掩盖真正瓶颈。
5. 许多“长上下文能力”收益可能来自位置外推、微调数据或额外训练，而不完全来自稀疏结构本身。
6. 在中等上下文长度上，规则 dense/FlashAttention 基线可能优于论文中未充分优化的 sparse baseline。

## Initial deliverables

- 可追溯候选论文集；
- 技术谱系与概念双向链接；
- 论文级实验与工程证据表；
- benchmark/metric 地图；
- 代表性开源实现清单；
- 非共识假设的支持、反对和未知证据矩阵；
- Error Book 与下一轮 Skill/脚本改进建议。

## Candidate-search stopping rule

在必需 facet 均至少达到候选级 `partial`、且连续两个定向检索 pass 没有新增 `core` 候选时停止；若请求预算、凭据、provider 或 citation graph 构成限制，则以相应限制停止并保留缺口。
