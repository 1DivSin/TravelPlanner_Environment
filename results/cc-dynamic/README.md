# CC Dynamic Workflow 实验结果

运行目录：`retry17-dynamic-20260831-233258`，来自 Dynamic 会话，使用原始的、
与压缩包一致的提示词。

## 结果

- 选定题目：30 道
- 交付：**30/30（100.00%）**
- Commonsense：**micro 97.08% / macro 76.67%**
- Hard constraints：**micro 98.41% / macro 86.67%**
- 最终通过：**22/30（73.33%）**
- 成功尝试记录费用：**$52.065332**
- 初始批次和最终三路重试合计的有效计算时间：**9472.012292 秒**

最终重试使用 3 个隔离分片，剩余 17 题全部交付成功。6 路尝试会造成网关
排队和 2400 秒超时，因此没有把它作为最终有效并发配置。

## Dynamic 证据

单题门禁观察到 1 次 `Workflow` 调用、1 个生成的 JavaScript Workflow、3 个
Workflow subagent，主会话没有直接调用 TravelPlanner MCP。因此这次确实经过了
Dynamic Workflow 路由，而不是普通的直连 MCP 规划。

本结果使用原始的 `Run a workflow...` 提示词。后续明确规定
`RECON → ASSEMBLE → VERIFY` 的提示词及其结果不属于本实验。

`formal-30-scores.json` 是逐题评分结果，`formal-30-report.md` 是精简报告，
`formal-30-attempts.jsonl` 是脱敏后的尝试记录。
