# 纯 CC 直连 MCP 环境

这是 `cc+旧` 会话中的纯 CC 基线。每道 validation 题都会启动一个新的
Claude Code 主进程，只允许调用 TravelPlanner MCP 工具，不使用 Workflow、
`agent()`、`parallel()`、`pipeline()` 或其他编排方式。

## Claude Code 启动参数

`runner.py` 集中保存基线提示词，并使用以下参数启动 Claude Code：

```text
--output-format stream-json
--verbose
--effort high
--setting-sources project,local
--mcp-config .\experiment\cc_pure\mcp.json
--strict-mcp-config
--allowed-tools mcp__travelplanner__*
--permission-mode dontAsk
--model claude-opus-5
-p
```

不设置 `--max-budget-usd`，也不设置其他强制预算上限。提示词不包含
Dynamic、Workflow 或 Ultracode 路由词；计划中的事实必须来自 MCP 返回值，
最终输出必须是一个 JSON 对象。

## 环境变量

| 变量 | 值或来源 |
| --- | --- |
| `ANTHROPIC_BASE_URL` | `https://penguinapi.org` |
| `ANTHROPIC_API_BASE` | `https://penguinapi.org` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `claude-opus-5` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `claude-opus-5` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `claude-haiku-4-5` |
| `CLAUDE_CODE_DISABLE_WORKFLOWS` | `1` |
| `CLAUDE_CODE_DISABLE_NATIVE_AUTH` | `1` |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | 由网关预检结果选择，并从本地密钥文件读取 |
| `CLAUDE_CONFIG_DIR` | 每题独立的 `runs/<run-id>/claude-config/<idx>/attempt-N` |
| `TEMP`、`TMP` | 每题独立的 `runs/<run-id>/temp/<idx>/attempt-N` |
| `TRAVELPLANNER_GATEWAY_KEY_FILE` | 可选覆盖；默认是 `D:\Downloads\penguin_win_bq.txt` |

外层并发上限为 3。每道题都有独立的模型可见工作目录、配置目录、临时目录，
最多进行三次独立尝试。复制或链接到该目录的内容仅包括 MCP 服务、runner、
TravelPlanner 工具/工具函数、180 题输入，以及 MCP 实际需要的数据库文件。

## 本地准备

仓库不提交约 342 MB 的数据库。请从记录实验使用的 TravelPlanner checkout
准备 `TravelPlanner/database/`，并使用 `uv` 安装
`experiment/requirements.lock.txt`。仓库中保留的 `TravelPlanner/tools/`、
`TravelPlanner/utils/` 和 `TravelPlanner/postprocess/example_evaluation.jsonl`
用于固定代码和题集边界。

运行完整尝试：

```powershell
$env:PYTHONPATH = "experiment"
uv run python experiment/cc_pure/runner.py `
  --queries TravelPlanner/postprocess/example_evaluation.jsonl `
  --run-id cc-pure-local `
  --concurrency 3
```

设置 `TRAVELPLANNER_GATEWAY_KEY_FILE` 时，runner 会使用该路径；否则使用上面
记录的本地默认路径。也可以显式传入 `--key-file <path>`。

runner 会在 `runs/<run-id>/` 下写入 `attempts.jsonl`、`timing.json`、
`gateway.json` 和各 checkpoint 的评分/失败文件。子进程环境会清理凭据，持久化
错误信息也会脱敏。

CC checkpoint 和 Dynamic 的选题评分共用 `experiment/evaluate_selected.py`；
两种实验只在 runner、提示词、选题范围和 checkpoint 调度上保留差异。
