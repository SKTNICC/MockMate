# MockMate Android（M1）

Kotlin / Jetpack Compose单Activity应用，通过HTTP连接真实FastAPI后端。包含登录、准备、材料核对、匹配、计划确认与重开，以及加密会话/操作日志和任务恢复。

状态见[工程进度](../docs/工程进度.md)，环境步骤见[本地开发](../docs/本地开发.md)。界面接线不等于所有真机场景已验收。

## 工具链与构建

| 项目 | 版本 |
|---|---|
| JDK | 本机验证21，编译目标Java17 |
| AGP / Gradle wrapper | 8.7.3 / 8.10.2 |
| Kotlin / Compose插件 | 2.0.21 |
| Compose BOM | 2024.10.01 |
| minSdk / targetSdk / compileSdk | 26 / 35 / 35 |
| applicationId | `com.mockmate.app`，version `0.1.0-m1` / code1 |
| HTTP | Ktor2.3.12 + OkHttp |

配置本机JDK和Android SDK35，Android Studio可生成不提交的`local.properties`。在本目录运行：

```powershell
.\gradlew.bat --no-daemon :app:testDebugUnitTest assembleDebug
# 只构建设备测试APK：
.\gradlew.bat --no-daemon assembleDebugAndroidTest
```

首次构建下载wrapper/依赖，需要网络；缓存齐全后才可加`--offline`。当前JVM单测为61项。

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

## 实现边界

`core/network`为API；`core/session`为加密会话/草稿；`core/operation`为原key、类型化payload和原子操作记录；`data`为恢复scope归并及唯一JobWatch；`ui`为Compose页面和ViewModel。

T01–T03恢复/并发/轮询原问题有回归及部分真机证据；普通材料保存/上传/确认的迟到回写隔离U01仍需修复。真实IME、系统选择器和A/B迟到手机证据尚不完整。M1不含正式面试问答、追问、报告或语音/视频。
