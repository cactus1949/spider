# Windows PowerShell 5.1 compatible. Dot-source to test with injected OS boundaries.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-AbsolutePath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'An absolute path is required' }
    $path = $Path.Replace('/', '\')
    if ($path.StartsWith('\\?\UNC\', [StringComparison]::OrdinalIgnoreCase)) {
        $path = '\\' + $path.Substring(8)
    } elseif ($path.StartsWith('\\?\', [StringComparison]::OrdinalIgnoreCase)) {
        $path = $path.Substring(4)
    }
    if ($path -notmatch '^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+(?:\\|$))') { throw 'An absolute DOS/UNC path is required' }
    if ($path -match '[\x00-\x1f"<>|*?]' -or $path.Substring(2).Contains(':')) { throw 'Invalid installation path' }
    foreach ($part in $path.Split('\')) {
        if ($part -ne '.' -and $part -ne '..' -and $part -match '[. ]$') { throw 'Ambiguous trailing dot/space in path' }
    }
    $full = [IO.Path]::GetFullPath($path)
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::Equals($full, $root, [StringComparison]::OrdinalIgnoreCase)) { return $root }
    return $full.TrimEnd('\')
}

function Resolve-Canonical([string]$Path, [hashtable]$Native) {
    $cursor = ConvertTo-AbsolutePath $Path
    $remaining = [Collections.Generic.List[string]]::new()
    while ($true) {
        # Probe must use CreateFile + GetFinalPathNameByHandle for existing paths.
        # Only genuine file/path-not-found can be represented as Exists=false.
        $entry = & $Native.Probe $cursor
        if ($entry.Exists) {
            if ($remaining.Count -gt 0 -and -not $entry.IsDirectory) { throw 'Existing ancestor is not a directory' }
            $canonical = ConvertTo-AbsolutePath $entry.Canonical
            foreach ($part in $remaining) { $canonical = [IO.Path]::Combine($canonical, $part) }
            return (ConvertTo-AbsolutePath $canonical)
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $cursor) { throw 'Cannot resolve an existing path ancestor' }
        $remaining.Insert(0, [IO.Path]::GetFileName($cursor))
        $cursor = $parent
    }
}

function Test-PathOverlap([string]$First, [string]$Second) {
    if ([string]::Equals($First, $Second, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $First.StartsWith($Second.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $Second.StartsWith($First.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-RegisteredText([hashtable]$Values, [string]$Name) {
    $value = $Values[$Name]
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) { throw 'Registered installation has no usable location' }
    $kindName = 'Kind:' + $Name
    if ($Values.ContainsKey($kindName)) {
        switch ($Values[$kindName]) {
            'String' { return $value }
            'ExpandString' { return [Environment]::ExpandEnvironmentVariables($value) }
            default { throw 'Unsupported installation registry value type' }
        }
    }
    # Injected boundaries may omit kind metadata; their string values are literal REG_SZ.
    return $value
}

function Get-UninstallExecutable([string]$Command) {
    $expanded = $Command.Trim()
    if ($expanded -match '^"([^"]+\.exe)"(?:\s+.*)?$') { return (ConvertTo-AbsolutePath $Matches[1]) }
    if ($expanded -match '^([^\s"]+\.exe)(?:\s+.*)?$') { return (ConvertTo-AbsolutePath $Matches[1]) }
    throw 'Ambiguous or invalid uninstall command; refusing to infer its directory'
}

function Get-IsolationRecords([hashtable]$Context, [hashtable]$Native, [string]$Root) {
    foreach ($hive in @('HKCU', 'HKLM')) {
        foreach ($view in @('32', '64')) {
            foreach ($identity in @(
                @{ Own = $true; Kind = 'Install'; Key = $Context.OwnInstallKey },
                @{ Own = $true; Kind = 'Uninstall'; Key = $Context.OwnUninstallKey },
                @{ Own = $false; Kind = 'Install'; Key = $Context.OtherInstallKey },
                @{ Own = $false; Kind = 'Uninstall'; Key = $Context.OtherUninstallKey }
            )) {
                $values = & $Native.ReadRegistry $hive $view $identity.Key
                if ($null -eq $values) { continue }
                $paths = [Collections.Generic.List[string]]::new()
                foreach ($name in @('InstallLocation', 'UninstallString', 'QuietUninstallString')) {
                    if (-not $values.ContainsKey($name)) { continue }
                    $text = Get-RegisteredText $values $name
                    if ($name -eq 'InstallLocation') {
                        $location = $text.Trim()
                        if ($location.StartsWith('"') -and $location.EndsWith('"')) { $location = $location.Substring(1, $location.Length - 2) }
                        $paths.Add((Resolve-Canonical $location $Native))
                    } else {
                        $executable = Get-UninstallExecutable $text
                        # Resolve the executable too: a linked uninstall file cannot disguise its real directory.
                        $paths.Add([IO.Path]::GetDirectoryName((Resolve-Canonical $executable $Native)))
                    }
                }
                if ($paths.Count -eq 0 -and $identity.Own -and $identity.Kind -eq 'Uninstall' -and $values.Count -eq 0) {
                    # Forget intentionally leaves own uninstall metadata. Corroborate it with the paired
                    # install location, or this session's checked scope (needed for uninstall-only records).
                    # Unknown empty metadata is NOT treated as an absent installation.
                    $paired = & $Native.ReadRegistry $hive $view $Context.OwnInstallKey
                    if ($null -ne $paired -and $paired.ContainsKey('InstallLocation') -and $paired.InstallLocation -is [string] -and
                        -not [string]::IsNullOrWhiteSpace($paired.InstallLocation)) {
                        $location = (Get-RegisteredText $paired 'InstallLocation').Trim()
                        if ($location.StartsWith('"') -and $location.EndsWith('"')) { $location = $location.Substring(1, $location.Length - 2) }
                        $paths.Add((Resolve-Canonical $location $Native))
                    } else {
                        $proof = & $Native.ReadApproval $Context.StatePath
                        $scope = "$hive/$view/$($identity.Key)"
                        if ($null -ne $proof -and $proof.Version -eq 1 -and $proof.Channel -ceq $Context.Channel -and
                            [string]::Equals($proof.Root, $Root, [StringComparison]::OrdinalIgnoreCase) -and $scope -in @($proof.OwnUninstallKeys)) {
                            $paths.Add($Root)
                        }
                    }
                }
                if ($paths.Count -eq 0) { throw 'Registered installation has no usable location' }
                [pscustomobject]@{
                    Hive = $hive; View = $view; Key = $identity.Key; Own = $identity.Own
                    Kind = $identity.Kind; Values = $values.Clone(); Paths = $paths.ToArray()
                }
            }
        }
    }
}

function Get-CleanupTargets([hashtable]$Context) {
    return @('resources', 'locales', $Context.OwnMainExe, $Context.UninstallFilename,
        '*.dll', '*.pak', '*.bin', 'icudtl.dat', 'LICENSE*', 'vk_swiftshader*')
}

function Assert-CleanupTree([string]$Path, [string]$Root, [hashtable]$Native, [bool]$AllowDirectory) {
    $pending = [Collections.Generic.List[string]]::new()
    $pending.Add($Path)
    while ($pending.Count -gt 0) {
        $current = $pending[$pending.Count - 1]
        $pending.RemoveAt($pending.Count - 1)
        $entry = & $Native.Probe $current
        if (-not $entry.Exists) { continue }
        # Root junctions are allowed, but no reparse point anywhere in a cleanup tree is followed.
        if ($entry.IsReparse) { throw 'Program cleanup contains a reparse point' }
        $canonical = Resolve-Canonical $current $Native
        if (-not $canonical.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Program cleanup escapes the canonical installation root'
        }
        if ($entry.IsDirectory) {
            if (-not $AllowDirectory) { throw 'Program file cleanup unexpectedly targets a directory' }
            foreach ($child in @(& $Native.Children $current)) { $pending.Add($child) }
        }
    }
}

function Assert-ProgramCleanup([hashtable]$Context, [string]$Root, [hashtable]$Native) {
    $entry = & $Native.Probe $Root
    if ($entry.Exists -and -not $entry.IsDirectory) { throw 'Installation target is not a directory' }
    $children = @()
    if ($entry.Exists) { $children = @(& $Native.Children $Root) }
    foreach ($relative in @(Get-CleanupTargets $Context)) {
        if ($relative.Contains('*')) {
            foreach ($child in $children) {
                if ([IO.Path]::GetFileName($child) -like $relative) { Assert-CleanupTree $child $Root $Native $false }
            }
        } else {
            Assert-CleanupTree ([IO.Path]::Combine($Root, $relative)) $Root $Native ($relative -in @('resources', 'locales'))
        }
    }
}

function Assert-CleanupTarget([string]$Target, [hashtable]$Context, [hashtable]$Native, [string]$ExpectedRoot = '') {
    if ($Target -notin @(Get-CleanupTargets $Context)) { throw 'Unapproved cleanup target' }
    $fresh = Test-InstallIsolation $Context $Native
    if ($ExpectedRoot -and -not [string]::Equals($fresh.Root, $ExpectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Canonical installation root changed before cleanup'
    }
}

function Test-InstallIsolation([hashtable]$Context, [hashtable]$Native) {
    $root = Resolve-Canonical $Context.InstallDir $Native
    if ([string]::Equals($root, [IO.Path]::GetPathRoot($root), [StringComparison]::OrdinalIgnoreCase)) { throw 'Installation at a filesystem root is forbidden' }
    $records = @(Get-IsolationRecords $Context $Native $root)
    foreach ($record in $records) {
        foreach ($path in $record.Paths) {
            if ($record.Own) {
                if (-not [string]::Equals($path, $root, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Own-channel directory migration is not supported; the legacy uninstaller will not be run'
                }
            } elseif (Test-PathOverlap $root $path) {
                throw 'Channel installation directories overlap (same, parent, child or junction)'
            }
        }
    }
    foreach ($relative in @($Context.OtherMainExe, ('resources\backend\' + $Context.OtherServiceExe))) {
        if ((& $Native.Probe ([IO.Path]::Combine($root, $relative))).Exists) { throw 'Target contains an other channel executable' }
    }
    Assert-ProgramCleanup $Context $root $Native
    return [pscustomobject]@{ Root = $root; Records = $records }
}

function Stop-OwnedProcesses([hashtable]$Context, $Evidence, [hashtable]$Native) {
    $expected = @(
        (Resolve-Canonical ([IO.Path]::Combine($Evidence.Root, $Context.OwnMainExe)) $Native),
        (Resolve-Canonical ([IO.Path]::Combine($Evidence.Root, 'resources\backend', $Context.OwnServiceExe)) $Native)
    )
    $handles = [Collections.Generic.List[object]]::new()
    $owned = [Collections.Generic.List[object]]::new()
    try {
        # Snapshot names are ONLY a suspicion filter, never authority to terminate.
        foreach ($candidate in @(& $Native.Processes)) {
            if ($candidate.Name -notin @($Context.OwnMainExe, $Context.OwnServiceExe)) { continue }
            $handle = & $Native.OpenProcess $candidate.Id
            $handles.Add($handle)
            $image = Resolve-Canonical (& $Native.QueryImage $handle) $Native
            foreach ($path in $expected) {
                if ([string]::Equals($image, $path, [StringComparison]::OrdinalIgnoreCase)) {
                    $owned.Add($handle)
                    break
                }
            }
        }
        # Verify every suspected target before making any destructive call.
        foreach ($handle in $owned) {
            & $Native.Terminate $handle
            & $Native.Wait $handle
        }
    } finally {
        $closeError = $null
        foreach ($handle in $handles) {
            try { & $Native.Close $handle } catch { if ($null -eq $closeError) { $closeError = $_ } }
        }
        if ($null -ne $closeError) { throw $closeError }
    }
}

function Forget-OwnedUninstall([hashtable]$Context, $Evidence, [hashtable]$Native) {
    if (-not [string]::Equals((Resolve-Canonical $Context.InstallDir $Native), $Evidence.Root, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Canonical installation root changed before Forget'
    }
    $records = @{}
    foreach ($record in $Evidence.Records) {
        if (-not $record.Own -or $record.Kind -ne 'Uninstall' -or $record.Key -cne $Context.OwnUninstallKey) { continue }
        foreach ($path in $record.Paths) {
            if (-not [string]::Equals($path, $Evidence.Root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unverified own uninstall record' }
        }
        # HKCU uninstall views share a key; HKLM views do not.
        $physicalKey = "$($record.Hive)/$($record.Key)"
        if ($record.Hive -ne 'HKCU') { $physicalKey = "$($record.Hive)/$($record.View)/$($record.Key)" }
        if ($records.ContainsKey($physicalKey)) {
            Assert-RegistryValuesEqual $records[$physicalKey].Values $record.Values
        } else {
            $records[$physicalKey] = $record
        }
    }
    foreach ($record in $records.Values) {
        if ($record.Values.ContainsKey('UninstallString') -or $record.Values.ContainsKey('QuietUninstallString')) {
            & $Native.ForgetRecord $record
        }
    }
}

function Assert-RegistryValuesEqual([hashtable]$Expected, [hashtable]$Actual) {
    if ($Expected.Count -ne $Actual.Count) { throw 'Registry changed after Check' }
    foreach ($name in $Expected.Keys) {
        if (-not $Actual.ContainsKey($name) -or $Actual[$name] -cne $Expected[$name]) { throw 'Registry changed after Check' }
    }
}

function Read-RegistryValues($Key) {
    $values = @{}
    $names = @($Key.GetValueNames())
    foreach ($name in @('InstallLocation', 'UninstallString', 'QuietUninstallString')) {
        if ($names -notcontains $name) { continue }
        $kind = $Key.GetValueKind($name)
        if ($kind -ne [Microsoft.Win32.RegistryValueKind]::String -and $kind -ne [Microsoft.Win32.RegistryValueKind]::ExpandString) {
            throw 'Unsupported installation registry value type'
        }
        $value = $Key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($value -isnot [string]) { throw 'Invalid installation registry value' }
        $values[$name] = $value
        $values['Kind:' + $name] = $kind.ToString()
    }
    return $values
}

function Open-RegistryBase([string]$Hive, [string]$View) {
    $registryHive = switch ($Hive) {
        'HKCU' { [Microsoft.Win32.RegistryHive]::CurrentUser }
        'HKLM' { [Microsoft.Win32.RegistryHive]::LocalMachine }
        default { throw 'Unsupported registry hive' }
    }
    $registryView = switch ($View) {
        '32' { [Microsoft.Win32.RegistryView]::Registry32 }
        '64' { [Microsoft.Win32.RegistryView]::Registry64 }
        default { throw 'Unsupported registry view' }
    }
    return [Microsoft.Win32.RegistryKey]::OpenBaseKey($registryHive, $registryView)
}

function New-WindowsBoundary {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Installer guard requires Windows' }
    if (-not ('SpiderInstaller.Native' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace SpiderInstaller {
    public sealed class PathInfo {
        public bool Exists, IsDirectory, IsReparse;
        public string Canonical;
    }
    public sealed class Candidate {
        public uint Id;
        public string Name;
    }
    public static class Native {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct ProcessEntry {
            public uint dwSize, cntUsage, th32ProcessID;
            public UIntPtr th32DefaultHeapID;
            public uint th32ModuleID, cntThreads, th32ParentProcessID;
            public int pcPriClassBase;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string szExeFile;
        }
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern SafeFileHandle CreateToolhelp32Snapshot(uint flags, uint processId);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool Process32FirstW(SafeFileHandle snapshot, ref ProcessEntry entry);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool Process32NextW(SafeFileHandle snapshot, ref ProcessEntry entry);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint processId);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool QueryFullProcessImageNameW(IntPtr handle, uint flags, StringBuilder path, ref uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool TerminateProcess(IntPtr handle, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint GetFileAttributesW(string path);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
        static Exception Error(string operation) {
            int code = Marshal.GetLastWin32Error();
            return new Win32Exception(code, operation + " failed (Win32 " + code + ")");
        }
        public static Candidate[] Processes() {
            var result = new List<Candidate>();
            using (SafeFileHandle snapshot = CreateToolhelp32Snapshot(2, 0)) {
                if (snapshot.IsInvalid) throw Error("CreateToolhelp32Snapshot");
                var entry = new ProcessEntry { dwSize = (uint)Marshal.SizeOf(typeof(ProcessEntry)) };
                if (!Process32FirstW(snapshot, ref entry)) throw Error("Process32First");
                do { result.Add(new Candidate { Id = entry.th32ProcessID, Name = entry.szExeFile }); }
                while (Process32NextW(snapshot, ref entry));
                int error = Marshal.GetLastWin32Error();
                if (error != 18) throw new Win32Exception(error, "Process32Next failed (Win32 " + error + ")");
            }
            return result.ToArray();
        }
        public static IntPtr OpenCandidate(uint processId) {
            // QUERY_LIMITED_INFORMATION | SYNCHRONIZE | TERMINATE. No debug privilege or ACL repair.
            IntPtr handle = OpenProcess(0x1000 | 0x100000 | 1, false, processId);
            if (handle == IntPtr.Zero) throw Error("OpenProcess");
            return handle;
        }
        public static string QueryImage(IntPtr handle) {
            var path = new StringBuilder(32768);
            uint length = (uint)path.Capacity;
            if (!QueryFullProcessImageNameW(handle, 0, path, ref length)) throw Error("QueryFullProcessImageName");
            return path.ToString();
        }
        public static void Terminate(IntPtr handle) {
            if (!TerminateProcess(handle, 1)) throw Error("TerminateProcess");
        }
        public static void Wait(IntPtr handle) {
            uint result = WaitForSingleObject(handle, 5000);
            if (result == 0) return;
            if (result == UInt32.MaxValue) throw Error("WaitForSingleObject");
            throw new InvalidOperationException("WaitForSingleObject did not confirm process exit (timeout or unexpected result)");
        }
        public static void Close(IntPtr handle) {
            if (!CloseHandle(handle)) throw Error("CloseHandle");
        }
        public static PathInfo Probe(string path) {
            uint attributes = GetFileAttributesW(path);
            if (attributes == UInt32.MaxValue) {
                int code = Marshal.GetLastWin32Error();
                if (code == 2 || code == 3) return new PathInfo { Exists = false };
                throw new Win32Exception(code, "GetFileAttributes failed (Win32 " + code + ")");
            }
            // No OPEN_REPARSE_POINT: canonicalize through the junction using the SAME file handle.
            using (SafeFileHandle handle = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw Error("CreateFile"); // Includes dangling junctions; never pretend they are missing.
                StringBuilder buffer = new StringBuilder(32768);
                uint count = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
                if (count == 0) throw Error("GetFinalPathNameByHandle");
                if (count >= buffer.Capacity) throw new InvalidOperationException("Canonical path exceeds buffer");
                return new PathInfo { Exists = true, Canonical = buffer.ToString(), IsDirectory = (attributes & 0x10) != 0, IsReparse = (attributes & 0x400) != 0 };
            }
        }
    }
}
'@ | Out-Null
    }
    return @{
        Probe = { param($path) [SpiderInstaller.Native]::Probe($path) }
        Children = { param($path) [IO.Directory]::GetFileSystemEntries($path) }
        Processes = { [SpiderInstaller.Native]::Processes() }
        OpenProcess = { param($processId) [SpiderInstaller.Native]::OpenCandidate([uint32]$processId) }
        QueryImage = { param($handle) [SpiderInstaller.Native]::QueryImage($handle) }
        Terminate = { param($handle) [SpiderInstaller.Native]::Terminate($handle) }
        Wait = { param($handle) [SpiderInstaller.Native]::Wait($handle) }
        Close = { param($handle) [SpiderInstaller.Native]::Close($handle) }
        ReadRegistry = {
            param($hive, $view, $keyName)
            $base = Open-RegistryBase $hive $view
            try {
                $key = $base.OpenSubKey($keyName, $false)
                if ($null -eq $key) { return $null }
                try { return (Read-RegistryValues $key) } finally { $key.Dispose() }
            } finally { $base.Dispose() }
        }
        ForgetRecord = {
            param($record)
            $base = Open-RegistryBase $record.Hive $record.View
            try {
                $key = $base.OpenSubKey($record.Key, $true)
                if ($null -eq $key) { throw 'Registry changed after Check' }
                try {
                    $remaining = $record.Values.Clone()
                    foreach ($name in @('UninstallString', 'QuietUninstallString')) {
                        if (-not $remaining.ContainsKey($name)) { continue }
                        # Re-read on the same writable registry handle immediately before EACH delete.
                        Assert-RegistryValuesEqual $remaining (Read-RegistryValues $key)
                        $key.DeleteValue($name, $true)
                        $remaining.Remove($name)
                        $remaining.Remove('Kind:' + $name)
                    }
                } finally { $key.Dispose() }
            } finally { $base.Dispose() }
        }
        ReadApproval = {
            param($path)
            try { return (ConvertFrom-Json ([IO.File]::ReadAllText($path))) }
            catch [IO.FileNotFoundException] { return $null }
            catch [IO.DirectoryNotFoundException] { return $null }
        }
        WriteApproval = {
            param($path, $approval)
            [IO.File]::WriteAllText($path, (ConvertTo-Json $approval -Compress), [Text.UTF8Encoding]::new($false))
        }
    }
}

function Get-GuardContext {
    $context = @{}
    foreach ($mapping in @(
        @('SPIDER_CHANNEL', 'Channel'), @('SPIDER_INSTALL_DIR', 'InstallDir'),
        @('SPIDER_OWN_MAIN_EXE', 'OwnMainExe'), @('SPIDER_OTHER_MAIN_EXE', 'OtherMainExe'),
        @('SPIDER_OWN_SERVICE_EXE', 'OwnServiceExe'), @('SPIDER_OTHER_SERVICE_EXE', 'OtherServiceExe'),
        @('SPIDER_OWN_INSTALL_KEY', 'OwnInstallKey'), @('SPIDER_OTHER_INSTALL_KEY', 'OtherInstallKey'),
        @('SPIDER_OWN_UNINSTALL_KEY', 'OwnUninstallKey'), @('SPIDER_OTHER_UNINSTALL_KEY', 'OtherUninstallKey'),
        @('SPIDER_UNINSTALL_FILENAME', 'UninstallFilename'), @('SPIDER_GUARD_STATE', 'StatePath')
    )) {
        $value = [Environment]::GetEnvironmentVariable($mapping[0], 'Process')
        if ([string]::IsNullOrWhiteSpace($value)) { throw ('Missing guard identity/environment: ' + $mapping[0]) }
        $context[$mapping[1]] = $value
    }
    if ($context.Channel -notin @('stable', 'beta')) { throw 'Invalid channel' }
    foreach ($name in @('OwnMainExe', 'OtherMainExe', 'OwnServiceExe', 'OtherServiceExe', 'UninstallFilename')) {
        if ($context[$name] -notmatch '^[^\\/:*?"<>|\x00-\x1f]+\.exe$') { throw 'Invalid executable identity' }
    }
    foreach ($name in @('OwnInstallKey', 'OtherInstallKey', 'OwnUninstallKey', 'OtherUninstallKey')) {
        if ($context[$name] -notmatch '^Software\\[^/\x00-\x1f]+$') { throw 'Invalid registry identity' }
    }
    if ($context.OwnMainExe -eq $context.OtherMainExe -or $context.OwnServiceExe -eq $context.OtherServiceExe -or
        $context.OwnInstallKey -eq $context.OtherInstallKey -or $context.OwnUninstallKey -eq $context.OtherUninstallKey) {
        throw 'Channel identities must be distinct'
    }
    $context.Arguments = [Environment]::GetEnvironmentVariable('SPIDER_GUARD_ARGUMENTS', 'Process')
    if ($null -eq $context.Arguments) { throw 'Missing guard arguments environment' }
    $context.InstallDir = ConvertTo-AbsolutePath $context.InstallDir
    $context.StatePath = ConvertTo-AbsolutePath $context.StatePath
    return $context
}

function Invoke-InstallerGuard([string]$Action, [hashtable]$Context, [hashtable]$Native, [string]$CleanupTarget = '') {
    if ($Action -notin @('Check', 'Stop', 'Forget', 'CheckCleanup')) { throw 'Unsupported installer guard action' }
    if ([string]$Context.Arguments -match '(?i)--delete-app-data') { throw 'Installer isolation must preserve user data' }
    $evidence = Test-InstallIsolation $Context $Native
    $approval = & $Native.ReadApproval $Context.StatePath
    if ($null -eq $approval) {
        if ($Action -ne 'Check') { throw 'Check approval is required before mutation' }
        $ownKeys = @($evidence.Records | Where-Object { $_.Own -and $_.Kind -eq 'Uninstall' } |
            ForEach-Object { "$($_.Hive)/$($_.View)/$($_.Key)" })
        & $Native.WriteApproval $Context.StatePath ([pscustomobject]@{
            Version = 1; Channel = $Context.Channel; Root = $evidence.Root; OwnUninstallKeys = $ownKeys
        })
    } elseif ($approval.Version -ne 1 -or $approval.Channel -cne $Context.Channel -or
        -not [string]::Equals($approval.Root, $evidence.Root, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Canonical installation root changed since first Check'
    }
    switch ($Action) {
        'Check' { return }
        'Stop' { Stop-OwnedProcesses $Context $evidence $Native; return }
        'Forget' { Forget-OwnedUninstall $Context $evidence $Native; return }
        'CheckCleanup' { Assert-CleanupTarget $CleanupTarget $Context $Native $evidence.Root; return }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $context = Get-GuardContext
        $action = [Environment]::GetEnvironmentVariable('SPIDER_GUARD_ACTION', 'Process')
        $target = [Environment]::GetEnvironmentVariable('SPIDER_CLEANUP_TARGET', 'Process')
        Invoke-InstallerGuard $action $context (New-WindowsBoundary) $target
        exit 0
    } catch {
        [Console]::Error.WriteLine('Installer isolation refused: ' + $_.Exception.Message)
        exit 2
    }
}
