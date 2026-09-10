# Dynamic 完整 180 题证据

结果：**113/180（62.78%）**，180 题全部有交付计划；不是父目录的 22/30。

- 预测：`dynamic180-predictions.jsonl`。
- 逐题评测：`dynamic180-rescored.json`。
- 来源：`source-manifest-180.json`，180 题都逐条匹配原始 plan，包含源记录行号、会话映射及公开文件 SHA-256；旧 `dynamic-source-manifest.json` 仅保留原始本地路径供追溯。
- 分析：`analysis.md` 与 `full180-comparison.json`。
- 现存记录：`raw-dynamic-selected-runs.zip`，覆盖来源清单关联的原始与扩展运行，共 571 个文件。`raw-dynamic-runs.zip` 是此前上传的早期运行包，不代表完整 180 题。
- Workflow：归档 `extracted-workflows/` 含 43 个从 Workflow/Write 调用参数恢复的脚本快照；`workflow-generation-manifest.json` 记录原会话、行号和 tool-use id。它们是保存的生成内容，未重新执行。

**对话覆盖尚不完整：按用户 query 精确匹配核验到 24/180 题的关联会话。** 这仅证明找到关联对话，不保证是最终选中 attempt 的完整调用链。其余索引见 `coverage.json`；有最终 plan 不等于保存了所有 subagent/tool output。来源目录未发现独立 JS 文件，生成脚本来自 JSONL 调用参数。

仅打包指定实验的白名单文件，排除认证配置、遥测、缓存与 shell 快照，脱敏凭据模式及本地根路径。归档中的日志统一转 UTF-8；源文件哈希保留在清单中。运行 `python verify_evidence.py` 可离线核验预测、来源、评测与压缩包哈希。
