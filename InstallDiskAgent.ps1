# ==============================================================================
#  DiskHealth Agent v2.1 - Windows Agent
#  Compatible: Windows 7 SP1+ (PS 2.0+), Windows 10/11
# ==============================================================================
param(
    [Parameter(Mandatory=$true)]
    [string]$ServerUrl,
    [int]$PollInterval    = 30,
    [string]$AgentVersion = "2.1.0",
    [string]$Title        = "DiskHealth Agent"
)
Set-StrictMode -Off
$ErrorActionPreference = "SilentlyContinue"

$_scriptPath = $MyInvocation.MyCommand.Path
if (-not $_scriptPath) { $_scriptPath = $PSCommandPath }
if (-not $_scriptPath) {
    foreach ($_candidate in @(
        "$env:ProgramFiles\DiskHealthAgent",
        "C:\Program Files\DiskHealthAgent",
        "C:\DiskHealthAgent"
    )) {
        if (Test-Path (Join-Path $_candidate "DiskHealthAgent.ps1")) {
            $_scriptPath = Join-Path $_candidate "DiskHealthAgent.ps1"
            break
        }
    }
}
$AgentDir = if ($_scriptPath) { Split-Path -Parent $_scriptPath } else { "$env:ProgramFiles\DiskHealthAgent" }
if (-not (Test-Path $AgentDir)) { New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null }

$IdFile   = Join-Path $AgentDir "agent_id.txt"
$LogFile  = Join-Path $AgentDir "agent.log"
function Write-Log {
    param([string]$Level, [string]$Message)
    $ts   = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}
function Log-Info  { param([string]$m); Write-Log "INFO " $m }
function Log-Warn  { param([string]$m); Write-Log "WARN " $m }
function Log-Error { param([string]$m); Write-Log "ERROR" $m }
function Get-AgentId {
    if (Test-Path $IdFile) {
        $id = (Get-Content $IdFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($id -match '^[0-9a-f\-]{36}$') { return $id }
    }
    $hostname = $env:COMPUTERNAME.ToLower().Trim()
    $machineGuid = ""
    foreach ($rp in @("HKLM:\SOFTWARE\Microsoft\Cryptography","HKLM:\SOFTWARE\Wow6432Node\Microsoft\Cryptography")) {
        try {
            $g = (Get-ItemProperty -Path $rp -Name MachineGuid -ErrorAction Stop).MachineGuid
            if ($g -and $g.Length -gt 10) { $machineGuid = $g.ToLower().Trim(); break }
        } catch {}
    }
    $id = $null
    if ($machineGuid) {
        try {
            $raw  = [System.Text.Encoding]::UTF8.GetBytes("$hostname|$machineGuid")
            $sha1 = [System.Security.Cryptography.SHA1]::Create()
            $hash = $sha1.ComputeHash($raw); $sha1.Dispose()
            $hash[6] = ($hash[6] -band 0x0F) -bor 0x50
            $hash[8] = ($hash[8] -band 0x3F) -bor 0x80
            $hex = [BitConverter]::ToString($hash[0..15]) -replace '-',''
            $id  = ("{0}-{1}-{2}-{3}-{4}" -f $hex.Substring(0,8),$hex.Substring(8,4),$hex.Substring(12,4),$hex.Substring(16,4),$hex.Substring(20,12)).ToLower()
        } catch {}
    }
    if (-not $id) {
        $bytes = New-Object byte[] 16
        $rng   = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        $rng.GetBytes($bytes)
        $bytes[6] = ($bytes[6] -band 0x0F) -bor 0x40
        $bytes[8] = ($bytes[8] -band 0x3F) -bor 0x80
        $hex = [BitConverter]::ToString($bytes) -replace '-',''
        $id  = ("{0}-{1}-{2}-{3}-{4}" -f $hex.Substring(0,8),$hex.Substring(8,4),$hex.Substring(12,4),$hex.Substring(16,4),$hex.Substring(20,12)).ToLower()
    }
    try { Set-Content -Path $IdFile -Value $id -Encoding ASCII } catch {}
    Log-Info "Generated new agent_id: $id (hostname=$hostname)"
    return $id
}
function Invoke-JsonPost {
    param([string]$Url, [string]$JsonBody)
    try {
        $req = [System.Net.WebRequest]::Create($Url)
        $req.Method="POST"; $req.ContentType="application/json"; $req.Timeout=15000
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
        $req.ContentLength=$bytes.Length
        $stream = $req.GetRequestStream(); $stream.Write($bytes,0,$bytes.Length); $stream.Close()
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd(); $reader.Close(); $resp.Close(); return $body
    } catch { Log-Error "POST $Url failed: $_"; return $null }
}
function Invoke-JsonGet {
    param([string]$Url)
    try {
        $req = [System.Net.WebRequest]::Create($Url)
        $req.Method="GET"; $req.Timeout=15000
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd(); $reader.Close(); $resp.Close(); return $body
    } catch { Log-Error "GET $Url failed: $_"; return $null }
}
function ConvertTo-SafeJson {
    param($obj)
    if ($null -eq $obj) { return "null" }
    if ($obj -is [bool]) { if ($obj) { return "true" } else { return "false" } }
    if ($obj -is [int] -or $obj -is [long] -or $obj -is [double] -or $obj -is [float] -or $obj -is [decimal]) {
        $d=[double]$obj; if([double]::IsNaN($d)-or[double]::IsInfinity($d)){return "null"}; return "$obj"
    }
    if ($obj -is [string]) {
        $s=$obj
        $s=$s-replace'\\','\\'; $s=$s-replace'"','\"'
        $s=$s-replace"`r",'\r'; $s=$s-replace"`n",'\n'; $s=$s-replace"`t",'\t'
        return "`"$s`""
    }
    if ($obj -is [hashtable] -or $obj -is [System.Collections.Specialized.OrderedDictionary]) {
        $pairs=@()
        foreach ($k in $obj.Keys) {
            $ks=([string]$k)-replace'\\','\\'-replace'"','\"'
            $vs=ConvertTo-SafeJson $obj[$k]; $pairs+="`"$ks`":$vs"
        }
        return "{"+($pairs-join",")+  "}"
    }
    if ($obj -is [System.Collections.IEnumerable]) {
        $items=@(); foreach ($item in $obj) { $items+=ConvertTo-SafeJson $item }
        return "["+($items-join",")+"]"
    }
    $s=([string]$obj)-replace'\\','\\'-replace'"','\"'; return "`"$s`""
}
function Get-LocalIP {
    try {
        $addrs=[System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())
        foreach ($a in $addrs) {
            if ($a.AddressFamily-eq[System.Net.Sockets.AddressFamily]::InterNetwork -and $a.ToString()-ne"127.0.0.1") {
                return $a.ToString()
            }
        }
    } catch {}
    return "127.0.0.1"
}
function Update-Self {
    $scriptUrl = "$ServerUrl/agent/agent.ps1"
    $dest = Join-Path $AgentDir "DiskHealthAgent.ps1"
    $tmp  = "$dest.new"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $scriptUrl -OutFile $tmp -ErrorAction Stop
        Move-Item -Force $tmp $dest
        Log-Info "Agent updated from $scriptUrl"
        # Also update tray script if available
        try {
            $trayUrl  = "$ServerUrl/agent/tray.ps1"
            $trayDest = Join-Path $AgentDir "DiskHealthTray.ps1"
            $trayTmp  = "$trayDest.new"
            Invoke-WebRequest -UseBasicParsing -Uri $trayUrl -OutFile $trayTmp -ErrorAction Stop
            Move-Item -Force $trayTmp $trayDest
            Log-Info "Tray script updated."
            # Restart tray for current user
            Get-WmiObject Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*DiskHealthTray*" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
            Start-Process powershell.exe -ArgumentList "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$trayDest`"" -ErrorAction SilentlyContinue
        } catch { Log-Info "Tray update skipped: $_" }
        try {
            Start-Process powershell.exe -ArgumentList "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dest`" -ServerUrl `"$ServerUrl`" -PollInterval $PollInterval"
        } catch {}
        exit 0
        return $true
    } catch {
        Log-Error "Update failed: $_"
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        return $false
    }
}
function Find-Smartctl {
    $paths=@("$env:ProgramFiles\smartmontools\bin\smartctl.exe","C:\Program Files\smartmontools\bin\smartctl.exe","C:\Program Files (x86)\smartmontools\bin\smartctl.exe")
    foreach ($p in $paths) { if (Test-Path $p) { return $p } }
    try { $r=Get-Command smartctl.exe -ErrorAction Stop; return $r.Source } catch {}
    return $null
}
function Normalize-Serial { param([string]$s); return ($s -replace '[\s\-_]','').ToUpper().Trim() }
function Get-SmartctlData {
    param([string]$Bin)
    $bySerial=@{}
    try {
        $scanLines = & $Bin --scan 2>$null
        $devices=@()
        foreach ($line in $scanLines) {
            $line=$line.Trim(); if (-not $line) { continue }
            if ($line -match '^(/dev/\S+)\s+-d\s+(\S+)') { $devices+=@{path=$matches[1];dtype=$matches[2]} }
            elseif ($line -match '^(/dev/\S+)') { $devices+=@{path=$matches[1];dtype='auto'} }
        }
        foreach ($dev in $devices) {
            try {
                $argList=@('-a','-j')
                if ($dev.dtype -ne 'auto') { $argList+=@('-d',$dev.dtype) }
                $argList+=$dev.path
                $jsonRaw = & $Bin $argList 2>$null | Out-String
                if (-not $jsonRaw -or $jsonRaw.Trim().Length -lt 20) { continue }
                $d=$jsonRaw|ConvertFrom-Json
                if (($d.smartctl.exit_status -band 1) -and -not $d.device) { continue }
                $serial=if($d.serial_number){$d.serial_number.Trim()}else{''}
                if (-not $serial) { continue }
                $key=Normalize-Serial $serial
                $e=@{
                    serial=$serial; dtype=$dev.dtype; path=$dev.path
                    model=if($d.model_name){$d.model_name.Trim()}else{$null}
                    interface=if($d.device -and $d.device.protocol){$d.device.protocol}else{$dev.dtype.ToUpper()}
                    smart_passed=$true; predict_failure=$false
                    temperature=$null; reallocated=$null; pending=$null; uncorrectable=$null
                    power_on_hours=$null; power_cycles=$null
                    host_reads_gb=$null; host_writes_gb=$null
                    available_spare=$null; percentage_used=$null
                    unsafe_shutdowns=$null; media_errors=$null; critical_warning=$null
                    smartctl_used=$true
                }
                if ($d.smart_status -and $null -ne $d.smart_status.passed) {
                    $e.smart_passed=[bool]$d.smart_status.passed
                    $e.predict_failure=-not [bool]$d.smart_status.passed
                }
                if ($d.temperature -and $null -ne $d.temperature.current) { $e.temperature=[int]$d.temperature.current }
                $nv=$d.nvme_smart_health_information_log
                if ($nv) {
                    if ($null -ne $nv.power_on_hours)   { $e.power_on_hours=[long]$nv.power_on_hours }
                    if ($null -ne $nv.power_cycles)     { $e.power_cycles=[long]$nv.power_cycles }
                    if ($null -ne $nv.unsafe_shutdowns) { $e.unsafe_shutdowns=[long]$nv.unsafe_shutdowns }
                    if ($null -ne $nv.media_errors)     { $e.media_errors=[long]$nv.media_errors }
                    if ($null -ne $nv.available_spare)  { $e.available_spare=[int]$nv.available_spare }
                    if ($null -ne $nv.percentage_used)  { $e.percentage_used=[int]$nv.percentage_used }
                    if ($null -ne $nv.critical_warning) { $e.critical_warning=$nv.critical_warning }
                    if ($null -ne $nv.host_reads)  { $e.host_reads_gb=[math]::Round([double]$nv.host_reads*512000/1GB,2) }
                    if ($null -ne $nv.host_writes) { $e.host_writes_gb=[math]::Round([double]$nv.host_writes*512000/1GB,2) }
                    $cw=$e.critical_warning
                    if ($cw -and $cw -ne 0 -and $cw -ne '0x00') { $e.predict_failure=$true }
                    if ($e.available_spare -ne $null -and $e.available_spare -le 10) { $e.predict_failure=$true }
                }
                if ($d.ata_smart_attributes -and $d.ata_smart_attributes.table) {
                    foreach ($attr in $d.ata_smart_attributes.table) {
                        $aid=[int]$attr.id
                        $rv=if($attr.raw -and $null -ne $attr.raw.value){[long]$attr.raw.value}else{0}
                        switch($aid){
                            5   { $e.reallocated=[int]$rv }
                            9   { $e.power_on_hours=$rv }
                            12  { $e.power_cycles=$rv }
                            187 { $e.uncorrectable=[int]$rv }
                            190 { if($null -eq $e.temperature){$e.temperature=[int]($rv -band 0xFF)} }
                            194 { if($null -eq $e.temperature){$e.temperature=[int]($rv -band 0xFF)} }
                            197 { $e.pending=[int]$rv }
                            198 { if($null -eq $e.uncorrectable){$e.uncorrectable=[int]$rv} }
                            241 { $e.host_writes_gb=[math]::Round($rv*512/1GB,2) }
                            242 { $e.host_reads_gb=[math]::Round($rv*512/1GB,2) }
                        }
                    }
                    if ($e.reallocated -ne $null -and $e.reallocated -gt 0) { $e.predict_failure=$true }
                }
                if ($null -eq $e.power_on_hours -and $d.power_on_time -and $null -ne $d.power_on_time.hours) {
                    $e.power_on_hours=[long]$d.power_on_time.hours
                }
                $bySerial[$key]=$e
            } catch { Log-Warn "smartctl parse error $($dev.path): $_" }
        }
    } catch { Log-Warn "smartctl --scan failed: $_" }
    return $bySerial
}
function Get-WmiSmartData {
    $result=@{}
    try {
        $statuses=Get-WmiObject -Namespace "root\wmi" -Class MSStorageDriver_FailurePredictStatus -ErrorAction Stop
        $rawData =Get-WmiObject -Namespace "root\wmi" -Class MSStorageDriver_FailurePredictData  -ErrorAction Stop
        $rawLookup=@{}; foreach ($r in $rawData) { $rawLookup[$r.InstanceName]=$r.VendorSpecific }
        foreach ($s in $statuses) {
            $inst=$s.InstanceName; $idx=0
            if ($inst -match '(\d+)$'){$idx=[int]$matches[1]}
            $e=@{predict_failure=[bool]$s.PredictFailure;temperature=$null;reallocated=$null;pending=$null;uncorrectable=$null;smartctl_used=$false}
            if ($rawLookup.ContainsKey($inst)) {
                $raw=$rawLookup[$inst]
                for ($i=2;$i-lt($raw.Count-12);$i+=12) {
                    $id=$raw[$i]; if($id-eq0){continue}
                    $rv=[long]$raw[$i+5]+([long]$raw[$i+6]-shl 8)+([long]$raw[$i+7]-shl 16)+([long]$raw[$i+8]-shl 24)
                    switch($id){
                        0xC2{$e.temperature=[int]($rv-band 0xFF)}
                        0x05{$e.reallocated=[int]$rv}
                        0xC5{$e.pending=[int]$rv}
                        0xC6{$e.uncorrectable=[int]$rv}
                    }
                }
            }
            $result[$idx]=$e
        }
    } catch { $result['_error'] = $_.ToString() }
    return $result
}
function Get-DiskHealth {
    $disks=@()
    $scBin=Find-Smartctl
    $scMap=@{}
    if ($scBin) { try { $scMap=Get-SmartctlData -Bin $scBin } catch { Log-Warn "smartctl failed: $_" } }
    $wmiMap=Get-WmiSmartData
    # Only warn about WMI if smartctl also got nothing — WMI fails on VMs/NVMe/no-admin which is normal when smartctl covers it
    if ($wmiMap.ContainsKey('_error')) {
        $wmiErr = $wmiMap['_error']; $wmiMap.Remove('_error')
        if ($scMap.Count -eq 0) { Log-Warn "No SMART data available (WMI: $wmiErr). Run agent as Administrator for full data." }
        else { Log-Info "WMI SMART skipped (smartctl active)." }
    }
    # Build index-ordered list from smartctl for positional fallback
    $scByIndex=@{}
    if ($scMap.Count -gt 0) {
        $scBinT=Find-Smartctl
        if ($scBinT) {
            $scanLines2 = & $scBinT --scan 2>$null
            $devIdx=0
            foreach ($line in $scanLines2) {
                $line=$line.Trim(); if (-not $line) { continue }
                if ($line -match '^(/dev/\S+)') {
                    # find which scMap entry came from this path
                    foreach ($kv in $scMap.GetEnumerator()) {
                        if ($kv.Value.path -eq $matches[1]) { $scByIndex[$devIdx]=$kv.Value; break }
                    }
                    $devIdx++
                }
            }
        }
    }
    foreach ($pd in (Get-WmiObject Win32_DiskDrive|Sort-Object Index)) {
        $idx=[int]$pd.Index
        $wmiSerial=if($pd.SerialNumber){$pd.SerialNumber.Trim()}else{''}
        $wmiModel =if($pd.Model){$pd.Model.Trim()}else{'Unknown'}
        $wmiIface =if($pd.InterfaceType){$pd.InterfaceType.Trim()}else{'Unknown'}
        $s=$null
        # 1st: match by serial
        if ($wmiSerial) {
            $normSerial=Normalize-Serial $wmiSerial
            if ($scMap.ContainsKey($normSerial)) { $s=$scMap[$normSerial] }
        }
        # 2nd: serial mismatch is common for NVMe — fall back to positional index match
        if (-not $s -and $scByIndex.ContainsKey($idx)) { $s=$scByIndex[$idx] }
        $smartStat='Unknown'
        $temp=$null;$realloc=$null;$pending=$null;$uncorr=$null
        $powerOnHrs=$null;$powerCyc=$null;$readsGB=$null;$writesGB=$null
        $availSpare=$null;$pctUsed=$null;$unsafeSD=$null;$mediaErr=$null
        $critWarn=$null;$scUsed=$false
        $model=$wmiModel;$serial=$wmiSerial;$iface=$wmiIface
        if ($s) {
            $scUsed=$true
            if ($s.model)     { $model=$s.model }
            if ($s.serial)    { $serial=$s.serial }
            if ($s.interface) { $iface=$s.interface }
            $temp=$s.temperature;$realloc=$s.reallocated;$pending=$s.pending;$uncorr=$s.uncorrectable
            $powerOnHrs=$s.power_on_hours;$powerCyc=$s.power_cycles
            $readsGB=$s.host_reads_gb;$writesGB=$s.host_writes_gb
            $availSpare=$s.available_spare;$pctUsed=$s.percentage_used
            $unsafeSD=$s.unsafe_shutdowns;$mediaErr=$s.media_errors;$critWarn=$s.critical_warning
            if ($s.predict_failure) { $smartStat='Critical' }
            else {
                $smartStat='Healthy'
                if ($realloc  -ne $null -and $realloc  -gt 0)  { $smartStat='Warning'  }
                if ($mediaErr -ne $null -and $mediaErr -gt 0)  { $smartStat='Warning'  }
                if ($pctUsed  -ne $null -and $pctUsed  -ge 90) { $smartStat='Warning'  }
                if ($availSpare -ne $null -and $availSpare -le 10) { $smartStat='Critical' }
            }
        } elseif ($wmiMap.ContainsKey($idx)) {
            $w=$wmiMap[$idx]
            $temp=$w.temperature;$realloc=$w.reallocated;$pending=$w.pending;$uncorr=$w.uncorrectable
            if ($w.predict_failure) { $smartStat='Critical' }
            else {
                $smartStat='Healthy'
                if ($realloc -ne $null -and $realloc -gt 0) { $smartStat='Warning' }
            }
        } else {
            $smartStat=switch($pd.Status){'OK'{'Healthy'}'Degraded'{'Warning'}'Error'{'Critical'}default{'Unknown'}}
        }
        $volumes=@()
        try {
            $devId=$pd.DeviceID-replace'\\','\\\\'
            $parts=Get-WmiObject -Query "ASSOCIATORS OF {Win32_DiskDrive.DeviceID='$devId'} WHERE AssocClass=Win32_DiskDriveToDiskPartition"
            foreach ($part in $parts) {
                $logs=Get-WmiObject -Query "ASSOCIATORS OF {Win32_DiskPartition.DeviceID='$($part.DeviceID)'} WHERE AssocClass=Win32_LogicalDiskToPartition"
                foreach ($ld in $logs) {
                    $tgb=$null;$fgb=$null;$pct=0
                    if ($ld.Size){$tgb=[math]::Round([long]$ld.Size/1GB,2)}
                    if ($ld.FreeSpace){$fgb=[math]::Round([long]$ld.FreeSpace/1GB,2)}
                    if ($tgb-and$tgb-gt0-and$fgb-ne$null){$pct=[math]::Round((($tgb-$fgb)/$tgb)*100,1)}
                    $volumes+=@{
                        drive=$ld.DeviceID
                        label=if($ld.VolumeName){$ld.VolumeName}else{''}
                        filesystem=if($ld.FileSystem){$ld.FileSystem}else{'Unknown'}
                        total_gb=$tgb;free_gb=$fgb;used_pct=$pct
                    }
                }
            }
        } catch {}
        $disks+=@{
            index=$idx; model=$model; serial=$serial; interface=$iface
            size_gb=if($pd.Size){[math]::Round([long]$pd.Size/1GB,1)}else{$null}
            smart_status=$smartStat; temperature=$temp
            reallocated=$realloc; pending=$pending; uncorrectable=$uncorr
            power_on_hours=$powerOnHrs; power_cycles=$powerCyc
            host_reads_gb=$readsGB; host_writes_gb=$writesGB
            available_spare=$availSpare; percentage_used=$pctUsed
            unsafe_shutdowns=$unsafeSD; media_errors=$mediaErr; critical_warning=$critWarn
            smartctl_used=$scUsed; volumes=$volumes
        }
    }
    return $disks
}
function Get-LoggedInUsers {
    $users=@()
    try {
        foreach ($s in (Get-WmiObject Win32_LoggedOnUser -ErrorAction Stop)) {
            if ($s.Antecedent-match'Domain="([^"]+)",Name="([^"]+)"') {
                $d=$matches[1]; $n=$matches[2]
                if ($n-notmatch'^(SYSTEM|LOCAL SERVICE|NETWORK SERVICE|DWM-\d+|UMFD-\d+)$') {
                    $e="$d\$n"; if ($users-notcontains$e){$users+=$e}
                }
            }
        }
    } catch {}
    if ($users.Count-eq0-and$env:USERNAME){$users+="$env:USERDOMAIN\$env:USERNAME"}
    return ($users-join", ")
}
function Build-Report { param([string]$AgentId,[string]$CmdId="")
    $os=Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue
    return @{
        agent_id=$AgentId; command_id=$CmdId
        hostname=$env:COMPUTERNAME; ip=Get-LocalIP
        os="Windows"; os_version=if($os){$os.Caption}else{""}
        agent_version=$AgentVersion; logged_users=Get-LoggedInUsers
        disks=@(Get-DiskHealth|ForEach-Object{$_}); Title=$Title
    }
}
function Register-Agent { param([string]$AgentId)
    $os=Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue
    $payload=@{
        agent_id=$AgentId; hostname=$env:COMPUTERNAME; ip=Get-LocalIP
        os="Windows"; os_version=if($os){$os.Caption}else{""}
        agent_version=$AgentVersion; logged_users=Get-LoggedInUsers; welcome_title=$Title
    }
    $resp=Invoke-JsonPost "$ServerUrl/api/register" (ConvertTo-SafeJson $payload)
    if ($resp){Log-Info "Registered OK."; return $true}
    Log-Warn "Registration failed."; return $false
}
function Send-Report { param([string]$AgentId,[string]$CmdId="")
    Log-Info "Collecting disk health data..."
    $report=Build-Report -AgentId $AgentId -CmdId $CmdId
    $json=ConvertTo-SafeJson $report
    if ($json.Length-lt10){Log-Error "JSON too short"; return}
    Log-Info "Sending report ($($json.Length) bytes)..."
    $resp=Invoke-JsonPost "$ServerUrl/api/report" $json
    if ($resp){Log-Info "Report accepted."}else{Log-Warn "Send failed."}
}
function Poll-Commands { param([string]$AgentId)
    $resp=Invoke-JsonGet "$ServerUrl/api/commands/$AgentId"
    if(-not$resp){return $null}
    $obj = $null
    try{$obj=$resp|ConvertFrom-Json}catch{return $null}
    $commands=$obj.commands

    # Read poll_interval from server response
    $serverInterval = $null
    try { $si=[int]$obj.poll_interval; if($si -ge 10){$serverInterval=$si} } catch {}

    if(-not$commands-or$commands.Count-eq0){return $serverInterval}
    foreach ($cmd in $commands) {
        $cmdId=$cmd.command_id; $action=$cmd.action
        switch($action){
            "get_disk_health" { Send-Report -AgentId $AgentId -CmdId $cmdId }
            "ping" {
                Invoke-JsonPost "$ServerUrl/api/ack" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=@{pong=$true; timestamp=(Get-Date).ToString("o")}
                })|Out-Null
                Log-Info "Ping ack'd."
            }
            "update_agent" {
                $updated = Update-Self
                $status  = if($updated){ @{updated=$true} } else { @{updated=$false} }
                Invoke-JsonPost "$ServerUrl/api/ack" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=$status
                })|Out-Null
            }
            default {
                Invoke-JsonPost "$ServerUrl/api/ack" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=@{error="unknown action $action"}
                })|Out-Null
            }
        }
    }
    return $serverInterval
}
function Main {
    $agentId=Get-AgentId; $pollCount=0; $registered=$false
    Log-Info "DiskHealth Agent v$AgentVersion starting. ID=$agentId  Server=$ServerUrl"
    # Write server URL file so the tray icon can open the web panel
    try { Set-Content -Path (Join-Path $AgentDir "server_url.txt") -Value $ServerUrl -Encoding UTF8 } catch {}
    while(-not$registered){
        $registered=Register-Agent -AgentId $agentId
        if(-not$registered){Log-Warn "Retrying in 15s..."; Start-Sleep -Seconds 15}
    }
    Send-Report -AgentId $agentId
    $CMD_INTERVAL    = 5
    $REPORT_INTERVAL = $PollInterval   # default from install param, overridden by server
    $lastReport   = (Get-Date)
    $lastRegister = (Get-Date)
    while($true){
        Start-Sleep -Seconds $CMD_INTERVAL
        try{
            $newInterval = Poll-Commands -AgentId $agentId
            if ($newInterval -ne $null -and $newInterval -ne $REPORT_INTERVAL) {
                Log-Info "Poll interval updated by server: $($REPORT_INTERVAL)s -> ${newInterval}s"
                $REPORT_INTERVAL = $newInterval
            }
            if (((Get-Date)-$lastRegister).TotalSeconds -ge 60){
                Register-Agent -AgentId $agentId|Out-Null
                $lastRegister=Get-Date
            }
            if (((Get-Date)-$lastReport).TotalSeconds -ge $REPORT_INTERVAL){
                Send-Report -AgentId $agentId
                $lastReport=Get-Date
            }
        }
        catch{Log-Error "Poll error: $_"}
    }
}
Main