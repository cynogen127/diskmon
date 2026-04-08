using System.Diagnostics;
using System.IO;

namespace SnipeAgent
{
    // Both PowerShell scripts are embedded as C# string constants.
    // At runtime they are extracted to a temp folder and executed — no external files needed.
    static class AgentScripts
    {
        public const string Agent = @"# ==============================================================================
#  DiskHealth Agent v2.1 - Windows Agent
#  Compatible: Windows 7 SP1+ (PS 2.0+), Windows 10/11
# ==============================================================================
param(
    [Parameter(Mandatory=$true)]
    [string]$ServerUrl,
    [int]$PollInterval    = 30,
    [string]$AgentVersion = ""2.1.0"",
    [string]$Title        = ""DiskHealth Agent""
)
Set-StrictMode -Off
$ErrorActionPreference = ""SilentlyContinue""
$AgentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$IdFile   = Join-Path $AgentDir ""agent_id.txt""
$LogFile  = Join-Path $AgentDir ""agent.log""
function Write-Log {
    param([string]$Level, [string]$Message)
    $ts   = (Get-Date).ToString(""yyyy-MM-dd HH:mm:ss"")
    $line = ""[$ts] [$Level] $Message""
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}
function Log-Info  { param([string]$m); Write-Log ""INFO "" $m }
function Log-Warn  { param([string]$m); Write-Log ""WARN "" $m }
function Log-Error { param([string]$m); Write-Log ""ERROR"" $m }
function Get-AgentId {
    if (Test-Path $IdFile) {
        $id = (Get-Content $IdFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($id -match '^[0-9a-f\-]{36}$') { return $id }
    }
    $hostname = $env:COMPUTERNAME.ToLower().Trim()
    $machineGuid = """"
    foreach ($rp in @(""HKLM:\SOFTWARE\Microsoft\Cryptography"",""HKLM:\SOFTWARE\Wow6432Node\Microsoft\Cryptography"")) {
        try {
            $g = (Get-ItemProperty -Path $rp -Name MachineGuid -ErrorAction Stop).MachineGuid
            if ($g -and $g.Length -gt 10) { $machineGuid = $g.ToLower().Trim(); break }
        } catch {}
    }
    $id = $null
    if ($machineGuid) {
        try {
            $raw  = [System.Text.Encoding]::UTF8.GetBytes(""$hostname|$machineGuid"")
            $sha1 = [System.Security.Cryptography.SHA1]::Create()
            $hash = $sha1.ComputeHash($raw); $sha1.Dispose()
            $hash[6] = ($hash[6] -band 0x0F) -bor 0x50
            $hash[8] = ($hash[8] -band 0x3F) -bor 0x80
            $hex = [BitConverter]::ToString($hash[0..15]) -replace '-',''
            $id  = (""{0}-{1}-{2}-{3}-{4}"" -f $hex.Substring(0,8),$hex.Substring(8,4),$hex.Substring(12,4),$hex.Substring(16,4),$hex.Substring(20,12)).ToLower()
        } catch {}
    }
    if (-not $id) {
        $bytes = New-Object byte[] 16
        $rng   = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        $rng.GetBytes($bytes)
        $bytes[6] = ($bytes[6] -band 0x0F) -bor 0x40
        $bytes[8] = ($bytes[8] -band 0x3F) -bor 0x80
        $hex = [BitConverter]::ToString($bytes) -replace '-',''
        $id  = (""{0}-{1}-{2}-{3}-{4}"" -f $hex.Substring(0,8),$hex.Substring(8,4),$hex.Substring(12,4),$hex.Substring(16,4),$hex.Substring(20,12)).ToLower()
    }
    try { Set-Content -Path $IdFile -Value $id -Encoding ASCII } catch {}
    Log-Info ""Generated new agent_id: $id (hostname=$hostname)""
    return $id
}
function Invoke-JsonPost {
    param([string]$Url, [string]$JsonBody)
    try {
        $req = [System.Net.WebRequest]::Create($Url)
        $req.Method=""POST""; $req.ContentType=""application/json""; $req.Timeout=15000
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
        $req.ContentLength=$bytes.Length
        $stream = $req.GetRequestStream(); $stream.Write($bytes,0,$bytes.Length); $stream.Close()
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd(); $reader.Close(); $resp.Close(); return $body
    } catch { Log-Error ""POST $Url failed: $_""; return $null }
}
function Invoke-JsonGet {
    param([string]$Url)
    try {
        $req = [System.Net.WebRequest]::Create($Url)
        $req.Method=""GET""; $req.Timeout=15000
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd(); $reader.Close(); $resp.Close(); return $body
    } catch { Log-Error ""GET $Url failed: $_""; return $null }
}
function ConvertTo-SafeJson {
    param($obj)
    if ($null -eq $obj) { return ""null"" }
    if ($obj -is [bool]) { if ($obj) { return ""true"" } else { return ""false"" } }
    if ($obj -is [int] -or $obj -is [long] -or $obj -is [double] -or $obj -is [float] -or $obj -is [decimal]) {
        $d=[double]$obj; if([double]::IsNaN($d)-or[double]::IsInfinity($d)){return ""null""}; return ""$obj""
    }
    if ($obj -is [string]) {
        $s=$obj
        $s=$s-replace'\\','\\'; $s=$s-replace'""','\""'
        $s=$s-replace""`r"",'\r'; $s=$s-replace""`n"",'\n'; $s=$s-replace""`t"",'\t'
        return ""`""$s`""""
    }
    if ($obj -is [hashtable] -or $obj -is [System.Collections.Specialized.OrderedDictionary]) {
        $pairs=@()
        foreach ($k in $obj.Keys) {
            $ks=([string]$k)-replace'\\','\\'-replace'""','\""'
            $vs=ConvertTo-SafeJson $obj[$k]; $pairs+=""`""$ks`"":$vs""
        }
        return ""{""+($pairs-join"","")+  ""}""
    }
    if ($obj -is [System.Collections.IEnumerable]) {
        $items=@(); foreach ($item in $obj) { $items+=ConvertTo-SafeJson $item }
        return ""[""+($items-join"","")+""]""
    }
    $s=([string]$obj)-replace'\\','\\'-replace'""','\""'; return ""`""$s`""""
}
function Get-LocalIP {
    try {
        $addrs=[System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())
        foreach ($a in $addrs) {
            if ($a.AddressFamily-eq[System.Net.Sockets.AddressFamily]::InterNetwork -and $a.ToString()-ne""127.0.0.1"") {
                return $a.ToString()
            }
        }
    } catch {}
    return ""127.0.0.1""
}
function Update-Self {
    $scriptUrl = ""$ServerUrl/agent/agent.ps1""
    $dest = Join-Path $AgentDir ""DiskHealthAgent.ps1""
    $tmp  = ""$dest.new""
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $scriptUrl -OutFile $tmp -ErrorAction Stop
        Move-Item -Force $tmp $dest
        Log-Info ""Agent updated from $scriptUrl""
        # Also update tray script if available
        try {
            $trayUrl  = ""$ServerUrl/agent/tray.ps1""
            $trayDest = Join-Path $AgentDir ""DiskHealthTray.ps1""
            $trayTmp  = ""$trayDest.new""
            Invoke-WebRequest -UseBasicParsing -Uri $trayUrl -OutFile $trayTmp -ErrorAction Stop
            Move-Item -Force $trayTmp $trayDest
            Log-Info ""Tray script updated.""
            # Register tray logon task if not already present
            $trayTask = Get-ScheduledTask -TaskName ""DiskHealthTray"" -ErrorAction SilentlyContinue
            if (-not $trayTask) {
                try {
                    $ta = New-ScheduledTaskAction -Execute ""powershell.exe"" -Argument ""-NonInteractive -ExecutionPolicy Bypass -File `""$trayDest`""""
                    $tr = New-ScheduledTaskTrigger -AtLogOn
                    Register-ScheduledTask -TaskName ""DiskHealthTray"" -Action $ta -Trigger $tr -RunLevel Limited -Force | Out-Null
                    Log-Info ""Tray logon task registered.""
                } catch { Log-Info ""Tray task registration skipped: $_"" }
            }
            # Restart tray for current user
            Get-WmiObject Win32_Process -Filter ""Name='powershell.exe'"" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like ""*DiskHealthTray*"" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
            Start-Process powershell.exe -ArgumentList ""-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `""$trayDest`"""" -ErrorAction SilentlyContinue
        } catch { Log-Info ""Tray update skipped: $_"" }
        try {
            Start-Process powershell.exe -ArgumentList ""-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `""$dest`"" -ServerUrl `""$ServerUrl`"" -PollInterval $PollInterval""
        } catch {}
        exit 0
        return $true
    } catch {
        Log-Error ""Update failed: $_""
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        return $false
    }
}
function Find-Smartctl {
    $paths=@(""$env:ProgramFiles\smartmontools\bin\smartctl.exe"",""C:\Program Files\smartmontools\bin\smartctl.exe"",""C:\Program Files (x86)\smartmontools\bin\smartctl.exe"")
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
            } catch { Log-Warn ""smartctl parse error $($dev.path): $_"" }
        }
    } catch { Log-Warn ""smartctl --scan failed: $_"" }
    return $bySerial
}
function Get-WmiSmartData {
    $result=@{}
    try {
        $statuses=Get-WmiObject -Namespace ""root\wmi"" -Class MSStorageDriver_FailurePredictStatus -ErrorAction Stop
        $rawData =Get-WmiObject -Namespace ""root\wmi"" -Class MSStorageDriver_FailurePredictData  -ErrorAction Stop
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
    if ($scBin) { try { $scMap=Get-SmartctlData -Bin $scBin } catch { Log-Warn ""smartctl failed: $_"" } }
    $wmiMap=Get-WmiSmartData
    # Only warn about WMI if smartctl also got nothing — WMI fails on VMs/NVMe/no-admin which is normal when smartctl covers it
    if ($wmiMap.ContainsKey('_error')) {
        $wmiErr = $wmiMap['_error']; $wmiMap.Remove('_error')
        if ($scMap.Count -eq 0) { Log-Warn ""No SMART data available (WMI: $wmiErr). Run agent as Administrator for full data."" }
        else { Log-Info ""WMI SMART skipped (smartctl active)."" }
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
            $parts=Get-WmiObject -Query ""ASSOCIATORS OF {Win32_DiskDrive.DeviceID='$devId'} WHERE AssocClass=Win32_DiskDriveToDiskPartition""
            foreach ($part in $parts) {
                $logs=Get-WmiObject -Query ""ASSOCIATORS OF {Win32_DiskPartition.DeviceID='$($part.DeviceID)'} WHERE AssocClass=Win32_LogicalDiskToPartition""
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
            if ($s.Antecedent-match'Domain=""([^""]+)"",Name=""([^""]+)""') {
                $d=$matches[1]; $n=$matches[2]
                if ($n-notmatch'^(SYSTEM|LOCAL SERVICE|NETWORK SERVICE|DWM-\d+|UMFD-\d+)$') {
                    $e=""$d\$n""; if ($users-notcontains$e){$users+=$e}
                }
            }
        }
    } catch {}
    if ($users.Count-eq0-and$env:USERNAME){$users+=""$env:USERDOMAIN\$env:USERNAME""}
    return ($users-join"", "")
}
function Build-Report { param([string]$AgentId,[string]$CmdId="""")
    $os=Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue
    return @{
        agent_id=$AgentId; command_id=$CmdId
        hostname=$env:COMPUTERNAME; ip=Get-LocalIP
        os=""Windows""; os_version=if($os){$os.Caption}else{""""}
        agent_version=$AgentVersion; logged_users=Get-LoggedInUsers
        disks=@(Get-DiskHealth|ForEach-Object{$_}); Title=$Title
    }
}
function Register-Agent { param([string]$AgentId)
    $os=Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue
    $payload=@{
        agent_id=$AgentId; hostname=$env:COMPUTERNAME; ip=Get-LocalIP
        os=""Windows""; os_version=if($os){$os.Caption}else{""""}
        agent_version=$AgentVersion; logged_users=Get-LoggedInUsers; welcome_title=$Title
    }
    $resp=Invoke-JsonPost ""$ServerUrl/api/register"" (ConvertTo-SafeJson $payload)
    if ($resp){Log-Info ""Registered OK.""; return $true}
    Log-Warn ""Registration failed.""; return $false
}
function Send-Report { param([string]$AgentId,[string]$CmdId="""")
    Log-Info ""Collecting disk health data...""
    $report=Build-Report -AgentId $AgentId -CmdId $CmdId
    $json=ConvertTo-SafeJson $report
    if ($json.Length-lt10){Log-Error ""JSON too short""; return}
    Log-Info ""Sending report ($($json.Length) bytes)...""
    $resp=Invoke-JsonPost ""$ServerUrl/api/report"" $json
    if ($resp){Log-Info ""Report accepted.""}else{Log-Warn ""Send failed.""}
}
function Poll-Commands { param([string]$AgentId)
    $resp=Invoke-JsonGet ""$ServerUrl/api/commands/$AgentId""
    if(-not$resp){return $null}
    $obj=$null
    try{$obj=$resp|ConvertFrom-Json}catch{return $null}
    $commands=$obj.commands
    # Read poll_interval from server response
    $serverInterval=$null
    try { $si=[int]$obj.poll_interval; if($si -ge 60){$serverInterval=$si} } catch {}
    if(-not$commands-or$commands.Count-eq0){return $serverInterval}
    foreach ($cmd in $commands) {
        $cmdId=$cmd.command_id; $action=$cmd.action
        switch($action){
            ""get_disk_health"" { Send-Report -AgentId $AgentId -CmdId $cmdId }
            ""ping"" {
                Invoke-JsonPost ""$ServerUrl/api/ack"" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=@{pong=$true; timestamp=(Get-Date).ToString(""o"")}
                })|Out-Null
                Log-Info ""Ping ack'd.""
            }
            ""update_agent"" {
                $updated = Update-Self
                $status  = if($updated){ @{updated=$true} } else { @{updated=$false} }
                Invoke-JsonPost ""$ServerUrl/api/ack"" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=$status
                })|Out-Null
            }
            ""clear_log"" {
                try {
                    if (Test-Path $LogFile) {
                        Clear-Content -Path $LogFile -Force -ErrorAction Stop
                        Log-Info ""Log cleared by remote command.""
                        $status = @{cleared=$true; message=""Log cleared successfully""}
                    } else {
                        $status = @{cleared=$false; message=""Log file not found""}
                    }
                } catch {
                    $status = @{cleared=$false; message=""Failed: $_""}
                }
                Invoke-JsonPost ""$ServerUrl/api/ack"" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=$status
                })|Out-Null
                Log-Info ""clear_log ack'd.""
            }
            default {
                Invoke-JsonPost ""$ServerUrl/api/ack"" (ConvertTo-SafeJson @{
                    command_id=$cmdId; result=@{error=""unknown action $action""}
                })|Out-Null
            }
        }
    }
    return $serverInterval
}
function Main {
    $agentId=Get-AgentId; $registered=$false
    Log-Info ""DiskHealth Agent v$AgentVersion starting. ID=$agentId  Server=$ServerUrl""
    # Write server URL so tray can build panel link immediately on startup
    try { Set-Content -Path (Join-Path $AgentDir ""server_url.txt"") -Value $ServerUrl -Encoding ASCII } catch {}
    while(-not$registered){
        $registered=Register-Agent -AgentId $agentId
        if(-not$registered){Log-Warn ""Retrying in 15s...""; Start-Sleep -Seconds 15}
    }
    Send-Report -AgentId $agentId
    $CMD_INTERVAL    = 5
    $REPORT_INTERVAL = [Math]::Max(60, $PollInterval)
    $lastReport   = Get-Date
    $lastRegister = Get-Date
    Log-Info ""Main loop started. CMD_INTERVAL=${CMD_INTERVAL}s  SCAN_INTERVAL=${REPORT_INTERVAL}s""
    while($true){
        Start-Sleep -Seconds $CMD_INTERVAL
        try{
            $newInterval = Poll-Commands -AgentId $agentId
            if ($newInterval -ne $null -and $newInterval -ge 60 -and $newInterval -ne $REPORT_INTERVAL) {
                Log-Info ""Poll interval updated by server: ${REPORT_INTERVAL}s -> ${newInterval}s""
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
        catch{Log-Error ""Poll error: $_""}
    }
}
Main
";

        public const string Tray = @"# DiskHealth Tray Icon
# Runs as logged-in user at logon - shows agent status in system tray
param([string]$InstallDir = ""$env:ProgramFiles\DiskHealthAgent"")

# ── Single-instance guard (prevents duplicate tray icons on reinstall) ──────
$mutexName = ""Global\DiskHealthTrayIcon""
$mutex     = New-Object System.Threading.Mutex($false, $mutexName)
$owned     = $false
try     { $owned = $mutex.WaitOne(0, $false) }
catch   [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { $mutex.Dispose(); exit 0 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$LogFile    = Join-Path $InstallDir ""agent.log""
$AgentTask  = ""DiskHealthAgent""

function Make-Icon {
    param([string]$Color = ""#22c55e"")
    # Draw at 32x32 so Windows taskbar renders it as large as other icons
    $bmp = New-Object System.Drawing.Bitmap(32,32)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.Clear([System.Drawing.Color]::Transparent)

    $col   = [System.Drawing.ColorTranslator]::FromHtml($Color)
    $light = [System.Drawing.Color]::FromArgb(255,
                [math]::Min(255,$col.R+80),
                [math]::Min(255,$col.G+80),
                [math]::Min(255,$col.B+80))
    $dark  = [System.Drawing.Color]::FromArgb(255,
                [math]::Max(0,$col.R-60),
                [math]::Max(0,$col.G-60),
                [math]::Max(0,$col.B-60))

    # Drive body
    $bodyBrush = New-Object System.Drawing.SolidBrush($col)
    $g.FillRectangle($bodyBrush, 2, 6, 28, 20)

    # Top highlight band
    $hlBrush = New-Object System.Drawing.SolidBrush($light)
    $g.FillRectangle($hlBrush, 2, 6, 28, 5)

    # Bottom shadow band
    $shBrush = New-Object System.Drawing.SolidBrush($dark)
    $g.FillRectangle($shBrush, 2, 21, 28, 5)

    # Outline
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(230,0,0,0), 1.5)
    $g.DrawRectangle($pen, 2, 6, 27, 19)

    # Drive label slot (dark bar in middle left — looks like a real HDD)
    $slotBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(100,0,0,0))
    $g.FillRectangle($slotBrush, 4, 14, 14, 4)

    # LED glow (large, right side)
    $glowBrush  = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(140,$col.R,$col.G,$col.B))
    $whiteBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $g.FillEllipse($glowBrush,  20, 13, 8, 8)
    $g.FillEllipse($whiteBrush, 22, 15, 4, 4)

    $g.Dispose()
    $bodyBrush.Dispose(); $hlBrush.Dispose(); $shBrush.Dispose()
    $pen.Dispose(); $slotBrush.Dispose(); $glowBrush.Dispose(); $whiteBrush.Dispose()

    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

function Get-AgentStatus {
    if (-not (Test-Path $LogFile)) { return @{color=""#f59e0b"";tip=""DiskHealth Agent`nLog not found""} }
    try {
        # Use FileShare::ReadWrite so we can read while the agent has the file open for writing
        $stream = [System.IO.File]::Open($LogFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $reader = New-Object System.IO.StreamReader($stream)
        $content = $reader.ReadToEnd()
        $reader.Close(); $stream.Close()
        $lines = $content -split ""`r?`n"" | Where-Object { $_ } | Select-Object -Last 10
        $last  = $lines | Where-Object { $_ -match '\[INFO \]|\[WARN \]|\[ERROR\]' } | Select-Object -Last 1
        $ts    = if ($last -match '\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]') { $matches[1] } else { ""Unknown"" }
        if     ($last -match 'Report accepted') { return @{color=""#22c55e"";tip=""DiskHealth Agent`nLast report: $ts`nStatus: OK""} }
        elseif ($last -match 'ERROR')           { return @{color=""#ef4444"";tip=""DiskHealth Agent`nLast event: $ts`nStatus: Error""} }
        elseif ($last -match 'WARN')            { return @{color=""#f59e0b"";tip=""DiskHealth Agent`nLast event: $ts`nStatus: Warning""} }
        else                                    { return @{color=""#22c55e"";tip=""DiskHealth Agent`nLast event: $ts`nStatus: Running""} }
    } catch { return @{color=""#f59e0b"";tip=""DiskHealth Agent`nCould not read log""} }
}

function Is-AgentRunning {
    $p = Get-WmiObject Win32_Process -Filter ""Name='powershell.exe'"" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -like ""*DiskHealthAgent.ps1*"" }
    return ($null -ne $p)
}

$status       = Get-AgentStatus
$tray         = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon    = Make-Icon -Color $status.color
$tray.Text    = (($status.tip -split ""`n"")[0..1] -join "" | "").Substring(0, [Math]::Min(63, (($status.tip -split ""`n"")[0..1] -join "" | "").Length))
$tray.Visible = $true

$menu     = New-Object System.Windows.Forms.ContextMenuStrip
$miTitle  = $menu.Items.Add(""DiskHealth Agent"")
$miTitle.Enabled = $false
$miTitle.Font    = New-Object System.Drawing.Font(""Segoe UI"", 8, [System.Drawing.FontStyle]::Bold)
$menu.Items.Add(""-"") | Out-Null

$miLog = $menu.Items.Add(""View Agent Log"")
$miLog.Add_Click({
    try {
        $tmp = Join-Path $env:TEMP ""DiskHealthAgent_log_view.txt""
        $stream = [System.IO.File]::Open($LogFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $reader = New-Object System.IO.StreamReader($stream)
        $content = $reader.ReadToEnd()
        $reader.Close(); $stream.Close()
        [System.IO.File]::WriteAllText($tmp, $content)
        Start-Process notepad.exe -ArgumentList $tmp -ErrorAction SilentlyContinue
    } catch {
        Start-Process notepad.exe -ArgumentList $LogFile -ErrorAction SilentlyContinue
    }
})

$miRestart = $menu.Items.Add(""Restart Agent"")
$miRestart.Add_Click({
    try {
        Stop-ScheduledTask  -TaskName $AgentTask -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $AgentTask -ErrorAction SilentlyContinue
        $tray.ShowBalloonTip(3000,""DiskHealth Agent"",""Agent restarted."",[System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        $tray.ShowBalloonTip(3000,""DiskHealth Agent"",""Restart failed: $_"",[System.Windows.Forms.ToolTipIcon]::Error)
    }
})

$menu.Items.Add(""-"") | Out-Null
$miExit = $menu.Items.Add(""Exit Tray Icon"")
$miExit.Add_Click({
    $tray.Visible = $false; $tray.Dispose()
    [System.Windows.Forms.Application]::Exit()
})
$tray.ContextMenuStrip = $menu

$timer          = New-Object System.Windows.Forms.Timer
$timer.Interval = 30000
$timer.Add_Tick({
    if (-not (Is-AgentRunning)) {
        $tray.Icon = Make-Icon -Color ""#ef4444""
        $tray.Text = ""DiskHealth Agent | NOT RUNNING""
    } else {
        $s = Get-AgentStatus
        $tray.Icon = Make-Icon -Color $s.color
        $tray.Text = (($s.tip -split ""`n"")[0..1] -join "" | "").Substring(0,[Math]::Min(63,(($s.tip -split ""`n"")[0..1] -join "" | "").Length))
    }
})
$timer.Start()

$tray.ShowBalloonTip(3000,""DiskHealth Agent"",""Monitoring disk health. Running in background."",[System.Windows.Forms.ToolTipIcon]::Info)
[System.Windows.Forms.Application]::Run()

# ── Cleanup ──────────────────────────────────────────────────────────────────
$timer.Dispose()
$tray.Dispose()
if ($owned) { $mutex.ReleaseMutex() }
$mutex.Dispose()
";

        public const string Installer = @"param(
    [string]$ServerUrl   = """",
    [int]$PollInterval   = 21600,
    [switch]$Uninstall   = $false,
    [string]$Title       = ""DiskHealth Agent - Master Sofa""
)
function Ensure-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $argList = ""-ExecutionPolicy Bypass -File `""$($MyInvocation.ScriptName)`""""
        if ($ServerUrl)    { $argList += "" -ServerUrl `""$ServerUrl`"""" }
        if ($PollInterval) { $argList += "" -PollInterval $PollInterval"" }
        if ($Uninstall)    { $argList += "" -Uninstall"" }
        if ($Title)        { $argList += "" -Title `""$Title`"""" }
        Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
        exit
    }
}
$ServiceName = ""DiskHealthAgent""
$InstallDir  = ""$env:ProgramFiles\DiskHealthAgent""
$AgentScript = Join-Path $InstallDir ""DiskHealthAgent.ps1""
function Write-Step { param([string]$m); Write-Host ""  [>] $m"" -ForegroundColor Cyan   }
function Write-OK   { param([string]$m); Write-Host ""  [OK] $m"" -ForegroundColor Green  }
function Write-Fail { param([string]$m); Write-Host ""  [!!] $m"" -ForegroundColor Red    }
function Write-Warn { param([string]$m); Write-Host ""  [!] $m""  -ForegroundColor Yellow }
function Check-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-WinMajor {
    $os = Get-WmiObject Win32_OperatingSystem
    return [int]([version]$os.Version).Major
}
function Test-ServerConn {
    param([string]$Url)
    try {
        $req = [System.Net.WebRequest]::Create(""$Url/health""); $req.Timeout=5000
        $resp = $req.GetResponse(); $resp.Close(); return $true
    } catch { return $false }
}
function Uninstall-Agent {
    Write-Step ""Removing scheduled task...""
    schtasks /Delete /TN $ServiceName /F 2>&1 | Out-Null
    Write-OK ""Task removed.""
    $procs=Get-WmiObject Win32_Process -Filter ""Name='powershell.exe'"" -ErrorAction SilentlyContinue
    foreach ($proc in $procs) {
        if ($proc.CommandLine -and $proc.CommandLine -like ""*DiskHealthAgent.ps1*"") {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
    if (Test-Path $InstallDir) {
        for ($t=1;$t-le5;$t++) {
            try { Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop; break }
            catch { Start-Sleep -Seconds 2 }
        }
    }
    netsh advfirewall firewall delete rule name=""DiskHealthAgent"" 2>&1 | Out-Null
    schtasks /Delete /TN ""DiskHealthTray"" /F 2>&1 | Out-Null
    Get-WmiObject Win32_Process -Filter ""Name='powershell.exe'"" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like ""*DiskHealthTray*"" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-OK ""Uninstall complete.""
}

function Install-Smartctl {
    $smartctlPath = ""$env:ProgramFiles\smartmontools\bin\smartctl.exe""
    if (Test-Path $smartctlPath) {
        Write-OK ""smartmontools already installed.""
        return $true
    }
    Write-Step ""Installing smartmontools (required for SSD/NVMe SMART data)...""
    # Try winget first (Windows 10 1709+)
    $wingetOk = $false
    try {
        $wg = Get-Command winget.exe -ErrorAction Stop
        if ($wg) {
            Write-Step ""Trying winget...""
            $proc = Start-Process -FilePath ""winget.exe"" `
                -ArgumentList ""install --id smartmontools.smartmontools --silent --accept-package-agreements --accept-source-agreements"" `
                -Wait -PassThru -WindowStyle Hidden
            if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq -1978335212) {
                # -1978335212 = already installed
                if (Test-Path $smartctlPath) {
                    Write-OK ""smartmontools installed via winget.""
                    $wingetOk = $true
                }
            }
        }
    } catch {}
    if ($wingetOk) { return $true }
    # Fallback: download MSI directly from smartmontools.org
    Write-Step ""winget unavailable or failed, downloading MSI directly...""
    try {
        $msiUrl  = ""https://www.smartmontools.org/airfiles/smartmontools-7.4-1.win32-setup.exe""
        $msiPath = Join-Path $env:TEMP ""smartmontools-setup.exe""
        $wc = New-Object System.Net.WebClient
        $wc.DownloadFile($msiUrl, $msiPath)
        $proc = Start-Process -FilePath $msiPath -ArgumentList ""/S"" -Wait -PassThru
        Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
        if (Test-Path $smartctlPath) {
            Write-OK ""smartmontools installed via direct download.""
            return $true
        } else {
            Write-Warn ""smartmontools installer ran but binary not found. SSD/NVMe data may be unavailable.""
            return $false
        }
    } catch {
        Write-Warn ""Could not install smartmontools: $_""
        Write-Warn ""SSD/NVMe SMART data will be unavailable. Install manually: winget install smartmontools.smartmontools""
        return $false
    }
}
function Install-Agent {
    if (-not $ServerUrl) { Write-Fail ""ServerUrl is required.""; exit 1 }
    if (-not $ServerUrl.StartsWith(""http"")) { Write-Fail ""ServerUrl must start with http://""; exit 1 }
    if (-not (Check-Admin)) { Write-Fail ""Must be run as Administrator.""; exit 1 }
    $winVer = Get-WinMajor
    Write-Step ""Creating install directory...""
    if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
    Write-OK ""Directory ready.""
    Write-Step ""Locating DiskHealthAgent.ps1...""
    $scriptDir    = Split-Path -Parent $MyInvocation.ScriptName
    $sourceScript = Join-Path $scriptDir ""DiskHealthAgent.ps1""
    if (-not (Test-Path $sourceScript)) { $sourceScript = Join-Path $PSScriptRoot ""DiskHealthAgent.ps1"" }
    if (-not (Test-Path $sourceScript)) { Write-Fail ""DiskHealthAgent.ps1 not found.""; exit 1 }
    Copy-Item -Path $sourceScript -Destination $AgentScript -Force
    Write-OK ""Agent script copied.""
    Write-Step ""Checking smartmontools...""
    Install-Smartctl | Out-Null
    $oldIdFile = Join-Path $InstallDir ""agent_id.txt""
    if (Test-Path $oldIdFile) { Remove-Item $oldIdFile -Force -ErrorAction SilentlyContinue }
    try { Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy RemoteSigned -Force } catch {}
    Write-Step ""Creating scheduled task '$ServiceName'...""
    $psExe  = ""powershell.exe""
    $psArgs = ""-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `""$AgentScript`"" -ServerUrl `""$ServerUrl`"" -PollInterval $PollInterval""
    schtasks /Delete /TN $ServiceName /F 2>&1 | Out-Null
    $taskCreated = $false
    if ($winVer -ge 8) {
        $xmlEscapedArgs = [System.Security.SecurityElement]::Escape($psArgs)
        $xml = @""
<?xml version=""1.0"" encoding=""UTF-16""?>
<Task version=""1.3"" xmlns=""http://schemas.microsoft.com/windows/2004/02/mit/task"">
  <RegistrationInfo><Description>DiskHealth Agent</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT15S</Delay></BootTrigger></Triggers>
  <Principals><Principal id=""Author""><UserId>SYSTEM</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <RestartOnFailure><Interval>PT1M</Interval><Count>9999</Count></RestartOnFailure>
  </Settings>
  <Actions><Exec>
    <Command>$psExe</Command>
    <Arguments>$xmlEscapedArgs</Arguments>
    <WorkingDirectory>$InstallDir</WorkingDirectory>
  </Exec></Actions>
</Task>
""@
        $xmlPath = Join-Path $env:TEMP ""diskhealth_task.xml""
        [System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::Unicode)
        $out = schtasks /Create /TN $ServiceName /XML $xmlPath /F 2>&1
        Remove-Item $xmlPath -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -eq 0) { Write-OK ""Scheduled task created.""; $taskCreated = $true }
        else { Write-Warn ""XML method failed: $out"" }
    }
    if (-not $taskCreated) {
        $out = schtasks /Create /TN $ServiceName /SC ONSTART /DELAY ""0000:15"" /TR ""$psExe $psArgs"" /RU SYSTEM /RL HIGHEST /F 2>&1
        if ($LASTEXITCODE -eq 0) { Write-OK ""Scheduled task created.""; $taskCreated = $true }
        else { Write-Fail ""Could not create scheduled task: $out"" }
    }
    Write-Step ""Testing server connection...""
    if (Test-ServerConn $ServerUrl) { Write-OK ""Server is reachable!"" }
    else { Write-Warn ""Server not reachable - agent will retry automatically."" }
    Write-Step ""Starting agent in background...""
    try {
        Start-Process -FilePath $psExe -ArgumentList $psArgs -WorkingDirectory $InstallDir -WindowStyle Hidden
        Write-OK ""Agent launched.""
    } catch { Write-Warn ""Auto-start failed - will start on reboot."" }
    Write-Step ""Setting up system tray icon...""
    $TrayScript = Join-Path $InstallDir ""DiskHealthTray.ps1""
    $TrayTask   = ""DiskHealthTray""
    if (-not (Test-Path $TrayScript)) {
        try {
            Write-Step ""Downloading tray script from server...""
            Invoke-WebRequest -UseBasicParsing -Uri ""$ServerUrl/agent/tray.ps1"" -OutFile $TrayScript -ErrorAction Stop
            Write-OK ""Tray script downloaded.""
        } catch { Write-Warn ""Could not download tray script: $_"" }
    }
    if (Test-Path $TrayScript) {
        $trayArgs = ""-NonInteractive -ExecutionPolicy Bypass -File `""$TrayScript`""""
        $trayCreated = $false
        # Use XML method to handle spaces in path correctly
        try {
            $xmlEscArgs  = [System.Security.SecurityElement]::Escape($trayArgs)
            $xmlEscExe   = [System.Security.SecurityElement]::Escape($psExe)
            $xmlEscDir   = [System.Security.SecurityElement]::Escape($InstallDir)
            $trayXml = @""
<?xml version=""1.0"" encoding=""UTF-16""?>
<Task version=""1.3"" xmlns=""http://schemas.microsoft.com/windows/2004/02/mit/task"">
  <RegistrationInfo><Description>DiskHealth Tray Icon</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id=""Author""><GroupId>S-1-5-32-545</GroupId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions><Exec>
    <Command>$xmlEscExe</Command>
    <Arguments>$xmlEscArgs</Arguments>
    <WorkingDirectory>$xmlEscDir</WorkingDirectory>
  </Exec></Actions>
</Task>
""@
            $trayXmlPath = Join-Path $env:TEMP ""diskhealthtray_task.xml""
            [System.IO.File]::WriteAllText($trayXmlPath, $trayXml, [System.Text.Encoding]::Unicode)
            schtasks /Delete /TN $TrayTask /F 2>&1 | Out-Null
            $out = schtasks /Create /TN $TrayTask /XML $trayXmlPath /F 2>&1
            Remove-Item $trayXmlPath -ErrorAction SilentlyContinue
            if ($LASTEXITCODE -eq 0) { $trayCreated = $true }
            else { Write-Warn ""Tray XML method failed: $out"" }
        } catch { Write-Warn ""Tray task XML error: $_"" }
        if ($trayCreated) {
            Write-OK ""Tray logon task created.""
            # Kill any running tray instance before starting the new one
            Get-WmiObject Win32_Process -Filter ""Name='powershell.exe'"" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like ""*DiskHealthTray*"" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
            try {
                Start-Process -FilePath $psExe -ArgumentList $trayArgs -WindowStyle Hidden
                Write-OK ""Tray icon launched.""
            } catch { Write-Warn ""Tray auto-start failed - will appear on next logon."" }
        } else { Write-Warn ""Could not create tray task - will appear on next logon."" }
    } else { Write-Warn ""DiskHealthTray.ps1 not found - skipping tray setup."" }
    Write-OK ""Installation complete! Reporting to: $ServerUrl""
}
Ensure-Admin
if ($Uninstall) { Uninstall-Agent } else { Install-Agent }
";

    }
}