# MockMate 后端（M1 + M2）

真实FastAPI + PostgreSQL后端，覆盖账户、材料/快照、准备、匹配、持久任务、计划和 M2 文字模拟面试（问答、追问、暂停/跳过/早退、面后评分报告、历史重开）。当前仍为开发基线，M1/M2 均未整体验收通过。

启动见[本地开发](../docs/overview/本地开发.md)，状态与限制见[工程进度](../docs/overview/工程进度.md)，M2 契约与验收口径见[M2 交接](../docs/M2/M2文字模拟面试开发交接.md)。

## 结构与版本

| 路径 | 作用 |
|---|---|
| `app/api/` | `/v1`路由、鉴权与对象归属；M2 面试路由在 `interviews_routes.py` |
| `app/services/` | 材料、准备、题库、计划、任务、幂等、内容导入和 `interviews.py` 状态机 |
| `app/ai/` | provider协议、DeepSeek adapter、测试provider、追问/评分提示词 |
| `app/workers/` | 独立worker、租约、fencing、显式重试、槽位复用、`follow_up`/`interview_report` |
| `alembic/` | 迁移，当前单一 head `a1b2c3d4e5f7` |
| `tests/` | 真实PostgreSQL集成测试、模拟外部HTTP、并发和契约回归 |
| `scripts/e2e_smoke.py` | M1 HTTP 冒烟 |
| `scripts/e2e_m2_smoke.py` | M2 六题闭环、追问、报告、历史与零回答早退冒烟 |
| `scripts/verify_m2_migration.py` | 临时库：旧 M1 计划 → 升级 head → 原 plan 开场 |

已验证Python3.12.10、PostgreSQL17.11；精确Python依赖在`requirements.txt`。SQLAlchemy2.0.36、Alembic1.14.0、FastAPI0.115.6、Pydantic2.10.4。

## 配置与入口

复制`.env.example`为本机`.env`，填写数据库连接和私有目录；`.env`不提交。两个终端均在`backend/`运行：

```powershell
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另一个终端：
python -m app.workers.runner
```

`GET /healthz`检查API；`GET /readyz`检查数据库和私有目录。worker独立启动，仅启动API不能完成生成任务。

| AI_PROVIDER | 行为 |
|---|---|
| `disabled` | 默认，AI任务明确返回未配置 |
| `test` | 仅非生产且显式`ALLOW_TEST_FIXTURES=true`，确定性合成输出 |
| `deepseek` | 已实现adapter；需要服务端模型/凭据和授权，真实链路尚未验收 |

测试provider不产生真实AI调用。`test_deepseek_flow.py`使用模拟HTTP验证真实adapter代码，也不是真实模型调用。

## 测试与内容

`python -m pytest tests -q`当前覆盖192项。默认仍使用`127.0.0.1:55432`、用户`mockmate`、数据库`mockmate_test`；也可显式设置`MOCKMATE_TEST_DATABASE_URL`指向回环主机上以`_test`结尾的独立库（例如`mockmate_m2_test`）。conftest 会拒绝与开发库同名、非回环主机或非测试库名，并在重建 schema 前用 `current_database()` 复核目标；每次会话重建该测试库schema。只连接独立本地测试实例，详见开发说明。

M2 面试报告只发布公开 DTO：覆盖度、逐题维度、逐字引文与有限优势/改进项；内部评分快照、未曝光题干和模型只读输出不进入接口。报告任务失败可显式重试，或发布 `quality=unscored` 的基础报告；零回答收尾直接发布 `quality=no_answers`，不调用模型。

`python -m app.cli seed --mode test`用于显式合成环境；`--mode production`不派生真实审核批准。CLI的`review-packet`和`import-content`接受外部离线内容与ledger路径。许可待确认的正式内容没有纳入此次公开源码。

题库必须满足真实审核、内容摘要、来源许可、岗位/级别和答案资格；`draft/review`不可选。计划发布在一个事务中完成，`PublicPlan`不返回题干/答案/评分细则。

Dockerfile/Compose为开发脚手架，本机未验证Docker运行；当前镜像只包含backend，seed所需的仓库级fixtures需要额外挂载/调整上下文，不能视为完整部署包。Compose中的口令是公开开发示例，不是生产凭据。本次不执行部署。
