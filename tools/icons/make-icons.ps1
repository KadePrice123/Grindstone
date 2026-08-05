# Renders the Grindstone mark to the platform icon formats the installers need.
#
#   powershell -ExecutionPolicy Bypass -File tools\icons\make-icons.ps1
#
# Source of truth is code/assets/branding/logo.svg - the static frame of it.
# That file's own comment says ".ico is generated from the static render"; this
# is that generator. The geometry below is the SVG's two paths transcribed into
# System.Drawing calls, because no Windows box can be assumed to have an SVG
# rasterizer and the mark is two triangles.
#
# Outputs (all committed, so no installer ever has to rasterize anything):
#   code/assets/branding/app.ico      Windows shortcut + BrowserWindow icon
#   code/assets/branding/app.icns     macOS .app bundle
#   code/assets/branding/icon-<n>.png Linux hicolor theme + .desktop
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$root  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$brand = Join-Path $root 'code\assets\branding'
if (-not (Test-Path $brand)) { throw "branding folder not found: $brand" }

$theme  = (Get-Content (Join-Path $brand 'branding.json') -Raw | ConvertFrom-Json).theme
$accent = $theme.accent          # #D98324
$plate  = $theme.dark.surface    # #191C1F

function ConvertTo-Color([string]$hex) {
  [System.Drawing.ColorTranslator]::FromHtml($hex)
}

# The mark, drawn into a size*size bitmap. viewBox is 0 0 64 64, so every
# coordinate below is the SVG's own number times (size/64).
function New-MarkBitmap([int]$size) {
  $bmp = New-Object System.Drawing.Bitmap($size, $size,
           [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try {
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.Clear([System.Drawing.Color]::Transparent)
    $s = $size / 64.0

    # Rounded plate. A bare stroke reads as noise at 16px against an arbitrary
    # taskbar colour; the plate gives the mark a consistent ground on both
    # light and dark shells.
    $inset = [Math]::Max(1.0, 2.0 * $s)
    $r     = [Math]::Max(2.0, 12.0 * $s)
    $rect  = New-Object System.Drawing.RectangleF($inset, $inset,
               ($size - 2 * $inset), ($size - 2 * $inset))
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r * 2
    $path.AddArc($rect.X, $rect.Y, $d, $d, 180, 90)
    $path.AddArc($rect.Right - $d, $rect.Y, $d, $d, 270, 90)
    $path.AddArc($rect.Right - $d, $rect.Bottom - $d, $d, $d, 0, 90)
    $path.AddArc($rect.X, $rect.Bottom - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    $plateBrush = New-Object System.Drawing.SolidBrush (ConvertTo-Color $plate)
    $g.FillPath($plateBrush, $path)
    $plateBrush.Dispose(); $path.Dispose()

    $acc = ConvertTo-Color $accent

    # <path d="M32 10 L51.05 43 L12.95 43 Z"/> stroke-width 4, linejoin round
    $outer = @(
      (New-Object System.Drawing.PointF((32.00 * $s), (10.0 * $s))),
      (New-Object System.Drawing.PointF((51.05 * $s), (43.0 * $s))),
      (New-Object System.Drawing.PointF((12.95 * $s), (43.0 * $s)))
    )
    $pen = New-Object System.Drawing.Pen($acc, [float](4.0 * $s))
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $g.DrawPolygon($pen, $outer)
    $pen.Dispose()

    # <path d="M32 41 L24.2 27.5 L39.8 27.5 Z" fill=currentColor opacity=0.4/>
    # 0.4 alpha over the plate rather than true compositing: identical result
    # here because the plate is opaque, and it survives the PNG round-trip.
    $inner = @(
      (New-Object System.Drawing.PointF((32.0 * $s), (41.0 * $s))),
      (New-Object System.Drawing.PointF((24.2 * $s), (27.5 * $s))),
      (New-Object System.Drawing.PointF((39.8 * $s), (27.5 * $s)))
    )
    $fill = New-Object System.Drawing.SolidBrush(
      [System.Drawing.Color]::FromArgb(102, $acc.R, $acc.G, $acc.B))
    $g.FillPolygon($fill, $inner)
    $fill.Dispose()
  } finally { $g.Dispose() }
  return $bmp
}

function Get-PngBytes([int]$size) {
  $bmp = New-MarkBitmap $size
  try {
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    return $ms.ToArray()
  } finally { $bmp.Dispose() }
}

function Get-BE([uint32]$v) {
  $b = [BitConverter]::GetBytes($v)
  if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($b) }
  return $b
}

# ---------------------------------------------------------------- Windows ICO
# PNG-compressed entries, which every Windows since Vista reads.
$icoSizes = @(16, 24, 32, 48, 64, 128, 256)
$pngs = @{}
foreach ($n in ($icoSizes + @(512))) { $pngs[$n] = Get-PngBytes $n }

$ms = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($ms)
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$icoSizes.Count)
$offset = 6 + 16 * $icoSizes.Count
foreach ($n in $icoSizes) {
  $data = $pngs[$n]
  $bw.Write([byte]($(if ($n -ge 256) { 0 } else { $n })))   # 0 means 256
  $bw.Write([byte]($(if ($n -ge 256) { 0 } else { $n })))
  $bw.Write([byte]0); $bw.Write([byte]0)
  $bw.Write([uint16]1); $bw.Write([uint16]32)
  $bw.Write([uint32]$data.Length); $bw.Write([uint32]$offset)
  $offset += $data.Length
}
# Three-arg overload on purpose: with a single argument PowerShell resolves
# Write(byte) against a byte[] and silently writes one byte per image.
foreach ($n in $icoSizes) {
  $data = [byte[]]$pngs[$n]
  $bw.Write($data, 0, $data.Length)
}
$bw.Flush()
[System.IO.File]::WriteAllBytes((Join-Path $brand 'app.ico'), $ms.ToArray())
$bw.Dispose(); $ms.Dispose()
Write-Host "wrote app.ico      ($($icoSizes.Count) sizes)"

# ------------------------------------------------------------------ macOS ICNS
# 'icns' + big-endian total length, then type/length/data triplets. The icp*
# and ic0* types below all accept a raw PNG payload.
$icnsMap = [ordered]@{ 'icp4' = 16; 'icp5' = 32; 'icp6' = 64
                       'ic07' = 128; 'ic08' = 256; 'ic09' = 512 }
$body = New-Object System.IO.MemoryStream
foreach ($type in $icnsMap.Keys) {
  $data = $pngs[$icnsMap[$type]]
  $t = [System.Text.Encoding]::ASCII.GetBytes($type)
  $body.Write($t, 0, 4)
  $len = Get-BE ([uint32]($data.Length + 8))
  $body.Write($len, 0, 4)
  $body.Write($data, 0, $data.Length)
}
$bodyBytes = $body.ToArray(); $body.Dispose()
$icns = New-Object System.IO.MemoryStream
$magic = [System.Text.Encoding]::ASCII.GetBytes('icns')
$icns.Write($magic, 0, 4)
$total = Get-BE ([uint32]($bodyBytes.Length + 8))
$icns.Write($total, 0, 4)
$icns.Write($bodyBytes, 0, $bodyBytes.Length)
[System.IO.File]::WriteAllBytes((Join-Path $brand 'app.icns'), $icns.ToArray())
$icns.Dispose()
Write-Host "wrote app.icns     ($($icnsMap.Count) sizes)"

# ------------------------------------------------------------------- Linux PNG
foreach ($n in @(48, 128, 256)) {
  [System.IO.File]::WriteAllBytes((Join-Path $brand "icon-$n.png"), $pngs[$n])
}
Write-Host "wrote icon-48.png icon-128.png icon-256.png"
