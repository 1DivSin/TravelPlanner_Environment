# TravelPlanner 实验环境

这里保存两个 Claude Code TravelPlanner 实验的可复现实验环境和结果。

内容拆成四个独立、可审阅的 PR：

1. 纯 CC 直连 MCP 环境（`experiment/cc_pure/`）
2. CC Dynamic Workflow 环境（`experiment/` 和 `experiment/dynamic/`）
3. 纯 CC 实验结果（`results/cc/`）
4. CC Dynamic Workflow 实验结果（`results/cc-dynamic/`）

环境和结果分开提交，避免修改运行环境时悄悄改变已记录的结论。

纯 CC 的安装方式和运行约束见
[experiment/cc_pure/README.md](experiment/cc_pure/README.md)；Dynamic 环境和
结果目录中的 README 会说明各自的提示词边界及排除项。
