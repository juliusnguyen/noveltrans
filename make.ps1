<#
Windows equivalent of the Makefile (GNU `make` isn't assumed to be installed). Mirrors
its targets 1:1, using `.venv\Scripts\...` instead of `.venv/bin/...`. The Makefile stays
authoritative for macOS/Linux; this script is Windows-only and not a general port.

Usage: .\make.ps1 <target>   e.g. .\make.ps1 run
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("run", "setup", "test", "test-live", "lint", "icon", "app", "zip", "clean")]
    [string]$Target = "run"
)

$ErrorActionPreference = "Stop"

# Absolute, not relative: several targets (e.g. "app") Push-Location into packaging/
# before invoking these, and a relative ".venv\..." would silently resolve against the
# wrong directory instead of erroring — PowerShell's `&` reports a missing relative exe
# path as a confusing "module could not be loaded" instead of "command not found".
$Venv = Join-Path $PSScriptRoot ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Pyinstaller = Join-Path $Venv "Scripts\pyinstaller.exe"
$Ruff = Join-Path $Venv "Scripts\ruff.exe"
$NovelTrans = Join-Path $Venv "Scripts\noveltrans.exe"

function Invoke-Setup {
    if (-not (Test-Path $Venv)) {
        uv venv --python 3.12 $Venv
    }
    uv pip install -e ".[dev]"
}

switch ($Target) {
    "setup" {
        Invoke-Setup
    }
    "run" {
        # Mở ứng dụng (tự cài đặt lần đầu nếu chưa có venv)
        if (-not (Test-Path $NovelTrans)) { Invoke-Setup }
        & $NovelTrans
    }
    "test" {
        # Chạy toàn bộ test offline
        & $Py -m pytest
    }
    "test-live" {
        # Test chạm site thật (kiểm tra site có đổi giao diện)
        & $Py -m pytest -m live
    }
    "lint" {
        # Kiểm tra lint
        & $Ruff check src tests
    }
    "icon" {
        # Build packaging/NovelTrans.ico from the already-committed NovelTrans.png.
        #
        # Deliberately does NOT re-run make_icon.py on Windows: that script draws the
        # glyph with the macOS-only "Songti SC" font, and this offscreen Qt platform
        # can't find any font at all to fall back to (QFontDatabase: Cannot find font
        # directory ...) — it silently renders blank tofu boxes instead of erroring.
        # Re-running it here would replace the correct, macOS-rendered PNG with a
        # broken one. If NovelTrans.png ever needs to change, regenerate it on macOS
        # via `make icon` and commit the result; this just derives the .ico from it.
        & $Py packaging/make_ico.py packaging/NovelTrans.png
    }
    "app" {
        # Đóng gói thành dist/NovelTrans/NovelTrans.exe (cần TTS: uv pip install -e ".[tts]")
        if (-not (Test-Path $Pyinstaller)) {
            uv pip install --python $Py pyinstaller
        }
        Push-Location packaging
        try {
            & $Pyinstaller --noconfirm --clean `
                --distpath ../dist --workpath ../build/pyinstaller NovelTrans-windows.spec
        } finally {
            Pop-Location
        }
        Write-Host "-> dist/NovelTrans/NovelTrans.exe"
    }
    "zip" {
        # Đóng gói .exe rồi nén thành dist/NovelTrans-windows.zip
        & $PSCommandPath app
        $zipPath = "dist/NovelTrans-windows.zip"
        if (Test-Path $zipPath) { Remove-Item $zipPath }
        Compress-Archive -Path "dist/NovelTrans" -DestinationPath $zipPath
        Write-Host "-> $zipPath"
    }
    "clean" {
        # Xoá venv và cache
        foreach ($p in @($Venv, ".pytest_cache", ".ruff_cache", "build", "dist")) {
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }
        Get-ChildItem -Path "src" -Filter "*.egg-info" -Directory -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force
    }
}
