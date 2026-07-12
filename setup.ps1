# =====================================================================
#  Beacon  -  first time setup
# =====================================================================
#  This script checks for everything the toolkit uses and helps you
#  install what is missing. It is safe to run more than once. Nothing
#  is installed without asking you first.
#
#  Steps:
#    1. Python 3.9+            (required to run the app)
#    2. A local virtual env    (.venv, optional but recommended)
#    3. Python packages        (pywinpty terminal, Ruff formatter, Jupyter)
#    4. Git                    (for the git and sync features)
#    5. A LaTeX distribution   (TinyTeX, for compiling papers)
#    6. Claude Code CLI        (optional, for the Claude assistant)
#    7. Pandoc                 (optional, Word/PDF export from LaTeX or Markdown)
#    8. Ollama                 (optional, free local AI models)
#
#  Run it by double clicking setup.bat, or in PowerShell:
#    powershell -ExecutionPolicy Bypass -File setup.ps1
# =====================================================================

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = [char]0x2713    # check mark
$dot = "  -"

function Have($name) {
    return $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}
function Section($n, $title) {
    Write-Host ""
    Write-Host ("["+$n+"/8] " + $title) -ForegroundColor Cyan
}
function Yes($question) {
    $a = Read-Host ($question + " [Y/n]")
    return ($a -eq "" -or $a -match "^[Yy]")
}

Write-Host "=====================================================" -ForegroundColor DarkCyan
Write-Host " Beacon - setup" -ForegroundColor White
Write-Host "=====================================================" -ForegroundColor DarkCyan

# ---- 1. Python -------------------------------------------------------
Section 1 "Python 3.9 or newer"
if (Have python) {
    $pv = (python --version) 2>&1
    Write-Host "$dot $ok Found $pv"
} else {
    Write-Host "$dot Python was not found on PATH." -ForegroundColor Yellow
    Write-Host "$dot Install it from https://python.org (check 'Add to PATH'), then re-run setup." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# ---- 2. Virtual environment -----------------------------------------
Section 2 "Local virtual environment (.venv)"
$venv = Join-Path $root ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
if (Test-Path $venvPy) {
    Write-Host "$dot $ok .venv already exists"
} else {
    Write-Host "$dot A virtual environment keeps this project's Python separate from your system."
    if (Yes "Create one in .venv now?") {
        python -m venv $venv
        if (Test-Path $venvPy) {
            Write-Host "$dot $ok Created .venv"
        } else {
            Write-Host "$dot Could not create .venv. The app will run with system Python instead." -ForegroundColor Yellow
        }
    } else {
        Write-Host "$dot Skipped. The app will run with system Python (that is fine)."
    }
}

# ---- 3. Python packages ---------------------------------------------
Section 3 "Python packages"
$py = "python"
if (Test-Path $venvPy) { $py = $venvPy }
& $py -m pip install --quiet --upgrade pip 2>$null

# pywinpty: real interactive terminal (optional)
Write-Host "$dot Installing pywinpty (enables the full interactive terminal; optional)…"
try {
    & $py -m pip install --quiet pywinpty 2>$null
    & $py -c "import winpty" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "$dot $ok pywinpty installed (real interactive terminal enabled)"
    } else {
        Write-Host "$dot pywinpty not installed. The app will use the simpler line terminal (that is fine)." -ForegroundColor Yellow
    }
} catch {
    Write-Host "$dot Could not install pywinpty. The app falls back to the line terminal." -ForegroundColor Yellow
}

# Ruff: powers Tools > Format Python file (small, always installed)
Write-Host "$dot Installing Ruff (enables Tools > Format Python file)…"
try {
    & $py -m pip install --quiet ruff 2>$null
    & $py -m ruff --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "$dot $ok Ruff installed (Python formatting enabled)"
    } else {
        Write-Host "$dot Ruff not installed. Install later with '$py -m pip install ruff' (or use Black)." -ForegroundColor Yellow
    }
} catch {
    Write-Host "$dot Could not install Ruff. Python formatting stays off until it is installed." -ForegroundColor Yellow
}

# Jupyter + nbconvert: powers Run notebook on .ipynb files (larger, optional)
Write-Host "$dot Jupyter lets you run .ipynb notebooks from the file tree (a larger download)."
if (Yes "Install Jupyter and nbconvert now?") {
    try {
        & $py -m pip install --quiet jupyter nbconvert 2>$null
        & $py -m jupyter --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "$dot $ok Jupyter installed (right-click a .ipynb to run it)"
        } else {
            Write-Host "$dot Jupyter did not install cleanly. Try '$py -m pip install jupyter nbconvert' later." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "$dot Could not install Jupyter. Notebook run stays off until it is installed." -ForegroundColor Yellow
    }
} else {
    Write-Host "$dot Skipped. Install later with '$py -m pip install jupyter nbconvert' to run notebooks."
}

# ---- 4. Git ----------------------------------------------------------
Section 4 "Git"
if (Have git) {
    Write-Host ("$dot $ok Found " + ((git --version) -replace "git version ",""))
} else {
    Write-Host "$dot Git was not found. The git and Sync features need it." -ForegroundColor Yellow
    Write-Host "$dot Install Git for Windows from https://git-scm.com, then re-run setup." -ForegroundColor Yellow
}

# ---- 5. LaTeX (TinyTeX) ---------------------------------------------
Section 5 "LaTeX distribution (latexmk)"
if (Have latexmk) {
    Write-Host "$dot $ok Found latexmk (LaTeX is ready)"
} else {
    Write-Host "$dot No LaTeX distribution was found. Compiling papers needs one." -ForegroundColor Yellow
    Write-Host "$dot TinyTeX is a small distribution (about 200 MB) and installs missing packages on demand."
    if (Yes "Download and install TinyTeX now?") {
        $bat = Join-Path $env:TEMP "install-tinytex.bat"
        try {
            Write-Host "$dot Downloading the TinyTeX installer..."
            Invoke-WebRequest "https://yihui.org/tinytex/install-bin-windows.bat" -OutFile $bat -UseBasicParsing
            Write-Host "$dot Running the installer (this can take a few minutes)..."
            & $bat
            Write-Host "$dot $ok TinyTeX install finished. Open a new terminal so latexmk is on PATH."
        } catch {
            Write-Host "$dot Automatic install failed. Install TinyTeX manually from https://yihui.org/tinytex/" -ForegroundColor Yellow
        }
    } else {
        Write-Host "$dot Skipped. Install a TeX distribution later (TinyTeX or MiKTeX or TeX Live)."
    }
}

# ---- 6. Claude Code CLI (optional) ----------------------------------
Section 6 "Claude Code CLI (optional, for the Claude assistant)"
if (Have claude) {
    Write-Host "$dot $ok Found the claude CLI"
} else {
    Write-Host "$dot Not found. The Claude assistant panel needs it (optional)."
    Write-Host "$dot Install Node.js from https://nodejs.org, then run:  npm install -g @anthropic-ai/claude-code"
    Write-Host "$dot Or skip it and use a local Ollama model, or add an OpenAI/Gemini/Grok key in Settings."
}

# ---- 7. Pandoc (optional, Word/PDF export from LaTeX or Markdown) ----
Section 7 "Pandoc (optional, for the Word and PDF export tools)"
$pandoc = $null
if (Have pandoc) { $pandoc = "pandoc" }
elseif (Test-Path "$env:LocalAppData\Pandoc\pandoc.exe") { $pandoc = "$env:LocalAppData\Pandoc\pandoc.exe" }
if ($pandoc) {
    Write-Host "$dot $ok Found Pandoc (LaTeX and Markdown export to Word/PDF enabled)"
} else {
    Write-Host "$dot Not found. The Tools > Export to Word (.docx) and Markdown to PDF features need it."
    Write-Host "$dot PDF export also uses your LaTeX distribution (step 5) as the PDF engine."
    if ((Have winget) -and (Yes "Install Pandoc now with winget?")) {
        winget install --id JohnMacFarlane.Pandoc --accept-source-agreements --accept-package-agreements --silent
        Write-Host "$dot $ok Pandoc install finished (open a new terminal so it is on PATH)."
    } else {
        Write-Host "$dot Skipped. Install later from https://pandoc.org if you want Word or PDF export."
    }
}

# ---- 8. Ollama (optional) -------------------------------------------
Section 8 "Ollama (optional, free local open-source AI)"
if (Have ollama) {
    Write-Host "$dot $ok Found Ollama. Pull a model with, for example:  ollama pull llama3.2"
} else {
    Write-Host "$dot Not found. For free local AI models with no API key, install Ollama from https://ollama.com"
}

# ---- Done ------------------------------------------------------------
Write-Host ""
Write-Host "=====================================================" -ForegroundColor DarkCyan
Write-Host " Setup complete." -ForegroundColor Green
Write-Host " Start the app by double clicking run_beacon.bat" -ForegroundColor White
Write-Host "=====================================================" -ForegroundColor DarkCyan
Write-Host ""
Read-Host "Press Enter to close"
