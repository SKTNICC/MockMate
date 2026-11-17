# M1 合成数据说明

本目录 JSON 由 `tools/m1/build_contracts.py` 生成，修改源后重新生成。所有人物、经历、ID与计划均为虚构；不是已入库、已审核或已运行的结果。

- `materials.json`：两份简历与两份 JD 的已确认结构样例，含无工作经历、无项目、自备／内置来源。
- `catalog.json`：Java 后端实习的开发样板、内置JD模板、技能小词表、简化评价量表；不代表最终首岗定案或完整岗位覆盖。
- `bank-candidates.json`：6条本轮原创候选，`draft`，未人工审核，不能满足真实各半配额。
- `content-reviews.json`：审核格式与空 ledger；由真实审核负责人填写，模型不能代签。
- `fact-cards.json`：待人工核验的官方依据摘要候选；默认未approved，不可充当真实上线评价事实依据。
- `internal-plan.example.json`：醒目标记 `test_only=true` 的6题结构正例，模拟银行题已审字段仅供规则测试，不能导入生产或声称人工审核通过。
- `public-plan.example.json`：对应可向手机返回的摘要；不含题干和答案。
- `rule-cases.json`：最小合格结构与配额／重复／引用／依据错误等反例，检查工具不调用服务或AI。

内容审核建议通过编辑独立受控 ledger 输入并用导入命令核对内容 hash，不直接改生成的样例JSON。正式资料和正式审核记录由实现者放入独立内容目录，保持来源清楚；不要覆盖 test_only 标记以冒充真实内容。
