# MockMate Android（M1 + M2）

Kotlin / Jetpack Compose单Activity应用，通过HTTP连接真实FastAPI后端。包含登录、准备、材料核对、匹配、计划确认与重开，以及 M2 文字模拟面试、加密回答草稿、追问/报告状态、面试记录和任务恢复。

状态见[工程进度](../docs/overview/工程进度.md)，环境步骤见[本地开发](../docs/overview/本地开发.md)，M2 交接见[M2文档](../docs/M2/M2文字模拟面试开发交接.md)。界面接线不等于所有真机场景已验收。

## 工具链与构建

| 项目 | 版本 |
|---|---|
| JDK | 本机验证21，编译目标Java17 |
| AGP / Gradle wrapper | 8.7.3 / 8.10.2 |
| Kotlin / Compose插件 | 2.0.21 |
| Compose BOM | 2024.10.01 |
| minSdk / targetSdk / compileSdk | 26 / 35 / 35 |
| applicationId | `com.mockmate.app`，version `0.2.0-m2` / code2 |
| HTTP | Ktor2.3.12 + OkHttp |

配置本机JDK和Android SDK35，Android Studio可生成不提交的`local.properties`。在本目录运行：

```powershell
.\gradlew.bat --no-daemon :app:testDebugUnitTest assembleDebug
# 只构建设备测试APK：
.\gradlew.bat --no-daemon assembleDebugAndroidTest
```

首次构建下载wrapper/依赖，需要网络；缓存齐全后才可加`--offline`。当前JVM单测为72项。

## 后端与手机

开发端点`http://127.0.0.1:8000/v1`由`app/build.gradle.kts`配置。USB联调先启动电脑API和worker，再执行`adb reverse tcp:8000 tcp:8000`。Debug允许开发HTTP；release不允许明文网络，正式端点/签名/部署尚未完成。

安装前确认设备，用`adb install -r`保留数据，禁止为冲突擅自卸载/清数据。设备测试会创建或切换合成账号，只在授权设备和测试后端运行。

不要在留存数据设备上直接跑`connectedDebugAndroidTest`：此前UTP安装曾卸载App。使用显式安装和选定instrumentation：

```powershell
adb install -r app\build\outputs\apk\debug\app-debug.apk
adb install -t -r app\build\outputs\apk\androidTest\debug\app-debug-androidTest.apk
adb shell am instrument -w -r -e class com.mockmate.app.M1DeviceUiTest `
    com.mockmate.app.test/androidx.test.runner.AndroidJUnitRunner
```

依赖准备账号/后台时序的测试还需相应参数与隔离配置，不能把该单例当整套设备验收。`tools/m1/e04_setup.py`准备合成测试数据，输出含测试token，保留在本机`outputs/`，不上传。

2026-11-17 在当前 API37 模拟器上完成：debug APK 安装、注册登录、准备列表、面试记录入口、进行中面试间与真实报告页手动验证；同一合成账号经真实 API 完成六题并生成报告，报告页显示覆盖度、单题分、维度与逐字证据。`M2EntryDeviceTest` 已编译，但 Espresso 在该镜像初始化 `InputManager.getInstance` 失败，instrumentation 未通过；真实手机/API30 仍需执行完整六题与恢复用例。

## 实现边界

`core/network`为API；`core/session`为加密会话/草稿（M2 回答草稿在 `InterviewDraftStore`）；`core/operation`为原key、类型化payload和原子操作记录；`data`为恢复scope归并及唯一JobWatch（M2 的 `follow_up`/`interview_report` 目标都是 session）；`ui`为Compose页面和ViewModel。

M2 面试间只展示已曝光回合；报告只发布覆盖度、逐题维度、逐字证据和有限优势/改进项。暂停中仍可查看已曝光回合，作答与跳过需先继续。计划页在无原场时显示本场处理说明并经用户同意后创建；有原场时直接打开或查看报告。

T01–T03恢复/并发/轮询原问题有回归及部分真机证据；普通材料保存/上传/确认的迟到回写隔离U01仍需修复。真实IME、系统选择器和A/B迟到手机证据尚不完整。M2 已完成模拟器入口冒烟与JVM组件测试，但六题真机闭环、真实模型和真实审核内容仍未验收。
