# Repair integrity labels left behind by the agent sandbox.
#
# The sandbox labels the project tree "Low" (mandatory integrity level) so the
# confined agent can write to it. Any .exe that carries a Low label is launched
# at Low integrity by Windows, and such a process cannot delete or write normal
# Medium-integrity files in %TEMP% or %APPDATA%. Symptoms:
#
#   * electron-vite dev / build dies with:
#       [vite:esbuild-transpile] remove C:\...\Temp\esbuild-...: Access is denied.
#   * Electron exits immediately after "start electron app..." (no window).
#   * backend / updater code fails with EACCES on %APPDATA% paths.
#
# This script resets every directory and every .exe/.com file under the project
# to a plain Medium label, which makes newly launched processes normal again.
# New files inherit from their (now Medium) parent directories, so a reinstall
# of node_modules will not reintroduce the problem until the sandbox labels the
# tree again. Re-run after any agent session that used the sandbox.
#
# Usage (execution policy blocks .ps1 by default):
#   powershell -ExecutionPolicy Bypass -File tools\fix_integrity_labels.ps1
#   powershell -ExecutionPolicy Bypass -File tools\fix_integrity_labels.ps1 -WhatIf

[CmdletBinding()]
param(
  [string]$Root = '',
  [switch]$WhatIf
)

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SpiderGdLabelFix {
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern uint SetNamedSecurityInfo(string name, int type, uint info, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string sddl, uint rev, out IntPtr sd, out uint size);
  [DllImport("advapi32.dll")]
  static extern bool GetSecurityDescriptorSacl(IntPtr sd, out bool present, out IntPtr sacl, out bool def);
  [DllImport("kernel32.dll")]
  static extern IntPtr LocalFree(IntPtr p);

  const uint LABEL_SECURITY_INFORMATION = 0x00000010;
  const int SE_FILE_OBJECT = 1;

  // inheritable=true -> "S:(ML;OICI;NW;;;ME)" (directory + inheritable)
  // inheritable=false -> "S:(ML;;NW;;;ME)"     (single file)
  public static int SetMedium(string path, bool inheritable) {
    string sddl = inheritable ? "S:(ML;OICI;NW;;;ME)" : "S:(ML;;NW;;;ME)";
    IntPtr sd; uint size;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1, out sd, out size)) return -1;
    try {
      bool present, def; IntPtr sacl;
      if (!GetSecurityDescriptorSacl(sd, out present, out sacl, out def)) return -2;
      return (int)SetNamedSecurityInfo(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION,
                                       IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, sacl);
    } finally { LocalFree(sd); }
  }
}
'@

if (-not $Root) {
  # $PSScriptRoot is not always populated in the param() default block.
  $Root = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path -LiteralPath $Root)) {
  throw "Root not found: $Root"
}
$Root = (Resolve-Path -LiteralPath $Root).Path
Write-Host "Scanning: $Root"

$dirs = New-Object System.Collections.Generic.List[string]
$exes = New-Object System.Collections.Generic.List[string]
$stack = New-Object System.Collections.Generic.Stack[string]
$stack.Push($Root)

while ($stack.Count -gt 0) {
  $current = $stack.Pop()
  $dirs.Add($current)
  try {
    foreach ($entry in [System.IO.Directory]::EnumerateFileSystemEntries($current)) {
      try {
        $attrs = [System.IO.File]::GetAttributes($entry)
        # Skip symlinks / junctions: they point at the same content twice.
        if ($attrs -band [System.IO.FileAttributes]::ReparsePoint) { continue }
        if ($attrs -band [System.IO.FileAttributes]::Directory) { $stack.Push($entry) }
        elseif ($entry -match '\.(exe|com)$') { $exes.Add($entry) }
      } catch { }
    }
  } catch { }
}

Write-Host ("directories: {0}, executables: {1}" -f $dirs.Count, $exes.Count)
if ($WhatIf) {
  Write-Host "WhatIf: nothing changed."
  return
}

$failed = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()
foreach ($d in $dirs) {
  if ([SpiderGdLabelFix]::SetMedium($d, $true) -ne 0) { $failed++ }
}
foreach ($f in $exes) {
  if ([SpiderGdLabelFix]::SetMedium($f, $false) -ne 0) { $failed++ }
}
$sw.Stop()

Write-Host ("done in {0:n1}s, failed (access denied / not owner): {1}" -f $sw.Elapsed.TotalSeconds, $failed)
if ($failed -gt 0) {
  Write-Host "Failed entries are usually sandbox-owned scratch dirs (shared-chrome-*, build/tmp); they do not affect dev."
}
Write-Host "Verify with: icacls `"$Root`" | findstr /i mandatory"
