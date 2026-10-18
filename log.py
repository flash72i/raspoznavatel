# -*- coding: utf-8 -*-
"""
log.py — журнал работы приложения «Распознаватель».

Ведёт кольцевой буфер последних событий (в памяти) и параллельно пишет
файл журнала в папке данных. Журнал показывается во встроенной панели
«Настройки → Журнал» и копируется в буфер одной кнопкой — это упрощает
диагностику (пользователь может прислать лог разработчику).
"""

import os
import threading
from collections import deque
from datetime import datetime

import config

_MAX_ENTRIES = 2000          # сколько строк держим в памяти
_MAX_FILE_BYTES = 5_000_000  # ротация файла журнала (~5 МБ)
_lock = threading.Lock()
_entries = deque(maxlen=_MAX_ENTRIES)
_log_file = os.path.join(config.user_data_dir(), "app.log")
_ui_callback = None            # функция для мгновенной отправки строк в интерфейс


def _rotate_if_needed():
    """Если журнал вырос больше _MAX_FILE_BYTES — предыдущий сохраняется в app.log.old."""
    try:
        if os.path.isfile(_log_file) and os.path.getsize(_log_file) > _MAX_FILE_BYTES:
            old = _log_file + ".old"
            if os.path.isfile(old):
                os.remove(old)
            os.replace(_log_file, old)
    except OSError:
        pass


def set_ui_callback(fn):
    """Регистрирует колбэк (fn(text)) для мгновенной доставки строк в UI."""
    global _ui_callback
    _ui_callback = fn


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(message: str, level: str = "INFO"):
    """
    Добавляет запись в журнал.
    :param message: текст (на русском)
    :param level: INFO / OK / WARN / ERROR
    """
    line = f"[{_stamp()}] [{level}] {message}"
    with _lock:
        _entries.append(line)
        try:
            _rotate_if_needed()
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass
    if _ui_callback:
        try:
            _ui_callback(line)
        except Exception:
            pass


def info(msg: str):
    log(msg, "INFO")


def ok(msg: str):
    log(msg, "OK")


def warn(msg: str):
    log(msg, "WARN")


def error(msg: str):
    log(msg, "ERROR")


def get_log(max_chars: int = 400000) -> str:
    """Возвращает весь журнал одной строкой (последние записи, до max_chars)."""
    with _lock:
        text = "\n".join(_entries)
    if len(text) > max_chars:
        text = "…(начало обрезано)…" + text[-max_chars:]
    return text


def clear_log():
    """Очищает журнал (в памяти и файл)."""
    with _lock:
        _entries.clear()
    try:
        if os.path.isfile(_log_file):
            os.remove(_log_file)
    except OSError:
        pass
    info("Журнал очищен")


def load_previous():
    """Подгружает записи прошлых запусков из файла (последние 500 строк)."""
    try:
        if os.path.isfile(_log_file):
            with open(_log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-500:]
            with _lock:
                for ln in lines:
                    _entries.append("── " + ln.rstrip())
    except OSError:
        pass
