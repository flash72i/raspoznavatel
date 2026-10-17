# -*- coding: utf-8 -*-
"""
config.py — пути к встроенным ресурсам приложения «Распознаватель».

Модуль умеет работать в двух режимах:
  1. Режим разработки — ресурсы лежат рядом с исходниками (папка resources/).
  2. Режим собранного .exe (--onefile) — ресурсы распакованы во временную
     папку sys._MEIPASS.

Все пути вычисляются один раз при импорте и доступны другим модулям.
"""

import os
import sys
import tempfile


def is_frozen() -> bool:
    """Возвращает True, если приложение запущено как собранный .exe (PyInstaller)."""
    return getattr(sys, "frozen", False)


def app_root() -> str:
    """Корневая папка приложения (внутри .exe — временная папка распаковки)."""
    if is_frozen():
        return sys._MEIPASS  # noqa: pylint (атрибут создаёт PyInstaller)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts: str) -> str:
    """Абсолютный путь к ресурсу внутри приложения."""
    return os.path.join(app_root(), *parts)


def _short_path_win(p: str) -> str:
    """
    Windows: конвертирует путь в короткое имя 8.3 (ASCII-безопасное).
    Нужно для внешних C/C++ библиотек (tesseract, ctranslate2, sentencepiece),
    которые не умеют открывать файлы по кириллическим путям в ANSI-кодировке.
    На других ОС возвращает путь без изменений.
    """
    if os.name != "nt":
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(2048)
        r = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 2048)
        return buf.value if r else p
    except Exception:
        return p


def _is_ascii(p: str) -> bool:
    try:
        p.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_safe_base() -> str:
    """
    Возвращает ASCII-безопасную базовую папку для рабочих данных
    (модель перевода, временные файлы внешних движков).

    Порядок выбора:
      1. Папка данных рядом с программой, если её путь целиком ASCII.
      2. Короткое имя 8.3 этой папки (если оно ASCII).
      3. C:\\ProgramData\\Raspoznavatel (создаётся без прав администратора).
      4. Системная временная папка (крайний случай).
    """
    base = user_data_dir()
    if _is_ascii(base):
        return base
    sp = _short_path_win(base)
    if _is_ascii(sp):
        return sp
    for cand in (
        r"C:\ProgramData\Raspoznavatel",
        os.path.join(os.environ.get("SystemDrive", "C:"), os.sep, "Raspoznavatel_tmp"),
    ):
        try:
            os.makedirs(cand, exist_ok=True)
            test = os.path.join(cand, ".w")
            with open(test, "w") as f:
                f.write("x")
            os.remove(test)
            if _is_ascii(cand):
                return cand
        except OSError:
            continue
    import tempfile
    return tempfile.gettempdir()


# База данных пользователя (имя — латиницей: внешние движки не любят кириллицу)
def user_data_dir() -> str:
    """
    Папка для пользовательских данных (настройки, временные снимки экрана).
    Создаётся рядом с .exe, если папка доступна для записи, иначе — во
    временной папке пользователя. В системный реестр ничего не пишется.
    """
    if is_frozen():
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(base, "Raspoznavatel_data")
    try:
        os.makedirs(candidate, exist_ok=True)
        # проверка на возможность записи
        test = os.path.join(candidate, ".write_test")
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
        return candidate
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), "Raspoznavatel_data")
        os.makedirs(fallback, exist_ok=True)
        return fallback


# ---------------------------------------------------------------------------
# Основные пути к ресурсам (встроенным в .exe)
# ---------------------------------------------------------------------------

UI_DIR = resource_path("ui")                      # HTML/CSS/JS интерфейса
INDEX_HTML = os.path.join(UI_DIR, "index.html")   # главная страница

RESOURCES_DIR = resource_path("resources")
TESSDATA_DIR = os.path.join(RESOURCES_DIR, "tessdata")        # rus/eng.traineddata
TESSERACT_DIR = os.path.join(RESOURCES_DIR, "tesseract")      # портативный Tesseract-OCR
ARGOS_DIR = os.path.join(RESOURCES_DIR, "argos")              # модель перевода EN->RU

# Путь к исполняемому файлу tesseract (портативная сборка)
if os.name == "nt":
    TESSERACT_CMD_CANDIDATES = [
        os.path.join(TESSERACT_DIR, "tesseract.exe"),
        os.path.join(TESSERACT_DIR, "tesseract"),
    ]
else:
    TESSERACT_CMD_CANDIDATES = [
        os.path.join(TESSERACT_DIR, "tesseract"),
        "tesseract",
    ]

SETTINGS_FILE = os.path.join(user_data_dir(), "settings.json")  # настройки пользователя
TMP_DIR = os.path.join(user_data_dir(), "tmp")                  # временные снимки экрана
os.makedirs(TMP_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# ASCII-безопасная рабочая база.
# Внешние C/C++ движки (tesseract.exe, ctranslate2, sentencepiece) на Windows
# не могут открывать файлы по путям с кириллицей (ANSI-кодировка). Поэтому:
#   • рабочая папка модели перевода и временные файлы внешних движков
#     размещаются в гарантированно ASCII-пути;
#   • переменные TEMP/TMP процесса подменяются на этот ASCII-путь
#     (pytesseract и tempfile тогда пишут временные файлы туда же).
# ---------------------------------------------------------------------------
ASCII_BASE = _ascii_safe_base()
ARGOS_WORK_DIR = os.path.join(ASCII_BASE, "argos_model")   # распакованная модель перевода
os.makedirs(ARGOS_WORK_DIR, exist_ok=True)

if os.name == "nt":
    _ascii_tmp = os.path.join(ASCII_BASE, "tmp")
    try:
        os.makedirs(_ascii_tmp, exist_ok=True)
        os.environ["TEMP"] = _ascii_tmp
        os.environ["TMP"] = _ascii_tmp
        # пересчитываем папку временных снимков — она тоже должна быть ASCII
        TMP_DIR = _ascii_tmp
    except OSError:
        pass

# Название и версия приложения (показываются в интерфейсе)
APP_NAME = "Распознаватель"
APP_VERSION = "2.4.0"

# Заголовок окна — используется и для поиска HWND (Win32) управления окном
WINDOW_TITLE = f"{APP_NAME} — распознавание QR, текста и перевод офлайн"


def webview_profile_dir() -> str:
    """
    Папка постоянного профиля WebView2 (кэш, куки движка браузера).
    Постоянный профиль (private_mode=False + storage_path) УСКОРЯЕТ повторные
    запуски окна: WebView2 не создаёт каждый раз временное окружение.
    Путь гарантированно ASCII (внешний движок Edge WebView2 этого требует).
    """
    d = os.path.join(ASCII_BASE, "webview_profile")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def autostart_shortcut_path():
    """
    Путь к ярлыку автозапуска в папке «Автозагрузка» текущего пользователя.
    Ярлык в Startup-папке НЕ требует прав администратора и НЕ трогает реестр.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(
        appdata,
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        "Raspoznavatel.lnk",
    )


def executable_path():
    """Путь к нашему exe (в сборке) или к python-скрипту (в разработке)."""
    if is_frozen():
        return sys.executable
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def find_tesseract_cmd():
    """Ищет встроенный tesseract; возвращает путь или None (тогда — из PATH)."""
    for cand in TESSERACT_CMD_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def find_argos_models():
    """Возвращает список файлов моделей Argos (.argosmodel / .ct2) внутри ресурсов."""
    models = []
    if os.path.isdir(ARGOS_DIR):
        for name in os.listdir(ARGOS_DIR):
            if name.endswith((".argosmodel", ".ct2")):
                models.append(os.path.join(ARGOS_DIR, name))
    return models


# ---------------------------------------------------------------------------
# Языки OCR и папка скриншотов
# ---------------------------------------------------------------------------

# Названия языков для интерфейса (код Tesseract -> русское имя).
# Любой язык можно добавить, просто положив <код>.traineddata в
# папку App/resources/tessdata — программа подхватит его автоматически.
LANG_NAMES = {
    "rus": "Русский", "eng": "Английский", "ukr": "Украинский",
    "bel": "Белорусский", "kaz": "Казахский", "deu": "Немецкий",
    "fra": "Французский", "spa": "Испанский", "ita": "Итальянский",
    "por": "Португальский", "pol": "Польский", "tur": "Турецкий",
    "nld": "Нидерландский", "ces": "Чешский", "jpn": "Японский",
    "chi_sim": "Китайский", "kor": "Корейский", "ara": "Арабский",
    "heb": "Иврит", "ell": "Греческий", "swe": "Шведский", "fin": "Финский",
}

# Приоритет для автоопределения (сначала самые вероятные)
AUTO_LANG_PRIORITY = [
    "rus", "eng", "chi_sim", "ukr", "deu", "fra", "spa", "ita", "por",
    "pol", "tur", "nld", "ces", "bel", "kaz",
]


def get_available_langs():
    """
    Список языков, доступных для OCR (по файлам .traineddata в tessdata).
    :return: [{'code': 'rus', 'name': 'Русский'}, ...] — сначала приоритетные.
    """
    found = set()
    if os.path.isdir(TESSDATA_DIR):
        for name in os.listdir(TESSDATA_DIR):
            if name.endswith(".traineddata"):
                code = name[: -len(".traineddata")]
                if code != "osd":
                    found.add(code)
    ordered = [c for c in AUTO_LANG_PRIORITY if c in found]
    ordered += sorted(found - set(ordered))
    return [{"code": c, "name": LANG_NAMES.get(c, c)} for c in ordered]


def get_screenshots_dir():
    """
    Папка для сохранения скриншотов: «Screenshots» рядом с программой
    (в портативной структуре — рядом с Raspoznavatel.exe). Создаётся
    при первом использовании.
    """
    if is_frozen():
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "Screenshots")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        d2 = os.path.join(user_data_dir(), "Screenshots")
        os.makedirs(d2, exist_ok=True)
        return d2
