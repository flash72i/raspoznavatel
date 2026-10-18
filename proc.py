# -*- coding: utf-8 -*-
"""
proc.py — запуск внешних процессов БЕЗ мелькания консольного окна.

Проблема: когда из оконного (GUI) приложения запускают консольную программу
(tesseract.exe, powershell.exe и т.п.) без специальных флагов, Windows
на мгновение создаёт и показывает чёрное окно командной строки.
Пользователь видит «промелькивание командной строки».

Решение: на Windows всегда передавать
  * creationflags = CREATE_NO_WINDOW (0x08000000) — консоль не создаётся;
  * startupinfo c STARTF_USESHOWWINDOW + SW_HIDE — страховка для окон.
На других ОС флаги не нужны и не добавляются.

Модуль также умеет «прокидывать» эти флаги в сторонние библиотеки,
которые вызывают subprocess сами (например pytesseract → tesseract.exe):
patch_module() подменяет у библиотеки объект subprocess на обёртку,
которая незаметно добавляет флаги к любому вызову (run/Popen/call/...).
"""

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
SW_HIDE = 0
STARTF_USESHOWWINDOW = 0x00000001


def silent_startupinfo():
    """STARTUPINFO со скрытым окном (только Windows, иначе None)."""
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    return si


def inject(kwargs, force=False):
    """
    Добавить в kwargs флаги скрытия консоли.
    Чистая функция — покрыта юнит-тестами (force=True проверяет на любой ОС).
    """
    if os.name == "nt" or force:
        flags = kwargs.get("creationflags") or 0
        kwargs["creationflags"] = flags | CREATE_NO_WINDOW
        if kwargs.get("startupinfo") is None:
            # на не-Windows startupinfo() вернёт None — флагов достаточно
            si = silent_startupinfo()
            if si is not None:
                kwargs["startupinfo"] = si
    return kwargs


def kwargs(**kw):
    """kwargs для subprocess.run/Popen с уже включённым скрытием консоли."""
    return inject(kw)


def run(*args, **kw):
    """subprocess.run без консольного окна."""
    return subprocess.run(*args, **inject(kw))


def popen(*args, **kw):
    """subprocess.Popen без консольного окна."""
    return subprocess.Popen(*args, **inject(kw))


def call(*args, **kw):
    """subprocess.call без консольного окна."""
    return subprocess.call(*args, **inject(kw))


def check_output(*args, **kw):
    """subprocess.check_output без консольного окна."""
    return subprocess.check_output(*args, **inject(kw))


class _SilentSubprocess:
    """
    Обёртка над модулем subprocess: все запуски получают флаги скрытия
    консоли, остальные атрибуты (PIPE, STARTUPINFO, ошибки…) проходят как есть.
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def run(self, *args, **kw):
        return self._real.run(*args, **inject(kw))

    def Popen(self, *args, **kw):  # noqa: N802 — имя как в subprocess
        return self._real.Popen(*args, **inject(kw))

    def call(self, *args, **kw):
        return self._real.call(*args, **inject(kw))

    def check_call(self, *args, **kw):
        return self._real.check_call(*args, **inject(kw))

    def check_output(self, *args, **kw):
        return self._real.check_output(*args, **inject(kw))


def patch_module(mod, name="subprocess"):
    """
    Заставить сторонний модуль запускать процессы без консольного окна:
    подменить у него атрибут `name` (по умолчанию subprocess) на обёртку.
    Безопасно вызывать повторно и на не-Windows (флаги не добавляются).
    """
    real = getattr(mod, name)
    if isinstance(real, _SilentSubprocess):
        return real
    wrapped = _SilentSubprocess(real)
    setattr(mod, name, wrapped)
    return wrapped
