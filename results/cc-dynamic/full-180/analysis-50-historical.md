# TravelPlanner：Dynamic 62.78% 与纯 CC 72% 的核查

核查日期：2026-09-06。只重算已有答案，没有调用模型生成新答案，也没有修改实验或 PR。

**结论：原始实验的差距存在，而且对齐同题后仍然是 62% 对 72%。这次差距主要来自全程交通兼容性，而不是普遍的检索能力下降、实体名称丢失或模型被换成 Haiku。当前 Dynamic 的提示词增加了分工与速度限制，却没有保证最终行程满足跨城市的统一规则。数据支持这个具体失败机制，但不足以证明 Dynamic 架构具有稳定的能力劣势。**

**先确认比较的究竟是哪几份结果。**

| 记录 | 范围 | 最终通过 | 说明 |
|---|---|---|---|
| PR4 原始纯 CC checkpoint | idx 1–50 | 36/50，72.00% | 本次从本地原始答案复现 |
| PR5 当前提交的 Dynamic | 选定的 30 题 | 22/30，73.33% | 最早的正式 30 题结果 |
| 本地原始提示词 Dynamic 扩展结果 | idx 1–180 | 113/180，62.78% | 本次从原始答案复现 |
| 上一行严格取与 CC 相同的题 | idx 1–50 | 31/50，62.00% | 用于分析方法差距 |

[PR4](https://github.com/1DivSin/TravelPlanner_Environment/pull/4) 当前 head 为 `007d40d44f771fd9d64dacbbe4e270112a7fa88f`；[PR5](https://github.com/1DivSin/TravelPlanner_Environment/pull/5) 当前 head 为 `7f897e1ac83dea8a8eaa3b0efd43bf411b1797c5`。62.78% 来自默认路径任务的 [progress-180.json](C:/Users/lbq/Documents/Codex/2026-08-31/codex-threads-01a05618-3344-77f0-870a/outputs/progress-180.json)，尚未包含在 PR5 当前结果文件中。

180 题汇总复用了早期 30 题中的 29 题；idx17 后来重跑，原失败变为通过。原 30 题索引在 180 题汇总中为 23/30，其余 150 题为 90/150。因而不能把 PR5 的 30 题与本地 180 题当作两次同范围实验。

本次核对了扩展运行保存的五份 source snapshot，其 `PROMPT_TEMPLATE` 与 PR5 原始提示词逐字相同；选择记录的清单没有引用后续 `combined-recon-assemble-verify-*` 实验。180 条记录的成本与已保存逐题评分一一对应；重新运行官方 commonsense/hard checkers 后，180 题的约束结果与保存结果一致。CC 使用本地原始记录复算后，50 题的约束结果也全部一致。

**1. 同题比较后，差距集中在 5 天、两城市题。**

| 相同题目 | CC | Dynamic | Dynamic 净变化 |
|---|---:|---:|---:|
| 3 天：idx 1–20 | 20/20，100% | 19/20，95% | −1 题 |
| 5 天：idx 21–40 | 12/20，60% | 7/20，35% | −5 题 |
| 7 天：idx 41–50 | 4/10，40% | 5/10，50% | +1 题 |
| 合计 | 36/50，72% | 31/50，62% | −5 题 |

PR4 的这 50 题全部是 easy，而 Dynamic 全集覆盖 easy/medium/hard 各 60 题，所以原先 180 对 50 的总分并不直接可比。但同题比较仍然出现 10 个百分点差距，因此不能用“Dynamic 题目更难”将差距全部解释掉。另一方面，7 天子集并没有下降，也不能说 Dynamic 随行程长度增加必然比 CC 更差。

逐题配对为：两边都通过 28 题，两边都失败 11 题；CC 独自通过 8 题，Dynamic 独自通过 3 题。

| CC 通过、Dynamic 失败的题 | Dynamic 的失败原因 |
|---|---|
| 18 | 预算超支 |
| 21、24、26、36、38、40 | 仅全程交通冲突；其他实际执行的约束检查均通过 |
| 50 | 全程交通冲突，同时城市顺序不合法 |

Dynamic 反过来做对了 CC 做错的 **28、43、48**，三题的 CC 失败原因也都是交通冲突。故差距可写为：交通相关损失 7 题，交通相关收益 3 题，另损失预算题 1 题，净损失 5 题。逐题证据见 [original-comparison.json](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/original-comparison.json)。

**2. 直接原因是全程规则与局部交通选择不一致。**

官方 [is_valid_transportation](C:/Users/lbq/Documents/psi-agent/_travelplanner_official/evaluation/commonsense_constraint.py:215) 对整份行程提取交通方式，只要同时出现以下任一组合便判失败：

- `Flight` 与 `Self-driving`；
- `Taxi` 与 `Self-driving`。

`Flight` 与 `Taxi` 可以混用。因此现实中常见的“飞机抵达，再租车去邻市”在这个 benchmark 中会被判错。这个规则不是由航班或距离 MCP 查询单独验证的：每一段交通都可以真实存在、城市也可以正确，但整趟行程仍然不合格。

第 21 题是最清楚的例子：

| 方法 | 全程交通安排 | 官方计算费用 / 预算 | 判定 |
|---|---|---|---|
| CC | St. Louis → Orlando：飞机；Orlando → Tampa：出租车；Tampa → St. Louis：飞机 | $2,772 / $2,900 | 通过 |
| Dynamic | St. Louis → Tampa：飞机；Tampa → Orlando：自驾；Orlando → St. Louis：飞机 | $2,414 / $2,900 | 仅交通冲突 |

Dynamic 的选择更便宜，每段交通也都有对应数据，但没有满足全程的交通兼容规则。第 24 题中，CC 全程自驾，Dynamic 则先飞过去、再自驾；第 26、36、38、40 题中，CC 采用全程航班，Dynamic 混入了自驾。这些案例表明，在本次运行中 CC 确实产出了更一致的整体交通方案，并非只利用交通字符串拼写绕过检查。

第 50 题，Dynamic 的路线是 Columbus → Houston → Austin → San Antonio → Houston → Columbus，除飞机与自驾混用之外，还重新进入了先前访问过的 Houston。官方 [城市顺序检查](C:/Users/lbq/Documents/psi-agent/_travelplanner_official/evaluation/commonsense_constraint.py:85) 禁止这种中途返回。CC 的路线是 Columbus → Dallas → Austin → Houston → Columbus，没有该问题。

第 18 题是另一项独立的全局检查遗漏：预算为 $2,200，官方价格计算出 Dynamic 为 **$2,298**、CC 为 **$2,037**。Dynamic 只超了 $98，但最终指标是整题全部约束同时通过，不能得到部分通过分。逐项复算见 [original-vs-published.json](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/original-vs-published.json) 中的 `case_costs`。

**3. 180 题全集也显示同一个失败集中点。**

Dynamic 共失败 67 题。交通冲突出现在 42 题，城市顺序失败出现在 23 题，其中 14 题重叠；两者合计覆盖 **51/67，76.12%** 的失败题。另有 14 题实体不在 sandbox、12 题信息缺失、7 题城市归属错误、5 题住宿连续晚数不合规等，类别之间有重叠，不能相加当作失败题数。

其中 16 题只有交通冲突，没有其他已执行约束失败。只涉及交通/城市顺序且硬约束已通过的共有 31 题。这里是在定位检查缺口，并不是声称修正交通后这些题必然全部通过；换交通还可能改变费用和时间。

在前 50 题里，Dynamic 的 sandbox 名称失败为 3 题，CC 为 4 题。因此，“多代理交接导致实体名称丢失”可以是个别失败的风险，但与这次主要净差距不符。把它当首要原因，会错过最集中的交通问题。

Dynamic 全集的 commonsense micro 为 92.71%，但 commonsense macro 只有 65%，最终为 62.78%；大量题目并不是所有局部内容都错了，而是被少数全程条件否决。3 天题通过 54/60，5 天为 32/60，7 天为 27/60，跨城市组合是当前实现的主要薄弱处。

**4. 为什么这版 Dynamic 更容易出现这种问题：代码能支持的解释与尚待验证的部分。**

[Dynamic 原始提示词](C:/Users/lbq/Documents/psi-agent/TravelPlanner_Environment/experiment/runner.py:43) 明确要求最多 3 phases、5 subagents，每个子代理最多 5 次工具调用、空结果只尝试一次替代、10 分钟内完成，并优先快速生成够用的方案。它没有明确写出上述全程交通兼容规则，也没有要求每次都执行一个针对这些具体规则的最终验证阶段。

[CC 提示词](C:/Users/lbq/Documents/psi-agent/TravelPlanner_Environment/experiment/cc_pure/runner.py:40) 没有这些分工和快速结束要求，还明确要求所有事实来自 MCP、不编造名称与交通细节。两者也分别使用 `high` 和 `ultracode`，所以它们不是只改变“是否用 Workflow”的严格单变量实验。

两边的运行器都在获得 JSON 计划后才交给离线 evaluator；没有一个必经的全程约束检查将失败信息反馈给本题模型，再要求其修复。启用 Workflow 本身不提供这种保证。

由此，最符合当前证据的机制是：Dynamic 增加了任务分工、局部搜索和结果整合，但没有保证相同程度的全局约束检查。城市之间的廉价短程自驾与城际航班被放在一起，造成局部可行、全程违规。一个 CC 会话在这批题中更常选出了全航班、全自驾或飞机加出租车的完整方案。

需要保留的因果边界是：逐题结果能证明“哪里错了”，提示词和运行器能证明“没有强制保障”；仅凭这些材料不能定量证明究竟有多少题由子代理上下文交接、5 次调用上限、速度偏好或 effort 变化造成。也不能断言 180 个 workflow 都没有任何自检。更详细的逐题 workflow 输入、输出与校验记录，以及单项消融，才足以拆开这些因素。

**5. 更高的费用没有对应更高的约束满足率。**

同一批 50 题，Dynamic 记录成本为 **$81.7230**，CC 为 **$54.2519**，前者高 **50.64%**。将各模型的 `model_usage` 汇总，Dynamic 输出 555,679 tokens，CC 输出 203,553 tokens，为 **2.73 倍**；不能只取 Dynamic 主会话的 `usage.output_tokens`，否则会漏掉子任务用量。

Dynamic 的 50 题中，Opus 普通记录费用约 $71.5855，带 `[1m]` 键的 Opus 记录费用约 $10.0562，Haiku 只有 $0.0813。`model` 字段常显示 Haiku，是 [解析代码](C:/Users/lbq/Documents/psi-agent/TravelPlanner_Environment/experiment/runner.py:237) 取 `next(iter(model_usage), None)` 的结果，不能据此认为规划被降级成 Haiku。

180 题所选答案全部交付，当前 67 道评分失败不是无答案或 API 超时。网络失败会影响尝试成本、耗时和数据收集进度，但不能直接解释这些已交付答案的逻辑错误。记录费用也不等于包含所有失败尝试的完整供应商账单。

**6. PR 发布材料另外存在字符编码不一致。**

PR4 发布版与本地原始计划在 **4、8、27、35、44、45** 六题发生了名称字符变化。例如，原始与数据库中的 `Waikīkī Beach` 在 GitHub 上传版成为 `Waik墨k墨 Beach`。我通过 GitHub 文件接口确认了上传内容，故不是终端显示问题。

在完全相同的官方检查器与本地数据库下：

- 本地原始 CC 答案复算仍是 **36/50，72%**，逐项约束与原保存分数一致。
- PR4 当前上传的答案复算是 **33/50，66%**；4、8、45 由通过变为失败。
- PR4 的评分文件仍然写着 36/50，没有与改坏的答案同步。

PR5 的上传答案也有 idx17、81 的字符变化，但这两题在原始 30 题评分中本来就失败。这个发布问题应修复为“保持原始计划字符串不变，仅删除需脱敏的元数据”，不应通过人工改写答案名字来回填成绩。它与原始实验中的 72% 对 62% 是两件事。

本次复算材料：[原始 CC](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/cc50-original-rescored.json)、[Dynamic 180 题](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/dynamic180-rescored.json)、[PR4 上传版](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/cc50-rescored.json)、[名称变化清单](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/original-vs-published.json)、[Dynamic 来源清单](C:/Users/lbq/Documents/psi-agent/review_tmp/travelplanner-pr4-pr5/dynamic-source-manifest.json)。

**最值得优先验证的改进是全程交通约束，而非继续增加子代理。** 保持其他条件不变，先给两种方法同样明确的交通兼容规则，观察这 7 道 Dynamic 交通损失题能恢复多少；随后再单独验证固定城市顺序、预算复算与最终校验是否带来额外收益。只加入 RECON/ASSEMBLE/VERIFY 阶段名称，不足以保证检查覆盖了具体规则。

当前配对比较只有 50 题，8 次 CC 独胜对 3 次 Dynamic 独胜的双侧精确 McNemar 检验为 p=0.2266。应表述为“这次运行中 Dynamic 净少对 5 题，主要差在全程交通一致性”，不宜据此宣称已经证明 Dynamic 一般性地弱于 CC。
