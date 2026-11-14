# Tutti uninstaller for Windows: irm https://raw.githubusercontent.com/ventura8/Tutti/main/install/uninstall.ps1 | iex
# Removes %LOCALAPPDATA%\tutti and its PATH entry. Your config is kept unless $env:TUTTI_PURGE = '1'.
$ErrorActionPreference = 'Stop'
$home_ = Join-Path $env:LOCALAPPDATA 'tutti'
$shim = Join-Path $home_ 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
[Environment]::SetEnvironmentVariable('Path', (($userPath -split ';') | Where-Object { $_ -and $_ -ne $shim }) -join ';', 'User')
if (Test-Path $home_) { Remove-Item -Recurse -Force $home_ }
if ($env:TUTTI_PURGE -eq '1') {
    $configHome = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }
    Remove-Item -Recurse -Force (Join-Path $configHome 'tutti') -ErrorAction SilentlyContinue
}
Write-Host 'tutti removed (config kept unless TUTTI_PURGE=1)'
