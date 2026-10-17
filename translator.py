# -*- coding: utf-8 -*-
"""
translator.py — офлайн-перевод между языками (v2.5: «как Яндекс Переводчик»).

Реализация напрямую поверх движка CTranslate2 + SentencePiece:
используются модели Argos Translate (.argosmodel) из папок
resources/argos (встроенные) и Raspoznavatel_data/models/argos (скачанные).

Возможности v2.5:
  1. АВТООПРЕДЕЛЕНИЕ исходного языка (src="auto") — по письменности и
     стоп-словам, как в Яндекс Переводчике.
  2. ДЕСЯТКИ языков: встроенные пакеты EN<->RU + любые другие скачиваются
     из официального индекса Argos прямо из программы (Настройки / выбор
     языка перевода): один раз нужен интернет, дальше — офлайн.
  3. ТРАНЗИТ ЧЕРЕЗ АНГЛИЙСКИЙ: если прямой модели пары нет, но есть
     src->en и en->tgt, перевод идёт в два hops автоматически
     (например RU->DE при пакетах RU->EN + EN->DE).
  4. Менеджер пакетов: список установленных, размеры, удаление.

Как и раньше: текст режется на предложения, кодируется SentencePiece,
переводится пучковым поиском CTranslate2 и декодируется обратно.
Интернет используется ТОЛЬКО при скачивании языкового пакета.
"""

import json
import os
import re
import threading
import time
import urllib.request
import zipfile

import config

# ctranslate2/sentencepiece импортируются ЛЕНИВО в init_model() —
# их импорт занимает секунды и не должен тормозить запуск программы.
_ct2 = None
_spm = None

UA = {"User-Agent": "Mozilla/5.0 (Raspoznavatel translator)"}
INDEX_URL = ("https://raw.githubusercontent.com/argosopentech/"
             "argospm-index/main/index.json")
INDEX_TTL = 7 * 24 * 3600          # кэш индекса — неделя


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

# ---------------------------------------------------------------------------
# Каталог языков перевода (коды Argos -> русские названия).
# Доступность каждого направления определяется индексом Argos и
# установленными пакетами; список для селекторов интерфейса.
# ---------------------------------------------------------------------------
LANGUAGE_CATALOG = {
    "en": "Английский", "ru": "Русский", "uk": "Украинский", "de": "Немецкий",
    "fr": "Французский", "es": "Испанский", "it": "Итальянский", "pt": "Португальский",
    "pl": "Польский", "tr": "Турецкий", "nl": "Нидерландский", "cs": "Чешский",
    "be": "Белорусский", "kk": "Казахский", "zh": "Китайский", "ja": "Японский",
    "ko": "Корейский", "ar": "Арабский", "he": "Иврит", "el": "Греческий",
    "sv": "Шведский", "fi": "Финский", "hu": "Венгерский", "ro": "Румынский",
    "bg": "Болгарский", "sr": "Сербский", "hr": "Хорватский", "sk": "Словацкий",
    "sl": "Словенский", "lt": "Литовский", "lv": "Латышский", "et": "Эстонский",
    "da": "Датский", "no": "Норвежский", "ca": "Каталанский", "gl": "Галисийский",
    "hi": "Хинди", "ur": "Урду", "fa": "Персидский", "id": "Индонезийский",
    "vi": "Вьетнамский", "th": "Тайский", "eo": "Эсперанто", "az": "Азербайджанский",
    "ka": "Грузинский", "hy": "Армянский", "mn": "Монгольский", "sq": "Албанский",
    "mk": "Македонский", "bs": "Боснийский", "tl": "Тагальский", "ms": "Малайский",
    "bn": "Бенгальский", "ta": "Тамильский", "te": "Телугу", "sw": "Суахили",
    "af": "Африкаанс", "cy": "Валлийский", "ga": "Ирландский", "is": "Исландский",
    "lb": "Люксембургский", "mt": "Мальтийский", "nn": "Нюнорск", "oc": "Окситанский",
}


def lang_name(code: str) -> str:
    return LANGUAGE_CATALOG.get((code or "").lower(), (code or "?").upper())


# ---------------------------------------------------------------------------
# Автоопределение языка текста (письменность + стоп-слова)
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "ru": {"и", "в", "не", "на", "что", "как", "это", "для", "или", "по", "с", "к", "а", "но", "его", "она", "они", "быть", "при", "от", "из", "у", "же", "вы", "мы", "он", "я", "нет", "да", "все", "так", "этот", "который", "был", "была", "было", "можно", "нужно"},
    "en": {"the", "and", "of", "to", "in", "is", "it", "for", "on", "with", "as", "by", "at", "from", "or", "an", "be", "this", "that", "are", "was", "were", "you", "we", "they", "not", "have", "has"},
    "uk": {"і", "на", "не", "що", "як", "це", "для", "або", "та", "з", "до", "він", "вона", "вони", "ми", "ви", "є", "був", "була", "можна", "треба", "цей", "який"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "für", "mit", "auf", "ein", "eine", "ich", "sie", "wir", "von", "zu", "den", "dem", "des", "im", "auch", "werden", "wie", "geht", "es", "dir", "heute", "hallo", "danke", "bitte", "gut", "wenn", "aber", "oder", "man", "hat", "haben", "kann", "muss", "mein", "meine"},
    "fr": {"le", "la", "les", "et", "est", "ne", "pas", "pour", "sur", "avec", "une", "un", "je", "tu", "il", "elle", "nous", "vous", "ils", "des", "du", "au", "aux", "ce", "qui", "que"},
    "es": {"el", "la", "los", "las", "y", "de", "que", "en", "no", "por", "con", "para", "una", "un", "es", "está", "se", "su", "al", "lo", "como", "más", "pero", "sus"},
    "it": {"il", "lo", "la", "i", "gli", "le", "e", "di", "che", "non", "per", "con", "una", "un", "è", "si", "su", "al", "alla", "come", "più", "ma", "sono"},
    "pt": {"o", "a", "os", "as", "e", "de", "que", "não", "para", "com", "uma", "um", "é", "se", "su", "no", "na", "como", "mais", "mas", "por", "em", "dos", "das", "você", "vocês", "muito", "também", "foi", "são", "está", "nos", "nas", "pelo", "pela", "meu", "minha"},
    "pl": {"i", "w", "nie", "na", "że", "jak", "to", "dla", "lub", "z", "do", "on", "ona", "oni", "my", "wy", "jest", "są", "był", "była", "można", "tego", "który"},
    "tr": {"bir", "ve", "için", "de", "da", "bu", "şu", "o", "ne", "mi", "mı", "mu", "mü", "ile", "olarak", "var", "yok", "değil", "ben", "sen", "biz", "siz", "onlar", "çok", "şey", "gibi", "kadar", "sonra", "çünkü", "ama", "fakat", "nasıl", "nasılsın", "merhaba", "günaydın", "teşekkürler", "lütfen", "evet", "hayır", "bugün", "yarın", "iyi", "güzel", "hava", "dünya", "nerede", "neden", "kim", "hangi", "kaç"},
    "nl": {"de", "het", "een", "en", "is", "niet", "voor", "op", "met", "van", "dat", "die", "ik", "je", "wij", "zij", "zijn", "was", "werd", "te", "in", "er"},
    "cs": {"a", "v", "ne", "na", "že", "jak", "to", "pro", "nebo", "s", "do", "on", "ona", "oni", "my", "vy", "je", "jsou", "byl", "byla", "lze", "který"},
}

_CYR_HINTS = (
    ("uk", set("іїєґІЇЄҐ")),
    ("be", set("ўіЫЎІ")),
    ("kk", set("әғқңөұүһіӘҒҚҢӨҰҮҺ")),
)
_LATENT_HINTS = (
    ("de", set("äöüßÄÖÜ")),
    ("fr", set("àâçèêëîïôùûœÀÂÇÈÊÎÏÔÙÛŒ")),
    ("es", set("ñ¿¡Ñ")),
    ("it", set("àèìòùÀÈÌÒÙ")),
    ("pt", set("ãõÃÕ")),
    ("pl", set("ąćęłńśźżĄĆĘŁŃŚŹŻ")),
    ("cs", set("ěřůďťňĚŘŮĎŤ")),
    ("tr", set("ğışĞİŞ")),
)


def detect_lang(text: str):
    """
    Определяет язык текста (чистая функция — тестируема).
    Письменность -> кандидаты -> стоп-слова. Возвращает код или None.
    """
    t = text or ""
    counts = {"cyr": 0, "lat": 0, "cjk": 0, "ar": 0, "he": 0, "el": 0,
              "th": 0, "hi": 0, "ka": 0, "hy": 0, "kana": 0, "hangul": 0}
    for ch in t:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            counts["cyr"] += 1
        elif (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A):
            counts["lat"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["cjk"] += 1
        elif 0x3040 <= o <= 0x30FF:
            counts["kana"] += 1
        elif 0xAC00 <= o <= 0xD7AF:
            counts["hangul"] += 1
        elif 0x0600 <= o <= 0x06FF:
            counts["ar"] += 1
        elif 0x0590 <= o <= 0x05FF:
            counts["he"] += 1
        elif 0x0370 <= o <= 0x03FF:
            counts["el"] += 1
        elif 0x0E00 <= o <= 0x0E7F:
            counts["th"] += 1
        elif 0x0900 <= o <= 0x097F:
            counts["hi"] += 1
        elif 0x10A0 <= o <= 0x10FF:
            counts["ka"] += 1
        elif 0x0530 <= o <= 0x058F:
            counts["hy"] += 1
    total = sum(counts.values())
    if total == 0:
        return None
    if counts["hangul"] * 3 >= total:
        return "ko"
    if counts["kana"] >= 1:
        return "ja"
    if counts["cjk"] * 2 >= total:
        return "zh"
    if counts["ar"] * 2 >= total:
        return "ar"
    if counts["he"] * 2 >= total:
        return "he"
    if counts["el"] * 2 >= total:
        return "el"
    if counts["th"] * 2 >= total:
        return "th"
    if counts["hi"] * 2 >= total:
        return "hi"
    if counts["ka"] * 2 >= total:
        return "ka"
    if counts["hy"] * 2 >= total:
        return "hy"
    if counts["cyr"] >= counts["lat"]:
        # кириллица: отличительные буквы украинских/белорусских/казахских букв
        chars = set(t)
        for code, marks in _CYR_HINTS:
            if chars & marks:
                return code
        words = set(w.lower().strip(".,!?;:()\"'«» ") for w in re.split(r"\s+", t))
        best, best_hits = "ru", 0
        for code in ("ru", "uk", "bg", "sr", "mk"):
            sw = _STOPWORDS.get(code)
            if not sw:
                continue
            hits = len(words & sw)
            if hits > best_hits:
                best, best_hits = code, hits
        return best if counts["cyr"] else None
    # латиница: диакритика (сильный признак) + стоп-слова (слабый).
    # Вес диакритики обратно пропорционален числу языков, у которых она
    # встречается: «ü» общая у de/tr (вес 1/2), а «ı» есть только у tr (вес 1) —
    # так турецкий текст не превращается в немецкий.
    chars = set(t)
    char_langs = {}
    for code, marks in _LATENT_HINTS:
        for ch in marks:
            char_langs.setdefault(ch, set()).add(code)
    scores = {}
    for code, marks in _LATENT_HINTS:
        w = sum(1.0 / len(char_langs[ch]) for ch in chars & marks)
        if w:
            scores[code] = scores.get(code, 0) + w * 3
    words = set(w.lower().strip(".,!?;:()\"'«» ") for w in re.split(r"\s+", t))
    for code, sw in _STOPWORDS.items():
        if code in ("ru", "uk"):
            continue
        hits = len(words & sw)
        if hits:
            scores[code] = scores.get(code, 0) + hits * 2
    if scores:
        best = sorted(scores.items(), key=lambda kv: -kv[1])[0][0]
        return best
    return "en"


# ---------------------------------------------------------------------------
# Доступные направления: встроенные + скачанные пакеты
# ---------------------------------------------------------------------------

def available_pairs() -> list:
    """
    Список доступных направлений перевода по файлам моделей
    (resources/argos + Raspoznavatel_data/models/argos).
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


def translation_path(src: str, tgt: str):
    """
    Как перевести пару (чистая функция — тестируема).
    :return: ('direct',) | ('via_en',) | ('missing', [нужные пакеты])
    """
    if (src, tgt) in available_pairs():
        return ("direct",)
    pairs = set(available_pairs())
    if (src, "en") in pairs and ("en", tgt) in pairs:
        return ("via_en",)
    need = []
    if (src, tgt) not in pairs:
        need = [f"{src}->{tgt}"]
    return ("missing", need)


# ---------------------------------------------------------------------------
# Индекс языковых пакетов Argos + скачивание
# ---------------------------------------------------------------------------

def fetch_index(force: bool = False, timeout: int = 45) -> list:
    """
    Индекс пакетов Argos (кэш 7 дней в папке данных; офлайн — старый кэш).
    :return: список записей индекса или []
    """
    cache = config.ARGOS_INDEX_CACHE
    if not force and os.path.isfile(cache):
        try:
            if time.time() - os.path.getmtime(cache) < INDEX_TTL:
                with open(cache, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    try:
        req = urllib.request.Request(INDEX_URL, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass
        return data
    except Exception:
        if os.path.isfile(cache):
            try:
                with open(cache, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []


def remote_packages(index=None) -> list:
    """
    Список загружаемых направлений с размерами:
    [{'src','tgt','url','size','name'}, ...] (только пары из каталога).
    """
    idx = index if index is not None else fetch_index()
    out = []
    seen = set()
    for p in idx or []:
        src = (p.get("from_code") or "").lower()
        tgt = (p.get("to_code") or "").lower()
        links = p.get("links") or []
        if not src or not tgt or not links:
            continue
        if src not in LANGUAGE_CATALOG or tgt not in LANGUAGE_CATALOG:
            continue
        if (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        out.append({
            "src": src, "tgt": tgt,
            "url": links[0],
            "name": links[0].rsplit("/", 1)[-1],
            "size": int(p.get("size") or 0),
        })
    return out


def download_pair(src: str, tgt: str, progress_cb=None) -> dict:
    """
    Скачивает языковой пакет src->tgt из индекса Argos в папку данных.
    :return: {'ok': bool, 'path': str|None, 'error': str|None, 'size': int}
    """
    def _report(pct, txt):
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    existing = _find_model_file(src, tgt)
    if existing:
        return {"ok": True, "path": existing, "error": None, "size": os.path.getsize(existing)}
    pkgs = [p for p in remote_packages() if p["src"] == src and p["tgt"] == tgt]
    if not pkgs:
        return {"ok": False, "path": None, "size": 0,
                "error": f"Пакет {src}->{tgt} не найден в индексе Argos."}
    pkg = pkgs[0]
    os.makedirs(config.USER_MODELS_DIR, exist_ok=True)
    dst = os.path.join(config.USER_MODELS_DIR, pkg["name"])
    _report(2, f"Скачиваю пакет {src}->{tgt}…")
    try:
        req = urllib.request.Request(pkg["url"], headers=UA)
        with urllib.request.urlopen(req, timeout=900) as r:
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
                        _report(2 + int(88 * done / total),
                                f"Скачиваю пакет {src}->{tgt}: "
                                f"{done // (1024 * 1024)} из {total // (1024 * 1024)} МБ")
        # проверка структуры zip (защита от заглушек с JSON-ошибками)
        with zipfile.ZipFile(dst + ".part") as zf:
            names = zf.namelist()
            if not any(n.endswith("sentencepiece.model") for n in names) \
                    or not any("/model/" in n for n in names):
                raise TranslateError("Скачанный файл не похож на модель перевода.")
        os.replace(dst + ".part", dst)
    except Exception as exc:
        try:
            if os.path.isfile(dst + ".part"):
                os.remove(dst + ".part")
        except OSError:
            pass
        return {"ok": False, "path": None, "size": 0,
                "error": f"Не удалось скачать пакет {src}->{tgt}: {exc}"}
    _report(100, "")
    return {"ok": True, "path": dst, "error": None, "size": os.path.getsize(dst)}


def remove_pair(src: str, tgt: str) -> dict:
    """Удаляет СКАЧАННЫЙ пакет (встроенные из resources/argos не трогает)."""
    path = _find_model_file(src, tgt)
    if not path:
        return {"ok": False, "error": f"Пакет {src}->{tgt} не установлен."}
    if not path.startswith(config.USER_MODELS_DIR):
        return {"ok": False, "error": "Встроенные пакеты удалять нельзя."}
    try:
        os.remove(path)
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def installed_packs() -> list:
    """
    Установленные пакеты с размерами и происхождением:
    [{'src','tgt','size','bundled': bool, 'path'}, ...]
    """
    out = []
    for path in config.find_argos_models():
        m = _PAIR_RE.search(os.path.basename(path))
        if not m:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        out.append({
            "src": m.group(1).lower(), "tgt": m.group(2).lower(),
            "size": size,
            "bundled": os.path.abspath(path).startswith(os.path.abspath(config.ARGOS_DIR)),
            "path": path,
        })
    return out


# ---------------------------------------------------------------------------
# Загрузка моделей и сам перевод
# ---------------------------------------------------------------------------

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
                _errors[key] = (f"Модель перевода {src}→{tgt} не найдена "
                                f"(доступны: {pairs}). Скачайте языковой пакет "
                                f"в настройках или выберите другую пару.")
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


def _translate_direct(text: str, src: str, tgt: str, progress_cb=None) -> dict:
    """Один проход модели src->tgt по предложениям."""
    entry = _cache[(src, tgt)]
    tokenizer, tr = entry["tokenizer"], entry["translator"]
    sentences = _split_sentences(text)
    if progress_cb:
        try:
            progress_cb(85, "Перевод…")
        except Exception:
            pass
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
    return " ".join(t for t in translated if t)


def translate(text: str, src: str = "en", tgt: str = "ru", progress_cb=None) -> dict:
    """
    Переводит текст полностью офлайн.

    v2.5: src может быть "auto" (автоопределение); при отсутствии прямой
    модели перевод идёт транзитом через английский (два прохода).

    :param text: исходный текст
    :param src, tgt: коды языков (например 'en', 'ru', 'auto')
    :param progress_cb: колбэк (pct:int, text:str) для шкалы прогресса
    :return: {'translated': str, 'ok': bool, 'error': str|None, 'src', 'tgt',
              'via': None|'en', 'detected': код|None}
    """
    text = (text or "").strip()
    src = (src or "en").strip().lower()
    tgt = (tgt or "ru").strip().lower()
    detected = None
    if src == "auto":
        detected = detect_lang(text)
        src = detected or "en"
    if not text:
        return {**{"translated": "", "ok": False, "error": "Пустой текст для перевода.",
                   "src": src, "tgt": tgt}, "via": None, "detected": detected}
    if src == tgt:
        return {**{"translated": text, "ok": True, "error": None, "src": src, "tgt": tgt,
                   "note": "Языки совпадают — текст оставлен без изменений."},
                "via": None, "detected": detected}

    def _report(pct, txt):
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    path = translation_path(src, tgt)
    if path[0] == "missing":
        need = ", ".join(path[1]) if len(path) > 1 else f"{src}->{tgt}"
        return {**{"translated": "", "ok": False, "src": src, "tgt": tgt,
                   "error": (f"Нет модели перевода {src}→{tgt}. Скачайте языковой "
                             f"пакет ({need}) в окне перевода — дальше он работает "
                             f"офлайн.")},
                "via": None, "detected": detected}

    if path[0] == "direct":
        if not init_model(src, tgt, progress_cb=progress_cb):
            return {**{"translated": "", "ok": False,
                       "error": _errors.get((src, tgt)) or f"Переводчик {src}→{tgt} недоступен.",
                       "src": src, "tgt": tgt}, "via": None, "detected": detected}
        try:
            out = _translate_direct(text, src, tgt, progress_cb)
            _report(100, "")
            return {**{"translated": out, "ok": True, "error": None,
                       "src": src, "tgt": tgt}, "via": None, "detected": detected}
        except Exception as exc:
            return {**{"translated": "", "ok": False, "error": f"Сбой перевода: {exc}",
                       "src": src, "tgt": tgt}, "via": None, "detected": detected}

    # транзит через английский
    if not init_model(src, "en", progress_cb=progress_cb) or \
            not init_model("en", tgt, progress_cb=progress_cb):
        err = _errors.get((src, "en")) or _errors.get(("en", tgt)) or \
            f"Переводчик {src}→{tgt} недоступен."
        return {**{"translated": "", "ok": False, "error": err,
                   "src": src, "tgt": tgt}, "via": "en", "detected": detected}
    try:
        _report(60, f"Перевод {src}→en…")
        mid = _translate_direct(text, src, "en")
        _report(80, f"Перевод en→{tgt}…")
        out = _translate_direct(mid, "en", tgt)
        _report(100, "")
        return {**{"translated": out, "ok": True, "error": None,
                   "src": src, "tgt": tgt,
                   "note": f"Переведено транзитом через английский ({src}→en→{tgt})."},
                "via": "en", "detected": detected}
    except Exception as exc:
        return {**{"translated": "", "ok": False, "error": f"Сбой перевода: {exc}",
                   "src": src, "tgt": tgt}, "via": "en", "detected": detected}


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
        "packs": installed_packs(),
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
