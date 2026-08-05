# The work the Windows installer does, with no UI in it.
#
# Dot-sourced by Install.ps1 (which supplies a log sink and a UI pump) and
# usable headless by anything else that dot-sources it and calls the steps.
# Nothing here writes outside the clone except the shortcuts, which are the
# whole point.

Set-StrictMode -Version Latest

$script:MinPython = [version]'3.12'
$script:MinNode   = [version]'20.0'

# Pinned fallbacks, used only when winget is unavailable or fails. Bump these
# when they age out; winget is the normal path and needs no maintenance.
$script:PyFallbackUrl   = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$script:NodeFallbackUrl = 'https://nodejs.org/dist/v22.20.0/node-v22.20.0-x64.msi'

# Replaced by the GUI. Defaults keep the module usable from a plain console.
$script:LogSink = { param($Message, $Kind) Write-Host $Message }
$script:PumpUi  = { }
$script:Cancel  = $false

function Write-Log { param([string]$Message, [string]$Kind = 'info') & $script:LogSink $Message $Kind }
function Write-Step { param([string]$Message) Write-Log $Message 'step' }
function Invoke-Pump { & $script:PumpUi }
function Test-Cancelled { if ($script:Cancel) { throw 'Cancelled.' } }

# ---------------------------------------------------------------- process I/O

# Start-Process joins -ArgumentList with plain spaces and quotes nothing, so a
# path with a space silently becomes two arguments. Build the command line by
# Windows' own rules instead and hand over one finished string.
function ConvertTo-CmdArg([string]$Value) {
  if ($Value -eq '') { return '""' }
  if ($Value -notmatch '[\s"]') { return $Value }
  $s = $Value -replace '(\\*)"', '$1$1\"'
  $s = $s -replace '(\\+)$', '$1$1'
  return '"' + $s + '"'
}

function Join-CmdArgs([string[]]$Values) {
  ($Values | ForEach-Object { ConvertTo-CmdArg $_ }) -join ' '
}

# A growing-file tail. The child writes to a temp file and we read whatever has
# landed so far; that keeps the UI thread free without threads or runspaces,
# and unlike StreamReader.ReadLine it cannot split a half-written line in two.
function New-Tail([string]$Path) {
  $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
          [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
  [pscustomobject]@{ Reader = New-Object System.IO.StreamReader($fs); Buffer = '' }
}

function Read-Tail($Tail, [switch]$Final) {
  $chunk = $Tail.Reader.ReadToEnd()
  if ($chunk) { $Tail.Buffer += $chunk }
  while (($i = $Tail.Buffer.IndexOf("`n")) -ge 0) {
    $line = $Tail.Buffer.Substring(0, $i).TrimEnd("`r")
    $Tail.Buffer = $Tail.Buffer.Substring($i + 1)
    if ($line.Trim()) { Write-Log $line 'proc' }
    Invoke-Pump
  }
  if ($Final -and $Tail.Buffer.Trim()) {
    Write-Log $Tail.Buffer.Trim() 'proc'
    $Tail.Buffer = ''
  }
}

function Stop-Tree([int]$ProcessId) {
  try { & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null } catch { }
}

<#
 .SYNOPSIS Run a child process, streaming its output into the log.
 .OUTPUTS  The exit code.
#>
function Invoke-Proc {
  param(
    [Parameter(Mandatory)][string]$FilePath,
    [string[]]$Arguments = @(),
    [string]$WorkingDirectory,
    [int]$TimeoutSec = 3600,
    [switch]$IgnoreExitCode
  )
  Test-Cancelled
  $outFile = [System.IO.Path]::GetTempFileName()
  $errFile = [System.IO.Path]::GetTempFileName()
  $params = @{
    FilePath = $FilePath; NoNewWindow = $true; PassThru = $true
    RedirectStandardOutput = $outFile; RedirectStandardError = $errFile
  }
  if ($Arguments.Count)  { $params.ArgumentList = (Join-CmdArgs $Arguments) }
  if ($WorkingDirectory) { $params.WorkingDirectory = $WorkingDirectory }

  $proc = Start-Process @params
  # Touching .Handle caches the process handle. Without it Windows closes the
  # handle at exit and .ExitCode reads back as null, so every command looks
  # like it failed with a blank exit code.
  $null = $proc.Handle
  $tails = @((New-Tail $outFile), (New-Tail $errFile))
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  try {
    while (-not $proc.HasExited) {
      foreach ($t in $tails) { Read-Tail $t }
      Invoke-Pump
      if ($script:Cancel) { Stop-Tree $proc.Id; throw 'Cancelled.' }
      if ((Get-Date) -gt $deadline) {
        Stop-Tree $proc.Id
        throw "$(Split-Path $FilePath -Leaf) exceeded $TimeoutSec s and was stopped."
      }
      Start-Sleep -Milliseconds 80
    }
    $proc.WaitForExit()
    foreach ($t in $tails) { Read-Tail $t -Final }
  } finally {
    foreach ($t in $tails) { try { $t.Reader.Dispose() } catch { } }
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
  }
  $code = $proc.ExitCode
  if ($code -ne 0 -and -not $IgnoreExitCode) {
    throw "$(Split-Path $FilePath -Leaf) failed (exit $code)."
  }
  return $code
}

# Captures output instead of logging it. For fast version probes only.
function Invoke-Capture {
  param([string]$FilePath, [string[]]$Arguments = @(), [int]$TimeoutSec = 20)
  $outFile = [System.IO.Path]::GetTempFileName()
  $errFile = [System.IO.Path]::GetTempFileName()
  try {
    $params = @{
      FilePath = $FilePath; NoNewWindow = $true; PassThru = $true
      RedirectStandardOutput = $outFile; RedirectStandardError = $errFile
    }
    if ($Arguments.Count) { $params.ArgumentList = (Join-CmdArgs $Arguments) }
    $proc = Start-Process @params
    $null = $proc.Handle   # see Invoke-Proc: required for .ExitCode to survive
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) { Stop-Tree $proc.Id; return $null }
    if ($proc.ExitCode -ne 0) { return $null }
    return (Get-Content $outFile -Raw -ErrorAction SilentlyContinue)
  } catch {
    return $null
  } finally {
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
  }
}

function Update-ProcessPath {
  $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

# ------------------------------------------------------------------ detection

<#
 .SYNOPSIS Locate a usable Python, newest first.
 .DESCRIPTION Skips the Microsoft Store alias stubs in WindowsApps: they are
   zero-byte reparse points that print nothing and open the Store, and treating
   one as a real interpreter is the classic "python is installed but nothing
   works" failure.
#>
function Find-Python {
  $found = @()
  $seen  = @{}

  $probe = {
    param($Exe, $Pre)
    $key = "$Exe|$($Pre -join ' ')"
    if ($seen.ContainsKey($key)) { return }
    $seen[$key] = $true
    $out = Invoke-Capture -FilePath $Exe -Arguments (@($Pre) + @(
      '-c', 'import sys;print("%d.%d.%d" % sys.version_info[:3])'))
    if (-not $out) { return }
    $text = $out.Trim()
    if ($text -notmatch '^\d+\.\d+\.\d+$') { return }
    $script:__pyFound += [pscustomobject]@{
      Exe = $Exe; Pre = @($Pre); Version = [version]$text
    }
  }
  $script:__pyFound = @()

  # The py launcher is the most reliable selector when several are installed.
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in '3.13', '3.12', '3') { & $probe 'py' @("-$v") }
  }
  foreach ($name in 'python', 'python3') {
    # -CommandType Application: guarantees an ApplicationInfo, which is the
    # only Get-Command result that actually carries .Source.
    foreach ($cmd in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
      $p = $cmd.Source
      if (-not $p) { continue }
      if ($p -like '*\WindowsApps\*') { continue }   # Store alias stub
      & $probe $p @()
    }
  }
  foreach ($glob in @(
      "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
      "$env:ProgramFiles\Python3*\python.exe",
      "${env:ProgramFiles(x86)}\Python3*\python.exe",
      "$env:SystemDrive\Python3*\python.exe")) {
    foreach ($p in @(Get-ChildItem $glob -ErrorAction SilentlyContinue)) {
      & $probe $p.FullName @()
    }
  }

  $found = $script:__pyFound
  Remove-Variable -Name __pyFound -Scope Script -ErrorAction SilentlyContinue
  if (-not $found) { return $null }
  $best = $found | Sort-Object Version -Descending | Select-Object -First 1
  if ($best.Version -lt $script:MinPython) { return $null }
  return $best
}

function Find-Node {
  $cands = @()
  foreach ($cmd in @(Get-Command node -All -CommandType Application -ErrorAction SilentlyContinue)) {
    if ($cmd.Source) { $cands += $cmd.Source }
  }
  $cands += @("$env:ProgramFiles\nodejs\node.exe",
              "$env:LOCALAPPDATA\Programs\nodejs\node.exe",
              "$env:APPDATA\npm\node.exe")
  foreach ($p in ($cands | Select-Object -Unique)) {
    if (-not (Test-Path $p)) { continue }
    $out = Invoke-Capture -FilePath $p -Arguments @('--version')
    if (-not $out) { continue }
    if ($out.Trim() -notmatch '^v(\d+)\.(\d+)\.') { continue }
    $ver = [version]"$($Matches[1]).$($Matches[2])"
    if ($ver -lt $script:MinNode) { continue }
    # npm ships beside node; the .cmd shim is what we actually invoke.
    $npm = Join-Path (Split-Path $p) 'npm.cmd'
    if (-not (Test-Path $npm)) { continue }
    return [pscustomobject]@{ Exe = $p; Npm = $npm; Version = $ver }
  }
  return $null
}

function Get-Prerequisites {
  Update-ProcessPath
  [pscustomobject]@{
    Python = Find-Python
    Node   = Find-Node
    Git    = (Get-Command git -ErrorAction SilentlyContinue) -ne $null
    Winget = (Get-Command winget -ErrorAction SilentlyContinue) -ne $null
  }
}

# --------------------------------------------------------------- installation

function Get-RemoteFile([string]$Url, [string]$Destination) {
  Write-Log "downloading $Url"
  # PowerShell 5.1 still defaults to TLS 1.0 against some hosts.
  [Net.ServicePointManager]::SecurityProtocol =
    [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
  $prev = $ProgressPreference
  $ProgressPreference = 'SilentlyContinue'   # the progress bar is ~10x slower
  try {
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 600
  } finally { $ProgressPreference = $prev }
  if (-not (Test-Path $Destination)) { throw "download produced no file: $Url" }
}

function Install-WithWinget([string]$PackageId, [switch]$UserScope) {
  $base = @('install', '--id', $PackageId, '-e', '--source', 'winget',
            '--silent', '--accept-package-agreements', '--accept-source-agreements',
            '--disable-interactivity')
  if ($UserScope) {
    # Not every package publishes a user-scope installer; fall through if so.
    $code = Invoke-Proc -FilePath 'winget.exe' -Arguments ($base + @('--scope', 'user')) `
                        -TimeoutSec 1800 -IgnoreExitCode
    if ($code -eq 0) { return $true }
    Write-Log 'user-scope install unavailable, retrying machine scope'
  }
  $code = Invoke-Proc -FilePath 'winget.exe' -Arguments $base -TimeoutSec 1800 -IgnoreExitCode
  return ($code -eq 0)
}

function Install-Python {
  Write-Step 'Installing Python 3.12'
  $ok = $false
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    $ok = Install-WithWinget -PackageId 'Python.Python.3.12' -UserScope
    if (-not $ok) { Write-Log 'winget could not install Python; falling back to python.org' 'warn' }
  }
  if (-not $ok) {
    $exe = Join-Path $env:TEMP 'grindstone-python-setup.exe'
    Get-RemoteFile $script:PyFallbackUrl $exe
    Write-Log 'running the python.org installer (per-user, adds itself to PATH)'
    # Per-user keeps this UAC-free; PrependPath is what makes `python` resolve
    # in the shell the user opens next.
    Invoke-Proc -FilePath $exe -Arguments @(
      '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1',
      'Include_test=0', 'SimpleInstall=1') -TimeoutSec 1800 | Out-Null
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
  }
  Update-ProcessPath
  $py = Find-Python
  if (-not $py) {
    throw 'Python installed but is still not on PATH. Close this window, sign out and back in, then run the installer again.'
  }
  Write-Log "Python $($py.Version) ready"
  return $py
}

function Install-Node {
  Write-Step 'Installing Node.js LTS'
  $ok = $false
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    # Node's MSI is machine-scope only, so Windows will raise a UAC prompt here.
    $ok = Install-WithWinget -PackageId 'OpenJS.NodeJS.LTS'
    if (-not $ok) { Write-Log 'winget could not install Node; falling back to nodejs.org' 'warn' }
  }
  if (-not $ok) {
    $msi = Join-Path $env:TEMP 'grindstone-node-setup.msi'
    Get-RemoteFile $script:NodeFallbackUrl $msi
    Write-Log 'running the nodejs.org installer (needs administrator approval)'
    Invoke-Proc -FilePath 'msiexec.exe' -Arguments @('/i', $msi, '/qn', '/norestart') `
                -TimeoutSec 1800 | Out-Null
    Remove-Item $msi -Force -ErrorAction SilentlyContinue
  }
  Update-ProcessPath
  $node = Find-Node
  if (-not $node) {
    throw 'Node installed but is still not on PATH. Close this window, sign out and back in, then run the installer again.'
  }
  Write-Log "Node $($node.Version) ready"
  return $node
}

# ------------------------------------------------------------------ the clone

function Get-VenvPython([string]$Root) { Join-Path $Root '.venv\Scripts\python.exe' }

function Install-PythonEnvironment {
  param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Python)
  $venvPy = Get-VenvPython $Root
  if (-not (Test-Path $venvPy)) {
    Write-Step 'Creating the virtual environment'
    Invoke-Proc -FilePath $Python.Exe -Arguments (@($Python.Pre) + @('-m', 'venv', (Join-Path $Root '.venv'))) `
                -TimeoutSec 600 | Out-Null
  } else {
    Write-Log 'virtual environment already present'
  }
  if (-not (Test-Path $venvPy)) { throw "venv creation did not produce $venvPy" }

  Write-Step 'Installing Python dependencies'
  Invoke-Proc -FilePath $venvPy -Arguments @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '-q',
    '-r', (Join-Path $Root 'requirements.txt')) -TimeoutSec 3600 | Out-Null
  Write-Log 'Python dependencies installed'
}

function Install-FrontendDependencies {
  param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Node)
  $appDir = Join-Path $Root 'code\app'
  Write-Step 'Installing frontend dependencies'
  Invoke-Proc -FilePath $Node.Npm -Arguments @('install', '--no-fund', '--no-audit') `
              -WorkingDirectory $appDir -TimeoutSec 3600 | Out-Null

  # Electron's postinstall can silently skip the binary download; without this
  # the app later dies with an opaque spawn ENOENT (fresh-clone test).
  $electron = Join-Path $appDir 'node_modules\electron\dist\electron.exe'
  if (-not (Test-Path $electron)) {
    Write-Log 'Electron binary missing - fetching it now'
    Invoke-Proc -FilePath $Node.Exe -Arguments @('node_modules\electron\install.js') `
                -WorkingDirectory $appDir -TimeoutSec 1800 | Out-Null
  }
  if (-not (Test-Path $electron)) { throw 'Electron binary could not be downloaded.' }
  Write-Log 'frontend dependencies installed'
}

function Build-App {
  param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Node)
  # `npm run start` is electron-vite PREVIEW: it serves out/, it does not
  # create it. Without this step a genuinely fresh clone has no out/ and the
  # shortcut opens an Electron process with nothing to load.
  Write-Step 'Building the app'
  Invoke-Proc -FilePath $Node.Npm -Arguments @('run', 'build') `
              -WorkingDirectory (Join-Path $Root 'code\app') -TimeoutSec 1800 | Out-Null
  $entry = Join-Path $Root 'code\app\out\main\index.js'
  if (-not (Test-Path $entry)) { throw "build finished but $entry is missing" }
  Write-Log 'build complete'
}

function Invoke-Gate {
  param([Parameter(Mandatory)][string]$Root)
  Write-Step 'Running the verification gate'
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    # The secret scan shells out to `git ls-files`; a ZIP download has no repo.
    Write-Log 'git not found - the gate needs it to scan tracked files. Skipping.' 'warn'
    return $false
  }
  $code = Invoke-Proc -FilePath (Get-VenvPython $Root) -Arguments @('selftest.py') `
                      -WorkingDirectory (Join-Path $Root 'code') -TimeoutSec 900 -IgnoreExitCode
  if ($code -ne 0) { throw 'The verification gate FAILED - see the FAIL line above.' }
  Write-Log 'gate passed'
  return $true
}

# ----------------------------------------------------------------- shortcuts

function Get-LaunchTarget([string]$Root) {
  # Target the Electron binary itself rather than npm: no console window, no
  # node wrapper process, and the taskbar button belongs to the app.
  [pscustomobject]@{
    Exe     = Join-Path $Root 'code\app\node_modules\electron\dist\electron.exe'
    Args    = '.'
    WorkDir = Join-Path $Root 'code\app'
    Icon    = Join-Path $Root 'code\assets\branding\app.ico'
  }
}

function New-Shortcut {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)]$Target,
    [string]$Description = 'Grindstone Investments'
  )
  $dir = Split-Path $Path -Parent
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  $shell = New-Object -ComObject WScript.Shell
  try {
    $sc = $shell.CreateShortcut($Path)
    $sc.TargetPath       = $Target.Exe
    $sc.Arguments        = $Target.Args
    $sc.WorkingDirectory = $Target.WorkDir
    $sc.Description      = $Description
    if (Test-Path $Target.Icon) { $sc.IconLocation = "$($Target.Icon),0" }
    $sc.Save()
  } finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
  }
  if (-not (Test-Path $Path)) { throw "shortcut was not created: $Path" }

  # Match the AppUserModelID the app sets at startup, so a pinned shortcut and
  # the running window are the same taskbar button rather than two.
  Set-ShortcutAppId -Path $Path -AppId 'com.grindstone.app'
  Write-Log "created $Path"
}

<#
 .SYNOPSIS Stamps System.AppUserModel.ID onto a .lnk.
 .DESCRIPTION The app calls setAppUserModelId('com.grindstone.app') at startup.
   If the shortcut does not carry the SAME id, Windows treats the shortcut and
   the running window as different apps and shows two taskbar buttons - one
   pinned and dead, one live. WScript.Shell cannot write the property store, so
   this goes through IShellLink's IPropertyStore directly.
#>
function Set-ShortcutAppId {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$AppId)
  if (-not ('Grindstone.ShortcutProps' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace Grindstone {
  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey {
    public Guid fmtid; public uint pid;
    public PropertyKey(Guid f, uint p) { fmtid = f; pid = p; }
  }

  // Minimal PROPVARIANT: only VT_LPWSTR is ever stored here. The union starts
  // at offset 8 on both x86 and x64 (2-byte vt + 6 bytes reserved).
  [StructLayout(LayoutKind.Explicit)]
  public sealed class PropVariant : IDisposable {
    [FieldOffset(0)] ushort vt;
    [FieldOffset(8)] IntPtr pointerValue;
    public PropVariant() { }
    public PropVariant(string value) {
      vt = 31; // VT_LPWSTR
      pointerValue = Marshal.StringToCoTaskMemUni(value);
    }
    public void Dispose() { PropVariantClear(this); GC.SuppressFinalize(this); }
    ~PropVariant() { PropVariantClear(this); }
    [DllImport("ole32.dll")]
    private static extern int PropVariantClear([In, Out] PropVariant pvar);
  }

  [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    void GetCount(out uint cProps);
    void GetAt(uint iProp, out PropertyKey pkey);
    void GetValue(ref PropertyKey key, [In, Out] PropVariant pv);
    void SetValue(ref PropertyKey key, [In] PropVariant pv);
    void Commit();
  }

  [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPersistFile {
    void GetClassID(out Guid pClassID);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, int dwMode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName,
              [MarshalAs(UnmanagedType.Bool)] bool fRemember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
  }

  [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
  public class ShellLink { }

  public static class ShortcutProps {
    // PKEY_AppUserModel_ID
    static readonly PropertyKey AppUserModelId = new PropertyKey(
      new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

    public static void SetAppId(string linkPath, string appId) {
      object link = new ShellLink();
      try {
        ((IPersistFile)link).Load(linkPath, 2 /* STGM_READWRITE */);
        IPropertyStore store = (IPropertyStore)link;
        using (PropVariant pv = new PropVariant(appId)) {
          PropertyKey key = AppUserModelId;
          store.SetValue(ref key, pv);
        }
        store.Commit();
        ((IPersistFile)link).Save(linkPath, true);
      } finally {
        Marshal.ReleaseComObject(link);
      }
    }
  }
}
'@
  }
  try {
    [Grindstone.ShortcutProps]::SetAppId($Path, $AppId)
  } catch {
    # Cosmetic only: the shortcut still launches, it just may not merge with
    # the running window's taskbar button.
    Write-Log "could not set the taskbar identity on $(Split-Path $Path -Leaf): $($_.Exception.Message)" 'warn'
  }
}

function Install-Shortcuts {
  param(
    [Parameter(Mandatory)][string]$Root,
    [switch]$Desktop, [switch]$StartMenu, [switch]$Taskbar
  )
  $target = Get-LaunchTarget $Root
  if (-not (Test-Path $target.Exe)) {
    throw "Electron binary not found at $($target.Exe) - dependency install did not complete."
  }
  $results = @{}

  $startMenuLnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Grindstone.lnk'
  # A taskbar pin needs a shortcut to pin, and the Start Menu is where it
  # belongs; create it whenever either option is on.
  if ($StartMenu -or $Taskbar) {
    Write-Step 'Creating the Start Menu shortcut'
    New-Shortcut -Path $startMenuLnk -Target $target
    $results.StartMenu = $true
  }
  if ($Desktop) {
    Write-Step 'Creating the desktop shortcut'
    New-Shortcut -Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Grindstone.lnk') -Target $target
    $results.Desktop = $true
  }
  if ($Taskbar) {
    Write-Step 'Pinning to the taskbar'
    $results.Taskbar = Add-TaskbarPin -ShortcutPath $startMenuLnk
  }
  return $results
}

<#
 .SYNOPSIS Best-effort taskbar pin.
 .DESCRIPTION Windows 10 1809 removed the "Pin to taskbar" verb from the shell
   automation surface and Windows 11 has never had it, specifically to stop
   installers doing this. We try the verb because it still works on older
   builds, and otherwise say so plainly rather than poking Taskband in the
   registry, which corrupts the taskbar as often as it works.
#>
function Add-TaskbarPin {
  param([Parameter(Mandatory)][string]$ShortcutPath)
  try {
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.Namespace((Split-Path $ShortcutPath -Parent))
    $item = $folder.ParseName((Split-Path $ShortcutPath -Leaf))
    $verb = $item.Verbs() | Where-Object { ($_.Name -replace '&', '') -match 'taskbar' } |
            Select-Object -First 1
    if ($verb) {
      $verb.DoIt()
      Start-Sleep -Milliseconds 400
      Write-Log 'pinned to the taskbar'
      return $true
    }
  } catch { }
  Write-Log 'Windows does not allow apps to pin to the taskbar on this version.' 'warn'
  Write-Log 'The Start Menu shortcut is ready - right-click it and choose "Pin to taskbar".' 'warn'
  return $false
}

function Start-App {
  param([Parameter(Mandatory)][string]$Root)
  $target = Get-LaunchTarget $Root
  Write-Step 'Starting Grindstone'
  Start-Process -FilePath $target.Exe -ArgumentList $target.Args -WorkingDirectory $target.WorkDir | Out-Null
}
