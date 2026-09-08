# CC 完整 180 题证据

结果：**132/180（73.33%）**，169 个交付计划、11 个最终失败记录。运行标识为 `cc-pure-bare-180-20260904-235302-8fdb77c5`。与父目录的早期 50 题 checkpoint 分开保存。

- 预测：`predictions-180.jsonl`（180 条，包含无计划的 11 条失败）；`attempts-180.jsonl` 保留全部 212 次尝试。
- 逐题评测：`cc-full-rescored.json`；汇总：`summary-180.json`。
- 来源/重试：`source-manifest.json` 逐题记录选中 attempt、行号及所有尝试状态，文件条目含源文件与公开文件 SHA-256。
- 分析：`analysis.md` 与 `full180-comparison.json`。
- 现存原始记录：`raw-cc-runs.zip`，28 个文件，包括全部尝试、计时、原始评分、runner、MCP 工具和输入快照。

**对话缺口：本批目录未发现完整会话或工具调用 transcript（0/180 可核验）。** 保存 runner 在内存中解析 stdout/stderr 后只写出结果记录；归档不是完整对话包。纯 CC 禁用了 Workflow，因此没有 Workflow 生成物。不能从早期 50 题或 Dynamic 复制对话来补齐。

`coverage.json` 列出缺口；`redaction-report.json` 说明排除凭据配置、遥测、shell 快照及内容脱敏规则。无需网络即可运行 `python verify_evidence.py` 检查 180 个唯一索引、评分汇总、来源计划匹配、归档完整性及哈希。`SHA256SUMS.json` 可校验公开文件。
