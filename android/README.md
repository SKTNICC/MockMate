# MockMate Android（M1 + M2 + M3）

Kotlin / Jetpack Compose单Activity应用，通过HTTP连接真实FastAPI后端。包含登录、准备、材料核对、匹配、计划确认与重开，M2 文字模拟面试，以及 M3 语音面试（16kHz PCM 录音、受控播题、转写核对、来源展示、加密本地 take 和恢复）。

状态见[工程进度](../docs/overview/工程进度.md)，环境步骤见[本地开发](../docs/overview/本地开发.md)，M3 见[M3交接](../docs/M3/M3语音模拟面试开发交接.md)与[验收记录](../docs/M3/M3验收记录.md)。界面接线不等于所有真机场景已验收。

## 工具链与构建

| 项目 | 版本 |
|---|---|
| JDK | 本机验证21，编译目标Java17 |
| AGP / Gradle wrapper | 8.7.3 / 8.10.2 |
| Kotlin / Compose插件 | 2.0.21 |
| Compose BOM | 2024.10.01 |
| minSdk / targetSdk / compileSdk | 26 / 35 / 35 |
| applicationId | `com.mockmate.app`，version `0.2.0-m2` / code2（M3 沿用） |
| HTTP | Ktor2.3.12 + OkHttp |
| 音频 | AudioRecord / MediaPlayer / AudioFocus；无第三方录音库 |

配置本机JDK和Android SDK35，Android Studio可生成不提交的`local.properties`。在本目录运行：

```powershell
.\gradlew.bat --no-daemon :app:testDebugUnitTest assembleDebug
# 只构建设备测试APK：
.\gradlew.bat --no-daemon assembleDebugAndroidTest
```

首次构建下载wrapper/依赖，需要网络；缓存齐全后才可加`--offline`。当前JVM单测为164项（2026-11-20第三轮独立复查）。

## 后端与手机

开发端点`http://127.0.0.1:8000/v1`由`app/build.gradle.kts`配置；可用 `-PmockmateApiBaseUrl=http://127.0.0.1:8101/v1` 构建隔离联调包。USB联调先启动电脑API和worker，再执行`adb reverse tcp:8000 tcp:8000`（隔离端口改成对应映射并核对`reverse --list`）。Debug允许开发HTTP；release不允许明文网络，正式端点/签名/部署尚未完成。

M3 首次录音会申请 `RECORD_AUDIO` 运行时权限；录音/播放在页面前台进行，离页/切后台停止并封存已采集片段，不自动继续。私有 take 元数据经 Keystore 加密，PCM 按账号/session/turn/take 隔离，系统备份排除 `files/voice` 和播报缓存。

安装前确认设备，用`adb install -r`保留数据，禁止为冲突擅自卸载/清数据。设备测试会创建或切换合成账号，只在授权设备和测试后端运行。

不要在留存数据设备上直接跑`connectedDebugAndroidTest`：此前UTP安装曾卸载App。使用显式安装和选定instrumentation：

```powershell
adb install -r app\build\outputs\apk\debug\app-debug.apk
adb install -t -r app\build\outputs\apk\androidTest\debug\app-debug-androidTest.apk
adb shell am instrument -w -r -e class com.mockmate.app.M1DeviceUiTest `
    com.mockmate.app.test/androidx.test.runner.AndroidJUnitRunner
```

依赖准备账号/后台时序的测试还需相应参数与隔离配置，不能把该单例当整套设备验收。`tools/m1/e04_setup.py`准备合成测试数据，输出含测试token，保留在本机`outputs/`，不上传。

M3 设备链路：

```powershell
# 隔离 test provider + 8101 后端并已 seed 时
.\gradlew.bat --no-daemon -PmockmateApiBaseUrl=http://127.0.0.1:8101/v1 :app:assembleDebug :app:assembleDebugAndroidTest
adb -s <serial> reverse tcp:8101 tcp:8101
adb -s <serial> shell am instrument -w -r -e class com.mockmate.app.M3VoiceDeviceTest `
    com.mockmate.app.test/androidx.test.runner.AndroidJUnitRunner
```

`M3VoiceDeviceTest` 默认用 debug-only fixture（1.2 秒合成 PCM）验证播题/录音/停止/上传/转写/核对/提交/追问与 2 题整场报告，并显示“调试 fixture”提示；**不是物理麦克风通过**。真机必须另做真实说话、耳机/来电、120 秒和 6 题。

2026-11-17 在当前 API37 模拟器上完成：debug APK 安装、注册登录、准备列表、面试记录入口、进行中面试间与真实报告页手动验证；同一合成账号经真实 API 完成六题并生成报告，报告页显示覆盖度、单题分、维度与逐字证据。`M2EntryDeviceTest` 已编译，但 Espresso 在该镜像初始化 `InputManager.getInstance` 失败，instrumentation 未通过；该API37失败属于历史记录；后续API30设备回归及其范围以M3验收记录为准，真人真实模型整场仍待完成。

## 实现边界

`core/network`为API；`core/session`为加密会话/草稿（M2 回答草稿在 `InterviewDraftStore`）；`core/audio`为 M3 AudioRecord/MediaPlayer/加密 take；`core/operation`为原key、类型化payload和原子操作记录；`data`为恢复scope归并及唯一JobWatch（M2 的 `follow_up`/`interview_report` 目标都是 session）；`ui`为Compose页面和ViewModel。

M2 面试间只展示已曝光回合；报告只发布覆盖度、逐题维度、逐字证据和有限优势/改进项。M3 语音报告同样只引用确认提交文本，并以“来源：语音转写（原样提交/人工核对）”标识，不评口音、音色或情绪。暂停中仍可查看已曝光回合，作答与跳过需先继续。计划页在无原场时显示本场处理说明并经用户同意后创建；有原场时直接打开或查看报告。

T01–T03恢复/并发/轮询及M3 F/G修复已有回归。U01的saveTextMaterial/uploadFile仍有同账号切页后迟到写回的静态风险，本次未动态复现或修复；confirmProfile已有页面/材料/revision保护。真实IME、系统选择器和跨账号迟到手机证据仍需补齐。M3真机fixture六题与120秒已有记录，真人真实模型六题、耳机/来电、权限和正式审核内容仍待验收。

真实麦克风声学回路测试 `M3RealMicDeviceTest` 必须显式传 `-e username <测试账号> -e password <本机生成的测试口令>`；不提供固定默认口令。账号准备和 `verify_speech.py --real` 可能调用真实服务，仅按负责人授权的受控预算执行，输出仅留忽略的本机目录。
