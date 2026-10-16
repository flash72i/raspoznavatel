# -*- coding: utf-8 -*-
"""
translator.py — офлайн-перевод с английского на русский.

Реализация напрямую поверх движка CTranslate2 + SentencePiece:
используется та же самая модель Argos Translate EN->RU (.argosmodel),
но БЕЗ тяжёлых зависимостей (stanza/torch не нужны) — это делает
собранный .exe заметно меньше и быстрее.

Как работает:
  1. Файл .argosmodel (это обычный zip) один раз распаковывается
     в папку данных приложения (рядом с .exe, НЕ в реестр/профиль).
  2. Загружается модель CTranslate2 (model/) и токенизатор
     SentencePiece (sentencepiece.model).
  3. Текст режется на предложения, кодируется в токены, переводится
     пучковым поиском (beam search) и декодируется обратно в текст.

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


_lock = threading.Lock()      # защита инициализации и перевода
_translator = None            # ctranslate2.Translator
_tokenizer = None             # sentencepiece.SentencePieceProcessor
_init_error = None            # текст последней ошибки инициализации

# Разбивка текста на предложения (замена stanza — лёгкая и достаточная)
_SENT_SPLIT = re.compile(r'(?<=[.!?…])\s+|\n+')
# Ограничение длины предложения (слишком длинные режем по запятым/точкам)
_MAX_SENT_LEN = 400


def _extract_model_dir() -> str:
    """
    Распаковывает встроенный .argosmodel (zip) в ASCII-безопасную рабочую
    папку (config.ARGOS_WORK_DIR — C++-движки не понимают кириллические пути).
    Распаковка выполняется один раз; при повторных запусках используется
    уже распакованная модель.

    :return: путь к папке с распакованной моделью (гарантированно ASCII)
    """
    models = config.find_argos_models()
    if not models:
        raise TranslateError(
            "Модель перевода EN->RU не найдена в ресурсах приложения "
            "(папка resources/argos, файл .argosmodel)."
        )
    model_file = models[0]
    stamp = os.path.basename(model_file)
    extract_root = config.ARGOS_WORK_DIR
    done_mark = os.path.join(extract_root, stamp + ".extracted")

    # ищем уже распакованную папку с моделью
    if os.path.isfile(done_mark):
        with open(done_mark, "r", encoding="utf-8") as f:
            inner = f.read().strip()
        cand = os.path.join(extract_root, inner, "model")
        if os.path.isdir(cand):
            return os.path.join(extract_root, inner)

    # распаковываем заново
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


def init_model() -> bool:
    """
    Ленивая инициализация модели перевода (один раз, потокобезопасно).
    :return: True при успехе
    """
    global _translator, _tokenizer, _init_error
    with _lock:
        if _translator is not None:
            return True
        _init_error = None
        global _ct2, _spm
        try:
            import ctranslate2 as _ct2_mod
            import sentencepiece as _spm_mod
            _ct2, _spm = _ct2_mod, _spm_mod
        except Exception as exc:
            _init_error = f"Компоненты перевода недоступны: {exc}"
            return False
        try:
            pkg_dir = _extract_model_dir()
            model_dir = os.path.join(pkg_dir, "model")
            sp_file = os.path.join(pkg_dir, "sentencepiece.model")
            if not os.path.isdir(model_dir) or not os.path.isfile(sp_file):
                _init_error = "В модели отсутствуют model/ или sentencepiece.model."
                return False
            # sentencepiece грузим ИЗ БАЙТОВ (model_proto) — это полностью
            # снимает проблему не-ASCII путей в C++-библиотеке.
            with open(sp_file, "rb") as f:
                _tokenizer = _spm.SentencePieceProcessor(model_proto=f.read())
            # ctranslate2 принимает только путь — он гарантированно ASCII
            # (config.ARGOS_WORK_DIR), плюс страховка коротким именем 8.3.
            _translator = _ct2.Translator(
                config._short_path_win(model_dir), device="cpu"
            )
            return True
        except Exception as exc:
            _init_error = f"Не удалось загрузить модель перевода: {exc}"
            _translator = None
            _tokenizer = None
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


def translate_en_to_ru(text: str) -> dict:
    """
    Переводит английский текст на русский полностью офлайн.

    :param text: исходный английский текст
    :return: {'translated': str, 'ok': bool, 'error': str|None}
    """
    text = (text or "").strip()
    if not text:
        return {"translated": "", "ok": False, "error": "Пустой текст для перевода."}

    if not init_model():
        return {"translated": "", "ok": False,
                "error": _init_error or "Переводчик недоступен."}

    try:
        sentences = _split_sentences(text)
        tokenized = [_tokenizer.encode(s, out_type=str) for s in sentences]
        with _lock:
            results = _translator.translate_batch(
                tokenized,
                replace_unknowns=True,
                max_batch_size=32,
                beam_size=4,
                length_penalty=0.2,   # параметры идентичны Argos Translate
            )
        translated = []
        for res in results:
            tokens = res[0]["tokens"] if res else []
            translated.append(_tokenizer.decode(tokens).strip())
        out = " ".join(t for t in translated if t)
        return {"translated": out, "ok": True, "error": None}
    except Exception as exc:
        return {"translated": "", "ok": False, "error": f"Сбой перевода: {exc}"}


def check_model() -> dict:
    """Состояние модели перевода (для вкладки «Настройки»)."""
    models = config.find_argos_models()
    info = {
        "ok": False,
        "library": _libs_available(),
        "models": [os.path.basename(m) for m in models],
        "error": None,
    }
    if init_model():
        info["ok"] = True
    else:
        info["error"] = _init_error
    return info
