param(
    [Parameter(Mandatory = $true)][string]$Adb,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$Pass,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [Parameter(Mandatory = $true)][ValidateSet('success', 'conflict')][string]$Mode,
    [string]$Token = '',
    [string]$PrepId = ''
)

$className = if ($Mode -eq 'success') {
    'com.mockmate.app.M1RestoreIsolationDeviceTest#succeededP1PlanDoesNotProjectIntoP2AndCanBeReopened'
} else {
    'com.mockmate.app.M1RestoreIsolationDeviceTest#supersededP1JobDoesNotProjectErrorIntoP2AndP1StaysWithoutPlan'
}
$marker = if ($Mode -eq 'success') { 'P1_RUNNING' } else { 'MISMATCH_READY' }
$log = Join-Path $OutDir "e04-m03-$Mode.log"
$runner = 'com.mockmate.app.test/androidx.test.runner.AndroidJUnitRunner'

& $Adb logcat -c
$arguments = @(
    'shell', 'am', 'instrument', '-w', '-r',
    '-e', 'e04_user', $User,
    '-e', 'e04_pass', $Pass,
    '-e', 'class', $className,
    $runner
)
$process = Start-Process -FilePath $Adb -ArgumentList $arguments -RedirectStandardOutput $log `
    -RedirectStandardError ($log + '.err') -NoNewWindow -PassThru
Write-Output "instrument pid=$($process.Id)"

$deadline = (Get-Date).AddSeconds(150)
$found = $false
while ((Get-Date) -lt $deadline) {
    $marks = & $Adb logcat -d -s M1D_M03:I 2>&1
    if ($marks -match $marker) {
        $found = $true
        Write-Output "marker $marker observed"
        break
    }
    if ($process.HasExited) { break }
    Start-Sleep -Seconds 1
}
if (-not $found) {
    Write-Output 'ISOLATION_MARKER_NOT_FOUND'
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    Get-Content $log -Tail 50 -ErrorAction SilentlyContinue
    exit 2
}

if ($Mode -eq 'conflict') {
    if (-not $Token -or -not $PrepId) {
        Write-Output 'MISSING_TOKEN_OR_PREP'
        exit 4
    }
    $headers = @{ Authorization = "Bearer $Token"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
    $prep = Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/preparations/$PrepId" -Headers $headers
    # 只改题量才会产生真实的新 revision；同内容 PUT 是幂等读回，不会 bump。
    $newCount = if ($prep.main_question_count -eq 6) { 4 } else { 6 }
    $body = @{
        revision = $prep.revision
        role_id = $prep.role_id
        level = $prep.level
        main_question_count = $newCount
        resume_snapshot_id = $prep.resume_snapshot_id
        jd_snapshot_id = $prep.jd_snapshot_id
    } | ConvertTo-Json
    $updated = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:8000/v1/preparations/$PrepId" `
        -Headers $headers -ContentType 'application/json' -Body $body
    Write-Output "revision bumped to $($updated.revision), count=$($updated.main_question_count)"
}

if (-not $process.WaitForExit(420000)) {
    Stop-Process -Id $process.Id -Force
    Write-Output 'ISOLATION_TIMEOUT'
    exit 3
}
Write-Output "instrument exit=$($process.ExitCode)"
Get-Content $log -Tail 60 -ErrorAction SilentlyContinue
