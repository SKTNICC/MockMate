# MockMate 后端（M1）

真实FastAPI + PostgreSQL后端，覆盖账户、材料/快照、准备、匹配、持久任务和计划。当前仍为开发基线，M1未整体验收通过。

启动见[本地开发](../docs/本地开发.md)，状态与限制见[工程进度](../docs/工程进度.md)。

## 结构与版本

| 路径 | 作用 |
|---|---|
| `app/api/` | `/v1`路由、鉴权与对象归属 |
| `app/services/` | 材料、准备、题库、计划、任务、幂等和内容导入 |
| `app/ai/` | provider协议、DeepSeek adapter、测试provider和提示词 |
| `app/workers/` | 独立worker、租约、fencing、显式重试和槽位复用 |
| `alembic/` | 迁移，当前head `9b2c3d4e5f6a` |
| `tests/` | 真实PostgreSQL集成测试、模拟外部HTTP及规则回归 |
| `scripts/e2e_smoke.py` | 针对已运行测试API的HTTP冒烟 |

已验证Python3.12.7、PostgreSQL16.4；精确Python依赖在`requirements.txt`。SQLAlchemy2.0.36、Alembic1.14.0、FastAPI0.115.6、Pydantic2.10.4。

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

`python -m pytest tests -q`当前覆盖169项。测试使用`127.0.0.1:55432`、用户`mockmate`、数据库`mockmate_test`，每次会话重建该测试库schema；只连接独立本地测试实例，详见开发说明。

`python -m app.cli seed --mode test`用于显式合成环境；`--mode production`不派生真实审核批准。CLI的`review-packet`和`import-content`接受外部离线内容与ledger路径。许可待确认的正式内容没有纳入此次公开源码。

题库必须满足真实审核、内容摘要、来源许可、岗位/级别和答案资格；`draft/review`不可选。计划发布在一个事务中完成，`PublicPlan`不返回题干/答案/评分细则。

Dockerfile/Compose为开发脚手架，本机未验证Docker运行；当前镜像只包含backend，seed所需的仓库级fixtures需要额外挂载/调整上下文，不能视为完整部署包。Compose中的口令是公开开发示例，不是生产凭据。本次不执行部署。
