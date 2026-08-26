---
id: "paper:longlora"
type: paper
title: "LongLoRA: Efficient Fine-tuning of Long-Context Large Language Models"
authors:
  - "Yukang Chen"
  - "Shengju Qian"
  - "Haotian Tang"
  - "Xin Lai"
  - "Zhijian Liu"
  - "Song Han"
  - "Jiaya Jia"
year: 2024
venue: "International Conference on Learning Representations (ICLR)"
identifiers:
  arxiv: "2309.12307"
  doi: null
urls:
  paper: "https://proceedings.iclr.cc/paper_files/paper/2024/file/211ab571cc9f3802afa6ffff52ae3e5b-Paper-Conference.pdf"
status: draft
---

# LongLoRA: Efficient Fine-tuning of Long-Context Large Language Models

## Problem

### Problem addressed

LongLoRA addresses how to extend a pretrained large language model to much longer context windows without paying the full computational cost of dense-attention, full-parameter fine-tuning. Standard self-attention scales quadratically with sequence length, while plain LoRA reduces trainable parameters but does not remove the long-sequence attention cost and performs poorly for context extension. Evidence: PDF pp. 1-2, Abstract and Section 1.

### Motivation

Long-context models are useful for tasks such as long-document understanding and question answering, but the paper reports that prior context-extension training required hardware at a scale that is inaccessible to many researchers. It therefore targets a method that can retain the pretrained model's standard attention architecture at inference while reducing training cost. Evidence: PDF p. 2, Section 1.

### Assumptions and scope

- The base models are pretrained Llama2 models with 7B, 13B, or 70B parameters.
- Position Interpolation is used to rescale position indices for the longer target windows.
- The main context-extension experiments use RedPajama for next-token training and evaluate language modeling or retrieval behavior.
- The largest configurations are trained on one machine with eight A100 GPUs.
- The empirical evidence is specific to the reported Llama2 configurations and does not establish equivalent behavior for every model family or positional encoding.

## Method

### Overview

LongLoRA combines training-time Shifted Sparse Attention (S2-Attn) with an expanded form of LoRA that also trains embedding and normalization layers. S2-Attn reduces the attention computation used during fine-tuning, while the trained model returns to its original dense self-attention architecture at inference. Position Interpolation supplies the longer positional range. Evidence: PDF p. 2, Figure 2; PDF pp. 5-6, Sections 3.2-3.3.

### Components

1. **Shifted Sparse Attention:** Split a long sequence into local groups. Half of the attention heads use unshifted groups; the other half shift the group partition by half a group, enabling information flow across neighboring groups. See [[concepts/shifted-sparse-attention]].
2. **Improved LoRA:** Train the LoRA weights together with embedding and normalization layers. The paper denotes this configuration as LoRA+ within its experiments.
3. **Position Interpolation:** Rescale position indices to the target context window.
4. **Optional supervised fine-tuning:** Further tune LongLoRA models on the LongAlpaca instruction-following dataset for long-context question answering.

### Difference from prior work

Unlike sparse-attention architectures designed as permanent replacements for dense attention, S2-Attn is used during fine-tuning and can be removed at inference. Unlike standard LoRA, LongLoRA exposes embedding and normalization parameters because increasing LoRA rank alone did not close the long-context performance gap to full fine-tuning. Evidence: PDF p. 5, Section 3.2; PDF p. 6, Table 2; PDF p. 9, Table 6.

## Key Claims

### C1

- **Statement:** S2-Attn performs local group attention during fine-tuning, shifts the grouping in half of the heads to communicate across groups, and permits the resulting model to use its original full-attention architecture at inference.
- **Attribution:** author
- **Evidence type:** author-stated
- **Evidence location:** PDF p. 2, Figure 2; PDF p. 5, Section 3.2 and Algorithm 1.
- **Scope:** The LongLoRA training procedure for decoder-only Llama2 models.
- **Evidence status:** located

### C2

- **Statement:** For Llama2 7B adapted to a 32,768-token target and evaluated on PG19 validation perplexity, rank-8 LoRA with trainable normalization and embedding layers reached 8.12, close to full fine-tuning at 8.08; rank-8 standard LoRA reached 11.44, and increasing standard LoRA rank to 16-256 did not close the gap.
- **Attribution:** author
- **Evidence type:** experiment-supported
- **Evidence location:** PDF p. 6, Table 2.
- **Scope:** Llama2 7B; RedPajama training; 32,768 target context; PG19 validation perplexity.
- **Evidence status:** located

### C3

- **Statement:** On one eight-A100 machine, LongLoRA fine-tuned Llama2 7B, 13B, and 70B to maximum context lengths of 100,000, 65,536, and 32,768 tokens, with reported PG19 perplexities of 2.52, 2.38, and 2.17 at those respective maximum evaluation lengths.
- **Attribution:** author
- **Evidence type:** experiment-supported
- **Evidence location:** PDF p. 7, Table 4.
- **Scope:** Flash-Attention2 and DeepSpeed stage 3; training and evaluation settings described for Table 4.
- **Evidence status:** located

### C4
                                                                                                                                                                                                                                                                                                                                               - **Attribution:** author
- **Evidence type:** experiment-supported
- **Evidence location:** PDF p. 8, Figure 4 and Section 4.2.
- **Scope:** Ten trials per tested document length with different random passkeys; Llama2 7B LongLoRA model.
- **Evidence status:** located

## Experiments

### Setup

- **Models:** Llama2 7B, 13B, and 70B.
- **Context extension:** Position Interpolation with target lengths from 8,192 to 100,000, depending on model size.
- **Training data:** RedPajama for context extension; LongAlpaca for the optional supervised fine-tuning stage.
- **Optimization:** next-token prediction with AdamW; learning rate 2e-5 for 7B/13B and 1e-5 for 70B; per-device batch size 1; gradient accumulation 8; reported global batch size 64 on eight GPUs; 1,000 context-extension steps.
- **Language-model evaluation:** PG19 and Proof-pile perplexity using a sliding window with stride 256.
- **Retrieval and downstream evaluation:** LongChat topic retrieval, passkey retrieval, LongBench, and LEval.
- **Evidence location:** PDF pp. 6-8, Sections 4.1-4.2; PDF pp. 14-16, Appendix B.

### Results

| ID | Model | Dataset / Benchmark | Setting | Baseline | Metric | Result | Evidence |
|---|---|---|---|---|---|---|---|
| E1 | Llama2 7B | PG19 validation | 32,768 target; rank-8 LoRA with normalization and embedding trainable | Full FT: 8.08; standard rank-8 LoRA: 11.44 | Perplexity, lower is better | 8.12 | PDF p. 6, Table 2 |
| E2 | Llama2 7B | PG19 | 100,000 training and evaluation context | No same-row baseline | Perplexity, lower is better | 2.52 | PDF p. 7, Table 4 |
| E3 | Llama2 13B | PG19 | 65,536 training and evaluation context | No same-row baseline | Perplexity, lower is better | 2.38 | PDF p. 7, Table 4 |
| E4 | Llama2 70B | PG19 | 32,768 training and evaluation context | No same-row baseline | Perplexity, lower is better | 2.17 | PDF p. 7, Table 4 |
| E5 | Llama2 13B LongLoRA | LongChat topic retrieval | 16k evaluation; model fine-tuned to 18k | LongChat-13B: 0.90 | Retrieval score, higher is better | 0.94 | PDF p. 7, Table 5 |
| E6 | Llama2 7B LongLoRA | Training efficiency | 65,536 context; 1,000 iterations; eight A100 GPUs | LoRA: 92.5 h / 71.1 GB; Full FT: OOM | Training hours / peak memory | 52.4 h / 69.8 GB | PDF p. 16, Table 12 |
| E7 | Llama2 7B LongLoRA | Passkey retrieval | Fine-tuned to 32,768; 10 trials per length | Base Llama2 7B degrades after 4k | Accuracy | No reported degradation until 33k-34k | PDF p. 8, Figure 4 |

## Limitations

### Reported limitations

- The extended models show some perplexity degradation at short evaluation contexts; the paper attributes this to Position Interpolation. Evidence: PDF p. 7, Section 4.2.
- With Flash-Attention2, the memory difference between LongLoRA and plain LoRA can be small even when the training-hour reduction is substantial. At 65,536 context, Table 12 reports 69.8 GB for LongLoRA and 71.1 GB for LoRA. Evidence: PDF pp. 15-16, Appendix B.5 and Table 12.

### Agent analysis

- The experiments cover Llama2 7B, 13B, and 70B; the conclusion describes compatibility with other LLMs and positional encodings as future work, so cross-family generality remains unverified. Evidence: PDF p. 9, Conclusion.
- The 100k result demonstrates trainability and reports PG19 perplexity, but the paper's detailed passkey-retrieval experiment uses a model fine-tuned to 32,768 tokens. The evidence therefore should not be generalized into broad downstream competence at 100k. Evidence: PDF p. 7, Table 4; PDF p. 8, Figure 4.

## Wiki Links

### Concepts and methods

- [[concepts/shifted-sparse-attention]]

### Benchmarks

- None yet; the V0 benchmark directory has no pages.

### Related papers

- None yet; the V0 paper directory had no related-paper pages before this ingestion.

## Open Questions

- Should reusable benchmark pages be added for PG19, Proof-pile, LongChat topic retrieval, passkey retrieval, LongBench, and LEval?
- Should Position Interpolation and LoRA receive concept pages after another ingested paper reuses them?
- The page remains `draft` until a separate evidence-verification workflow independently checks the extraction.
