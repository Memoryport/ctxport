# 给朋友的 Claude Code：真实数据复现（第二位真实用户）

把下面整段原样发给朋友的 Claude Code。前提：朋友已经装好 ctxport，并导入了自己的聊天记录（ChatGPT 官方导出，加上用过的其他平台）。

---

你在帮我参加一个研究项目：测量“把个人记忆从一个 AI 助手迁移到另一个助手”时会丢掉多少信息。代码在 ctxport 仓库的 `research/migration-loss` 分支，目录是 `benchmarks/migration/`。请严格按下面的步骤做，每一步做完都告诉我结果，再继续下一步。

**规则（最重要）**
- 我的聊天内容、题目、答案都只留在本机的 `~/.ctxport/research/real/` 下。不要把任何原文、题目或答案贴进回复、提交到 git，或者发到任何网站。最后只把 `report.md` 和 `split-report.md` 里的汇总数字给我看。
- 每一步跑模型之前，先告诉我大概要调用多少次，等我同意再跑。
- 不要修改 `~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`，也不要改 ctxport 数据库里的数据。

**步骤**
1. `git fetch && git switch research/migration-loss`，然后运行 `.venv/bin/python -m pytest -q tests/test_migration_analysis.py tests/test_distill_years.py` 确认测试通过。
2. 隔离检查：运行 `benchmarks/migration/llm.py` 对应的泄漏测试，用一个空上下文问模型“我叫什么、在哪上学、邮箱是什么”，必须全部回答 UNKNOWN。如果模型说出了我的任何真实信息，就停下来告诉我。
3. 如果 `~/.ctxport/distilled/facts.json` 不存在，先用 `ctxport distill --agent claude-code` 生成（会用我的 Claude 额度，先告诉我要处理多少个对话）。
4. 起草题库：`.venv/bin/python benchmarks/migration/realdata.py bank --per-year 80`，然后 `realdata.py label`，再用 `open ~/.ctxport/research/real/label.html` 打开标注页给我。我标完会导出 `gold.json`，放到 `~/.ctxport/research/real/`。
5. 我会把从 ChatGPT 导出的“你对我的了解”整段文字存成 `~/.ctxport/research/real/b1-chatgpt.txt`。等文件到位再继续。
6. 构建上下文：先跑 `realdata.py contexts`，再跑 `real_extra.py contexts`。
7. 读题：`realdata.py read --readers claude:haiku --workers 2`（所有方法都读）。
8. 评分和报告：先跑 `realdata.py judge`，再跑 `realdata.py report --labeled-only` 和 `real_extra.py split-report > ~/.ctxport/research/real/split-report.md`。
9. 只把两份报告里的表格（方法、准确率、各年份、和 b1-chatgpt 的差值）给我看。

---

## 你（项目作者）这边要做的

- 先把 `benchmarks/migration/`、`tests/` 和这份文档推到 `research/migration-loss` 分支，朋友才拉得到代码。
- 朋友的结果作为第二个真实用户（N=2），在论文里和你的结果分开报告。不要合并成一个“平均值”，两人的题目和历史完全不同。
- 涉及他人数据：论文里只报告汇总数字，并写明朋友是知情同意、在本地运行、只回传汇总指标的。招更多志愿者之前，先问清楚学校是否需要伦理审批（plan.md 里已经提过）。
