# -*- coding: utf-8 -*-
"""
translator.py — офлайн-перевод между языками (v2.3: ЛЮБЫЕ направления).

Реализация напрямую поверх движка CTranslate2 + SentencePiece:
используются модели Argos Translate (.argosmodel) из папки resources/argos.
В комплекте: EN→RU и RU→EN. Любой другой язык — просто положите файл
translate-<откуда>_<куда>-*.argosmodel в resources/argos, программа
подхватит направление автоматически.

Как работает:
  1. Файл .argosmodel (это обычный zip) один раз распаковывается в рабочую
     папку данных приложения (ASCII-путь — C++-движки не любят кириллицу).
  2. Загружаются модель CTranslate2 (model/) и токенизатор SentencePiece.
  3. Каждая пара языков кэшируется отдельно (первая загрузка ~10-30 с,
     дальше мгновенно).
  4. Текст режется на предложения, кодируется в токены, переводится
     пучковым поиском (beam search) и декодируется обратно.

Интернет не используется ни на одном шаге.
"""

import os
import re
import threading
import zipfile

import config

# ctranslate2/sentencepiece импортируются ЛЕНИВО в init_model() —
# их импорт занимает секунды и не должен тормозить запуск программы.
_ct2 = None
_spm = None


def _libs_available() -> bool:
    """Проверка доступности библиотек без тяжёлого импорта."""
    import importlib.util
    try:
        return (importlib.util.find_spec("ctranslate2") is not None and
                importlib.util.find_spec("sentencepiece") is not None)
    except Exception:
        return False


class TranslateError(Exception):
    """Ошибка перевода."""


_lock = threading.Lock()          # защита инициализации и перевода
_cache = {}                       # (src, tgt) -> {"translator":…, "tokenizer":…}
_errors = {}                      # (src, tgt) -> текст ошибки инициализации

# Имя модели Argos: translate-en_ru-1_9.argosmodel / translate-ptbr_en-*.argosmodel
_PAIR_RE = re.compile(r"translate-([a-z]{2,3}(?:[a-z0-9]+)?)_([a-z]{2,3}(?:[a-z0-9]+)?)", re.I)

# Разбивка текста на предложения (замена stanza — лёгкая и достаточная)
_SENT_SPLIT = re.compile(r'(?<=[.!?…])\s+|\n+')
# Ограничение длины предложения (слишком длинные режем по запятым/точкам)
_MAX_SENT_LEN = 400


def available_pairs() -> list:
    """
    Список доступных направлений перевода по файлам моделей в resources/argos.
    :return: [('en', 'ru'), ('ru', 'en'), ...]
    """
    pairs = []
    for path in config.find_argos_models():
        m = _PAIR_RE.search(os.path.basename(path))
        if m:
            pair = (m.group(1).lower(), m.group(2).lower())
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _find_model_file(src: str, tgt: str):
    """Файл модели для конкретной пары (или None)."""
    for path in config.find_argos_models():
        m = _PAIR_RE.search(os.path.basename(path))
        if m and m.group(1).lower() == src and m.group(2).lower() == tgt:
            return path
    return None


def _extract_model_dir(model_file: str) -> str:
    """
    Распаковывает .argosmodel (zip) в ASCII-безопасную рабочую папку.
    Распаковка выполняется один раз на модель; повторные запуски используют
    уже распакованную папку.
    :return: путь к папке с распакованной моделью (гарантированно ASCII)
    """
    stamp = os.path.basename(model_file)
    extract_root = config.ARGOS_WORK_DIR
    done_mark = os.path.join(extract_root, stamp + ".extracted")

    if os.path.isfile(done_mark):
        with open(done_mark, "r", encoding="utf-8") as f:
            inner = f.read().strip()
        cand = os.path.join(extract_root, inner, "model")
        if os.path.isdir(cand):
            return os.path.join(extract_root, inner)

    os.makedirs(extract_root, exist_ok=True)
    with zipfile.ZipFile(model_file) as zf:
        zf.extractall(extract_root)
        inner = None
        for name in zf.namelist():
            parts = name.split("/")
            if len(parts) > 1 and parts[0]:
                inner = parts[0]
                break
    if not inner:
        raise TranslateError("Некорректный формат модели (.argosmodel пуст).")
    with open(done_mark, "w", encoding="utf-8") as f:
        f.write(inner)
    return os.path.join(extract_root, inner)


def init_model(src: str = "en", tgt: str = "ru", progress_cb=None) -> bool:
    """
    Ленивая инициализация модели для пары языков (один раз, потокобезопасно).
    :return: True при успехе
    """
    global _ct2, _spm
    key = (src, tgt)

    def _report(pct, txt):
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    with _lock:
        if key in _cache:
            _report(100, "")
            return True
        _errors.pop(key, None)
        try:
            import ctranslate2 as _ct2_mod
            import sentencepiece as _spm_mod
            _ct2, _spm = _ct2_mod, _spm_mod
        except Exception as exc:
            _errors[key] = f"Компоненты перевода недоступны: {exc}"
            return False
        try:
            model_file = _find_model_file(src, tgt)
            if not model_file:
                pairs = ", ".join(f"{a}→{b}" for a, b in available_pairs()) or "нет"
                _errors[key] = (f"Модель перевода {src}→{tgt} не найдена в "
                                f"resources/argos (доступны: {pairs}).")
                return False
            _report(15, f"Готовлю модель {src}→{tgt}…")
            pkg_dir = _extract_model_dir(model_file)
            model_dir = os.path.join(pkg_dir, "model")
            sp_file = os.path.join(pkg_dir, "sentencepiece.model")
            if not os.path.isdir(model_dir) or not os.path.isfile(sp_file):
                _errors[key] = "В модели отсутствуют model/ или sentencepiece.model."
                return False
            _report(55, f"Загружаю модель {src}→{tgt}…")
            # sentencepiece грузим ИЗ БАЙТОВ (model_proto) — это полностью
            # снимает проблему не-ASCII путей в C++-библиотеке.
            with open(sp_file, "rb") as f:
                tokenizer = _spm.SentencePieceProcessor(model_proto=f.read())
            # ctranslate2 принимает только путь — он гарантированно ASCII
            translator = _ct2.Translator(
                config._short_path_win(model_dir), device="cpu"
            )
            _cache[key] = {"translator": translator, "tokenizer": tokenizer}
            _report(100, "")
            return True
        except Exception as exc:
            _errors[key] = f"Не удалось загрузить модель {src}→{tgt}: {exc}"
            return False


def _split_sentences(text: str):
    """Режет текст на предложения (для пакетного перевода)."""
    raw = [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip()]
    sentences = []
    for s in raw:
        while len(s) > _MAX_SENT_LEN:
            cut = s.rfind(",", 0, _MAX_SENT_LEN)
            if cut < _MAX_SENT_LEN // 2:
                cut = s.rfind(" ", 0, _MAX_SENT_LEN)
            if cut <= 0:
                cut = _MAX_SENT_LEN
            sentences.append(s[:cut + 1])
            s = s[cut + 1:].strip()
        if s:
            sentences.append(s)
    return sentences


def translate(text: str, src: str = "en", tgt: str = "ru", progress_cb=None) -> dict:
    """
    Переводит текст полностью офлайн.

    :param text: исходный текст
    :param src, tgt: коды языков (например 'en', 'ru')
    :param progress_cb: колбэк (pct:int, text:str) для шкалы прогресса
    :return: {'translated': str, 'ok': bool, 'error': str|None, 'src', 'tgt'}
    """
    text = (text or "").strip()
    src = (src or "en").strip().lower()
    tgt = (tgt or "ru").strip().lower()
    if not text:
        return {"translated": "", "ok": False, "error": "Пустой текст для перевода.",
                "src": src, "tgt": tgt}
    if src == tgt:
        return {"translated": text, "ok": True, "error": None, "src": src, "tgt": tgt,
                "note": "Языки совпадают — текст оставлен без изменений."}

    def _report(pct, txt):
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    if not init_model(src, tgt, progress_cb=progress_cb):
        return {"translated": "", "ok": False,
                "error": _errors.get((src, tgt)) or f"Переводчик {src}→{tgt} недоступен.",
                "src": src, "tgt": tgt}

    try:
        entry = _cache[(src, tgt)]
        tokenizer, tr = entry["tokenizer"], entry["translator"]
        sentences = _split_sentences(text)
        _report(85, "Перевод…")
        tokenized = [tokenizer.encode(s, out_type=str) for s in sentences]
        with _lock:
            results = tr.translate_batch(
                tokenized,
                replace_unknowns=True,
                max_batch_size=32,
                beam_size=4,
                length_penalty=0.2,   # параметры идентичны Argos Translate
            )
        translated = []
        for res in results:
            tokens = res[0]["tokens"] if res else []
            translated.append(tokenizer.decode(tokens).strip())
        out = " ".join(t for t in translated if t)
        _report(100, "")
        return {"translated": out, "ok": True, "error": None, "src": src, "tgt": tgt}
    except Exception as exc:
        return {"translated": "", "ok": False, "error": f"Сбой перевода: {exc}",
                "src": src, "tgt": tgt}


def translate_en_to_ru(text: str) -> dict:
    """Обратная совместимость: перевод EN→RU."""
    return translate(text, "en", "ru")


def check_model() -> dict:
    """Состояние моделей перевода (для вкладки «Настройки»)."""
    pairs = available_pairs()
    info = {
        "ok": False,
        "library": _libs_available(),
        "models": [os.path.basename(m) for m in config.find_argos_models()],
        "pairs": [f"{a}->{b}" for a, b in pairs],
        "error": None,
    }
    if ("en", "ru") in pairs and init_model("en", "ru"):
        info["ok"] = True
    elif pairs:
        info["ok"] = True     # движок есть, просто нет дефолтной пары
        info["error"] = _errors.get(("en", "ru"))
    else:
        info["error"] = "Модели перевода не найдены в resources/argos"
    return info
