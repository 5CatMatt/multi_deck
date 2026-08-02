<#
.SYNOPSIS
    Registers the multi_deck agent to start at logon.

.DESCRIPTION
    Creates a Scheduled Task rather than a Startup shortcut, because the task can run with
    highest privileges. That matters: input synthesised by the agent cannot reach elevated
    windows unless the agent is itself elevated. (Keystrokes the *device* sends as HID are
    unaffected — they are real keycodes and always get through. This only concerns the
    agent-side actions.)

    Registering a task with RunLevel Highest requires an elevated PowerShell. If you do not
    need to drive elevated windows, -NoElevation registers a normal-privilege task instead,
    which works without admin rights.

    Uses pythonw.exe so no console window appears at logon.

.EXAMPLE
    .\install_autostart.ps1                 # needs an elevated PowerShell
    .\install_autostart.ps1 -NoElevation    # works unelevated, cannot reach admin windows
    .\install_autostart.ps1 -Remove
#>

[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$NoElevation,
    [string]$TaskName = "multi_deck agent"
)

$ErrorActionPreference = "Stop"

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Remove) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } catch {
        Write-Host "No scheduled task named '$TaskName' was registered."
    }
    return
}

$elevated = Test-Elevated

if (-not $NoElevation -and -not $elevated) {
    Write-Host ""
    Write-Warning "Registering a task with highest privileges requires an elevated PowerShell."
    Write-Host ""
    Write-Host "Either open PowerShell as Administrator and re-run this script, or run:"
    Write-Host ""
    Write-Host "    .\install_autostart.ps1 -NoElevation"
    Write-Host ""
    Write-Host "-NoElevation works without admin rights. The only thing it gives up is that"
    Write-Host "agent-synthesised keystrokes (AHK actions) cannot reach windows running as"
    Write-Host "administrator. Launching apps, macros and the stats page are unaffected, and"
    Write-Host "the ten-key still works everywhere because that is real USB HID from the deck."
    Write-Host ""
    throw "Not elevated. See the options above."
}

$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolve the interpreter by asking Python where it actually lives, rather than trusting
# PATH. On Windows `pythonw.exe` frequently resolves to the Microsoft Store execution alias
# in WindowsApps, which is a different installation without deckhost on its path -- and
# Store aliases are unreliable when launched from a scheduled task.
$realPython = & python -c "import sys; print(sys.executable)" 2>$null
if (-not $realPython) {
    throw "No working Python found on PATH. Try: python -V"
}

# Confirm this interpreter can actually see the package before wiring it to logon. Use the
# console build for the check, since pythonw swallows output and its exit code is unhelpful.
& $realPython -c "import deckhost" 2>$null
if (-not $?) {
    throw "deckhost is not importable by $realPython. Run:  pip install -e `"$agentDir`""
}

# pythonw.exe beside it keeps the console hidden at logon.
$pythonw = Join-Path (Split-Path -Parent $realPython) "pythonw.exe"
if (Test-Path $pythonw) {
    $python = $pythonw
} else {
    Write-Warning "pythonw.exe not found beside $realPython; a console window will appear at logon."
    $python = $realPython
}

$runLevel = if ($NoElevation) { "Limited" } else { "Highest" }

Write-Host "Interpreter : $python"
Write-Host "Working dir : $agentDir"
Write-Host "Run level   : $runLevel"

$action = New-ScheduledTaskAction -Execute $python -Argument "-m deckhost --tray" -WorkingDirectory $agentDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# LogonType must be "Interactive". The scheduling COM API calls this value "InteractiveToken"
# and much of the documentation uses that spelling, but the PowerShell enum does not accept it.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel $runLevel

# The deck may not be plugged in at logon, and the agent should keep running regardless of
# battery state, so the default power conditions are unhelpful here.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

try {
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "multi_deck touchscreen deck companion agent" `
        -Force `
        -ErrorAction Stop | Out-Null
} catch {
    throw "Register-ScheduledTask failed: $($_.Exception.Message)"
}

# Verify rather than assume. Register-ScheduledTask can report a non-terminating CIM error
# that slips past ErrorActionPreference, and a script that claims success after failing is
# far worse than one that fails loudly -- you go looking for the bug in the wrong place.
$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $registered) {
    throw "Registration reported no error but the task does not exist. Nothing was installed."
}

Write-Host ""
Write-Host "Registered '$TaskName' (run level: $runLevel), verified present."
Write-Host ""
Write-Host "  Start now   : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Stop        : Stop-ScheduledTask  -TaskName '$TaskName'   (or Quit from the tray)"
Write-Host "  Remove      : .\install_autostart.ps1 -Remove"
Write-Host "  Log         : $env:LOCALAPPDATA\multi_deck\deckhost.log"
Write-Host ""
Write-Host "There is no console window under pythonw, so the log file and the tray icon are"
Write-Host "how you see what it is doing."
Write-Host ""
Write-Host "Windows 11 hides new tray icons by default. Click the '^' chevron next to the"
Write-Host "clock to find it, then drag it onto the taskbar to keep it visible."
