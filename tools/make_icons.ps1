# Regenerates the PNG icons in assets/. Colours must match SCREEN_COLORS in
# screen_control/identify.py.
#   pwsh tools/make_icons.ps1
Add-Type -AssemblyName System.Drawing

$assets = Join-Path $PSScriptRoot '..\assets'
$size = 256
$colors = @('#2563EB', '#B45309', '#047857', '#DC2626', '#7C3AED', '#BE185D', '#0E7490', '#4D7C0F', '#C2410C')

function New-Canvas {
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'AntiAliasGridFit'
    $g.Clear([System.Drawing.Color]::Transparent)
    return $bmp, $g
}

function Get-RoundRect([float]$x, [float]$y, [float]$w, [float]$h, [float]$r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r * 2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

function Draw-Monitor($g, [string]$hex, [string]$label, [float]$ox = 0, [float]$oy = 0, [float]$scale = 1) {
    $c = [System.Drawing.ColorTranslator]::FromHtml($hex)
    $brush = New-Object System.Drawing.SolidBrush $c
    $stand = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 120, 120, 120))
    # stand
    $g.FillRectangle($stand, $ox + 108 * $scale, $oy + 196 * $scale, 40 * $scale, 28 * $scale)
    $g.FillPath($stand, (Get-RoundRect ($ox + 72 * $scale) ($oy + 220 * $scale) (112 * $scale) (18 * $scale) (8 * $scale)))
    # screen
    $g.FillPath($brush, (Get-RoundRect ($ox + 16 * $scale) ($oy + 28 * $scale) (224 * $scale) (172 * $scale) (22 * $scale)))
    if ($label) {
        $fontSize = if ($label.Length -gt 1) { 84 } else { 118 }
        $font = New-Object System.Drawing.Font 'Segoe UI', ($fontSize * $scale), ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
        $fmt = New-Object System.Drawing.StringFormat
        $fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
        $rect = New-Object System.Drawing.RectangleF ($ox + 16 * $scale), ($oy + 30 * $scale), (224 * $scale), (172 * $scale)
        $g.DrawString($label, $font, [System.Drawing.Brushes]::White, $rect, $fmt)
    }
}

function Save($bmp, $g, [string]$name) {
    $g.Dispose()
    $bmp.Save((Join-Path $assets $name), [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}

for ($i = 1; $i -le $colors.Count; $i++) {
    $bmp, $g = New-Canvas
    Draw-Monitor $g $colors[$i - 1] "$i"
    Save $bmp $g "screen_$i.png"
}

$bmp, $g = New-Canvas
Draw-Monitor $g '#5B6B82' ''
Save $bmp $g 'screen.png'

# all screens: two overlapping monitors
$bmp, $g = New-Canvas
Draw-Monitor $g $colors[1] '' 70 -10 0.68
Draw-Monitor $g $colors[0] '' 6 70 0.68
Save $bmp $g 'screens.png'

# identify: monitor with "?"
$bmp, $g = New-Canvas
Draw-Monitor $g '#5B6B82' '?'
Save $bmp $g 'identify.png'

# brightness: sun
$bmp, $g = New-Canvas
$sun = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#D97706'))
$pen = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#D97706')), 18
$pen.StartCap = 'Round'; $pen.EndCap = 'Round'
$g.FillEllipse($sun, 78, 78, 100, 100)
for ($a = 0; $a -lt 360; $a += 45) {
    $rad = $a * [Math]::PI / 180
    $g.DrawLine($pen, [float](128 + 74 * [Math]::Cos($rad)), [float](128 + 74 * [Math]::Sin($rad)), [float](128 + 108 * [Math]::Cos($rad)), [float](128 + 108 * [Math]::Sin($rad)))
}
Save $bmp $g 'brightness.png'

# contrast: half-filled circle
$bmp, $g = New-Canvas
$ring = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#64748B')), 16
$g.FillEllipse([System.Drawing.Brushes]::White, 28, 28, 200, 200)
$g.FillPie((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#1F2937'))), 28, 28, 200, 200, 90, 180)
$g.DrawEllipse($ring, 28, 28, 200, 200)
Save $bmp $g 'contrast.png'

# HDR: rounded badge with gradient
$bmp, $g = New-Canvas
$rect = New-Object System.Drawing.Rectangle 8, 56, 240, 144
$grad = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, ([System.Drawing.ColorTranslator]::FromHtml('#7C3AED')), ([System.Drawing.ColorTranslator]::FromHtml('#F59E0B')), 0.0
$g.FillPath($grad, (Get-RoundRect 8 56 240 144 30))
$font = New-Object System.Drawing.Font 'Segoe UI', 92, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
$fmt = New-Object System.Drawing.StringFormat
$fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
$g.DrawString('HDR', $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF 8, 60, 240, 144), $fmt)
Save $bmp $g 'hdr.png'

# app: monitor with a small sun
$bmp, $g = New-Canvas
Draw-Monitor $g '#2563EB' ''
$sunBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#F5C518'))
$sunPen = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#F5C518')), 10
$sunPen.StartCap = 'Round'; $sunPen.EndCap = 'Round'
$g.FillEllipse($sunBrush, 98, 84, 60, 60)
for ($a = 0; $a -lt 360; $a += 45) {
    $rad = $a * [Math]::PI / 180
    $g.DrawLine($sunPen, [float](128 + 44 * [Math]::Cos($rad)), [float](114 + 44 * [Math]::Sin($rad)), [float](128 + 62 * [Math]::Cos($rad)), [float](114 + 62 * [Math]::Sin($rad)))
}
Save $bmp $g 'app.png'

# error: red monitor with "!"
$bmp, $g = New-Canvas
Draw-Monitor $g '#DC2626' '!'
Save $bmp $g 'error.png'

Write-Host "Icons written to $assets"
