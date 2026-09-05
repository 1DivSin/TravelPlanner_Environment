# CC Dynamic Workflow 实验环境

这里是 CC Dynamic Workflow 版本的实验环境。`CC` 表示执行宿主，不代表
这是一个不使用 Workflow 的基线。

## 触发方式与隔离

`experiment/runner.py` 保留原始 `PROMPT_TEMPLATE`，并使用以下参数以非交互
方式启动 Claude Code：

```text
--output-format stream-json
--verbose
--effort ultracode
--setting-sources project,local
--mcp-config experiment/mcp.json
--strict-mcp-config
--allowed-tools Workflow,mcp__travelplanner__*
--permission-mode dontAsk
--model claude-opus-5
-p
```

不要加入 `--bare`。在 Claude Code 2.1.220 中，`--bare` 会使内置工具列表只剩
`Bash`、`Edit` 和 `Read`，即使 Ultracode 已映射到 `xhigh`，Workflow 也不会
注册。

`experiment/run_penguin_30.ps1` 会为每次运行创建独立的
`runs/dynamic/<run-id>/`，并隔离 Claude 配置目录和临时目录。规范输出始终
保存在该运行目录，不复制到旧的共享输出目录。

## 原始提示词

原始提示词保存在 `experiment/runner.py` 的 `PROMPT_TEMPLATE` 中，要求 Claude：

- 使用最多 3 个 phase、最多 5 个 subagent 的小型 Workflow；
- 优先顺序执行，只对相互独立的搜索并行化；
- 每个 subagent 最多调用 5 次工具，并最多尝试一次备用搜索；
- 使用 TravelPlanner MCP 查询航班、住宿、餐厅、景点和距离；
- 只返回规定格式的逐日 TravelPlanner JSON。

提示词中没有额外加入 `dynamic` 或 `ultracode`。Workflow 路由由
`--effort ultracode` 触发。

## 已验证的运行行为

单题门禁实验观察到 1 次 `Workflow` 调用、1 个生成的 JavaScript Workflow
和 3 个 Workflow subagent；主会话没有直接调用 TravelPlanner MCP。

正式实验使用独立的 `CLAUDE_CONFIG_DIR` 和临时目录。6 路并发会造成网关排队
和 2400 秒超时；3 路是实测有效的最高并发。实验结果另行放在结果 PR 中，
避免环境修改改变已记录的基线。
