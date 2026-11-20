# Self-contained Windows PowerShell 5.1 / PowerShell 7 tests. No Pester required.
# Every OS boundary is injected: no process termination, registry writes or real junctions.
# Run: powershell.exe -NoProfile -File .\frontend\resources\installer-guard.Tests.ps1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'These Windows-path tests require Windows; they do not validate Win32 integration.'
}
. (Join-Path $PSScriptRoot 'installer-guard.ps1')

function Assert-Equal($Actual, $Expected) {
    if ($Actual -cne $Expected) { throw "Expected [$Expected], got [$Actual]" }
}
function Assert-Throws([scriptblock]$Body, [string]$Pattern) {
    $caught = $false
    try { & $Body | Out-Null } catch {
        $caught = $true
        if ($_.Exception.Message -notmatch $Pattern) { throw }
    }
    if (-not $caught) { throw "Expected refusal: $Pattern" }
}
$script:passed = 0
function Test-Case([string]$Name, [scriptblock]$Body) {
    & $Body
    $script:passed++
    Write-Output "PASS $Name"
}

function New-Fixture([string]$Channel = 'stable') {
    $context = @{
        Channel = $Channel; InstallDir = 'C:\Apps\own'; UninstallFilename = 'Uninstall own.exe'
        OwnMainExe = 'own-main.exe'; OtherMainExe = 'other-main.exe'
        OwnServiceExe = 'own-service.exe'; OtherServiceExe = 'other-service.exe'
        OwnInstallKey = 'Software\fixture-own'; OtherInstallKey = 'Software\fixture-other'
        OwnUninstallKey = 'Software\Uninstall\fixture-own'; OtherUninstallKey = 'Software\Uninstall\fixture-other'
        StatePath = 'C:\Temp\approval.json'; Arguments = ''
    }
    $state = @{
        Paths = @{}; Registry = @{}; Reads = [Collections.Generic.List[string]]::new()
        Events = [Collections.Generic.List[string]]::new(); Approval = $null
        Processes = @(); Images = @{}; HandleIds = @{}
        QueryFailure = $false; OpenFailure = $false; TerminateFailure = $false; WaitFailure = $false
    }
    $state.Paths['C:\'] = @{ Exists = $true; IsDirectory = $true; IsReparse = $false; Canonical = 'C:\' }
    $state.Paths['D:\'] = @{ Exists = $true; IsDirectory = $true; IsReparse = $false; Canonical = 'D:\' }
    $compareRegistry = ${function:Assert-RegistryValuesEqual}
    $native = @{
        Probe = {
            param($path)
            if ($state.Paths.ContainsKey($path)) {
                $entry = $state.Paths[$path]
                if ($entry.ContainsKey('Error')) { throw $entry.Error }
                return [pscustomobject]$entry
            }
            return [pscustomobject]@{ Exists = $false; IsDirectory = $false; IsReparse = $false; Canonical = $null }
        }.GetNewClosure()
        Children = {
            param($path)
            @($state.Paths.Keys | Where-Object { [IO.Path]::GetDirectoryName($_) -eq $path })
        }.GetNewClosure()
        ReadRegistry = {
            param($hive, $view, $key)
            $id = "$hive/$view/$key"
            $state.Reads.Add($id)
            if ($state.Registry.ContainsKey($id)) { return $state.Registry[$id] }
            return $null
        }.GetNewClosure()
        ForgetRecord = {
            param($record)
            $id = "$($record.Hive)/$($record.View)/$($record.Key)"
            & $compareRegistry $record.Values $state.Registry[$id]
            foreach ($name in @('UninstallString', 'QuietUninstallString')) {
                if ($record.Values.ContainsKey($name)) {
                    $state.Events.Add("forget:$id/$name")
                    $state.Registry[$id].Remove($name)
                    $state.Registry[$id].Remove('Kind:' + $name)
                }
            }
        }.GetNewClosure()
        ReadApproval = { param($path) return $state.Approval }.GetNewClosure()
        WriteApproval = { param($path, $approval) $state.Approval = $approval }.GetNewClosure()
        Processes = { return $state.Processes }.GetNewClosure()
        OpenProcess = {
            param($processId)
            $state.Events.Add("open:$processId")
            if ($state.OpenFailure) { throw 'OpenProcess access denied' }
            $handle = "handle-$processId"
            $state.HandleIds[$handle] = $processId
            return $handle
        }.GetNewClosure()
        QueryImage = {
            param($handle)
            $state.Events.Add("query:$handle")
            if ($state.QueryFailure) { throw 'QueryFullProcessImageName failed' }
            return $state.Images[$handle]
        }.GetNewClosure()
        Terminate = {
            param($handle)
            $state.Events.Add("terminate:$handle")
            if ($state.TerminateFailure) { throw 'TerminateProcess failed' }
        }.GetNewClosure()
        Wait = {
            param($handle)
            $state.Events.Add("wait:$handle")
            if ($state.WaitFailure) { throw 'WaitForSingleObject timeout' }
        }.GetNewClosure()
        Close = { param($handle) $state.Events.Add("close:$handle") }.GetNewClosure()
    }
    return @{ Context = $context; State = $state; Native = $native }
}
function Add-Path($Fixture, $Path, $Canonical = $Path, [bool]$Reparse = $false, [bool]$Directory = $true) {
    $Fixture.State.Paths[$Path] = @{ Exists = $true; IsDirectory = $Directory; IsReparse = $Reparse; Canonical = $Canonical }
}
function Add-Registration($Fixture, [bool]$Own, $Hive, $View, $Location, $Command = $null) {
    $views = if ($Hive -eq 'HKCU') { @('32', '64') } else { @($View) }
    $key = if ($Own) { $Fixture.Context.OwnInstallKey } else { $Fixture.Context.OtherInstallKey }
    $values = @{ InstallLocation = $Location }
    foreach ($alias in $views) { $Fixture.State.Registry["$Hive/$alias/$key"] = $values }
    if ($null -ne $Command) {
        $key = if ($Own) { $Fixture.Context.OwnUninstallKey } else { $Fixture.Context.OtherUninstallKey }
        $values = @{ UninstallString = $Command; QuietUninstallString = "$Command /S" }
        foreach ($alias in $views) { $Fixture.State.Registry["$Hive/$alias/$key"] = $values }
    }
}

Test-Case 'canonical existing junction and missing descendant use handle-resolved ancestor' {
    $f = New-Fixture
    Add-Path $f 'C:\alias' 'D:\real' $true
    Assert-Equal (Resolve-Canonical 'C:\alias\new\child' $f.Native) 'D:\real\new\child'
    Assert-Equal (Resolve-Canonical 'c:\ALIAS' $f.Native) 'D:\real'
    $f.State.Paths['C:\broken'] = @{ Error = 'CreateFile junction resolution failed' }
    Assert-Throws { Resolve-Canonical 'C:\broken\child' $f.Native } 'CreateFile'
    Assert-Throws { Resolve-Canonical 'relative\path' $f.Native } 'absolute'
}

Test-Case 'both channels and all four registry scopes reject same/parent/child overlaps' {
    foreach ($channel in @('stable', 'beta')) {
        foreach ($hive in @('HKCU', 'HKLM')) {
            foreach ($view in @('32', '64')) {
                foreach ($other in @('C:\Apps\own', 'C:\Apps', 'C:\Apps\own\nested')) {
                    $f = New-Fixture $channel
                    Add-Registration $f $false $hive $view $other
                    Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'overlap'
                }
            }
        }
    }
}

Test-Case 'own same canonical upgrade is allowed; own different target migration is refused' {
    $f = New-Fixture
    Add-Path $f 'C:\alias' 'C:\Apps\own' $true
    Add-Registration $f $true 'HKCU' '64' 'C:\alias' '"C:\Apps\own\Uninstall own.exe" /currentuser'
    $result = Test-InstallIsolation $f.Context $f.Native
    Assert-Equal $result.Root 'C:\Apps\own'
    Assert-Equal $f.State.Reads.Count 16
    $f.Context.InstallDir = 'D:\Moved'
    Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'migration'
}

Test-Case 'uninstall-only locations and command aliases cannot hide another channel' {
    foreach ($commandName in @('UninstallString', 'QuietUninstallString')) {
        $f = New-Fixture
        Add-Path $f 'C:\alias' 'C:\Apps\own' $true
        $f.State.Registry["HKLM/32/$($f.Context.OtherUninstallKey)"] = @{ $commandName = '"C:\alias\uninstall.exe" /S' }
        Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'overlap'
    }
    $f = New-Fixture
    $f.State.Registry["HKCU/64/$($f.Context.OwnUninstallKey)"] = @{ UninstallString = 'C:\Apps with spaces\uninstall.exe /S' }
    Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'Ambiguous'
}

Test-Case 'unregistered other main/backend executables block installation' {
    foreach ($relative in @('other-main.exe', 'resources\backend\other-service.exe')) {
        $f = New-Fixture
        Add-Path $f (Join-Path $f.Context.InstallDir $relative) -Directory $false
        Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'other channel executable'
    }
}

Test-Case 'OS read failures and malformed records are refusals, not absent registrations' {
    $f = New-Fixture
    $f.Native.ReadRegistry = { throw 'Registry access denied' }
    Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'access denied'
    $f = New-Fixture
    $f.State.Registry["HKCU/32/$($f.Context.OwnInstallKey)"] = @{}
    Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'no usable location'
}

Test-Case 'cleanup refuses reparse resources/locales and leaves root user data unexamined' {
    foreach ($relative in @('resources', 'locales', 'resources\backend', 'locales\file.pak', 'own-main.exe')) {
        $f = New-Fixture
        Add-Path $f 'C:\Apps\own'
        Add-Path $f 'C:\Apps\own\resources'
        Add-Path $f 'C:\Apps\own\locales'
        Add-Path $f (Join-Path $f.Context.InstallDir $relative) 'D:\unrelated' $true
        Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'reparse'
    }
    $f = New-Fixture
    Add-Path $f 'C:\alias' 'C:\Apps\own' $true
    Add-Path $f 'C:\Apps\own'
    foreach ($relative in @('data', 'logs', 'tasks', 'workflows', 'downloads', 'archives', '.env')) {
        Add-Path $f (Join-Path $f.Context.InstallDir $relative) 'D:\user-data' $true
    }
    $f.Context.InstallDir = 'C:\alias'
    Assert-Equal (Test-InstallIsolation $f.Context $f.Native).Root 'C:\Apps\own'
    foreach ($relative in @('data', '.env', '..\other', 'resources\..\data')) {
        Assert-Throws { Assert-CleanupTarget $relative $f.Context $f.Native } 'cleanup target'
    }
}

Test-Case 'first Check pins canonical root; later junction retarget cannot authorize mutation' {
    $f = New-Fixture
    Add-Path $f 'C:\Apps\own' 'D:\first' $true
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    Assert-Equal $f.State.Events.Count 0
    Add-Path $f 'C:\Apps\own' 'D:\second' $true
    Assert-Throws { Invoke-InstallerGuard 'Stop' $f.Context $f.Native } 'root changed'
    Assert-Equal $f.State.Events.Count 0
}

Test-Case 'only exact owned images terminate on the same queried handle, never prefixes or descendants' {
    foreach ($channel in @('stable', 'beta')) {
        $f = New-Fixture $channel
        $f.State.Processes = @(
            @{ Id = 101; Name = 'own-main.exe' }, @{ Id = 102; Name = 'own-service.exe' },
            @{ Id = 103; Name = 'own-main.exe' }, @{ Id = 104; Name = 'child.exe' },
            @{ Id = 105; Name = 'other-service.exe' }, @{ Id = 106; Name = 'own-service.exe' }
        )
        $f.State.Images['handle-101'] = 'C:\Apps\own\own-main.exe'
        $f.State.Images['handle-102'] = 'C:\Apps\own\resources\backend\own-service.exe'
        $f.State.Images['handle-103'] = 'C:\Apps\own-backup\own-main.exe'
        $f.State.Images['handle-106'] = 'C:\Apps\own\nested\own-service.exe'
        Stop-OwnedProcesses $f.Context (Test-InstallIsolation $f.Context $f.Native) $f.Native
        Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'terminate:*' }) -join ',') 'terminate:handle-101,terminate:handle-102'
        Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'wait:*' }) -join ',') 'wait:handle-101,wait:handle-102'
        Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'close:*' }).Count) 4
        Assert-Equal (@($f.State.Events | Where-Object { $_ -in @('open:104', 'open:105') }).Count) 0
    }
}

Test-Case 'PID reuse cannot replace the queried handle; all candidates verify before any termination' {
    $f = New-Fixture
    $f.State.Processes = @(@{ Id = 101; Name = 'own-main.exe' })
    $f.Native.QueryImage = {
        param($handle)
        $f.State.HandleIds[$handle] = 999 # PID now refers to something else; the held handle remains the identity.
        return 'C:\Apps\own\own-main.exe'
    }.GetNewClosure()
    Stop-OwnedProcesses $f.Context (Test-InstallIsolation $f.Context $f.Native) $f.Native
    Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'terminate:*' }) -join ',') 'terminate:handle-101'
    $f = New-Fixture
    $f.State.Processes = @(@{ Id = 101; Name = 'own-main.exe' }, @{ Id = 102; Name = 'own-service.exe' })
    $f.Native.QueryImage = {
        param($handle)
        if ($handle -eq 'handle-102') { throw 'Cannot verify suspected target' }
        return 'C:\Apps\own\own-main.exe'
    }.GetNewClosure()
    Assert-Throws { Stop-OwnedProcesses $f.Context (Test-InstallIsolation $f.Context $f.Native) $f.Native } 'Cannot verify'
    Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'terminate:*' }).Count) 0
    Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'close:*' }).Count) 2
}

Test-Case 'open, query, terminate and wait errors propagate and close held handles' {
    foreach ($failure in @('OpenFailure', 'QueryFailure', 'TerminateFailure', 'WaitFailure')) {
        $f = New-Fixture
        $f.State.Processes = @(@{ Id = 101; Name = 'own-main.exe' })
        $f.State.Images['handle-101'] = 'C:\Apps\own\own-main.exe'
        $f.State[$failure] = $true
        Invoke-InstallerGuard 'Check' $f.Context $f.Native
        Assert-Throws { Invoke-InstallerGuard 'Stop' $f.Context $f.Native } 'denied|failed|timeout'
        if ($failure -ne 'OpenFailure') { Assert-Equal $f.State.Events[$f.State.Events.Count - 1] 'close:handle-101' }
    }
}

Test-Case 'shared HKCU uninstall views forget each command once and remain valid for cleanup' {
    $f = New-Fixture
    Add-Registration $f $true 'HKCU' '32' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe" /currentuser'
    Add-Registration $f $false 'HKCU' '64' 'D:\Other' '"D:\Other\uninstall.exe" /currentuser'
    $key32 = "HKCU/32/$($f.Context.OwnUninstallKey)"
    $key64 = "HKCU/64/$($f.Context.OwnUninstallKey)"
    Assert-Equal ([object]::ReferenceEquals($f.State.Registry[$key32], $f.State.Registry[$key64])) $true
    $f.State.Registry[$key32].InstallLocation = 'C:\Apps\own'
    $otherInstall = $f.State.Registry["HKCU/32/$($f.Context.OtherInstallKey)"].Clone()
    $otherUninstall = $f.State.Registry["HKCU/32/$($f.Context.OtherUninstallKey)"].Clone()
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    Assert-Equal $f.State.Events.Count 0
    Assert-Equal $f.State.Reads.Count 16
    Invoke-InstallerGuard 'Forget' $f.Context $f.Native
    Assert-Equal $f.State.Events.Count 2
    foreach ($name in @('UninstallString', 'QuietUninstallString')) {
        Assert-Equal (@($f.State.Events | Where-Object { $_ -like "forget:HKCU/*/$($f.Context.OwnUninstallKey)/$name" }).Count) 1
    }
    foreach ($view in @('32', '64')) {
        $values = $f.State.Registry["HKCU/$view/$($f.Context.OwnUninstallKey)"]
        Assert-Equal ($values.ContainsKey('UninstallString')) $false
        Assert-Equal ($values.ContainsKey('QuietUninstallString')) $false
        Assert-Equal $values.Count 1
        Assert-Equal $values.InstallLocation 'C:\Apps\own'
        Assert-Equal $f.State.Registry["HKCU/$view/$($f.Context.OwnInstallKey)"].InstallLocation 'C:\Apps\own'
        Assert-RegistryValuesEqual $otherInstall $f.State.Registry["HKCU/$view/$($f.Context.OtherInstallKey)"]
        Assert-RegistryValuesEqual $otherUninstall $f.State.Registry["HKCU/$view/$($f.Context.OtherUninstallKey)"]
    }
    Invoke-InstallerGuard 'CheckCleanup' $f.Context $f.Native 'resources'
    Assert-Equal $f.State.Events.Count 2
}

Test-Case 'conflicting shared HKCU snapshots refuse Forget before any registry mutation' {
    $f = New-Fixture
    Add-Registration $f $true 'HKCU' '32' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe" /currentuser'
    Add-Registration $f $true 'HKLM' '32' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe" /allusers'
    $key32 = "HKCU/32/$($f.Context.OwnUninstallKey)"
    $key64 = "HKCU/64/$($f.Context.OwnUninstallKey)"
    Assert-Equal ([object]::ReferenceEquals($f.State.Registry[$key32], $f.State.Registry[$key64])) $true
    $before = $f.State.Registry[$key32].Clone()
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    $readRegistry = $f.Native.ReadRegistry
    $f.Native.ReadRegistry = {
        param($hive, $view, $key)
        $values = & $readRegistry $hive $view $key
        if ($hive -eq 'HKCU' -and $view -eq '64' -and $key -ceq $f.Context.OwnUninstallKey) {
            $values = $values.Clone()
            $values.QuietUninstallString = '"C:\Apps\own\uninstall.exe" /changed'
        }
        return $values
    }.GetNewClosure()
    Assert-Throws { Invoke-InstallerGuard 'Forget' $f.Context $f.Native } 'Registry changed after Check'
    Assert-Equal $f.State.Events.Count 0
    Assert-RegistryValuesEqual $before $f.State.Registry[$key32]
    Assert-RegistryValuesEqual $before $f.State.Registry[$key64]
    Assert-Equal $f.State.Registry["HKLM/32/$($f.Context.OwnUninstallKey)"].UninstallString '"C:\Apps\own\uninstall.exe" /allusers'
}

Test-Case 'Forget validates all own uninstall paths before any registry mutation' {
    $f = New-Fixture
    Add-Registration $f $true 'HKLM' '32' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe"'
    Add-Registration $f $true 'HKLM' '64' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe"'
    $evidence = Test-InstallIsolation $f.Context $f.Native
    $record = @($evidence.Records | Where-Object { $_.Own -and $_.Kind -eq 'Uninstall' -and $_.Hive -eq 'HKLM' -and $_.View -eq '64' })[0]
    $record.Paths = @('D:\Other')
    Assert-Throws { Forget-OwnedUninstall $f.Context $evidence $f.Native } 'Unverified own uninstall record'
    Assert-Equal $f.State.Events.Count 0
}

Test-Case 'Forget only removes checked own commands in all registry scopes; other registrations survive' {
    $f = New-Fixture
    foreach ($hive in @('HKCU', 'HKLM')) {
        foreach ($view in @('32', '64')) {
            Add-Registration $f $true $hive $view 'C:\Apps\own' '"C:\Apps\own\uninstall.exe" /currentuser'
            Add-Registration $f $false $hive $view 'D:\Other' '"D:\Other\uninstall.exe" /currentuser'
        }
    }
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    Invoke-InstallerGuard 'Forget' $f.Context $f.Native
    Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'forget:*' }).Count) 6
    Assert-Equal (@($f.State.Events | Where-Object { $_ -match 'fixture-other' }).Count) 0
    foreach ($hive in @('HKCU', 'HKLM')) {
        foreach ($view in @('32', '64')) {
            $own = $f.State.Registry["$hive/$view/$($f.Context.OwnUninstallKey)"]
            Assert-Equal ($own.ContainsKey('UninstallString')) $false
            Assert-Equal ($own.ContainsKey('QuietUninstallString')) $false
            Assert-Equal $f.State.Registry["$hive/$view/$($f.Context.OwnInstallKey)"].InstallLocation 'C:\Apps\own'
            Assert-Equal $f.State.Registry["$hive/$view/$($f.Context.OtherInstallKey)"].InstallLocation 'D:\Other'
            Assert-Equal $f.State.Registry["$hive/$view/$($f.Context.OtherUninstallKey)"].UninstallString '"D:\Other\uninstall.exe" /currentuser'
            Assert-Equal $f.State.Registry["$hive/$view/$($f.Context.OtherUninstallKey)"].QuietUninstallString '"D:\Other\uninstall.exe" /currentuser /S'
            if ($hive -eq 'HKLM') {
                foreach ($name in @('UninstallString', 'QuietUninstallString')) {
                    Assert-Equal (@($f.State.Events | Where-Object { $_ -ceq "forget:$hive/$view/$($f.Context.OwnUninstallKey)/$name" }).Count) 1
                }
            }
        }
    }
    Invoke-InstallerGuard 'CheckCleanup' $f.Context $f.Native 'resources'
}

Test-Case 'Forget refuses changed or denied registry values and never executes an uninstaller' {
    $f = New-Fixture
    Add-Registration $f $true 'HKCU' '64' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe"'
    $evidence = Test-InstallIsolation $f.Context $f.Native
    $f.State.Registry["HKCU/64/$($f.Context.OwnUninstallKey)"].UninstallString = '"D:\Other\uninstall.exe"'
    Assert-Throws { Forget-OwnedUninstall $f.Context $evidence $f.Native } 'Registry changed'
    Assert-Equal $f.State.Events.Count 0
    $f = New-Fixture
    Add-Registration $f $true 'HKCU' '64' 'C:\Apps\own' '"C:\Apps\own\uninstall.exe"'
    $f.Native.ForgetRecord = { throw 'Registry write access denied' }
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    Assert-Throws { Invoke-InstallerGuard 'Forget' $f.Context $f.Native } 'access denied'
}

Test-Case 'uninstall delete-app-data flag is refused before any boundary mutation' {
    $f = New-Fixture
    $f.Context.Arguments = '"C:\Apps\own\Uninstall own.exe" /S --delete-app-data'
    Assert-Throws { Invoke-InstallerGuard 'Check' $f.Context $f.Native } 'user data'
    Assert-Equal $f.State.Events.Count 0
    Assert-Equal $f.State.Reads.Count 0
}

Test-Case 'cleanup rechecks its pinned root after reading approval and never follows a retarget' {
    $f = New-Fixture
    Add-Path $f 'C:\Apps\own' 'D:\first' $true
    Add-Path $f 'D:\first'
    Add-Path $f 'D:\second'
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    $f.Native.ReadApproval = {
        param($path)
        $f.State.Paths['C:\Apps\own'].Canonical = 'D:\second'
        return $f.State.Approval
    }.GetNewClosure()
    Assert-Throws { Invoke-InstallerGuard 'CheckCleanup' $f.Context $f.Native 'resources' } 'root changed'
    Assert-Equal $f.State.Events.Count 0
}

Test-Case 'all handles are closed even when one CloseHandle reports an OS failure' {
    $f = New-Fixture
    $f.State.Processes = @(@{ Id = 101; Name = 'own-main.exe' }, @{ Id = 102; Name = 'own-main.exe' })
    $f.State.Images['handle-101'] = 'D:\unrelated\own-main.exe'
    $f.State.Images['handle-102'] = 'D:\unrelated\own-main.exe'
    $f.Native.Close = {
        param($handle)
        $f.State.Events.Add("close:$handle")
        if ($handle -eq 'handle-101') { throw 'CloseHandle failed' }
    }.GetNewClosure()
    Assert-Throws { Stop-OwnedProcesses $f.Context (Test-InstallIsolation $f.Context $f.Native) $f.Native } 'CloseHandle failed'
    Assert-Equal (@($f.State.Events | Where-Object { $_ -like 'close:*' }).Count) 2
}

Test-Case 'uninstall-only checked commands can be forgotten; unknown empty own metadata fails closed' {
    $f = New-Fixture
    $key = "HKCU/64/$($f.Context.OwnUninstallKey)"
    $f.State.Registry[$key] = @{ UninstallString = '"C:\Apps\own\uninstall.exe" /S' }
    Invoke-InstallerGuard 'Check' $f.Context $f.Native
    Invoke-InstallerGuard 'Forget' $f.Context $f.Native
    Invoke-InstallerGuard 'CheckCleanup' $f.Context $f.Native 'resources'
    Assert-Equal $f.State.Registry[$key].Count 0
    $f.State.Approval = $null
    Assert-Throws { Invoke-InstallerGuard 'Check' $f.Context $f.Native } 'no usable location'
}

Test-Case 'REG_SZ percent names stay literal; only REG_EXPAND_SZ expands environment variables' {
    $name = 'SPIDER_GUARD_TEST_ROOT'
    $previous = [Environment]::GetEnvironmentVariable($name, 'Process')
    try {
        [Environment]::SetEnvironmentVariable($name, 'C:\Apps\own', 'Process')
        $f = New-Fixture
        $key = "HKCU/64/$($f.Context.OtherInstallKey)"
        $f.State.Registry[$key] = @{ InstallLocation = '%SPIDER_GUARD_TEST_ROOT%'; 'Kind:InstallLocation' = 'ExpandString' }
        Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'overlap'
        $f = New-Fixture
        $f.Context.InstallDir = 'C:\%SPIDER_GUARD_TEST_ROOT%\own'
        $f.State.Registry[$key] = @{ InstallLocation = $f.Context.InstallDir; 'Kind:InstallLocation' = 'String' }
        Assert-Throws { Test-InstallIsolation $f.Context $f.Native } 'overlap'
    } finally { [Environment]::SetEnvironmentVariable($name, $previous, 'Process') }
}

Write-Output "$script:passed injected-boundary tests passed (Win32 integration NOT tested)."
