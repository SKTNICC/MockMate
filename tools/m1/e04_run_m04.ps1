param(
    [Parameter(Mandatory = $true)][string]$Adb,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$Pass,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$phase1 = Join-Path $OutDir 'e04-m04-phase1.log'
$phase2 = Join-Path $OutDir 'e04-m04-phase2.log'
$markers = Join-Path $OutDir 'e04-m04-markers.log'
$runner = 'com.mockmate.app.test/androidx.test.runner.AndroidJUnitRunner'

function Start-Instrument([string]$Class, [string]$LogPath) {
    $arguments = @(
        'shell', 'am', 'instrument', '-w', '-r',
        '-e', 'e04_user', $User,
        '-e', 'e04_pass', $Pass,
        '-e', 'class', $Class,
        $runner
    )
    return Start-Process -FilePath $Adb -ArgumentList $arguments -RedirectStandardOutput $LogPath `
        -RedirectStandardError ($LogPath + '.err') -NoNewWindow -PassThru
}

& $Adb logcat -c
$p1 = Start-Instrument 'com.mockmate.app.M1RunningJobPhase1Test' $phase1
Write-Output "phase1 started pid=$($p1.Id)"

$deadline = (Get-Date).AddSeconds(150)
$started = $false
while ((Get-Date) -lt $deadline) {
    $marks = & $Adb logcat -d -s M1D_M07:I M1D_M04:I 2>&1
    if ($marks -match 'GENERATION_STARTED') {
        $started = $true
        break
    }
    if ($p1.HasExited) { break }
    Start-Sleep -Seconds 1
}
if (-not $started) {
    Write-Output 'GENERATION_MARKER_NOT_FOUND'
    if (-not $p1.HasExited) { Stop-Process -Id $p1.Id -Force }
    Get-Content $phase1 -Tail 40 -ErrorAction SilentlyContinue
    exit 2
}

# 看到“执行中 + 前后台已复验”后立刻停进程，确保冷启时任务仍在 running。
Start-Sleep -Seconds 3
& $Adb shell am force-stop com.mockmate.app
& $Adb shell am force-stop com.mockmate.app.test
Start-Sleep -Seconds 2
if (-not $p1.HasExited) { Stop-Process -Id $p1.Id -Force }
& $Adb logcat -d -s M1D_M07:I M1D_M04:I 2>&1 | Set-Content -Encoding utf8 $markers

# 等设备端 instrumentation 彻底退出，避免新 am instrument 与旧会话竞争而挂起。
for ($i = 0; $i -lt 30; $i++) {
    $alive = & $Adb shell ps -A 2>&1 | Select-String 'com\.mockmate\.app'
    if (-not $alive) { break }
    Start-Sleep -Seconds 1
}
& $Adb shell am force-stop com.mockmate.app
& $Adb shell am force-stop com.mockmate.app.test
Start-Sleep -Seconds 10

$p2 = Start-Instrument 'com.mockmate.app.M1RunningJobPhase2Test' $phase2
Write-Output "phase2 started pid=$($p2.Id)"
if (-not $p2.WaitForExit(240000)) {
    Stop-Process -Id $p2.Id -Force
    Write-Output 'PHASE2_TIMEOUT'
    exit 3
}
Write-Output "phase2 exit=$($p2.ExitCode)"
Get-Content $phase2 -Tail 60 -ErrorAction SilentlyContinue
