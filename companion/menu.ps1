#Requires -Version 7.0
<#
.SYNOPSIS
    One menu for every program involved in the allowance link.

.DESCRIPTION
    The programs themselves live at their canonical paths (windows_companion.py
    at the repository root, the rest under tools\) so there is exactly one copy
    of each. This menu is the single place they are all reachable from, which is
    the point of the companion\ folder.

.EXAMPLE
    .\menu.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

$config = Get-CompanionConfig
$python = Resolve-Python -Configured $config.PythonPath

function Get-Entries {
    $entries = [System.Collections.Generic.List[object]]::new()
    $add = {
        param($Label, $Script, $Arguments, $Note)
        $path = if ($Script -eq '<self>') { $Script } else { Join-Path $script:RepoRoot $Script }
        $exists = ($Script -eq '<self>') -or (Test-Path -LiteralPath $path)
        $entries.Add([pscustomobject]@{
                Label     = $Label
                Script    = $Script
                Arguments = $Arguments
                Note      = $Note
                Exists    = $exists
            })
    }

    & $add '持续同步额度（前台运行，Ctrl+C 停止）' 'start-companion.ps1' @() '推荐日常使用'
    & $add '只读体检：额度 / 板子 / GATT / 电源策略' 'diagnose.ps1' @() '排障第一步'
    & $add '只读：打印本机额度 JSON（不碰蓝牙）' 'windows_companion.py' @('--json-only', '-v') ''
    & $add '写入一次额度' 'windows_companion.py' @('--once', '-v') ''
    & $add '探测额度服务并保持链路（不写入）' 'windows_companion.py' @('--probe-only', '--hold-seconds', '60', '-v') ''
    & $add '列出 Windows 已知 BLE 设备' 'tools\ble_devices.py' @() '确认板子在不在'
    & $add '扫描 BLE 设备' 'tools\ble_scan.py' @() '约 30 秒'
    & $add '主机侧配对记录诊断' 'tools\ble_host_diag.py' @() '需要板子地址，会自动填入'
    & $add '重新配对（清主机侧残留绑定）' 'windows_companion.py' @('--repair-pairing', '-v') '会改动本机蓝牙状态'
    & $add '查看蓝牙网卡/板子的省电策略' 'tools\bt_power_repair.py' @() '管理员可加 --apply'
    & $add '开关蓝牙无线电（治主机栈假死）' 'tools\bt_radio_toggle.py' @() '所有蓝牙设备会断几秒'
    & $add '查看板子 HID 能力' 'tools\hid_caps.py' @() '应看到 usagePage=0xFF00'
    & $add '按桌面端分帧直发一条 RPC' 'tools\hid_rpc.py' @('--method', 'sys.version', '--listen', '5') ''
    & $add '查看桌面端进程与网卡状态' 'tools\codex_host_state.py' @() ''
    & $add '查看后台运行状态（任务 / 进程 / 日志）' 'status-companion.ps1' @() '确认常驻是否在跑'
    & $add '停止后台运行（保留自启注册）' 'stop-companion.ps1' @() ''
    & $add '注册开机自启（隐藏窗口的计划任务）' 'install-autostart.ps1' @() '不需要管理员'
    & $add '取消开机自启' 'uninstall-autostart.ps1' @() ''
    return $entries
}

$entries = Get-Entries

while ($true) {
    Write-Host ''
    Write-Host '  Codex Micro companion' -ForegroundColor Cyan
    Write-Host "    repo   : $script:RepoRoot"
    Write-Host "    python : $(if ($python) { $python } else { 'NOT FOUND' })"
    Write-Host ''

    for ($i = 0; $i -lt $entries.Count; $i++) {
        $entry = $entries[$i]
        $marker = if ($entry.Exists) { ' ' } else { '!' }
        $colour = if ($entry.Exists) { 'Gray' } else { 'DarkGray' }
        $line = '  {0,2}){1} {2}' -f ($i + 1), $marker, $entry.Label
        Write-Host $line -ForegroundColor $colour
        if ($entry.Note) { Write-Host "        $($entry.Note)" -ForegroundColor DarkGray }
    }
    Write-Host '   0)  退出' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  "!" marks a program that is not present in this checkout.' -ForegroundColor DarkGray

    $choice = Read-Host '  Select'
    if ($choice -eq '0' -or $choice -eq 'q' -or $choice -eq '') { break }

    $index = 0
    if (-not [int]::TryParse($choice, [ref] $index)) { continue }
    if ($index -lt 1 -or $index -gt $entries.Count) { continue }

    $entry = $entries[$index - 1]
    if (-not $entry.Exists) {
        Write-Bad "$($entry.Script) is not present in this checkout."
        continue
    }
    if (-not $python) {
        Write-Bad 'No Python interpreter found; set PythonPath in companion\config.psd1.'
        continue
    }

    Write-Host ''
    switch ($entry.Script) {
        'start-companion.ps1' {
            & (Join-Path $PSScriptRoot 'start-companion.ps1')
        }
        'diagnose.ps1' {
            & (Join-Path $PSScriptRoot 'diagnose.ps1')
        }
        'status-companion.ps1' {
            & (Join-Path $PSScriptRoot 'status-companion.ps1')
        }
        'stop-companion.ps1' {
            & (Join-Path $PSScriptRoot 'stop-companion.ps1')
        }
        'install-autostart.ps1' {
            & (Join-Path $PSScriptRoot 'install-autostart.ps1') -StartNow
        }
        'uninstall-autostart.ps1' {
            & (Join-Path $PSScriptRoot 'uninstall-autostart.ps1')
        }
        default {
            $arguments = @($entry.Arguments)
            if ($entry.Script -like '*ble_host_diag.py') {
                $address = Get-BoardAddress -Configured $config.DeviceAddress -Python $python
                if (-not $address) {
                    Write-Bad 'The board is not enumerated, so there is no address to inspect.'
                    continue
                }
                $arguments = @($address)
            }
            $needsAddress = @('--once', '--probe-only', '--repair-pairing') |
                Where-Object { $entry.Arguments -contains $_ }
            if ($entry.Script -eq 'windows_companion.py' -and $needsAddress) {
                $address = Get-BoardAddress -Configured $config.DeviceAddress -Python $python
                if (-not $address) {
                    Write-Bad 'The board is not enumerated. Pair it from Settings first.'
                    continue
                }
                $arguments = @('--device-address', $address) + $arguments
            }

            $target = Join-Path $script:RepoRoot $entry.Script
            Write-Host "  > python $($entry.Script) $($arguments -join ' ')" -ForegroundColor DarkGray
            Write-Host ''
            & $python $target @arguments
            Write-Host ''
            Write-Host "  exit code $LASTEXITCODE" -ForegroundColor DarkGray
            Write-Host '  Press Enter to return to the menu.' -ForegroundColor DarkGray
            Read-Host | Out-Null
        }
    }
}
