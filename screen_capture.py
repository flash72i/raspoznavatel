# -*- coding: utf-8 -*-
"""
screen_capture.py — затемнение экрана, выделение области мышью, снимок.

Схема:
  1. Полноэкранное полупрозрачное окно (tkinter) поверх всех окон.
  2. Пользователь растягивает прямоугольник мышью (Esc — отмена).
  3. Снимок области: основной способ — PIL.ImageGrab (нативный GDI, самый
     надёжный на Windows), запасной — mss.
  4. Снимок сохраняется в папку данных; в интерфейс передаётся только
     МАЛЕНЬКОЕ превью (сжатый JPEG до 640 px) — большие base64-строки
     ломают мост pywebview и замедляют работу.
"""

import base64
import io
import os
import time

import config
import log

# PIL и mss импортируются ЛЕНИВО (при первом использовании) — импорт
# библиотек на старте задерживал появление окна.
_PIL = None          # (Image, ImageGrab) или None
_MSS = None          # (mss, mss.tools) или None
_PIL_CHECKED = False
_MSS_CHECKED = False


def _get_pil():
    global _PIL, _PIL_CHECKED
    if not _PIL_CHECKED:
        _PIL_CHECKED = True
        try:
            from PIL import Image, ImageGrab
            _PIL = (Image, ImageGrab)
        except Exception:  # pragma: no cover
            _PIL = None
    return _PIL


def _get_mss():
    global _MSS, _MSS_CHECKED
    if not _MSS_CHECKED:
        _MSS_CHECKED = True
        try:
            import mss
            import mss.tools
            _MSS = (mss, mss.tools)
        except Exception:  # pragma: no cover
            _MSS = None
    return _MSS


def pil_available() -> bool:
    """Доступен ли Pillow (проверка без кэшированного тяжёлого импорта на старте)."""
    return _get_pil() is not None


def mss_available() -> bool:
    """Доступен ли mss."""
    return _get_mss() is not None


class CaptureError(Exception):
    """Ошибка захвата экрана."""


class RegionSelector:
    """
    Полноэкранный оверлей для выделения прямоугольной области мышью.
    Возвращает {'left','top','width','height'} в экранных пикселях или None.
    """

    def __init__(self):
        self.region = None

    def select(self):
        import tkinter as tk  # локальный импорт — не грузим Tk при старте

        # Windows: делаем процесс DPI-осведомым, чтобы координаты выделения
        # совпадали с физическими пикселями экрана (иначе на масштабе 125/150%
        # рамка «уезжает» от курсора, а снимок получается не той области).
        if os.name == "nt":
            try:
                import ctypes
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)
                except Exception:
                    ctypes.windll.user32.SetProcessDpiAware()
            except Exception:
                pass

        root = tk.Tk()
        root.title("Выделите область")
        root.attributes("-fullscreen", True)
        root.attributes("-alpha", 0.35)          # затемнение экрана
        root.attributes("-topmost", True)
        root.configure(bg="black", cursor="crosshair")

        canvas = tk.Canvas(root, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            root.winfo_screenwidth() // 2, 40,
            text="Зажмите левую кнопку мыши и выделите нужную область экрана",
            fill="#22d3ee", font=("Segoe UI", 15, "bold")
        )
        canvas.create_text(
            root.winfo_screenwidth() // 2, 68,
            text="Отпустите кнопку — область будет распознана.  Esc или правая кнопка — отмена.",
            fill="#9fb0e8", font=("Segoe UI", 11)
        )

        state = {"start": None, "rect_id": None, "size_id": None}

        def on_press(event):
            state["start"] = (event.x_root, event.y_root)
            state["rect_id"] = canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline="#22d3ee", width=2)
            state["size_id"] = canvas.create_text(
                event.x + 6, event.y + 14, anchor="nw", text="",
                fill="#22d3ee", font=("Segoe UI", 10, "bold"))

        def on_drag(event):
            if state["start"] is None or state["rect_id"] is None:
                return
            x0 = state["start"][0] - root.winfo_rootx()
            y0 = state["start"][1] - root.winfo_rooty()
            canvas.coords(state["rect_id"], x0, y0, event.x, event.y)
            if state["size_id"] is not None:
                w = abs(event.x - x0)
                h = abs(event.y - y0)
                canvas.coords(state["size_id"], min(x0, event.x) + 6, min(y0, event.y) + 8)
                canvas.itemconfigure(state["size_id"], text=f"{w} × {h}")

        def on_release(event):
            if state["start"] is None:
                return
            x0, y0 = state["start"]
            x1, y1 = event.x_root, event.y_root
            left, top = min(x0, x1), min(y0, y1)
            width, height = abs(x1 - x0), abs(y1 - y0)
            if width > 5 and height > 5:
                self.region = {"left": left, "top": top,
                               "width": width, "height": height}
            root.destroy()

        def on_escape(_event=None):
            root.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        root.bind("<Escape>", on_escape)
        root.bind("<Button-3>", on_escape)   # правая кнопка — тоже отмена

        log.info("Окно выделения области открыто")
        root.mainloop()
        log.info("Окно выделения области закрыто")
        return self.region


def grab_region(region: dict) -> str:
    """
    Снимок области экрана. Основной способ — PIL.ImageGrab (нативный GDI),
    запасной — mss. Возвращает путь к сохранённому PNG.
    """
    out_path = os.path.join(config.TMP_DIR, f"capture_{int(time.time() * 1000)}.png")
    left, top = int(region["left"]), int(region["top"])
    width, height = int(region["width"]), int(region["height"])

    # --- способ 1: PIL.ImageGrab (самый надёжный на Windows) ---
    pil = _get_pil()
    if pil is not None:
        ImageGrab = pil[1]
        try:
            im = ImageGrab.grab(
                bbox=(left, top, left + width, top + height),
                all_screens=True,
            )
            im.save(out_path, "PNG")
            log.ok(f"Снимок {width}×{height} сохранён (ImageGrab): {out_path}")
            return out_path
        except Exception as exc:
            log.warn(f"ImageGrab не сработал ({exc}) — пробую mss")

    # --- способ 2: mss ---
    m = _get_mss()
    if m is not None:
        mss, mss_tools = m
        try:
            with mss.mss() as sct:
                shot = sct.grab({"left": left, "top": top,
                                 "width": width, "height": height})
                png_bytes = mss_tools.to_png(shot.rgb, shot.size)
            with open(out_path, "wb") as f:
                f.write(png_bytes)
            log.ok(f"Снимок {width}×{height} сохранён (mss): {out_path}")
            return out_path
        except Exception as exc:
            raise CaptureError(f"Не удалось сделать снимок экрана: {exc}") from exc

    raise CaptureError("Компоненты захвата экрана (PIL/mss) недоступны.")


def file_to_base64(path: str) -> str:
    """Читает файл и возвращает data-URL (используется только для мелких файлов)."""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    return f"data:image/{ext};base64,{data}"


def preview_base64(path: str, max_side: int = 640, quality: int = 72) -> str:
    """
    МАЛЕНЬКОЕ превью изображения (JPEG, до max_side px) в виде data-URL.
    Именно оно передаётся в интерфейс — мост pywebview надёжно работает
    только с небольшими строками (десятки-сотни КБ, не мегабайты).
    """
    pil = _get_pil()
    if pil is None:
        return ""
    Image = pil[0]
    try:
        im = Image.open(path)
        im.thumbnail((max_side, max_side))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as exc:
        log.warn(f"Не удалось построить превью: {exc}")
        return ""


def capture_region_interactive():
    """
    Полный цикл: выделение области -> снимок.
    :return: {'path', 'base64'(превью), 'width', 'height'} или None (отмена)
    """
    region = RegionSelector().select()
    if not region:
        return None
    time.sleep(0.25)   # даём окну выделения полностью закрыться
    path = grab_region(region)
    return {
        "path": path,
        "base64": preview_base64(path),
        "width": region["width"],
        "height": region["height"],
    }


def cleanup_temp_captures(max_files: int = 10):
    """Удаляет старые временные снимки."""
    try:
        files = [
            os.path.join(config.TMP_DIR, f)
            for f in os.listdir(config.TMP_DIR)
            if (f.startswith("capture_") or f.startswith("drop_")) and f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tiff"))
        ]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[max_files:]:
            os.remove(old)
    except OSError:
        pass
