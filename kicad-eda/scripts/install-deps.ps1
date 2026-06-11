# kicad-eda external dependency installer (Windows).
# Idempotent: checks each dependency first, installs only what's missing.
# Python packages are handled automatically by scripts/launch-server.js;
# this script covers the three EXTERNAL tools the pipeline needs.
#
#   .\install-deps.ps1            check + install via winget
#   .\install-deps.ps1 -CheckOnly  report status, change nothing

param([switch]$CheckOnly)

$ok = $true

function Find-KicadCli {
    $found = Get-Command kicad-cli -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($base in @("$env:LOCALAPPDATA\Programs\KiCad", "$env:ProgramFiles\KiCad")) {
        if (Test-Path $base) {
            $hit = Get-ChildItem $base -Directory | Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "bin\kicad-cli.exe" } |
                Where-Object { Test-Path $_ } | Select-Object -First 1
            if ($hit) { return $hit }
        }
    }
    return $null
}

# --- KiCad 9/10 (kicad-cli, bundled pcbnew Python, stock libraries) ---------
$cli = Find-KicadCli
if ($cli) {
    $ver = & $cli version 2>$null | Select-Object -First 1
    Write-Host "[ok] KiCad: $ver ($cli)"
} elseif ($CheckOnly) {
    Write-Host "[MISSING] KiCad - install with: winget install KiCad.KiCad"
    $ok = $false
} else {
    Write-Host "[..] Installing KiCad via winget (large download)..."
    winget install --id KiCad.KiCad --silent --accept-package-agreements --accept-source-agreements
    $cli = Find-KicadCli
    if ($cli) { Write-Host "[ok] KiCad installed: $cli" } else { Write-Host "[FAIL] KiCad install failed"; $ok = $false }
}

# --- Java 17-21 JRE (Freerouting 1.9.0, bundled in server/tools) ------------
$java = Get-Command java -ErrorAction SilentlyContinue
if (-not $java -and $env:JAVA_HOME) {
    $cand = Join-Path $env:JAVA_HOME "bin\java.exe"
    if (Test-Path $cand) { $java = @{ Source = $cand } }
}
if (-not $java) {
    $bundled = Get-ChildItem (Join-Path $PSScriptRoot "..\server\tools") -Recurse -Filter "java.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bundled) { $java = @{ Source = $bundled.FullName } }
}
if ($java) {
    Write-Host "[ok] Java: $($java.Source)"
} elseif ($CheckOnly) {
    Write-Host "[MISSING] Java - install with: winget install EclipseAdoptium.Temurin.21.JRE"
    $ok = $false
} else {
    Write-Host "[..] Installing Temurin 21 JRE via winget..."
    winget install --id EclipseAdoptium.Temurin.21.JRE --silent --accept-package-agreements --accept-source-agreements
    if (Get-Command java -ErrorAction SilentlyContinue) { Write-Host "[ok] Java installed" }
    else { Write-Host "[warn] Java installed but not on PATH yet - open a new terminal" }
}

# --- Python 3.10+ (server venv is built automatically on first launch) ------
$py = $null
foreach ($c in @("py", "python", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd; break }
}
if ($py) {
    Write-Host "[ok] Python: $($py.Source)"
} elseif ($CheckOnly) {
    Write-Host "[MISSING] Python - install with: winget install Python.Python.3.12"
    $ok = $false
} else {
    Write-Host "[..] Installing Python 3.12 via winget..."
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
}

# --- Freerouting jar (bundled - just verify) ---------------------------------
$jar = Join-Path $PSScriptRoot "..\server\tools\freerouting-1.9.0.jar"
if (Test-Path $jar) { Write-Host "[ok] Freerouting 1.9.0 jar bundled" }
else { Write-Host "[MISSING] $jar - re-install the plugin"; $ok = $false }

if ($ok) { Write-Host "`nAll dependencies present." } else { Write-Host "`nSome dependencies missing - see above."; exit 1 }
