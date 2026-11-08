# Enshrouded Tools installer. Run it with:
#   irm https://raw.githubusercontent.com/pgpavlides/enshrouded-tools-public/main/install.ps1 | iex
# Run the same line again to update. Settings and saved routes are kept.

function Install-EnshroudedTools {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $base = 'https://raw.githubusercontent.com/pgpavlides/enshrouded-tools-public/main'
    $files = 'enshrouded_tools.pyw', 'fishing_bot.py', 'route.py', 'requirements.txt'

    # ENSHROUDED_TOOLS_DIR installs somewhere else without starting the app (for testing).
    $testing = [bool]$env:ENSHROUDED_TOOLS_DIR
    if ($testing) { $dir = $env:ENSHROUDED_TOOLS_DIR } else { $dir = Join-Path $env:LOCALAPPDATA 'EnshroudedTools' }

    function Find-Python {
        $found = @()
        $ErrorActionPreference = 'Continue'
        $probe = 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
        $where = 'import sys; print(sys.executable)'
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & $py.Source -3 -c $probe 2>$null
            if ($LASTEXITCODE -eq 0) { $found += (& $py.Source -3 -c $where 2>$null) }
        }
        foreach ($name in 'python', "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe") {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($cmd) {
                & $cmd.Source -c $probe 2>$null
                if ($LASTEXITCODE -eq 0) { $found += (& $cmd.Source -c $where 2>$null) }
            }
        }
        return ($found | Select-Object -First 1)
    }

    $python = Find-Python
    if (-not $python) {
        Write-Host 'Python not found. Installing Python 3.13 with winget...'
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw 'winget is missing. Install Python 3.13 from python.org, then run this again.'
        }
        winget install --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
        $python = Find-Python
        if (-not $python) { throw 'Python install failed. Install Python 3.13 from python.org, then run this again.' }
    }
    Write-Host "Python: $python"

    # Stop a running copy so the new files are the ones that start.
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*$dir*enshrouded_tools.pyw*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    foreach ($file in $files) {
        Write-Host "Downloading $file"
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$file" -OutFile (Join-Path $dir $file)
    }

    $venv = Join-Path $dir '.venv'
    if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
        Write-Host 'Creating the Python environment...'
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    Write-Host 'Installing packages (the first time takes a minute)...'
    & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --disable-pip-version-check -r (Join-Path $dir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Package install failed.' }

    if ($testing) {
        Write-Host "Installed to $dir (test mode: not started)."
        return
    }

    $pythonw = Join-Path $venv 'Scripts\pythonw.exe'
    $app = Join-Path $dir 'enshrouded_tools.pyw'
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Enshrouded Tools.lnk'))
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$app`""
    $link.WorkingDirectory = $dir
    $link.Save()

    Start-Process -FilePath $pythonw -ArgumentList "`"$app`"" -WorkingDirectory $dir
    Write-Host 'Done. Enshrouded Tools is running: look for the tray icon. A desktop shortcut was added.'
}

Install-EnshroudedTools
