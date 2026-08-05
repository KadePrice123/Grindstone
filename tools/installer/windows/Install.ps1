<#
.SYNOPSIS
  Grindstone's Windows installer: a small window with the choices, then it does
  everything else.

.DESCRIPTION
  Normally reached by double-clicking Install.cmd in the repo root. It detects
  Python and Node, installs whichever are missing, builds the clone, runs the
  verification gate and creates the shortcuts you tick.

  WinForms is used deliberately: it is part of Windows itself, so the installer
  has no prerequisite of its own. Anything Python- or Node-based would need the
  very things it exists to install.

.PARAMETER NoUi
  Run every step with no window, for scripted or unattended installs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File Install.ps1 -NoUi
#>
[CmdletBinding()]
param(
  [switch]$NoUi,
  [switch]$NoShortcuts,
  [switch]$NoGate,
  [switch]$Taskbar,
  [switch]$Launch,
  [string]$Root
)

$ErrorActionPreference = 'Stop'

# Everything the installer prints also lands here, so a failed install can be
# diagnosed after the fact instead of from memory.
$script:LogFile = Join-Path $env:TEMP 'grindstone-setup.log'
function Write-LogFile([string]$Text) {
  try { Add-Content -Path $script:LogFile -Value $Text -Encoding UTF8 -ErrorAction Stop } catch { }
}
Write-LogFile "`r`n===== Grindstone setup $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="

# The window is launched hidden, so anything that fails BEFORE the form exists
# would otherwise vanish without a trace. Surface it in a message box instead.
trap {
  $where = if ($_.InvocationInfo) { " (line $($_.InvocationInfo.ScriptLineNumber))" } else { '' }
  Write-LogFile "FATAL$where : $($_.Exception.Message)"
  Write-LogFile ($_.ScriptStackTrace)
  if (-not $NoUi) {
    try {
      Add-Type -AssemblyName System.Windows.Forms
      [void][System.Windows.Forms.MessageBox]::Show(
        "$($_.Exception.Message)$where`n`nFull log: $script:LogFile",
        'Grindstone Setup could not start',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error)
    } catch { }
  }
  break
}

# Walk up to the clone root rather than counting directories: this file can be
# moved a level deeper without the installer silently pointing at the wrong tree.
if (-not $Root) {
  $dir = $PSScriptRoot
  while ($dir -and -not (Test-Path (Join-Path $dir 'requirements.txt'))) {
    $dir = Split-Path -Parent $dir
  }
  $Root = $dir
}
if (-not $Root -or -not (Test-Path (Join-Path $Root 'code\selftest.py'))) {
  throw "Could not find the Grindstone repo root above $PSScriptRoot. Install.cmd and the tools\ folder must stay inside the clone."
}
. (Join-Path $PSScriptRoot 'Steps.ps1')

$theme = (Get-Content (Join-Path $Root 'code\assets\branding\branding.json') -Raw |
          ConvertFrom-Json).theme
$IconFile = Join-Path $Root 'code\assets\branding\app.ico'

# ============================================================== headless mode
if ($NoUi) {
  $script:LogSink = {
    param($Message, $Kind)
    $prefix = switch ($Kind) { 'step' { '==> ' } 'warn' { 'warn: ' } default { '    ' } }
    Write-Host "$prefix$Message"
  }
  $pre = Get-Prerequisites
  $python = if ($pre.Python) { $pre.Python } else { Install-Python }
  $node   = if ($pre.Node)   { $pre.Node }   else { Install-Node }
  Install-PythonEnvironment -Root $Root -Python $python
  Install-FrontendDependencies -Root $Root -Node $node
  Build-App -Root $Root -Node $node
  if (-not $NoGate) { Invoke-Gate -Root $Root | Out-Null }
  if (-not $NoShortcuts) {
    Install-Shortcuts -Root $Root -Desktop -StartMenu -Taskbar:$Taskbar | Out-Null
  }
  if ($Launch) { Start-App -Root $Root }
  Write-Host ''
  Write-Host 'Setup complete.'
  return
}

# =================================================================== the window
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

function C([string]$Hex) { [System.Drawing.ColorTranslator]::FromHtml($Hex) }
$ColBg      = C $theme.dark.bg
$ColSurface = C $theme.dark.surface
$ColRaised  = C $theme.dark.surfaceRaised
$ColBorder  = C $theme.dark.border
$ColText    = C $theme.dark.text
$ColDim     = C $theme.dark.textDim
$ColAccent  = C $theme.accent
$ColGain    = C $theme.dark.gain
$ColLoss    = C $theme.dark.loss

$FontUi    = New-Object System.Drawing.Font('Segoe UI', 9.75)
$FontBold  = New-Object System.Drawing.Font('Segoe UI Semibold', 9.75, [System.Drawing.FontStyle]::Bold)
$FontTitle = New-Object System.Drawing.Font('Segoe UI Light', 20)
$FontMono  = New-Object System.Drawing.Font('Consolas', 8.75)

$form = New-Object System.Windows.Forms.Form
$form.Text            = 'Grindstone Setup'
$form.ClientSize      = New-Object System.Drawing.Size(660, 700)
$form.StartPosition   = 'CenterScreen'
$form.BackColor       = $ColBg
$form.ForeColor       = $ColText
$form.Font            = $FontUi
$form.MinimumSize     = New-Object System.Drawing.Size(560, 560)
$form.AutoScaleMode   = [System.Windows.Forms.AutoScaleMode]::Dpi
if (Test-Path $IconFile) { $form.Icon = New-Object System.Drawing.Icon($IconFile) }

function New-Label($Text, $X, $Y, $W, $H, $Font, $Color) {
  $l = New-Object System.Windows.Forms.Label
  $l.Text = $Text; $l.AutoSize = $false
  $l.Location = New-Object System.Drawing.Point($X, $Y)
  $l.Size = New-Object System.Drawing.Size($W, $H)
  $l.Font = $Font; $l.ForeColor = $Color; $l.BackColor = [System.Drawing.Color]::Transparent
  return $l
}

# ---------------------------------------------------------------------- header
$header = New-Object System.Windows.Forms.Panel
$header.Location = New-Object System.Drawing.Point(0, 0)
$header.Size = New-Object System.Drawing.Size(660, 72)
$header.BackColor = $ColSurface
$header.Anchor = 'Top,Left,Right'
$form.Controls.Add($header)

# The PNG, not the .ico: System.Drawing.Icon.ToBitmap() cannot decode
# PNG-compressed icon entries and throws. Form.Icon is fine because Windows
# itself decodes that one.
$LogoPng = Join-Path $Root 'code\assets\branding\icon-128.png'
if (Test-Path $LogoPng) {
  $pic = New-Object System.Windows.Forms.PictureBox
  $pic.Location = New-Object System.Drawing.Point(20, 16)
  $pic.Size = New-Object System.Drawing.Size(40, 40)
  $pic.SizeMode = 'Zoom'
  $pic.Image = [System.Drawing.Image]::FromFile($LogoPng)
  $pic.BackColor = [System.Drawing.Color]::Transparent
  $header.Controls.Add($pic)
}
$header.Controls.Add((New-Label 'Grindstone' 72 12 400 34 $FontTitle $ColText))
$header.Controls.Add((New-Label 'Trading platform setup' 74 46 400 18 $FontUi $ColDim))

# ------------------------------------------------------------------- detection
$form.Controls.Add((New-Label 'ON THIS COMPUTER' 22 88 300 18 $FontBold $ColDim))
$lblPython = New-Label 'checking...' 22 110 610 20 $FontUi $ColDim
$lblNode   = New-Label ''            22 132 610 20 $FontUi $ColDim
$lblGit    = New-Label ''            22 154 610 20 $FontUi $ColDim
$form.Controls.AddRange(@($lblPython, $lblNode, $lblGit))

# --------------------------------------------------------------------- options
$form.Controls.Add((New-Label 'INSTALL OPTIONS' 22 190 300 18 $FontBold $ColDim))

function New-Check($Text, $Y, $Checked, $Sub) {
  $c = New-Object System.Windows.Forms.CheckBox
  $c.Text = $Text
  $c.Checked = $Checked
  $c.Location = New-Object System.Drawing.Point(22, $Y)
  $c.Size = New-Object System.Drawing.Size(610, 20)
  $c.ForeColor = $ColText
  $c.FlatStyle = 'Flat'
  $c.Anchor = 'Top,Left,Right'
  $form.Controls.Add($c)
  # The sub-caption rides in .Tag so callers can retitle it later.
  if ($Sub) {
    $sub = New-Label $Sub 41 ($Y + 19) 590 16 $FontUi $ColDim
    $form.Controls.Add($sub)
    $c.Tag = $sub
  }
  return $c
}

$chkDeps = New-Check 'Install missing prerequisites' 214 $true `
             'Python and Node.js, fetched from winget or the official sites.'
$chkDesk = New-Check 'Desktop shortcut' 256 $true `
             'A Grindstone icon on the desktop that opens the app directly.'
$chkMenu = New-Check 'Start Menu shortcut' 298 $true $null
$chkPin  = New-Check 'Pin to the taskbar' 320 $false `
             'Best effort - Windows 11 blocks apps from pinning themselves.'
$chkGate = New-Check 'Run the verification gate when finished' 362 $true `
             'Proves the install is complete and correct. Takes about a minute.'
$chkOpen = New-Check 'Open Grindstone when setup finishes' 404 $true $null

# --------------------------------------------------------------------- actions
$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = 'Install'
$btnInstall.Location = New-Object System.Drawing.Point(22, 436)
$btnInstall.Size = New-Object System.Drawing.Size(130, 34)
$btnInstall.FlatStyle = 'Flat'
$btnInstall.BackColor = $ColAccent
$btnInstall.ForeColor = C '#1A1206'
$btnInstall.Font = $FontBold
$btnInstall.FlatAppearance.BorderSize = 0
$form.Controls.Add($btnInstall)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = 'Close'
$btnClose.Location = New-Object System.Drawing.Point(160, 436)
$btnClose.Size = New-Object System.Drawing.Size(96, 34)
$btnClose.FlatStyle = 'Flat'
$btnClose.BackColor = $ColRaised
$btnClose.ForeColor = $ColText
$btnClose.FlatAppearance.BorderColor = $ColBorder
$form.Controls.Add($btnClose)

$lblStatus = New-Label '' 268 445 370 20 $FontUi $ColDim
$lblStatus.Anchor = 'Top,Left,Right'
$form.Controls.Add($lblStatus)

$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(22, 480)
$bar.Size = New-Object System.Drawing.Size(616, 6)
$bar.Style = 'Continuous'
$bar.Anchor = 'Top,Left,Right'
$bar.Visible = $false   # an empty trough before you press Install reads as broken
$form.Controls.Add($bar)

# A 1px frame: WinForms has no border colour of its own, so the panel behind
# the log provides one and the log fills the rest.
$logFrame = New-Object System.Windows.Forms.Panel
$logFrame.Location = New-Object System.Drawing.Point(22, 496)
$logFrame.Size = New-Object System.Drawing.Size(616, 184)
$logFrame.BackColor = $ColBorder
$logFrame.Padding = New-Object System.Windows.Forms.Padding(1)
$logFrame.Anchor = 'Top,Bottom,Left,Right'
$form.Controls.Add($logFrame)

$log = New-Object System.Windows.Forms.RichTextBox
$log.Dock = 'Fill'
$log.BackColor = $ColSurface
$log.ForeColor = $ColDim
$log.Font = $FontMono
$log.BorderStyle = 'None'
$log.ReadOnly = $true
$log.Text = ''
$logFrame.Controls.Add($log)

# ------------------------------------------------------------------- log sink
$script:LogSink = {
  param($Message, $Kind)
  $color = switch ($Kind) {
    'step'  { $ColAccent }
    'warn'  { $ColLoss }
    'ok'    { $ColGain }
    default { $ColDim }
  }
  Write-LogFile "$(if ($Kind -eq 'step') { '==> ' } elseif ($Kind -eq 'warn') { 'warn: ' } else { '    ' })$Message"
  $prefix = if ($Kind -eq 'step') { "`r`n" } else { '' }
  $log.SelectionStart = $log.TextLength
  $log.SelectionLength = 0
  $log.SelectionColor = $color
  $log.AppendText("$prefix$Message`r`n")
  $log.ScrollToCaret()
  if ($Kind -eq 'step') { $lblStatus.Text = $Message }
}
$script:PumpUi = { [System.Windows.Forms.Application]::DoEvents() }

# ------------------------------------------------------------------ detection
$script:Pre = $null
function Update-Detection {
  $lblPython.Text = 'checking for Python and Node...'
  $lblNode.Text = ''; $lblGit.Text = ''
  [System.Windows.Forms.Application]::DoEvents()

  $script:Pre = Get-Prerequisites
  $ok = [char]0x2713; $no = [char]0x2717

  if ($script:Pre.Python) {
    $lblPython.Text = "$ok  Python $($script:Pre.Python.Version)"
    $lblPython.ForeColor = $ColGain
  } else {
    $lblPython.Text = "$no  Python 3.12 or newer - will be installed"
    $lblPython.ForeColor = $ColAccent
  }
  if ($script:Pre.Node) {
    $lblNode.Text = "$ok  Node.js $($script:Pre.Node.Version)"
    $lblNode.ForeColor = $ColGain
  } else {
    $lblNode.Text = "$no  Node.js 20 or newer - will be installed"
    $lblNode.ForeColor = $ColAccent
  }
  if ($script:Pre.Git) {
    $lblGit.Text = "$ok  Git"
    $lblGit.ForeColor = $ColGain
  } else {
    $lblGit.Text = "$no  Git - not required to run, but the verification gate needs it"
    $lblGit.ForeColor = $ColDim
  }

  # Deliberately NOT disabled when nothing is missing: WinForms paints a
  # disabled checkbox in system grey, which is unreadable on this background,
  # and the box is a no-op anyway (the install path only fetches what is
  # actually absent). Retitle the caption instead.
  $chkDeps.Checked = $true
  if ($script:Pre.Python -and $script:Pre.Node -and $chkDeps.Tag) {
    $chkDeps.Tag.Text = 'Nothing missing - Python and Node.js are already installed.'
  }
  Write-Log 'Ready. Choose your options, then press Install.'
  Write-Log "A full log is kept at $script:LogFile"
}

# -------------------------------------------------------------------- install
$script:Running = $false

$doInstall = {
  if ($script:Running) { return }
  $script:Running = $true
  $script:Cancel = $false
  $btnInstall.Enabled = $false
  $btnInstall.BackColor = $ColRaised
  $btnInstall.ForeColor = $ColDim
  $btnClose.Text = 'Cancel'
  foreach ($c in @($chkDeps, $chkDesk, $chkMenu, $chkPin, $chkGate, $chkOpen)) { $c.Enabled = $false }
  $bar.Visible = $true
  $bar.Style = 'Marquee'
  $log.Clear()

  try {
    if (-not $script:Pre) { $script:Pre = Get-Prerequisites }
    $python = $script:Pre.Python
    $node   = $script:Pre.Node

    if (-not $python) {
      if (-not $chkDeps.Checked) { throw 'Python 3.12+ is required. Tick "Install missing prerequisites" or install it yourself.' }
      $python = Install-Python
    }
    if (-not $node) {
      if (-not $chkDeps.Checked) { throw 'Node.js 20+ is required. Tick "Install missing prerequisites" or install it yourself.' }
      $node = Install-Node
    }

    Install-PythonEnvironment -Root $Root -Python $python
    Install-FrontendDependencies -Root $Root -Node $node
    Build-App -Root $Root -Node $node
    if ($chkGate.Checked) { Invoke-Gate -Root $Root | Out-Null }
    if ($chkDesk.Checked -or $chkMenu.Checked -or $chkPin.Checked) {
      Install-Shortcuts -Root $Root -Desktop:$chkDesk.Checked `
                        -StartMenu:$chkMenu.Checked -Taskbar:$chkPin.Checked | Out-Null
    }

    $bar.Style = 'Continuous'; $bar.Value = 100
    Write-Log 'Setup complete.' 'ok'
    $lblStatus.Text = 'Setup complete.'
    $lblStatus.ForeColor = $ColGain
    $btnClose.Text = 'Close'
    if ($chkOpen.Checked) { Start-App -Root $Root; $form.Close() }
  } catch {
    $bar.Style = 'Continuous'; $bar.Value = 0
    Write-Log '' 'info'
    Write-Log $_.Exception.Message 'warn'
    $lblStatus.Text = 'Setup failed.'
    $lblStatus.ForeColor = $ColLoss
    $btnInstall.Enabled = $true
    $btnInstall.BackColor = $ColAccent
    $btnInstall.ForeColor = C '#1A1206'
    $btnInstall.Text = 'Retry'
    $btnClose.Text = 'Close'
    foreach ($c in @($chkDesk, $chkMenu, $chkPin, $chkGate, $chkOpen)) { $c.Enabled = $true }
    $chkDeps.Enabled = -not $script:Pre.Python -or -not $script:Pre.Node
  } finally {
    $script:Running = $false
  }
}

$btnInstall.Add_Click($doInstall)
$btnClose.Add_Click({
  if ($script:Running) {
    # Mid-install: signal the step loop, which stops the child process.
    $script:Cancel = $true
    $lblStatus.Text = 'Cancelling...'
  } else { $form.Close() }
})
$form.Add_FormClosing({
  param($eventSender, $e)
  if ($script:Running) {
    $script:Cancel = $true
    $e.Cancel = $true   # let the running step unwind rather than orphan a child
  }
})
$form.Add_Shown({ Update-Detection })

[void]$form.ShowDialog()
