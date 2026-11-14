# install_task.ps1 - 注册/管理 FreeModal 数据脑的本机定时刷新任务
#
# 任务内容：每小时运行一次 scripts/local_refresh.py --publish
# 行为：拉线上 -> 校验 -> 编译 -> 写 out/ -> 经 GitHub API 发布 offers.json
#
# 注意：
# - 任务只在本机开机时运行；关机期间的窗口不会补跑（StartWhenAvailable 会在开机后
#   尽快补跑一次错过的窗口，但不会补齐所有窗口）。
# - 使用 Interactive 登录类型：在用户登录会话中运行，锁屏可以跑，注销后不跑；
#   本机不开机也不跑。当前账户无需管理员权限即可注册。
# - 日志：logs/refresh.log（每次运行追加一行 JSON）
#   报告：out/refresh-report.json（最近一次运行的完整结果）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 install
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 status
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 run
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 logs
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 remove

param(
    [ValidateSet('install', 'remove', 'status', 'run', 'logs')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$TaskName = 'FreeModal-DataBrain-Refresh'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $PSScriptRoot 'local_refresh.py'
$LogPath = Join-Path $RepoRoot 'logs\refresh.log'

function Resolve-Python {
    $candidate = (Get-Command python.exe -ErrorAction SilentlyContinue)
    if (-not $candidate) { $candidate = (Get-Command python -ErrorAction SilentlyContinue) }
    if (-not $candidate) { throw '找不到 python，请先安装 Python 3 并加入 PATH' }
    return $candidate.Source
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "任务未注册：$TaskName"
        return 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $code = [int]$info.LastTaskResult
    $hint = switch ($code) {
        0 { '成功' }
        1 { '校验/编译/发布失败（看 logs/refresh.log 与 out/refresh-report.json）' }
        2 { '用法错误' }
        3 { '数据老化告警：数据超过阈值未更新' }
        4 { '缺少凭证：无法自动发布' }
        5 { '网络不可达：线上源与发布接口都失败' }
        267009 { '正在运行' }
        267011 { '尚未运行' }
        default { '未知（看 logs/refresh.log）' }
    }
    Write-Host ("任务      : " + $task.TaskName)
    Write-Host ("状态      : " + $task.State)
    Write-Host ("上次运行  : " + $info.LastRunTime)
    Write-Host ("上次结果  : " + $code + " (0x" + $code.ToString('X') + ") " + $hint)
    Write-Host ("下次运行  : " + $info.NextRunTime)
    Write-Host ("错过次数  : " + $info.NumberOfMissedRuns)
    Write-Host ("日志      : " + $LogPath)
    return 0
}

switch ($Action) {
    'install' {
        $python = Resolve-Python
        if (-not (Test-Path $ScriptPath)) { throw "找不到脚本：$ScriptPath" }

        $taskAction = New-ScheduledTaskAction `
            -Execute $python `
            -Argument ('"' + $ScriptPath + '" --publish') `
            -WorkingDirectory $RepoRoot
        $trigger = New-ScheduledTaskTrigger `
            -Once `
            -At (Get-Date).AddMinutes(2) `
            -RepetitionInterval (New-TimeSpan -Hours 1) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        $settings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal `
            -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) `
            -LogonType Interactive `
            -RunLevel Limited

        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $taskAction `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Description 'FreeModal 数据脑：每小时编译并发布 offers.json（本机链路；关机窗口不补跑）' `
            -Force | Out-Null

        Write-Host "已注册计划任务：$TaskName"
        Write-Host "  程序    : $python"
        Write-Host "  参数    : `"$ScriptPath`" --publish"
        Write-Host "  目录    : $RepoRoot"
        Write-Host "  节奏    : 每 1 小时，注册后约 2 分钟首次运行"
        Write-Host "  日志    : $LogPath"
        Write-Host ''
        Write-Host '说明：任务只在本机开机且用户已登录时运行（锁屏可以跑，注销不跑）；关机会错过窗口（开机后 StartWhenAvailable 会补跑一次）。'
        Write-Host '查看：powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 status'
        Show-Status | Out-Null
    }
    'remove' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Host "任务未注册：$TaskName"
            exit 1
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除计划任务：$TaskName"
    }
    'status' {
        exit (Show-Status)
    }
    'run' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) { throw "任务未注册：$TaskName，请先运行 install" }
        Start-ScheduledTask -TaskName $TaskName
        Write-Host '已触发，等待任务结束（最多 3 分钟）…'
        $deadline = (Get-Date).AddMinutes(3)
        do {
            Start-Sleep -Seconds 3
            $state = (Get-ScheduledTask -TaskName $TaskName).State
        } while ($state -eq 'Running' -and (Get-Date) -lt $deadline)
        exit (Show-Status)
    }
    'logs' {
        if (-not (Test-Path $LogPath)) {
            Write-Host '暂无日志：' $LogPath
            exit 0
        }
        Get-Content -Path $LogPath -Tail 20 -Encoding UTF8
        exit 0
    }
}
