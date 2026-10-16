# -*- coding: utf-8 -*-
"""
ocr.py — офлайн-распознавание текста (OCR) через локальный Tesseract.

Поддерживается ЛЮБОЙ язык, для которого в папке resources/tessdata лежит
файл <код>.traineddata (по умолчанию: русский, английский, украинский,
немецкий, французский, испанский, итальянский, португальский, польский,
турецкий, нидерландский, чешский, белорусский, казахский).
Новые языки добавляются простым копированием .traineddata в эту папку.

Режим «авто»: программа сама определяет язык изображения — делает быстрые
пробные прогоны на уменьшенной копии картинки для каждого языка-кандидата
и выбирает язык с наибольшей средней уверенностью.
"""

import os
import re

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


def available_lang_codes():
    """Коды языков, доступные по файлам .traineddata."""
    return [l["code"] for l in config.get_available_langs()]


def _resolve_langs(langs):
    """
    Приводит входной параметр языков к строке Tesseract.
    langs: 'auto' | ['rus','eng'] | None
    Возвращает (lang_str, is_auto)
    """
    if langs == "auto" or not langs:
        return "auto", True
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


def _mean_confidence(data) -> float:
    """Средняя уверенность по словам из результата image_to_data."""
    confs = []
    try:
        for c, w in zip(data.get("conf", []), data.get("text", [])):
            c = float(c)
            if c >= 0 and str(w).strip():
                confs.append(c)
    except Exception:
        pass
    return sum(confs) / len(confs) if confs else -1.0


# Отличительные символы языков (латиница) — для сужения круга кандидатов
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


def _narrow_candidates(probe_text: str, codes: list) -> list:
    """
    Сужает список языков-кандидатов по письменности и диакритике
    пробного распознавания (обычно достаточно одного прогона с 'eng').
    """
    if not probe_text:
        return codes
    letters = [ch for ch in probe_text if ch.isalpha()]
    if not letters:
        return codes
    cyr = sum(1 for ch in letters if "\u0400" <= ch <= "\u04ff")
    lat = sum(1 for ch in letters if "a" <= ch.lower() <= "z" or "\u00c0" <= ch <= "\u024f")

    if cyr > lat:
        # кириллица: базовый набор + язык по отличительным буквам
        group = [c for c in ("rus", "ukr", "bel", "kaz") if c in codes]
        for lang, marks in CYRILLIC_HINTS.items():
            if lang in codes and marks & set(probe_text):
                if lang not in group[:2]:
                    group.insert(0, lang)
        return group or codes
    if lat > 0:
        # латиница: английский + языки, чьи диакритические буквы встретились
        votes = []
        chars = set(probe_text)
        for lang, marks in DIACRITIC_HINTS.items():
            hits = len(chars & marks)
            if hits and lang in codes:
                votes.append((hits, lang))
        votes.sort(reverse=True)
        group = ["eng"] if "eng" in codes else []
        group += [lang for _, lang in votes[:3]]
        # если диакритики нет вовсе — добавим пару популярных для страховки
        if len(group) <= 1:
            group += [c for c in ("deu", "fra", "spa") if c in codes and c not in group][:2]
        return group
    return codes


def detect_language(image_path: str, max_candidates: int = 8) -> dict:
    """
    Автоопределение языка изображения.
    Алгоритм: два пробных прогона (латиница/кириллица), затем ДВА турнира
    уверенности (среди латинских и среди кириллических кандидатов) и выбор
    чемпиона с большим счётом. «Галлюцинации» чужой письменности дают низкую
    уверенность (~40-60), настоящий язык — высокую (~85-96), поэтому метод
    работает надёжно и не требует хрупких порогов.
    :return: {'lang': код, 'scores': [(язык, счёт), ...], 'tried': N}
    """
    if not PYTESSERACT_AVAILABLE:
        raise OcrError("Компонент OCR (pytesseract) недоступен.")

    codes = available_lang_codes()
    if not codes:
        raise OcrError("В папке tessdata нет ни одного языка (.traineddata).")
    if len(codes) == 1:
        return {"lang": codes[0], "scores": [(codes[0], 0.0)], "tried": 1}

    from PIL import Image
    small = _load_gray_small(image_path, max_side=800)
    pil_img = Image.fromarray(small) if small is not None else Image.open(image_path)

    cache = {}

    def probe(lang):
        """Пробный прогон: возвращает (текст, средняя уверенность). Кэширует."""
        if lang in cache:
            return cache[lang + "_text"], cache[lang]
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
        except Exception:
            text, conf = "", -1.0
        cache[lang] = conf
        cache[lang + "_text"] = text
        return text, conf

    # --- пробные прогоны ---
    probe_lat, conf_lat = probe("eng") if "eng" in codes else ("", -1.0)
    cyr_probe_lang = next((c for c in ("rus", "ukr", "bel", "kaz") if c in codes), None)
    probe_cyr, conf_cyr = probe(cyr_probe_lang) if cyr_probe_lang else ("", -1.0)

    def tournament(cands):
        res = []
        for code in cands:
            _, conf = probe(code)
            res.append((code, conf))
        res.sort(key=lambda x: x[1], reverse=True)
        return res

    # --- латинская группа: английский + голоса диакритики + популярные ---
    votes = []
    chars = set(probe_lat)
    for lang, marks in DIACRITIC_HINTS.items():
        hits = len(chars & marks)
        if hits and lang in codes:
            votes.append((hits, lang))
    votes.sort(reverse=True)
    lat_group = (["eng"] if "eng" in codes else []) + [l for _, l in votes[:3]]
    for extra in ("deu", "fra", "spa", "ita", "por", "pol", "tur", "nld", "ces"):
        if len(lat_group) >= max_candidates:
            break
        if extra in codes and extra not in lat_group:
            lat_group.append(extra)

    # --- кириллическая группа: порядок по отличительным буквам ---
    cyr_group = [c for c in ("rus", "ukr", "bel", "kaz") if c in codes]
    for lang, marks in CYRILLIC_HINTS.items():
        if lang in codes and (marks & set(probe_cyr)):
            if lang in cyr_group:
                cyr_group.remove(lang)
            cyr_group.insert(0, lang)

    lat_scores = tournament(lat_group) if lat_group else []
    cyr_scores = tournament(cyr_group) if cyr_group else []
    scores = sorted(lat_scores + cyr_scores, key=lambda x: x[1], reverse=True)
    if not scores:
        scores = [(codes[0], -1.0)]

    best_lat = lat_scores[0] if lat_scores else (None, -1.0)
    best_cyr = cyr_scores[0] if cyr_scores else (None, -1.0)

    # Тай-брейк: на «гладком» тексте (без диакритики) уверенности всех языков
    # почти равны (95-96%), а распознанный текст одинаков. Поэтому при отсутствии
    # диакритических маркеров предпочитаем английский (латиница) и русский
    # (кириллица) — самые частые сценарии.
    lat_score_map = dict(lat_scores)
    cyr_score_map = dict(cyr_scores)
    if not votes and "eng" in lat_score_map and best_lat[0] is not None:
        if lat_score_map["eng"] >= best_lat[1] - 1.5:
            best_lat = ("eng", lat_score_map["eng"])
    cyr_hinted = any(lang in codes and (marks & set(probe_cyr))
                     for lang, marks in CYRILLIC_HINTS.items())
    if not cyr_hinted and "rus" in cyr_score_map and best_cyr[0] is not None:
        if cyr_score_map["rus"] >= best_cyr[1] - 1.5:
            best_cyr = ("rus", cyr_score_map["rus"])

    # чемпион: кириллица побеждает, если не отстаёт более чем на 3 пункта
    if best_cyr[0] is not None and best_cyr[1] >= best_lat[1] - 3.0:
        chosen = best_cyr[0]
    elif best_lat[0] is not None:
        chosen = best_lat[0]
    else:
        chosen = scores[0][0]

    log.info(f"Автоопределение языка: «{chosen}» "
             f"(лат: {best_lat[0]}={round(best_lat[1],1)}, "
             f"кир: {best_cyr[0]}={round(best_cyr[1],1)})")
    return {"lang": chosen, "scores": scores, "tried": len(scores)}


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


def ocr_image(image_path: str, langs=None) -> dict:
    """
    Распознаёт ВЕСЬ текст на изображении.

    :param image_path: путь к файлу изображения
    :param langs: 'auto' — определить автоматически, либо список кодов ['rus','eng']
    :return: {'text', 'lang', 'confidence', 'words', 'detected'}
    """
    if not PYTESSERACT_AVAILABLE:
        raise OcrError("Компонент OCR (pytesseract) недоступен.")
    if not os.path.isfile(image_path):
        raise OcrError(f"Файл не найден: {image_path}")

    detected = None
    lang_str, is_auto = _resolve_langs(langs)
    if is_auto:
        det = detect_language(image_path)
        detected = det["lang"]
        lang_str = detected
    _check_lang_files(lang_str)
    log.info(f"OCR: файл «{os.path.basename(image_path)}», языки «{lang_str}»")

    prepared = _preprocess(image_path)
    try:
        from PIL import Image
        with Image.open(prepared) as im:
            data = pytesseract.image_to_data(
                im, lang=lang_str, output_type=Output.DICT,
                config="--psm 3 --oem 1"
            )
            full = pytesseract.image_to_string(
                im, lang=lang_str, config="--psm 3 --oem 1"
            )
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

    log.ok(f"OCR завершён: {len(text)} симв., уверенность {confidence}%, язык {lang_str}")
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
