# Tutti installer for Windows: irm https://raw.githubusercontent.com/ventura8/Tutti/main/install/install.ps1 | iex
# Installs (or updates) the latest Tutti from GitHub into %LOCALAPPDATA%\tutti with its own Python venv and adds a `tutti`
# command to your user PATH. Installs Git and Python with winget when missing (winget asks you). Re-run to update. No secrets.
$ErrorActionPreference = 'Stop'
$repo = if ($env:TUTTI_REPO) { $env:TUTTI_REPO } else { 'https://github.com/ventura8/Tutti.git' }
$home_ = Join-Path $env:LOCALAPPDATA 'tutti'
$app = Join-Path $home_ 'app'
$shim = Join-Path $home_ 'bin'
function Need($cmd, $winget) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "tutti: installing $winget with winget"
        winget install --id $winget -e --source winget --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
}
Need git 'Git.Git'
Need python 'Python.Python.3.13'
if (Test-Path (Join-Path $app '.git')) {
    git -C $app pull --ff-only --quiet
    if ($LASTEXITCODE -ne 0) { throw "tutti: git pull in $app failed (exit $LASTEXITCODE); nothing was changed. Fix the clone (for example remove $app\.git\index.lock) and run the installer again." }
    # An install made before .gitattributes kept its CRLF scripts through every pull (git rewrites only changed files),
    # so bash still could not run scripts/slot.sh (QA round 2, 2026-11-14). The scripts are written out again under the
    # current line-ending rules; this folder is the installer's own clone, never edited by hand.
    Get-ChildItem -LiteralPath $app -Recurse -File -Filter '*.sh' | Where-Object { $_.FullName -notmatch '\\\.(git|venv)\\' } | Remove-Item -Force
    Remove-Item -LiteralPath (Join-Path $app 'bin\tutti') -Force -ErrorAction SilentlyContinue
    git -C $app checkout --quiet -- .
    if ($LASTEXITCODE -ne 0) { throw "tutti: git checkout in $app failed (exit $LASTEXITCODE): run git -C $app checkout -- . and then the installer again." }
} else {
    New-Item -ItemType Directory -Force -Path $home_ | Out-Null
    git clone --quiet --depth 1 $repo $app
}
python -m venv (Join-Path $app '.venv')
& (Join-Path $app '.venv\Scripts\python.exe') -m pip install --quiet --upgrade pip
& (Join-Path $app '.venv\Scripts\python.exe') -m pip install --quiet -r (Join-Path $app 'requirements.txt')
New-Item -ItemType Directory -Force -Path $shim | Out-Null
Set-Content -Path (Join-Path $shim 'tutti.cmd') -Encoding ASCII -Value "@`"$app\.venv\Scripts\python.exe`" `"$app\bin\tutti`" %*"
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $shim) { [Environment]::SetEnvironmentVariable('Path', "$userPath;$shim", 'User') }
$env:Path += ";$shim"
$configHome = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }
$configDir = Join-Path $configHome 'tutti'
$configPath = Join-Path $configDir 'config.yaml'
if (-not (Test-Path -LiteralPath $configPath)) {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $app 'tutti.config.example.yaml') -Destination $configPath
    Write-Host "tutti: wrote example config to $configPath"
}
Write-Host "tutti $(git -C $app rev-parse --short HEAD) installed. Open a new terminal, then: edit $configPath (unless TUTTI_CONFIG or a project tutti.config.yaml takes precedence), then run: tutti config; tutti workers"
