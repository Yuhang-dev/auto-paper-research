---
id: "concept:shifted-sparse-attention"
type: concept
title: "Shifted Sparse Attention"
aliases:
  - "S2-Attn"
  - "S²-Attn"
kind: method
status: draft
---

# Shifted Sparse Attention

## Definition

Shifted Sparse Attention is LongLoRA's training-time attention method for long-context fine-tuning. It divides a long sequence into local attention groups. Half of the attention heads use the original grouping, while the other half shift the partition by half a group so information can cross neighboring group boundaries. The trained model can return to standard full self-attention at inference.

## Scope

The paper applies S2-Attn to context extension of pretrained Llama2 models. It is intended to reduce the attention computation used during fine-tuning, not to define a permanent sparse-attention inference architecture. Its reported evidence covers Llama2 7B, 13B, and 70B configurations in LongLoRA.

## Distinguishing Features

- Uses unshifted local groups in half of the attention heads and half-group-shifted groups in the other half.
- Enables communication across adjacent groups without restoring dense attention during training.
- Uses tensor shifting and reshaping around a standard attention operation; Algorithm 1 presents the core transformation in two key lines.
- Retains the pretrained model's original dense-attention architecture for inference.
- Is empirically distinguished from dilated, block-sparse, and stride-sparse alternatives in the LongLoRA ablation.

## Provenance

- **Source paper:** [[papers/longlora]]
- **Evidence location:** PDF p. 5, Section 3.2 and Algorithm 1; PDF p. 9, Table 6.

## Linked Papers

- [[papers/longlora]]

## Related Concepts

- None yet; Position Interpolation and LoRA do not have concept pages in V0.

## Notes

Do not treat `S2-Attn` as a synonym for sparse attention in general. It refers specifically to the shifted two-pattern grouping introduced by LongLoRA. This page is `draft`; its evidence has been located but not independently verified.
