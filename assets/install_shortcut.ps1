# Create (or refresh) the Image Organizer shortcut on the user's Desktop.
# Usage:  powershell -ExecutionPolicy Bypass -File assets\install_shortcut.ps1

$ErrorActionPreference = 'Stop'

$desktop  = [Environment]::GetFolderPath('Desktop')
$lnkPath  = Join-Path $desktop 'Image Organizer.lnk'
$batPath  = Join-Path $desktop 'Image Organizer.bat'
$iconPath = Join-Path $PSScriptRoot 'icon.ico'

if (-not (Test-Path $batPath)) {
    Write-Error "Launcher not found: $batPath"
}
if (-not (Test-Path $iconPath)) {
    Write-Error "Icon not found: $iconPath"
}

$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath       = $batPath
$lnk.WorkingDirectory = $desktop
$lnk.IconLocation     = "$iconPath,0"
$lnk.WindowStyle      = 7   # minimized
$lnk.Description      = 'Launch the Image Organizer desktop UI'
$lnk.Save()

Write-Host "Shortcut written: $lnkPath"
