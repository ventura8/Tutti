# bootstrap.ps1: installs Tutti on Windows in user space (no admin), then runs `tutti setup`.
# Usage: powershell -ExecutionPolicy Bypass -File tutti-bootstrap.ps1 [-Join <household invite>] [-Version <x.y.z>] [-NoSetup]
# Get it with the one line in docs/INSTALL.md, which checks this file's SHA-256 before running it. uv and gh are checked
# against the SHA-256 pinned here, Tutti's tarball against the release's SHA256SUMS; Git comes from winget (user scope),
# which checks its own installers. It never asks for or stores a credential. Re-run it to repair an install.
param([string]$Join = '', [string]$Version = '', [switch]$NoSetup)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$UvVersion = '0.13.8'
$UvSum = '529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8'
$GhVersion = '2.104.0'
$GhSum = '08c06d10482f12c478018cd259f64acbd5f625712cadf331aad19ba973c879e5'
$Repo = 'ventura8/Tutti'
$TuttiHome = if ($env:TUTTI_HOME) { $env:TUTTI_HOME } else { Join-Path $env:LOCALAPPDATA 'tutti' }
$Bin = Join-Path $TuttiHome 'bin'
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("tutti-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $Bin, $Tmp, (Join-Path $TuttiHome 'versions') | Out-Null

function Get-Checked($Url, $Out, $Sum) {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Out
    $got = (Get-FileHash -Algorithm SHA256 $Out).Hash.ToLower()
    if ($got -ne $Sum) { throw "tutti: $(Split-Path -Leaf $Out) failed its checksum (got $got, want $Sum): not installed; run this again" }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host 'tutti: installing Git with winget (user scope)'
    winget install --id Git.Git -e --scope user --source winget --accept-package-agreements --accept-source-agreements
}
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Host "tutti: installing uv $UvVersion (checksum pinned)"
    Get-Checked "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" (Join-Path $Tmp 'uv.zip') $UvSum
    Expand-Archive -Force (Join-Path $Tmp 'uv.zip') $Bin
    $uv = Join-Path $Bin 'uv.exe'
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "tutti: installing gh $GhVersion (checksum pinned)"
    Get-Checked "https://github.com/cli/cli/releases/download/v$GhVersion/gh_${GhVersion}_windows_amd64.zip" (Join-Path $Tmp 'gh.zip') $GhSum
    Expand-Archive -Force (Join-Path $Tmp 'gh.zip') (Join-Path $Tmp 'gh')
    $ghExe = Get-ChildItem -Recurse -Filter gh.exe (Join-Path $Tmp 'gh') | Select-Object -First 1   # the zip's folder layout varies
    Copy-Item $ghExe.FullName $Bin
}

$base = if ($Version) { "https://github.com/$Repo/releases/download/v$Version" } else { "https://github.com/$Repo/releases/latest/download" }
$sums = (Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS").Content -split "`n"
if (-not $Version) {
    $Version = $sums | ForEach-Object { if ($_ -match '^[0-9a-f]{64}  tutti-([0-9.]+)\.tar\.gz$') { $Matches[1] } } |
        Sort-Object { [version]$_ } | Select-Object -Last 1
    if (-not $Version) { throw 'tutti: the latest release lists no tarball in SHA256SUMS: try again later' }
}
$name = "tutti-$Version.tar.gz"
$want = $sums | ForEach-Object { if ($_ -match "^([0-9a-f]{64})  $([regex]::Escape($name))$") { $Matches[1] } } | Select-Object -First 1
if (-not $want) { throw "tutti: $name is not in the release's SHA256SUMS: not installed" }
Get-Checked "https://github.com/$Repo/releases/download/v$Version/$name" (Join-Path $Tmp $name) $want
$target = Join-Path $TuttiHome "versions\$Version"
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
New-Item -ItemType Directory -Force -Path $target | Out-Null
tar -xzf (Join-Path $Tmp $name) -C $target --strip-components 1
Write-Host "tutti: installing Python and PyYAML into $TuttiHome\venv"
& $uv venv --quiet --allow-existing --python 3.12 (Join-Path $TuttiHome 'venv')
& $uv pip install --quiet --python (Join-Path $TuttiHome 'venv\Scripts\python.exe') -r (Join-Path $target 'requirements.txt')
$current = Join-Path $TuttiHome 'current'
if (Test-Path $current) { (Get-Item $current).Delete() }
New-Item -ItemType Junction -Path $current -Target $target | Out-Null
Set-Content -Path (Join-Path $Bin 'tutti.cmd') -Encoding ASCII -Value "@`"$TuttiHome\venv\Scripts\python.exe`" `"$current\bin\tutti`" %*"
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $Bin) { [Environment]::SetEnvironmentVariable('Path', "$userPath;$Bin", 'User') }
$env:Path += ";$Bin"
Remove-Item -Recurse -Force $Tmp
Write-Host "tutti: Tutti $Version installed"
if (-not $NoSetup) {
    if ($Join) { & (Join-Path $Bin 'tutti.cmd') setup --join $Join } else { & (Join-Path $Bin 'tutti.cmd') setup }
}
