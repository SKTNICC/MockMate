# MockMate

面向个人材料的面试准备与模拟面试App，采用Kotlin/Jetpack Compose、FastAPI和PostgreSQL。

已实现M1准备与个性化计划、M2文字模拟面试、M3按回合语音面试。**M3工程修复已通过独立复查，完整真人真机与正式内容验收尚未完成，不能作为正式上线版本。**

## 开发与进度

- [已开发内容与工程进度](docs/overview/工程进度.md)：模块、代码位置、分层验证、限制及剩余工作。
- [本地开发](docs/overview/本地开发.md)：依赖、数据库、API/worker、Android构建与隔离测试。
- [文档索引](docs/README.md) · [后端说明](backend/README.md) · [Android说明](android/README.md)
- M3：[交接](docs/M3/M3语音模拟面试开发交接.md) · [验收记录](docs/M3/M3验收记录.md) · [最新独立验收](docs/M3/fixes/acceptance-review/2026-11-20-05-M3第三轮独立验收.md)
- 接续：[人工验收与交付](docs/M3/M3人工验收与最终交付方案.md) · [人工真机步骤](docs/M3/M3人工真机测试步骤.md)

后端为真实API、数据库和worker。真实DeepSeek文字/ASR/TTS已有受控小样本记录，自动化回归使用明确标识的test provider和合成fixture。最新独立基线为后端289项、Android JVM164项、契约89 schemas/50 operations/25 vectors。部分真机自动化及120秒录音已有证据，真人六题、耳机/来电、权限和正式双审仍待完成。

本次提交标记“m3阶段 未真机实测”按负责人要求保留，表示完整真人真机验收未完成；详细边界见工程进度。M4视频、专项练习和长期画像未实现。

## 目录

| 目录 | 内容 |
|---|---|
| `android/` | 客户端、加密草稿/录音、恢复、JVM及设备测试 |
| `backend/` | API、迁移、worker、文字/语音adapter及测试 |
| `contracts/m1/` | 共享OpenAPI和JSON Schema，沿用目录名 |
| `fixtures/m1/` | 合成样例、测试题库、规则向量 |
| `tools/m1/` | 契约维护源、检查器、隔离测试工具 |
| `docs/` | 产品里程碑、开发、进度和阶段交接/验收 |

共享契约先改`tools/m1/build_contracts.py`再生成并同步两端。服务端独立保证所有权、版本、材料门槛、审核资格、来源各半与去重。

## 数据与发布范围

归档实现、配置模板、正式测试、合成fixture和文档。真实Key/token、.env、私人简历/JD/录音、数据库、APK、原始证据/日志、本机缓存、调研/第三方下载仓库及许可待确认内容不上传。

test_only/synthetic_test不代表真实人工审核。生产禁止测试fixture，未审内容不能生产选题。本次没有部署、修改远端权限或新增许可证。
