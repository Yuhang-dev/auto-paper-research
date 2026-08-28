# DoneCriteria 激活清单

正式 `research/long-context-sparse-models/done-criteria.yaml` 当前必须保持 `draft`。
只有下列条件逐项完成并由综述负责人确认后，才可把 `status` 改为 `active`。

## 运行语义

- [ ] retrieval Canary 至少成功一次，所有硬边界为 true；
- [ ] screening Canary 至少成功一次，并完成候选人工抽查；
- [ ] ingest → verification 的隔离闭环至少成功一次；
- [ ] Ctrl+C / publish exception 的 Wiki transaction rollback 测试通过；
- [ ] deadline 强制终止只留下隔离 workspace，不修改正式真源；
- [ ] 同一个 `--thread` 的 replan/checkpoint resume 行为已验证；
- [ ] trajectory 可从 SQLite checkpoint 重建，且不是第二运行真源。

## 检索与筛选质量

- [ ] `max_provider_query_calls` 与 provider retry 的统计口径已确认；
- [ ] duplicate provenance merge 与新增 identity truncation 的测试通过；
- [ ] core / adjacent / background / exclude 的人工标注样本足够；
- [ ] 重点检查 false promotion rate，不能只报告 recall；
- [ ] candidate facet heuristic 仅用于路由，不被解释为 evidence coverage；
- [ ] 失败、凭证阻塞、未完成 screening 的轮次不会计入 saturation；
- [ ] 用真实 search rounds 观察 `new_core_papers` 分布后再确认 saturation threshold。

## 证据与完成门槛

- [ ] 每个 `facet_requirements` 的状态语义得到人工认可；
- [ ] 是否需要 `minimum_verified_experiments` 已明确；
- [ ] context bucket 和 engineering metric 是否按全部 experiment 还是 verified-only 计数已明确；
- [ ] non-consensus review 接受 supported-consensus / contested / insufficient-evidence 三种结果；
- [ ] 不以“必须找到若干 contested claim”作为结束目标；
- [ ] `require_no_open_blocking_gaps` 的标注流程和责任人明确；
- [ ] 数量门槛被视为 anti-premature-stop guardrail，而非综述完整性的科学定义。

## 可复现性与安全

- [ ] 所有语义模型输出均有 content-addressed artifact；
- [ ] artifact 包含 Skill/schema hash、输入 source hash、模型名和结构化输出；
- [ ] publication manifest 与 immutable artifact 分离；
- [ ] 人工标注使用 `target_id + source_sha256` sidecar，不覆盖原始输出；
- [ ] Key 仅存在进程环境；Wiki、YAML、SQLite、artifact、trajectory 和日志均无明文 Key；
- [ ] 模型配置被限制为 `deepseek-v4-flash`；
- [ ] SQLite、缓存、Canary 和 PDF 均位于 D 盘项目范围内。

## 激活动作

全部完成后：

1. 记录审核日期、负责人和依据的 Canary run IDs；
2. 更新参数审计文档中的经验阈值；
3. 单独提交把 `status: draft` 改为 `status: active` 的小变更；
4. 合并前再次执行完整离线测试和一个 1-query screening Canary。
