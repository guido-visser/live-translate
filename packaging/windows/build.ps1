# Builds packaging\windows\Output\LiveTranslate-Setup-<version>.exe
# Requires Inno Setup 6 (https://jrsoftware.org/isdl.php or: winget install JRSoftware.InnoSetup).
param([string]$UvVersion = "0.13.6")
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

& (Join-Path $bin "uv.exe") lock --project $root
if ($LASTEXITCODE -ne 0) { throw "uv lock failed" }

$version = (Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 not found" }

& $iscc "/DAppVersion=$version" (Join-Path $here "LiveTranslate.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Installer written to $(Join-Path $here 'Output')"
