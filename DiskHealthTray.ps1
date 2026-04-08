# ==============================================================================
#  DiskHealth Tray Icon v2.2
#  Runs as logged-in user at logon — shows agent status in system tray
# ==============================================================================
param([string]$InstallDir = "")

# Resolve install directory — same logic as the agent script
if (-not $InstallDir) {
    foreach ($_c in @(
        "$env:ProgramFiles\DiskHealthAgent",
        "C:\Program Files\DiskHealthAgent",
        "C:\DiskHealthAgent"
    )) {
        if (Test-Path (Join-Path $_c "DiskHealthAgent.ps1")) { $InstallDir = $_c; break }
    }
}
if (-not $InstallDir) { $InstallDir = "$env:ProgramFiles\DiskHealthAgent" }

# ── Single-instance guard ─────────────────────────────────────────────────────
$mutexName = "Global\DiskHealthTrayIcon"
$mutex     = New-Object System.Threading.Mutex($false, $mutexName)
$owned     = $false
try     { $owned = $mutex.WaitOne(0, $false) }
catch   [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { $mutex.Dispose(); exit 0 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$LogFile       = Join-Path $InstallDir "agent.log"
$ServerUrlFile = Join-Path $InstallDir "server_url.txt"
$AgentIdFile   = Join-Path $InstallDir "agent_id.txt"
$NotifyFile    = Join-Path $InstallDir "update_notify.txt"
$AgentTask     = "DiskHealthAgent"

# ── Panel URL (includes agent_id for auto-login) ──────────────────────────────
function Get-PanelUrl {
    try {
        if (-not (Test-Path $ServerUrlFile)) { return $null }
        $base = (Get-Content $ServerUrlFile -Raw -ErrorAction SilentlyContinue).Trim()
        if (-not $base -or $base -notmatch "^https?://") { return $null }
        if (Test-Path $AgentIdFile) {
            $id = (Get-Content $AgentIdFile -Raw -ErrorAction SilentlyContinue).Trim()
            if ($id -match "^[0-9a-f\-]{36}$") { return "$base/?agent_id=$id" }
        }
        return $base
    } catch { return $null }
}

# ── Icon builder ──────────────────────────────────────────────────────────────
function Make-Icon {
    param([string]$Color = "#22c55e")
    $bmp = New-Object System.Drawing.Bitmap(32,32)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.Clear([System.Drawing.Color]::Transparent)
    $col   = [System.Drawing.ColorTranslator]::FromHtml($Color)
    $light = [System.Drawing.Color]::FromArgb(255,[math]::Min(255,$col.R+80),[math]::Min(255,$col.G+80),[math]::Min(255,$col.B+80))
    $dark  = [System.Drawing.Color]::FromArgb(255,[math]::Max(0,$col.R-60),[math]::Max(0,$col.G-60),[math]::Max(0,$col.B-60))
    $bb = New-Object System.Drawing.SolidBrush($col);   $g.FillRectangle($bb, 2, 6, 28, 20)
    $hb = New-Object System.Drawing.SolidBrush($light); $g.FillRectangle($hb, 2, 6, 28, 5)
    $sb = New-Object System.Drawing.SolidBrush($dark);  $g.FillRectangle($sb, 2, 21, 28, 5)
    $pn = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(220,0,0,0), 1.5)
    $g.DrawRectangle($pn, 2, 6, 27, 19)
    $sl = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(100,0,0,0))
    $g.FillRectangle($sl, 4, 14, 14, 4)
    $gl = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(140,$col.R,$col.G,$col.B))
    $wh = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $g.FillEllipse($gl, 20, 13, 8, 8); $g.FillEllipse($wh, 22, 15, 4, 4)
    $g.Dispose()
    $bb.Dispose(); $hb.Dispose(); $sb.Dispose(); $pn.Dispose()
    $sl.Dispose(); $gl.Dispose(); $wh.Dispose()
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

# ── Agent status from log file ────────────────────────────────────────────────
function Get-AgentStatus {
    if (-not (Test-Path $LogFile)) {
        # Log doesn't exist yet — agent is still starting or install is fresh
        # Check if install dir itself exists to give better hint
        if (-not (Test-Path $InstallDir)) {
            return @{color="#f59e0b";tip="DiskHealth Agent`nInstall dir not found: $InstallDir`nStatus: Not installed?";status="warning"}
        }
        return @{color="#22c55e";tip="DiskHealth Agent`nWaiting for first log entry...`nStatus: Starting";status="starting"}
    }
    try {
        $stream  = [System.IO.File]::Open($LogFile,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::ReadWrite)
        $reader  = New-Object System.IO.StreamReader($stream)
        $content = $reader.ReadToEnd(); $reader.Close(); $stream.Close()
        $lines = $content -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 60
        $last  = $lines | Where-Object { $_ -match "\[INFO \]|\[WARN \]|\[ERROR\]" } | Select-Object -Last 1
        $ts    = if ($last -match "\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]") { $matches[1] } else { "Unknown" }

        if ($last -match "Report accepted") {
            return @{color="#22c55e";tip="DiskHealth Agent`nLast report: $ts`nStatus: OK";status="ok"}
        }
        elseif ($last -match "ERROR") {
            return @{color="#ef4444";tip="DiskHealth Agent`nLast event: $ts`nStatus: Error";status="error"}
        }
        elseif ($last -match "WARN") {
            return @{color="#f59e0b";tip="DiskHealth Agent`nLast event: $ts`nStatus: Warning";status="warning"}
        }
        else {
            return @{color="#22c55e";tip="DiskHealth Agent`nLast event: $ts`nStatus: Running";status="ok"}
        }
    } catch {
        return @{color="#22c55e";tip="DiskHealth Agent`nChecking status...";status="starting"}
    }
}

# ── Is the agent process alive? ───────────────────────────────────────────────
# IMPORTANT: The agent runs as SYSTEM. From a user-context process,
# WMI returns NULL for CommandLine of SYSTEM processes (security restriction).
# We therefore NEVER rely on CommandLine matching.
# Instead we use log file recency (agent re-registers every 60s → writes to log)
# and scheduled task state as a secondary check.
function Is-AgentRunning {
    # ── Method 1 (primary): Log file freshness ────────────────────────────────
    # Agent writes to log at minimum every ~60 s (re-register heartbeat).
    # If the log was modified within 3 minutes the agent is definitely alive.
    try {
        if (Test-Path $LogFile) {
            $age = (Get-Date) - (Get-Item $LogFile -ErrorAction SilentlyContinue).LastWriteTime
            if ($age.TotalMinutes -lt 3) { return $true }
        }
    } catch {}

    # ── Method 2 (secondary): Scheduled task state ───────────────────────────
    # When the infinite-loop script is running, task state = Running.
    # Note: from a non-elevated user session Get-ScheduledTask may show
    # "Ready" even while running — so this is a best-effort fallback only.
    try {
        $task = Get-ScheduledTask -TaskName $AgentTask -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq "Running") { return $true }
    } catch {}

    # ── Method 3 (fallback): Any powershell process running the agent ─────────
    # We can't read CommandLine for SYSTEM processes, but we CAN check if any
    # powershell.exe is running AND the log exists and was written at some point.
    # Only use this as last resort to avoid false positives.
    try {
        $psProcs = @(Get-Process -Name powershell,pwsh -ErrorAction SilentlyContinue)
        if ($psProcs.Count -gt 0 -and (Test-Path $LogFile)) {
            $logAge = (Get-Date) - (Get-Item $LogFile).LastWriteTime
            # If log exists and a powershell process is running, give benefit of doubt
            # for a longer window (e.g., very long poll intervals)
            if ($logAge.TotalMinutes -lt 15) { return $true }
        }
    } catch {}

    return $false
}

# ── Update notification balloon ───────────────────────────────────────────────
function Check-UpdateNotify {
    if (-not (Test-Path $NotifyFile)) { return }
    try {
        $msg = [System.IO.File]::ReadAllText($NotifyFile).Trim()
        Remove-Item $NotifyFile -Force -ErrorAction SilentlyContinue
        if (-not $msg) { return }

        # Choose icon and title based on message content
        if ($msg -match "started|starting|Downloading") {
            # Show yellow "updating" icon temporarily
            $tray.Icon = Make-Icon -Color "#f59e0b"
            $tray.Text = "DiskHealth Agent | UPDATING..."
            $tray.ShowBalloonTip(7000, "DiskHealth - Updating", $msg, [System.Windows.Forms.ToolTipIcon]::Info)
            # Reset icon after 8 seconds (new agent will start and update it properly)
            $script:_UpdateIconResetAt = (Get-Date).AddSeconds(8)
        }
        elseif ($msg -match "success|completed|Restarting") {
            $tray.ShowBalloonTip(6000, "DiskHealth - Updated", $msg, [System.Windows.Forms.ToolTipIcon]::Info)
        }
        elseif ($msg -match "FAILED|failed|error") {
            $tray.ShowBalloonTip(8000, "DiskHealth - Update Failed", $msg, [System.Windows.Forms.ToolTipIcon]::Error)
        }
        else {
            $tray.ShowBalloonTip(5000, "DiskHealth - Update", $msg, [System.Windows.Forms.ToolTipIcon]::Info)
        }
    } catch {}
}

# ── Startup ───────────────────────────────────────────────────────────────────
# Grace period: stay green for 45 s after startup so we don't flash
# "NOT RUNNING" while the agent/log is still initialising after install.
$script:_StartTime          = Get-Date
$script:_GracePeriod        = 45
$script:_LastStatus         = "ok"
$script:_UpdateIconResetAt  = [DateTime]::MinValue

$st = Get-AgentStatus
# Never start orange — even if log doesn't exist yet
if ($st.status -eq "starting" -or $st.status -eq "unknown") {
    $st = @{color="#22c55e";tip="DiskHealth Agent`nStarting up...";status="ok"}
}

$tray         = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon    = Make-Icon -Color $st.color
$tray.Text    = (($st.tip -split "`n")[0..1] -join " | ").Substring(0,[Math]::Min(63,(($st.tip -split "`n")[0..1] -join " | ").Length))
$tray.Visible = $true

# ── Left-click → open web panel ──────────────────────────────────────────────
$tray.Add_Click({
    if ([System.Windows.Forms.Control]::MouseButtons -eq [System.Windows.Forms.MouseButtons]::Left) {
        $url = Get-PanelUrl    # was incorrectly Get-ServerUrl in old versions
        if ($url) {
            Start-Process $url -ErrorAction SilentlyContinue
        } else {
            [System.Windows.Forms.MessageBox]::Show(
                "Server URL not found.`nThe agent may not have connected yet.",
                "DiskHealth Agent",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Information
            ) | Out-Null
        }
    }
})

# ── Context menu ──────────────────────────────────────────────────────────────
$menu    = New-Object System.Windows.Forms.ContextMenuStrip
$miTitle = $menu.Items.Add("DiskHealth Agent")
$miTitle.Enabled = $false
$miTitle.Font    = New-Object System.Drawing.Font("Segoe UI", 8, [System.Drawing.FontStyle]::Bold)
$menu.Items.Add("-") | Out-Null

$miPanel = $menu.Items.Add("Open Web Panel")
$miPanel.Add_Click({
    $url = Get-PanelUrl
    if ($url) {
        Start-Process $url -ErrorAction SilentlyContinue
    } else {
        [System.Windows.Forms.MessageBox]::Show(
            "Server URL not found.`nThe agent may not have connected yet.",
            "DiskHealth Agent",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
    }
})

$miLog = $menu.Items.Add("View Agent Log")
$miLog.Add_Click({
    try {
        $tmp = Join-Path $env:TEMP "DiskHealth_log_view.txt"
        $fs  = [System.IO.File]::Open($LogFile,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::ReadWrite)
        $rd  = New-Object System.IO.StreamReader($fs)
        $c   = $rd.ReadToEnd(); $rd.Close(); $fs.Close()
        [System.IO.File]::WriteAllText($tmp, $c)
        Start-Process notepad.exe -ArgumentList $tmp -ErrorAction SilentlyContinue
    } catch { Start-Process notepad.exe -ArgumentList $LogFile -ErrorAction SilentlyContinue }
})

$miRestart = $menu.Items.Add("Restart Agent")
$miRestart.Add_Click({
    try {
        Stop-ScheduledTask  -TaskName $AgentTask -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $AgentTask -ErrorAction SilentlyContinue
        $tray.ShowBalloonTip(3000,"DiskHealth Agent","Agent restarted.",[System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        $tray.ShowBalloonTip(3000,"DiskHealth Agent","Restart failed.",[System.Windows.Forms.ToolTipIcon]::Error)
    }
})

$menu.Items.Add("-") | Out-Null
$miExit = $menu.Items.Add("Exit Tray Icon")
$miExit.Add_Click({
    $tray.Visible = $false
    $tray.Dispose()
    [System.Windows.Forms.Application]::Exit()
})
$tray.ContextMenuStrip = $menu

# ── Timer ─────────────────────────────────────────────────────────────────────
$timer          = New-Object System.Windows.Forms.Timer
$timer.Interval = 8000   # check every 8 seconds for fast update notification
$timer.Add_Tick({
    # 1. Check for update notifications written by agent
    Check-UpdateNotify

    # 2. Reset update icon after delay if needed
    if ($script:_UpdateIconResetAt -gt [DateTime]::MinValue -and (Get-Date) -ge $script:_UpdateIconResetAt) {
        $script:_UpdateIconResetAt = [DateTime]::MinValue
        # Don't override — let normal status check below update the icon
    }

    # 3. Within grace period, always show green (agent may still be starting)
    $inGrace = ((Get-Date) - $script:_StartTime).TotalSeconds -lt $script:_GracePeriod
    if ($inGrace) { return }

    # 4. Check if agent process/task is alive
    if (-not (Is-AgentRunning)) {
        # Only alert once per transition to "stopped"
        if ($script:_LastStatus -ne "stopped") {
            $tray.Icon = Make-Icon -Color "#ef4444"
            $tray.Text = "DiskHealth Agent | NOT RUNNING"
            $tray.ShowBalloonTip(6000,"DiskHealth Agent","Agent is not running! Right-click and choose Restart Agent.",[System.Windows.Forms.ToolTipIcon]::Warning)
        }
        $script:_LastStatus = "stopped"
        return
    }

    # 5. Agent is running — read status from log
    $s = Get-AgentStatus
    # Ignore "starting" to avoid flickering during log rotation
    if ($s.status -eq "starting") { return }

    # 6. Balloon only on meaningful status transitions
    if ($s.status -ne $script:_LastStatus -and $script:_LastStatus -ne "ok" -or
        ($s.status -eq "error"   -and $script:_LastStatus -ne "error") -or
        ($s.status -eq "warning" -and $script:_LastStatus -ne "warning")) {
        if ($s.status -eq "error") {
            $tray.ShowBalloonTip(8000,"DiskHealth Agent - ALERT",$s.tip,[System.Windows.Forms.ToolTipIcon]::Error)
        } elseif ($s.status -eq "warning") {
            $tray.ShowBalloonTip(6000,"DiskHealth Agent",$s.tip,[System.Windows.Forms.ToolTipIcon]::Warning)
        } elseif ($s.status -eq "ok" -and $script:_LastStatus -eq "stopped") {
            $tray.ShowBalloonTip(4000,"DiskHealth Agent","Agent is back online.",[System.Windows.Forms.ToolTipIcon]::Info)
        }
    }

    $script:_LastStatus = $s.status
    $tray.Icon = Make-Icon -Color $s.color
    # Fix: compute tooltip once, not twice (old bug caused double function call)
    $tipText = (($s.tip -split "`n")[0..1] -join " | ")
    $tray.Text = $tipText.Substring(0,[Math]::Min(63,$tipText.Length))
})
$timer.Start()

$tray.ShowBalloonTip(3000,"DiskHealth Agent","Monitoring disk health. Click icon to open web panel.",[System.Windows.Forms.ToolTipIcon]::Info)
[System.Windows.Forms.Application]::Run()

# ── Cleanup ───────────────────────────────────────────────────────────────────
$timer.Dispose()
$tray.Dispose()
if ($owned) { $mutex.ReleaseMutex() }
$mutex.Dispose()