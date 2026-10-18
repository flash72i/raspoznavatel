#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capture_e2e.py — сквозной тест захвата экрана (Xvfb + xdotool).

Проверяет ВСЮ цепочку, которая падает у пользователя:
  оверлей выделения (tkinter) -> drag мышью -> снимок (ImageGrab/mss) -> файл.
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import screen_capture  # noqa: E402

result = {"shot": None, "error": None}


def worker():
    try:
        result["shot"] = screen_capture.capture_region_interactive()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"


def main():
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(2.5)   # даём оверлею открыться

    # симулируем выделение области мышью (как это делает пользователь)
    cmds = [
        ["xdotool", "mousemove", "200", "150"],
        ["xdotool", "mousedown", "1"],
        ["xdotool", "mousemove", "--sync", "620", "430"],
        ["xdotool", "mouseup", "1"],
    ]
    for c in cmds:
        subprocess.run(c, check=False, capture_output=True)
        time.sleep(0.4)

    t.join(timeout=20)

    if result["error"]:
        print("✗ ОШИБКА захвата:", result["error"])
        return 1
    shot = result["shot"]
    if not shot:
        print("✗ Выделение вернуло None (отмена?)")
        return 1
    print("✓ область выделена:", shot["width"], "×", shot["height"], "px")
    if not os.path.isfile(shot["path"]):
        print("✗ файл снимка не создан")
        return 1
    print("✓ снимок сохранён:", shot["path"], os.path.getsize(shot["path"]), "байт")
    if not shot["base64"].startswith("data:image/jpeg;base64,"):
        print("✗ превью не JPEG data-url")
        return 1
    print("✓ превью:", len(shot["base64"]) // 1024, "КБ (компактное)")
    if len(shot["base64"]) > 400 * 1024:
        print("✗ превью слишком большое (>400 КБ) — мост pywebview мог бы сломаться")
        return 1
    print("✓ превью достаточно маленькое для моста pywebview")
    print("\nИТОГ: захват экрана работает от выделения до файла")
    return 0


if __name__ == "__main__":
    sys.exit(main())
