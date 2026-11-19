# Builds packaging\windows\Output\LiveTranslate-Setup-<version>.exe
# Requires Inno Setup 6 (https://jrsoftware.org/isdl.php or: winget install JRSoftware.InnoSetup).
#
# Signed release builds (build.yml) call it twice with -SignedUninstallerDir:
#   1. -UninstallerOnly: Inno Setup writes uninst-<version>-<hash>.e32 to that folder and stops;
#      the workflow has it signed.
#   2. With the signed .e32 in that folder: the installer, with a signed uninstaller and temp copy.
param(
    [string]$UvVersion = "0.13.6",
    [string]$SignedUninstallerDir = "",
    [switch]$UninstallerOnly
)
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$root = Resolve-Path (Join-Path $here "..\..")
$bin = Join-Path $here "bin"
New-Item -ItemType Directory -Force $bin | Out-Null

if (-not (Test-Path (Join-Path $bin "uv.exe"))) {
    $zip = Join-Path $env:TEMP "uv-$UvVersion.zip"
    $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
    Write-Host "Downloading $url"
    Invoke-WebRequest $url -OutFile $zip
    Expand-Archive $zip -DestinationPath $bin -Force
}

# Microsoft Visual C++ runtime; Setup runs it when the PC's copy is older than 14.40.
$vcRedist = Join-Path $bin "vc_redist.x64.exe"
if (-not (Test-Path $vcRedist)) {
    Write-Host "Downloading the Visual C++ runtime"
    Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcRedist
}

# From here on, failures are checked via $LASTEXITCODE: under "Stop", Windows PowerShell 5.1
# turns any stderr output of a native program (uv logs progress there) into an error.
$ErrorActionPreference = "Continue"

& (Join-Path $bin "uv.exe") lock --project $root
if ($LASTEXITCODE -ne 0) { throw "uv lock failed" }

$version = (Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 not found" }

$isccArgs = @("/DAppVersion=$version")
if ($SignedUninstallerDir) {
    New-Item -ItemType Directory -Force $SignedUninstallerDir | Out-Null
    $SignedUninstallerDir = (Resolve-Path $SignedUninstallerDir).Path
    $isccArgs += "/DSignedUninstallerDir=$SignedUninstallerDir"
}
& $iscc @isccArgs (Join-Path $here "LiveTranslate.iss")
$exit = $LASTEXITCODE
if ($UninstallerOnly) {
    # Inno Setup stops on purpose here ("please attach your digital signature"); the .e32 is what we want.
    $e32 = Get-ChildItem $SignedUninstallerDir -Filter "uninst-*.e32" | Select-Object -First 1
    if (-not $e32) { throw "Inno Setup wrote no uninstaller to sign (exit code $exit)" }
    Write-Host "Uninstaller to sign: $($e32.FullName)"
    exit 0
}
if ($exit -ne 0) { throw "Inno Setup failed" }
Write-Host "Installer written to $(Join-Path $here 'Output')"
