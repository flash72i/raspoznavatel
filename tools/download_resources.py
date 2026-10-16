# -*- coding: utf-8 -*-
"""
tools/download_resources.py — ОДНОРАЗОВАЯ подготовка ресурсов для сборки.

Скачивает в папки проекта (рядом с исходниками):
  • resources/tessdata/rus.traineddata и eng.traineddata (языковые модели OCR);
  • resources/argos/translate-en_ru-*.argosmodel (офлайн-модель перевода EN->RU);
  • ui/assets/fonts/*.woff2 (локальные шрифты Inter, кириллица + латиница);
  • копирует портативный Tesseract-OCR в resources/tesseract, если он уже
    установлен в системе (C:\\Program Files\\Tesseract-OCR).

ВНИМАНИЕ: интернет нужен ТОЛЬКО один раз — при подготовке сборки.
Само приложение «Распознаватель» работает полностью офлайн.

Запуск:  python tools/download_resources.py
"""

import json
import os
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCES = os.path.join(ROOT, "resources")
TESSDATA = os.path.join(RESOURCES, "tessdata")
ARGOS = os.path.join(RESOURCES, "argos")
Tesseract_DST = os.path.join(RESOURCES, "tesseract")
FONTS = os.path.join(ROOT, "ui", "assets", "fonts")

UA = {"User-Agent": "Mozilla/5.0 (подготовка сборки Распознаватель)"}


def download(url: str, dst: str, desc: str) -> bool:
    """Скачивает файл с показом прогресса. Возвращает True при успехе."""
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        print(f"[пропущено] {desc} уже существует: {dst}")
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    print(f"[скачивание] {desc}\n    {url}")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=600) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dst + ".part", "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r    {done // 1024 // 1024} МБ / {total // 1024 // 1024} МБ ({pct}%)",
                              end="", flush=True)
        print()
        os.replace(dst + ".part", dst)
        print(f"[готово] {desc}")
        return True
    except Exception as exc:
        print(f"[ОШИБКА] не удалось скачать {desc}: {exc}")
        if os.path.isfile(dst + ".part"):
            os.remove(dst + ".part")
        return False


def step_tessdata():
    """Языковые модели Tesseract: русский и английский (быстрый вариант)."""
    print("\n=== 1/4. Языковые модели OCR (tessdata) ===")
    for lang in ("rus", "eng"):
        download(
            f"https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata",
            os.path.join(TESSDATA, f"{lang}.traineddata"),
            f"tessdata_fast / {lang}.traineddata",
        )


def step_argos():
    """Офлайн-модель перевода EN -> RU из официального индекса Argos."""
    print("\n=== 2/4. Модель перевода Argos EN->RU (~200 МБ) ===")
    try:
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
            headers=UA)
        idx = json.loads(urllib.request.urlopen(req, timeout=60).read())
        for p in idx:
            if p.get("from_code") == "en" and p.get("to_code") == "ru":
                link = p["links"][0]
                name = link.rsplit("/", 1)[-1]
                download(link, os.path.join(ARGOS, name), f"Argos {name}")
                return
        print("[ОШИБКА] модель en->ru не найдена в индексе Argos")
    except Exception as exc:
        print(f"[ОШИБКА] не удалось получить индекс Argos: {exc}")


def step_fonts():
    """Шрифты Inter (woff2, кириллица + латиница) — встраиваются в exe."""
    print("\n=== 3/4. Локальные шрифты Inter ===")
    base = "https://cdn.jsdelivr.net/npm/@fontsource/inter@5.0.16/files/"
    for weight in (400, 600, 800):
        for subset in ("cyrillic", "latin"):
            name = f"inter-{subset}-{weight}-normal.woff2"
            download(base + name, os.path.join(FONTS, name), f"шрифт {name}")


def step_tesseract():
    """
    Портативный движок Tesseract-OCR — полностью автоматически:
      1) если уже лежит в resources/tesseract — пропускаем;
      2) если Tesseract установлен в системе — копируем оттуда;
      3) иначе — скачиваем официальный установщик UB Mannheim и ставим
         его ТИХО (/VERYSILENT, появится один запрос UAC), затем копируем
         папку движка в ресурсы проекта.
    """
    print("\n=== 4/4. Движок Tesseract-OCR ===")
    dst_exe = os.path.join(Tesseract_DST, "tesseract.exe")
    if os.path.isfile(dst_exe):
        print(f"[пропущено] Tesseract уже лежит в {Tesseract_DST}")
        return True

    # --- поиск установленного в системе ---
    for src in (
        r"C:\Program Files\Tesseract-OCR",
        r"C:\Program Files (x86)\Tesseract-OCR",
    ):
        if os.path.isfile(os.path.join(src, "tesseract.exe")):
            print(f"[копирование] {src} -> {Tesseract_DST}")
            shutil.copytree(src, Tesseract_DST, dirs_exist_ok=True)
            print("[готово] Tesseract скопирован в ресурсы проекта")
            return True

    # --- автоматическое скачивание и тихая установка ---
    if os.name != "nt":
        print("[пропущено] Автоматическая установка Tesseract доступна только в Windows.")
        print("           При сборке через GitHub Actions движок ставится шагом choco.")
        return False

    installer_url = ("https://digi.bib.uni-mannheim.de/tesseract/"
                     "tesseract-ocr-w64-setup-5.4.0.20240606.exe")
    installer = os.path.join(ROOT, "tools", "tesseract-ocr-w64-setup.exe")
    if not download(installer_url, installer, "установщик Tesseract-OCR 5.4.0 (x64)"):
        return False

    print("[установка] Тихая установка Tesseract (появится один запрос UAC — нажмите «Да»)…")
    import subprocess
    try:
        subprocess.run(
            [installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            check=True, timeout=600,
        )
    except Exception as exc:
        print(f"[ВНИМАНИЕ] Тихая установка не удалась: {exc}")
        print("           Запустите установщик вручную: " + installer)
        return False

    for src in (
        r"C:\Program Files\Tesseract-OCR",
        r"C:\Program Files (x86)\Tesseract-OCR",
    ):
        if os.path.isfile(os.path.join(src, "tesseract.exe")):
            print(f"[копирование] {src} -> {Tesseract_DST}")
            shutil.copytree(src, Tesseract_DST, dirs_exist_ok=True)
            print("[готово] Tesseract скопирован в ресурсы проекта")
            return True

    print("[ВНИМАНИЕ] Установка завершилась, но папка Tesseract-OCR не найдена.")
    return False


def main():
    print("Подготовка ресурсов приложения «Распознаватель»…")
    print(f"Корень проекта: {ROOT}")
    step_tessdata()
    step_argos()
    step_fonts()
    step_tesseract()
    print("\nПроверка состава ресурсов:")
    ok = True
    for path, desc in [
        (os.path.join(TESSDATA, "rus.traineddata"), "русский OCR"),
        (os.path.join(TESSDATA, "eng.traineddata"), "английский OCR"),
        (os.path.join(FONTS, "inter-cyrillic-400-normal.woff2"), "шрифты"),
        (os.path.join(Tesseract_DST, "tesseract.exe"), "движок Tesseract"),
    ]:
        mark = "ОК " if os.path.isfile(path) else "НЕТ"
        if mark == "НЕТ":
            ok = False
        print(f"  [{mark}] {desc}: {path}")
    models = [f for f in os.listdir(ARGOS)] if os.path.isdir(ARGOS) else []
    mark = "ОК " if models else "НЕТ"
    if not models:
        ok = False
    print(f"  [{mark}] модель перевода: {ARGOS} {models}")
    print("\nВСЁ ГОТОВО к сборке!" if ok else
          "\nНекоторые ресурсы отсутствуют — см. сообщения выше. Сборка возможна, "
          "но соответствующие функции не будут работать.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
