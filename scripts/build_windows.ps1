$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Resources = Join-Path $Root "Lecture Slide Capture.app\Contents\Resources"
$Requirements = Join-Path $Resources "requirements.txt"
$Spec = Join-Path $Root "packaging\windows\LectureSlideCapture.windows.spec"

Set-Location $Root

if ($env:PYTHON) {
    $Python = $env:PYTHON
} else {
    $Python = "python"
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r $Requirements pyinstaller
& $Python -m PyInstaller --clean --noconfirm $Spec

Write-Host ""
Write-Host "Windows executable created at:"
Write-Host (Join-Path $Root "dist\LectureSlideCapture.exe")
