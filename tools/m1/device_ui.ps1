# 真机 UI 辅助脚本（测试工具，不是产品代码）。
# 用 adb + uiautomator 读取当前界面节点并点击/输入；仅用于本项目已授权设备。
# 用法：
#   . .\tools\m1\device_ui.ps1
#   Show-UiTexts
#   Tap-UiText -Text "登录"
#   Input-AdbText -Text "dev_user_01"

$script:Adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
# 可选：连接多台设备时指定序列号，例如 emulator-5554
$script:AdbSerial = $null
$script:UiXmlPath = Join-Path $env:TEMP "mockmate-ui.xml"

function Get-UiDocument {
    $target = if ($script:AdbSerial) { @("-s", $script:AdbSerial) } else { @() }
    & $script:Adb @target shell uiautomator dump /sdcard/mockmate-ui.xml | Out-Null
    & $script:Adb @target pull /sdcard/mockmate-ui.xml $script:UiXmlPath 2>&1 | Out-Null
    [xml]$xml = Get-Content -Path $script:UiXmlPath -Raw -Encoding UTF8
    return $xml
}

function Get-UiNodes {
    $xml = Get-UiDocument
    $nodes = $xml.SelectNodes("//node")
    $result = @()
    foreach ($node in $nodes) {
        $text = $node.GetAttribute("text")
        $desc = $node.GetAttribute("content-desc")
        $bounds = $node.GetAttribute("bounds")
        if ([string]::IsNullOrEmpty($text) -and [string]::IsNullOrEmpty($desc)) { continue }
        if ($bounds -match "\[(\d+),(\d+)\]\[(\d+),(\d+)\]") {
            $x1 = [int]$Matches[1]; $y1 = [int]$Matches[2]; $x2 = [int]$Matches[3]; $y2 = [int]$Matches[4]
            $result += [pscustomobject]@{
                Text  = if ($text) { $text } else { $desc }
                Class = $node.GetAttribute("class")
                Clickable = $node.GetAttribute("clickable")
                X = [int](($x1 + $x2) / 2)
                Y = [int](($y1 + $y2) / 2)
                Bounds = $bounds
            }
        }
    }
    return $result
}

function Show-UiTexts {
    Get-UiNodes | Format-Table -AutoSize
}

function Find-UiNode {
    param(
        [string]$Text,
        [string]$Contains,
        [switch]$Exact
    )
    $nodes = Get-UiNodes
    if ($Text) {
        if ($Exact) { return $nodes | Where-Object { $_.Text -eq $Text } | Select-Object -First 1 }
        return $nodes | Where-Object { $_.Text -like "*$Text*" } | Select-Object -First 1
    }
    if ($Contains) {
        return $nodes | Where-Object { $_.Text -like "*$Contains*" } | Select-Object -First 1
    }
    return $null
}

function Tap-UiPoint {
    param([int]$X, [int]$Y)
    $target = if ($script:AdbSerial) { @("-s", $script:AdbSerial) } else { @() }
    & $script:Adb @target shell input tap $X $Y | Out-Null
}

function Tap-UiText {
    param([string]$Text, [switch]$Exact, [switch]$Contains)
    $node = if ($Contains) { Find-UiNode -Contains $Text } else { Find-UiNode -Text $Text -Exact:$Exact }
    if ($null -eq $node) { throw "未找到界面文字: $Text" }
    Tap-UiPoint -X $node.X -Y $node.Y
    Start-Sleep -Milliseconds 700
    return $node
}

function Input-AdbText {
    param([string]$Text)
    # adb input text 只可靠支持 ASCII；空格需要转义为 %s。
    $escaped = $Text.Replace(" ", "%s")
    $target = if ($script:AdbSerial) { @("-s", $script:AdbSerial) } else { @() }
    & $script:Adb @target shell input text $escaped | Out-Null
    Start-Sleep -Milliseconds 300
}

function Save-UiScreenshot {
    param([string]$Path)
    $dir = Split-Path $Path -Parent
    if ($dir) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $target = if ($script:AdbSerial) { @("-s", $script:AdbSerial) } else { @() }
    & $script:Adb @target exec-out screencap -p > $Path
    return (Get-Item $Path).FullName
}

function Invoke-UiSwipeUp {
    param([int]$Count = 1)
    for ($i = 0; $i -lt $Count; $i++) {
        $target = if ($script:AdbSerial) { @("-s", $script:AdbSerial) } else { @() }
        & $script:Adb @target shell input swipe 540 1700 540 600 300 | Out-Null
        Start-Sleep -Milliseconds 500
    }
}
