[CmdletBinding()]
param(
    [ValidateSet("install", "start", "stop", "restart", "status", "uninstall")]
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonwPath = Join-Path $env:USERPROFILE "anaconda3\envs\lol-env\pythonw.exe"
$pythonPath = Join-Path $env:USERPROFILE "anaconda3\envs\lol-env\python.exe"
$script:ExitCode = 0
$taskDefinitions = @(
    [pscustomobject]@{ Name = "LoLHighlights-Scheduler"; Arguments = "-m automation.run --schedule" },
    [pscustomobject]@{ Name = "LoLHighlights-ClipWorker"; Arguments = "-m automation.run --clip-worker" },
    [pscustomobject]@{ Name = "LoLHighlights-LiveSplit"; Arguments = "-m automation.run --live-split" },
    [pscustomobject]@{ Name = "LoLHighlights-Dashboard"; Arguments = "-m dashboard.watch_progress" }
)

function Get-ManagedTask {
    param([Parameter(Mandatory)]$Definition)

    Get-ScheduledTask -TaskName $Definition.Name -ErrorAction SilentlyContinue
}

function Stop-ManagedTasks {
    param([switch]$Disable)

    foreach ($definition in $taskDefinitions) {
        $task = Get-ManagedTask -Definition $definition
        if ($null -ne $task -and $task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $definition.Name
            Write-Host "[OK] stopped $($definition.Name)"
        }
        if ($Disable -and $null -ne $task -and $task.State -ne "Disabled") {
            Disable-ScheduledTask -TaskName $definition.Name | Out-Null
            Write-Host "[OK] disabled $($definition.Name)"
        }
    }
}

function Stop-LegacyWorkers {
    $commandPattern = "automation\.run.*--(?:schedule|clip-worker|live-split)|dashboard\.watch_progress"

    try {
        $processes = Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -in @("python.exe", "pythonw.exe") -and
                $_.ExecutablePath -in @($pythonPath, $pythonwPath) -and
                $_.CommandLine -match $commandPattern
            }
    }
    catch {
        throw "Cannot inspect existing workers. Run this script from your normal Windows account. $($_.Exception.Message)"
    }

    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force
        Write-Host "[OK] stopped legacy worker PID $($process.ProcessId)"
    }
}

function Start-ManagedTasks {
    foreach ($definition in $taskDefinitions) {
        $task = Get-ManagedTask -Definition $definition
        if ($null -eq $task) {
            throw "Scheduled task is not installed: $($definition.Name)"
        }
        if ($task.State -eq "Disabled") {
            Enable-ScheduledTask -TaskName $definition.Name | Out-Null
            $task = Get-ManagedTask -Definition $definition
            Write-Host "[OK] enabled $($definition.Name)"
        }
        if ($task.State -ne "Running") {
            Start-ScheduledTask -TaskName $definition.Name
            Write-Host "[OK] started $($definition.Name)"
        }
    }
}

function Install-ManagedTasks {
    if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
        throw "lol-env pythonw.exe not found: $pythonwPath"
    }

    Stop-ManagedTasks
    Stop-LegacyWorkers

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $watchdogTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1)
    $triggers = @($logonTrigger, $watchdogTrigger)
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 255 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    foreach ($definition in $taskDefinitions) {
        $taskAction = New-ScheduledTaskAction `
            -Execute $pythonwPath `
            -Argument $definition.Arguments `
            -WorkingDirectory $projectRoot

        Register-ScheduledTask `
            -TaskName $definition.Name `
            -Action $taskAction `
            -Trigger $triggers `
            -Settings $settings `
            -Principal $principal `
            -Description "LoL Highlights supervised background process" `
            -Force | Out-Null

        Write-Host "[OK] installed $($definition.Name)"
    }

    Start-ManagedTasks
}

function Show-ManagedTaskStatus {
    $missing = $false

    foreach ($definition in $taskDefinitions) {
        $task = Get-ManagedTask -Definition $definition
        if ($null -eq $task) {
            $missing = $true
            [pscustomobject]@{
                TaskName       = $definition.Name
                State          = "NotInstalled"
                LastRunTime    = $null
                LastTaskResult = $null
            }
            continue
        }

        $info = Get-ScheduledTaskInfo -TaskName $definition.Name
        [pscustomobject]@{
            TaskName       = $definition.Name
            State          = $task.State
            LastRunTime    = $info.LastRunTime
            LastTaskResult = $info.LastTaskResult
        }
    }

    if ($missing) {
        $script:ExitCode = 1
    }
}

switch ($Action) {
    "install" {
        Install-ManagedTasks
        Show-ManagedTaskStatus | Format-Table -AutoSize
    }
    "start" {
        Start-ManagedTasks
        Show-ManagedTaskStatus | Format-Table -AutoSize
    }
    "stop" {
        Stop-ManagedTasks -Disable
        Show-ManagedTaskStatus | Format-Table -AutoSize
    }
    "restart" {
        Stop-ManagedTasks
        Stop-LegacyWorkers
        Start-ManagedTasks
        Show-ManagedTaskStatus | Format-Table -AutoSize
    }
    "status" {
        Show-ManagedTaskStatus | Format-Table -AutoSize
    }
    "uninstall" {
        Stop-ManagedTasks
        foreach ($definition in $taskDefinitions) {
            if ($null -ne (Get-ManagedTask -Definition $definition)) {
                Unregister-ScheduledTask -TaskName $definition.Name -Confirm:$false
                Write-Host "[OK] uninstalled $($definition.Name)"
            }
        }
    }
}

if ($script:ExitCode -ne 0) {
    exit $script:ExitCode
}
