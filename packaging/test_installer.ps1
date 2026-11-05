# Install the built Windows installer for this user, start the installed app once, then uninstall it and check
# that nothing is left. CI and the release workflow run this after packaging/build.py.
$ErrorActionPreference = 'Stop'
$setup = Get-ChildItem release\*-setup.exe | Select-Object -First 1
if (-not $setup) { throw "no installer in release\" }
$dir = Join-Path $env:LOCALAPPDATA 'Programs\Pythonator'
Start-Process $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/CURRENTUSER', '/NORESTART' -Wait
$exe = Join-Path $dir 'Pythonator.exe'
if (-not (Test-Path $exe)) { throw "the installer didn't put Pythonator.exe in $dir" }
python packaging/smoke_test.py $exe
if ($LASTEXITCODE -ne 0) { throw "the installed app failed its smoke test" }
Start-Process (Join-Path $dir 'unins000.exe') -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
$deadline = (Get-Date).AddSeconds(90)  # the uninstaller finishes in a copy of itself
while ((Test-Path $exe) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
if (Test-Path $exe) { throw "the uninstaller left Pythonator.exe behind" }
Write-Output "installer ok: installed, started, uninstalled"
