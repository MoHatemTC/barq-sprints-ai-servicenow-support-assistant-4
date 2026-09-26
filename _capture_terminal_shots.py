"""Capture real Windows console windows showing the live demo / pytest output."""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parent
SHOT_DIR = ROOT / "Screenshots"
SHOT_DIR.mkdir(exist_ok=True)

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
GetWindowText = user32.GetWindowTextW
GetWindowTextLength = user32.GetWindowTextLengthW
IsWindowVisible = user32.IsWindowVisible
GetWindowRect = user32.GetWindowRect
SetForegroundWindow = user32.SetForegroundWindow
ShowWindow = user32.ShowWindow
SW_RESTORE = 9
SW_SHOW = 5


def _window_title(hwnd: int) -> str:
    length = GetWindowTextLength(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    GetWindowText(hwnd, buf, length + 1)
    return buf.value


def find_window_by_title_substr(substr: str) -> int | None:
    found = []

    def callback(hwnd, _lparam):
        if IsWindowVisible(hwnd):
            title = _window_title(hwnd)
            if substr in title:
                found.append(hwnd)
        return True

    EnumWindows(EnumWindowsProc(callback), 0)
    return found[0] if found else None


def grab_window(hwnd: int, dest: Path) -> None:
    ShowWindow(hwnd, SW_RESTORE)
    ShowWindow(hwnd, SW_SHOW)
    SetForegroundWindow(hwnd)
    time.sleep(0.6)
    rect = wintypes.RECT()
    GetWindowRect(hwnd, ctypes.byref(rect))
    bbox = (rect.left, rect.top, rect.right, rect.bottom)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(dest)
    print(f"saved {dest} size={img.size} bbox={bbox}")


def open_console(title: str, text_file: Path) -> subprocess.Popen:
    # Visible PowerShell window showing the real command output file.
    ps = (
        f"$Host.UI.RawUI.WindowTitle = '{title}'; "
        "$Host.UI.RawUI.BackgroundColor = 'Black'; "
        "$Host.UI.RawUI.ForegroundColor = 'Gray'; "
        "Clear-Host; "
        f"Get-Content -LiteralPath '{text_file}' -Encoding UTF8; "
        "Write-Host ''; "
        "Write-Host '--- end of captured terminal output ---'"
    )
    return subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoExit",
            "-Command",
            ps,
        ],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def capture_file(title: str, text_file: Path, png_name: str) -> None:
    proc = open_console(title, text_file)
    hwnd = None
    for _ in range(40):
        time.sleep(0.25)
        hwnd = find_window_by_title_substr(title)
        if hwnd:
            break
    if not hwnd:
        proc.terminate()
        raise RuntimeError(f"Could not find console window titled {title}")
    grab_window(hwnd, SHOT_DIR / png_name)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    mapping = [
        ("BARQ_S1_GROUNDED", SHOT_DIR / "_01.txt", "01_grounded_run.png"),
        ("BARQ_S2_DECLINE", SHOT_DIR / "_02.txt", "02_deterministic_decline.png"),
        ("BARQ_S3_INJECTION", SHOT_DIR / "_03.txt", "03_prompt_injection_defense.png"),
        ("BARQ_S4_IRRELEVANT", SHOT_DIR / "_04.txt", "04_irrelevant_kb_decline.png"),
        ("BARQ_S5_PYTEST", SHOT_DIR / "_05.txt", "05_pytest_results.png"),
    ]
    for title, src, png in mapping:
        print("capturing", png)
        capture_file(title, src, png)
    print("done")


if __name__ == "__main__":
    main()
