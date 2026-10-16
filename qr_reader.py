# -*- coding: utf-8 -*-
"""
qr_reader.py — распознавание QR-кодов и штрих-кодов (EAN-13, EAN-8, Code128 и др.).

Используются несколько движков по приоритету (полностью офлайн):
  1. zxing-cpp — быстрый и всеядный декодер (QR, EAN, Code128, ...);
  2. pyzbar (ZBar) — классический декодер;
  3. cv2.QRCodeDetector — запасной вариант из OpenCV.
"""

import importlib.util
import os

import config
import log


def _spec(name):
    """Проверка доступности пакета БЕЗ тяжёлого импорта (для диагностики)."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# Тяжёлые библиотеки импортируются лениво — при первом распознавании
_cv2 = _np = None
_zxingcpp = None
_pyzbar = None


def _get_cv2():
    global _cv2, _np
    if _cv2 is None:
        try:
            import cv2
            import numpy as np
            _cv2, _np = cv2, np
        except Exception as exc:
            log.warn(f"OpenCV недоступен: {exc}")
            return None, None
    return _cv2, _np


def _get_zxing():
    global _zxingcpp
    if _zxingcpp is None:
        try:
            import zxingcpp
            _zxingcpp = zxingcpp
        except Exception as exc:
            log.warn(f"zxing-cpp недоступен: {exc}")
            return None
    return _zxingcpp


def _get_pyzbar():
    global _pyzbar
    if _pyzbar is None:
        try:
            from pyzbar import pyzbar
            _pyzbar = pyzbar
        except Exception as exc:
            log.warn(f"pyzbar недоступен: {exc}")
            return None
    return _pyzbar


class QrError(Exception):
    """Ошибка распознавания кодов."""


def _load_image(path: str):
    """Загружает изображение в BGR-массив OpenCV (пути с кириллицей поддерживаются)."""
    cv2, np = _get_cv2()
    if cv2 is None:
        raise QrError("Компонент OpenCV недоступен.")
    if not os.path.isfile(path):
        raise QrError(f"Файл не найден: {path}")
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise QrError("Не удалось открыть изображение. Поддерживаются PNG, JPG, BMP, GIF, TIFF, WEBP.")
    return img


def _normalize(text) -> str:
    """Аккуратно превращает результат декодера в строку."""
    if isinstance(text, bytes):
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return text.decode(enc)
            except UnicodeDecodeError:
                continue
        return text.decode("utf-8", errors="replace")
    return str(text or "")


def _decode_zxing(img) -> list:
    """Декодирование через zxing-cpp."""
    results = []
    zxingcpp = _get_zxing()
    if zxingcpp is None:
        return results
    try:
        for r in zxingcpp.read_barcodes(img):
            results.append({
                "type": str(getattr(r.format, "name", "неизвестно")).replace("_", "-"),
                "text": _normalize(r.text),
                "engine": "zxing-cpp",
            })
    except Exception:
        pass
    return results


def _decode_pyzbar(img) -> list:
    """Декодирование через pyzbar/ZBar."""
    results = []
    pyzbar = _get_pyzbar()
    if pyzbar is None:
        return results
    try:
        for r in pyzbar.decode(img):
            results.append({
                "type": str(getattr(r, "type", "неизвестно")),
                "text": _normalize(r.data),
                "engine": "pyzbar",
            })
    except Exception:
        pass
    return results


def _decode_cv2(img) -> list:
    """Запасное декодирование QR через OpenCV."""
    results = []
    cv2, _ = _get_cv2()
    if cv2 is None:
        return results
    try:
        detector = cv2.QRCodeDetector()
        ok, texts, _, _ = detector.detectAndDecodeMulti(img)
        if ok:
            for t in texts:
                if t:
                    results.append({"type": "QR-CODE", "text": _normalize(t), "engine": "opencv"})
    except Exception:
        pass
    return results


def _preprocess_variants(img):
    """
    Генерирует варианты изображения для повышения шансов распознавания
    «сложных» кодов: оригинал, увеличенный, в градациях серого, бинаризованный.
    """
    variants = [img]
    cv2, _ = _get_cv2()
    if cv2 is None:
        return variants
    try:
        h, w = img.shape[:2]
        if max(h, w) < 800:
            scale = 800.0 / max(h, w)
            variants.append(cv2.resize(img, None, fx=scale, fy=scale,
                                       interpolation=cv2.INTER_CUBIC))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        variants.append(gray)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(binary)
    except Exception:
        pass
    return variants


def decode_image(image_path: str) -> dict:
    """
    Распознаёт все QR-коды и штрих-коды на изображении.

    :return: {'results': [{'type', 'text', 'engine'}, ...], 'count': N}
    """
    img = _load_image(image_path)
    found = []
    seen = set()

    def add(items):
        for it in items:
            key = (it["type"].upper(), it["text"])
            if it["text"] and key not in seen:
                seen.add(key)
                found.append(it)

    for variant in _preprocess_variants(img):
        add(_decode_zxing(variant))
        add(_decode_pyzbar(variant))
        if found:
            break  # первый успешный вариант — выходим

    if not found:
        add(_decode_cv2(img))

    return {"results": found, "count": len(found)}


def check_engines() -> dict:
    """Доступность движков распознавания кодов (для вкладки «Настройки»)."""
    zx = _spec("zxingcpp")
    zb = _spec("pyzbar")
    cv = _spec("cv2")
    return {
        "zxing-cpp": zx,
        "pyzbar (ZBar)": zb,
        "opencv": cv,
        "ok": zx or zb or cv,
    }
