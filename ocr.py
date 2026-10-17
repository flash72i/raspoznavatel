# -*- coding: utf-8 -*-
"""
ocr.py — офлайн-распознавание текста (OCR) через локальный Tesseract (v2.3).

Поддерживается ЛЮБОЙ язык, для которого в папке resources/tessdata лежит
файл <код>.traineddata (по умолчанию: русский, английский, китайский,
украинский, немецкий, французский, испанский, итальянский, португальский,
польский, турецкий, нидерландский, чешский, белорусский, казахский).
Новые языки добавляются простым копированием .traineddata в эту папку.

Режим «авто» (v2.3 — быстрее и точнее на смешанном тексте):
  1. ТРИ параллельных пробных прогона на уменьшенной копии (rus/eng/chi_sim)
     вместо последовательного «турнира» из 8-14 прогонов;
  2. по письменностям и уверенностям выбирается НАБОР языков (1-3),
     например «rus+eng» для смешанного текста;
  3. ФИНАЛЬНЫЙ проход выполняется сразу всеми выбранными языками —
     Tesseract сам разбирает смешанный текст (русский + английский +
     китайский на одной картинке распознаются вместе, а не «одним языком»).
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import log

# Переменная окружения для Tesseract: где лежат .traineddata
os.environ.setdefault("TESSDATA_PREFIX", config.TESSDATA_DIR)

try:
    import pytesseract
    from pytesseract import Output

    _cmd = config.find_tesseract_cmd()
    if _cmd:
        pytesseract.pytesseract.tesseract_cmd = _cmd
    PYTESSERACT_AVAILABLE = True
except Exception:  # pragma: no cover
    PYTESSERACT_AVAILABLE = False

# cv2/numpy импортируются ЛЕНИВО (тяжёлый импорт ~1-3 с — не тормозим запуск)
_cv2 = None
_np = None


def _get_cv2():
    """Ленивая загрузка OpenCV: возвращает (cv2, np) или (None, None)."""
    global _cv2, _np
    if _cv2 is None:
        try:
            import cv2
            import numpy as np
            _cv2, _np = cv2, np
        except Exception:
            return None, None
    return _cv2, _np


class OcrError(Exception):
    """Ошибка распознавания текста."""


# Языки-зонды для определения письменности (идут параллельно)
PROBE_LANGS = ("rus", "eng", "chi_sim")

# Отличительные символы языков (латиница) — для уточнения европейского языка
DIACRITIC_HINTS = {
    "deu": set("äöüßÄÖÜ"),
    "fra": set("àâçèêëîïôùûœÀÂÇÈÊÎÏÔÙÛŒ"),
    "spa": set("ñ¿¡Ñ"),
    "ita": set("àèìòùÀÈÌÒÙ"),
    "por": set("ãõÃÕ"),
    "pol": set("ąćęłńśźżĄĆĘŁŃŚŹŻ"),
    "ces": set("ěřůďťňĚŘŮĎŤŇ"),
    "tur": set("ğışĞİŞ"),
}
# Отличительные символы кириллических языков
CYRILLIC_HINTS = {
    "ukr": set("іїєґІЇЄҐ"),
    "bel": set("ўіЫЎІ"),
    "kaz": set("әғқңөұүһіӘҒҚҢӨҰҮҺ"),
}


def available_lang_codes():
    """Коды языков, доступные по файлам .traineddata."""
    return [l["code"] for l in config.get_available_langs()]


def _resolve_langs(langs):
    """
    Приводит входной параметр языков к строке Tesseract.
    langs: 'auto' | ['rus','eng'] | 'rus+eng' | None
    Возвращает (lang_str, is_auto)
    """
    if langs == "auto" or not langs:
        return "auto", True
    if isinstance(langs, str):
        langs = [c.strip() for c in langs.split("+") if c.strip()]
    avail = set(available_lang_codes())
    picked = [c for c in langs if c in avail]
    if not picked:
        picked = [c for c in ("rus", "eng") if c in avail] or list(avail)[:1]
    return "+".join(picked), False


def _check_lang_files(lang_str: str):
    for lang in lang_str.split("+"):
        path = os.path.join(config.TESSDATA_DIR, f"{lang}.traineddata")
        if not os.path.isfile(path):
            raise OcrError(
                f"Не найден языковой файл «{lang}.traineddata». "
                "Положите его в папку App/resources/tessdata."
            )


def _load_gray_small(image_path: str, max_side: int = 520):
    """Загружает уменьшенную копию в градациях серого (для быстрых проб)."""
    cv2, np = _get_cv2()
    if cv2 is None:
        return None
    try:
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        h, w = img.shape[:2]
        m = max(h, w)
        if m > max_side:
            img = cv2.resize(img, (int(w * max_side / m), int(h * max_side / m)),
                             interpolation=cv2.INTER_AREA)
        return img
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Определение языков (v2.3: параллельные пробы + комбинированный набор)
# ---------------------------------------------------------------------------

def _script_counts(text: str) -> dict:
    """Счётчики символов по письменностям (чистая функция — тестируема)."""
    c = {"cyr": 0, "lat": 0, "cjk": 0}
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            c["cyr"] += 1
        elif (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A) or (0x00C0 <= o <= 0x024F):
            c["lat"] += 1
        elif (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0xF900 <= o <= 0xFAFF):
            c["cjk"] += 1
    return c


def choose_langs(probes: dict, available: list) -> list:
    """
    Чистая функция выбора языков по результатам проб.
    :param probes: {lang: (text, confidence)}
    :param available: список доступных кодов языков
    :return: упорядоченный список (1-3 языка) для КОМБИНИРОВАННОГО финального
             прохода, например ['rus', 'eng'] для смешанного текста.
    """
    if not available:
        return ["eng"]
    confs = {l: float(c) for l, (t, c) in probes.items()}
    all_text = " ".join(t for t, _ in probes.values())
    sc = _script_counts(all_text)
    letters = max(1, sc["cyr"] + sc["lat"] + sc["cjk"])
    cyr_r, lat_r, cjk_r = sc["cyr"] / letters, sc["lat"] / letters, sc["cjk"] / letters

    chosen = []
    # CJK: китайская проба на НЕкитайском тексте «галлюцинирует» иероглифами,
    # поэтому её собственный текст НЕ считается доказательством письменности.
    # Верим только уверенной пробе (≥68% и ≥3 иероглифов) либо иероглифам,
    # которые увидели ДРУГИЕ движки.
    chi_text, chi_conf = probes.get("chi_sim", ("", -1.0))
    chi_cjk = _script_counts(chi_text)["cjk"]
    other_text = " ".join(t for l, (t, c) in probes.items() if l != "chi_sim")
    other_cjk = _script_counts(other_text)["cjk"]
    if "chi_sim" in available and ((chi_conf >= 68 and chi_cjk >= 3) or other_cjk >= 5):
        chosen.append("chi_sim")
    # письменность считается присутствующей, если её доля заметна
    # ИЛИ соответствующая проба дала высокую уверенность
    if "rus" in available and (cyr_r >= 0.10 or confs.get("rus", -1) >= 58):
        chosen.append("rus")
    if "eng" in available and (lat_r >= 0.10 or confs.get("eng", -1) >= 58):
        chosen.append("eng")

    # кириллические «родственники»: отличительные буквы в тексте пробы rus
    if cyr_r >= 0.10 or "rus" in chosen:
        rus_text = probes.get("rus", ("", 0))[0]
        for lang, marks in CYRILLIC_HINTS.items():
            if lang in available and (marks & set(rus_text)) and len(chosen) < 3:
                if lang not in chosen:
                    chosen.append(lang)

    # европейская диакритика: добавляем ОДИН самый вероятный язык
    if lat_r >= 0.10:
        votes = []
        chars = set(all_text)
        for lang, marks in DIACRITIC_HINTS.items():
            hits = len(chars & marks)
            if hits and lang in available:
                votes.append((hits, lang))
        votes.sort(reverse=True)
        for _, lang in votes[:1]:
            if lang not in chosen and len(chosen) < 3:
                chosen.append(lang)

    if not chosen:
        # ничего не уверенно — берём язык с лучшей пробной уверенностью,
        # иначе первый доступный (rus/eng в приоритете)
        if confs:
            best = max(confs.items(), key=lambda kv: kv[1])[0]
            if best in available:
                chosen = [best]
        if not chosen:
            chosen = [c for c in ("rus", "eng") if c in available][:1] or [available[0]]

    # сортировка по уверенности (первый язык Tesseract слегка приоритетнее)
    chosen.sort(key=lambda l: -confs.get(l, 0))
    return chosen[:3]


def _probe_one(pil_img, lang):
    """Один пробный прогон на уменьшенной копии. Возвращает (текст, уверенность)."""
    try:
        data = pytesseract.image_to_data(
            pil_img, lang=lang, output_type=Output.DICT,
            config="--psm 3 --oem 1"
        )
        words = []
        for t, c in zip(data.get("text", []), data.get("conf", [])):
            t = str(t or "").strip()
            if t and float(c) >= 0:
                words.append((t, float(c)))
        text = " ".join(w for w, _ in words)
        conf = sum(c for _, c in words) / len(words) if words else -1.0
        return text, conf
    except Exception as exc:
        log.warn(f"Проба языка «{lang}» не удалась: {exc}")
        return "", -1.0


def detect_language(image_path: str, progress_cb=None) -> dict:
    """
    Автоопределение набора языков изображения (v2.3).
    Три параллельных пробных прогона (rus/eng/chi_sim) на уменьшенной копии
    → чистая функция choose_langs() → комбинированный набор языков.

    :param progress_cb: необязательный колбэк (pct:int, text:str)
    :return: {'lang': 'rus+eng', 'langs': [...], 'scores': [...], 'tried': N}
    """
    if not PYTESSERACT_AVAILABLE:
        raise OcrError("Компонент OCR (pytesseract) недоступен.")
    codes = available_lang_codes()
    if not codes:
        raise OcrError("В папке tessdata нет ни одного языка (.traineddata).")
    if len(codes) == 1:
        return {"lang": codes[0], "langs": codes[:1], "scores": [(codes[0], 0.0)], "tried": 1}

    from PIL import Image
    small = _load_gray_small(image_path, max_side=480)
    pil_img = Image.fromarray(small) if small is not None else Image.open(image_path)

    probe_langs = [l for l in PROBE_LANGS if l in codes] or codes[:3]

    def _report(pct, txt):
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    probes = {}
    with ThreadPoolExecutor(max_workers=min(3, len(probe_langs))) as ex:
        futs = {ex.submit(_probe_one, pil_img, l): l for l in probe_langs}
        done = 0
        for fut in as_completed(futs):
            probes[futs[fut]] = fut.result()
            done += 1
            _report(10 + int(35 * done / len(futs)),
                    f"Определяю язык… ({done}/{len(futs)})")

    langs = choose_langs(probes, codes)
    scores = sorted(((l, c) for l, (t, c) in probes.items()), key=lambda x: -x[1])
    log.info(f"Автоопределение языков: «{'+'.join(langs)}» "
             f"(пробы: {', '.join(f'{l}={round(c, 1)}' for l, c in scores)})")
    return {"lang": "+".join(langs), "langs": langs, "scores": scores, "tried": len(probes)}


def _preprocess(image_path: str) -> str:
    """
    Лёгкая предобработка для повышения качества OCR: градации серого,
    увеличение мелких изображений, бинаризация по Оцу.
    Возвращает путь к подготовленному файлу (или исходный путь).
    """
    cv2, np = _get_cv2()
    if cv2 is None:
        return image_path
    try:
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return image_path
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        if max(h, w) < 1000:
            scale = 1000.0 / max(h, w)
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        out_path = os.path.join(config.TMP_DIR, "_ocr_prepared.png")
        ok, buf = cv2.imencode(".png", binary)
        if ok:
            buf.tofile(out_path)
            return out_path
    except Exception:
        pass
    return image_path


def _text_from_data(data) -> str:
    """
    Собирает полный текст из результата image_to_data (по номерам
    блоков/абзацев/строк). Позволяет НЕ запускать tesseract второй раз
    (image_to_string) — финальный проход становится вдвое быстрее,
    а слова и уверенность берутся из одного и того же прогона.
    """
    lines = {}
    try:
        for i, txt in enumerate(data.get("text", [])):
            txt = str(txt or "").strip()
            if not txt:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(txt)
    except Exception:
        return ""
    return "\n".join(" ".join(words) for words in lines.values())


def ocr_image(image_path: str, langs=None, progress_cb=None) -> dict:
    """
    Распознаёт ВЕСЬ текст на изображении (поддерживает смешанные языки).

    :param image_path: путь к файлу изображения
    :param langs: 'auto' — определить автоматически (набор языков),
                  либо список/строку ['rus','eng'] / 'rus+eng'
    :param progress_cb: колбэк (pct:int, text:str) для шкалы прогресса
    :return: {'text', 'lang', 'confidence', 'words', 'detected'}
    """
    if not PYTESSERACT_AVAILABLE:
        raise OcrError("Компонент OCR (pytesseract) недоступен.")
    if not os.path.isfile(image_path):
        raise OcrError(f"Файл не найден: {image_path}")

    def _report(pct, txt):
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    _report(5, "Готовлю изображение…")
    detected = None
    lang_str, is_auto = _resolve_langs(langs)
    if is_auto:
        det = detect_language(image_path, progress_cb=progress_cb)
        detected = det["lang"]
        lang_str = detected
    _check_lang_files(lang_str)
    log.info(f"OCR: файл «{os.path.basename(image_path)}», языки «{lang_str}»")

    _report(50, f"Распознаю текст ({lang_str.replace('+', ' + ')})…")
    prepared = _preprocess(image_path)
    try:
        from PIL import Image
        with Image.open(prepared) as im:
            data = pytesseract.image_to_data(
                im, lang=lang_str, output_type=Output.DICT,
                config="--psm 3 --oem 1"
            )
            _report(88, "Собираю результат…")
            # текст собираем из тех же данных — второй запуск tesseract не нужен
            full = _text_from_data(data)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrError(
            "Не найден движок Tesseract-OCR (папка App/resources/tesseract)."
        ) from exc
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError(f"Сбой распознавания текста: {exc}") from exc
    finally:
        if prepared != image_path and os.path.isfile(prepared):
            try:
                os.remove(prepared)
            except OSError:
                pass

    text = re.sub(r"[ \t]+\n", "\n", (full or "").strip())

    confs = []
    try:
        for c, w in zip(data.get("conf", []), data.get("text", [])):
            c = float(c)
            if c >= 0 and str(w).strip():
                confs.append(c)
    except Exception:
        pass
    confidence = round(sum(confs) / len(confs), 1) if confs else None

    _report(100, "")
    log.ok(f"OCR завершён: {len(text)} симв., уверенность {confidence}%, языки {lang_str}")
    return {
        "text": text,
        "lang": lang_str,
        "confidence": confidence,
        "words": len(confs),
        "detected": detected,
    }


def check_engine() -> dict:
    """Проверка доступности движка OCR (для диагностики)."""
    result = {"ok": False, "version": None, "langs": [], "error": None}
    if not PYTESSERACT_AVAILABLE:
        result["error"] = "pytesseract не установлен"
        return result
    try:
        result["version"] = str(pytesseract.get_tesseract_version())
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"Tesseract не найден: {exc}"
        return result
    result["langs"] = available_lang_codes()
    return result
