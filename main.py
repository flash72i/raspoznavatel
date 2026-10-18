# -*- coding: utf-8 -*-
"""
main.py — точка входа приложения «Распознаватель» v2.4.

• Безрамочное окно с кастомной тёмной панелью заголовка.
• Системный трей (для автозапуска свёрнутым: Raspoznavatel.exe --minimized).
• Глобальные горячие клавиши задаются пользователем в настройках
  (по умолчанию: Ctrl+Shift+S — распознавание области, Ctrl+Shift+P — скриншот;
  можно назначить любую комбинацию от 2 клавиш, например Ctrl+F9).
• Тяжёлые компоненты грузятся лениво — окно появляется за 1-2 секунды.

КРИТИЧНО (исправлено в v2.2):
  1. Api передаётся в webview.create_window(js_api=api) — без этого
     window.pywebview.api в JavaScript пуст и НИ ОДНА кнопка не работает.
  2. До события loaded интерфейс НЕ дёргает evaluate_js (журнал буферизуется
     в Api) — раньше каждый log.info() до webview.start() блокировался на 20 с
     и уходил в рекурсию: окно не появлялось вовсе.
  3. Обработчики событий loaded/closing выполняются в фоновом потоке:
     вызов evaluate_js из GUI-потока WebView2 приводит к взаимной блокировке.
  4. Постоянный профиль WebView2 (private_mode=False + storage_path) —
     повторные запуски заметно быстрее.

Диагностические режимы (переменные окружения):
  RASPOZ_SELFTEST=1 — отчёт о компонентах (Raspoznavatel_data/selftest_report.txt);
  RASPOZ_E2E=1      — автосамопроверка интерфейса и моста JS↔Python
                      (Raspoznavatel_data/e2e_report.txt), окно закрывается само;
  RASPOZ_DEBUG=1    - запуск с инструментами разработчика (WebView2 DevTools).

Приложение полностью офлайн.
"""

import multiprocessing
import os
import sys
import threading

import config
import log
from api import Api


def selftest():
    """
    Самодиагностика (RASPOZ_SELFTEST=1): полный отчёт пишется в файл
    Raspoznavatel_data/selftest_report.txt рядом с программой.
    Проверяет ВСЕ компоненты прямо внутри собранного exe.
    """
    import io
    import json
    import ocr
    import qr_reader
    import screen_capture
    import translator
    out = io.StringIO()

    def p(*a):
        out.write(" ".join(str(x) for x in a) + "\n")

    p("=== САМОДИАГНОСТИКА «Распознаватель» v" + config.APP_VERSION + " ===")
    p("frozen:", config.is_frozen(), "| root:", config.app_root())
    p("data dir:", config.user_data_dir())

    p("\n[1] Tesseract:")
    p("   ", json.dumps(ocr.check_engine(), ensure_ascii=False))

    p("[2] OCR (автоязык):")
    try:
        from PIL import Image, ImageDraw
        tmp = os.path.join(config.TMP_DIR, "selftest.png")
        im = Image.new("RGB", (700, 140), "white")
        ImageDraw.Draw(im).text((20, 50), "Selftest OK 12345", fill="black")
        im.save(tmp)
        r = ocr.ocr_image(tmp, "auto")
        p("    текст:", repr(r["text"]), "| язык:", r["lang"])
    except Exception as exc:
        p("    ОШИБКА:", exc)

    p("[3] Движки QR:")
    p("   ", json.dumps(qr_reader.check_engines(), ensure_ascii=False))

    p("[4] QR-распознавание (генерация + чтение):")
    try:
        import numpy as np
        cv2 = qr_reader._get_cv2()[0]
        # рисуем простейший QR через zxing нельзя — используем штатный детектор
        # на пустом изображении просто проверяем, что движки не падают:
        img = np.full((200, 200, 3), 255, dtype=np.uint8)
        tmp2 = os.path.join(config.TMP_DIR, "selftest_qr.png")
        ok, buf = cv2.imencode(".png", img)
        buf.tofile(tmp2)
        res = qr_reader.decode_image(tmp2)
        p("    decode_image отработал без сбоев, найдено:", res["count"])
    except Exception as exc:
        p("    ОШИБКА:", exc)

    p("[5] Перевод (офлайн):")
    p("   ", json.dumps(translator.translate_en_to_ru("Good morning, my friend!"),
                        ensure_ascii=False))

    p("[6] tkinter (окно выделения области):")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.geometry("200x100+50+50")
        root.after(800, root.destroy)   # окно само закроется через 0.8 с
        root.mainloop()
        p("    OK: окно Tk создаётся и закрывается")
    except Exception as exc:
        p("    ОШИБКА:", exc)

    p("[7] PIL (Pillow):", screen_capture.pil_available(),
      "| mss:", screen_capture.mss_available())

    p("[8] Папка скриншотов:", config.get_screenshots_dir())

    p("[9] Горячие клавиши (keyboard):")
    try:
        import keyboard
        keyboard.add_hotkey("ctrl+shift+f12", lambda: None)
        keyboard.unhook_all_hotkeys()
        p("    OK: регистрация комбинаций работает")
    except Exception as exc:
        p("    ОШИБКА:", exc)

    p("[10] Превью base64:")
    try:
        b = screen_capture.preview_base64(os.path.join(config.TMP_DIR, "selftest.png"))
        p("     размер превью:", len(b) // 1024, "КБ (должно быть <300 КБ)")
    except Exception as exc:
        p("     ОШИБКА:", exc)

    p("=== ЗАВЕРШЕНО ===")
    report = out.getvalue()
    try:
        path = os.path.join(config.user_data_dir(), "selftest_report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
    except OSError:
        pass
    try:
        print(report)
    except Exception:
        pass


def main():
    multiprocessing.freeze_support()

    # режим самодиагностики: отчёт в файл, затем выход
    if os.environ.get("RASPOZ_SELFTEST") == "1":
        selftest()
        return

    # v2.6.0: ОДИН экземпляр программы. Повторный двойной клик по exe
    # не плодит второй процесс, а открывает окно уже работающего.
    try:
        import tray as _tray_single
        if not _tray_single.claim_single_instance():
            log.info("Запуск повторный: окно открыто в уже работающем "
                     "экземпляре — этот процесс завершается")
            return
    except Exception as exc:
        log.warn(f"Проверка одиночного экземпляра не удалась: {exc}")

    minimized = "--minimized" in sys.argv[1:]
    debug = os.environ.get("RASPOZ_DEBUG") == "1"

    import webview  # pywebview (импорт лёгкий — тяжёлое внутри лениво)

    # ВАЖНО: Api создаётся ДО окна и передаётся в create_window(js_api=api) —
    # иначе window.pywebview.api в JavaScript будет пуст (кнопки «не работают»).
    api = Api()

    window = webview.create_window(
        title=config.WINDOW_TITLE,
        url=config.INDEX_HTML,
        width=1240,
        height=860,
        min_size=(1024, 680),
        background_color="#070b16",
        text_select=True,
        frameless=True,       # кастомная панель заголовка вместо системной
        easy_drag=False,      # перетаскивание только за .pywebview-drag-region
        resizable=True,
        hidden=minimized,     # старт свёрнутым (автозапуск)
        js_api=api,           # ← мост JS↔Python (в v2.1 отсутствовал!)
    )
    api.attach_window(window, started_hidden=minimized)

    # Журнал: строки буферизуются в Api и улетают в интерфейс после loaded —
    # evaluate_js до webview.start() блокировался бы на 20 с на каждой строке.
    log.set_ui_callback(api.push_log_line)
    log.load_previous()
    log.info("=" * 62)
    log.info(f"Запуск «{config.APP_NAME}» v{config.APP_VERSION}"
             + (" (автозапуск, свёрнуто)" if minimized else ""))
    log.info(f"Режим: {'портативный' if config.is_frozen() else 'разработка'} | "
             f"Python {sys.version.split()[0]} | данные: {config.user_data_dir()}")

    # --- системный трей ---
    try:
        import tray

        def tray_show():
            try:
                window.show()
                api.win_restore_from_tray()
            except Exception as exc:
                log.error(f"Не удалось показать окно из трея: {exc}")

        def tray_toggle():
            """v2.5: клик левой кнопкой по иконке трея — показать, если окно
            спрятано, и спрятать, если оно открыто."""
            try:
                if api._visible:
                    api.win_hide_to_tray()
                else:
                    tray_show()
            except Exception as exc:
                log.error(f"Трей: не удалось переключить окно: {exc}")

        api._tray = tray.TrayIcon(
            on_show=tray_show,
            on_exit=lambda: window.destroy(),
            tooltip=f"{config.APP_NAME} v{config.APP_VERSION}",
            on_toggle=tray_toggle,
            is_visible=lambda: bool(api._visible),
        )
        api._tray.start()
    except Exception as exc:
        log.warn(f"Трей недоступен: {exc}")

    # --- горячие клавиши из настроек ---
    def register_hotkeys_safe():
        try:
            api.register_hotkeys()
        except Exception as exc:
            log.error(f"Сбой регистрации горячих клавиш: {exc}")

    def started_worker():
        """Всё послестартовое — в фоновом потоке (evaluate_js из GUI-потока = блокировка)."""
        try:
            # v2.5: вернуть окну стили сворачивания (клик по панели задач)
            try:
                api._fix_window_styles()
            except Exception as exc:
                log.warn(f"Стили окна: {exc}")
            langs = [l["code"] for l in config.get_available_langs()]
            log.ok(f"Интерфейс загружен. Языков OCR доступно: {len(langs)}")
            register_hotkeys_safe()
            api._status("Готов к работе.")
            # прогрев перевода — в фоне и с задержкой, чтобы не тормозить старт
            try:
                api.preload_translator()
            except Exception:
                pass
            # автосамопроверка интерфейса (RASPOZ_E2E=1)
            if os.environ.get("RASPOZ_E2E") == "1":
                try:
                    import e2e_selfcheck
                    e2e_selfcheck.run(window, api)
                except ImportError as exc:
                    log.warn(f"E2E-модуль недоступен: {exc}")
                except Exception as exc:
                    log.error(f"Сбой E2E-самопроверки: {exc}")
        except Exception as exc:
            log.error(f"Сбой в обработчике loaded: {exc}")

    def on_started():
        threading.Thread(target=started_worker, daemon=True).start()

    def on_closing():
        # журнал больше не шлём в UI (окно закрывается; evaluate_js в этот
        # момент ломает закрытие в GTK-бэкенде) — запись в файл продолжается
        try:
            log.set_ui_callback(None)
        except Exception:
            pass
        log.info("Окно закрывается")
        try:
            if api._tray:
                api._tray.stop()
        except Exception:
            pass
        return True

    window.events.loaded += on_started
    window.events.closing += on_closing

    # Постоянный профиль WebView2 (private_mode=False + storage_path):
    # движок не создаёт каждый раз временное окружение — повторный запуск
    # окна заметно быстрее. Папка профиля — в ASCII-безопасной базе данных.
    webview.start(
        debug=debug,
        private_mode=False,
        storage_path=config.webview_profile_dir(),
    )
    log.info("Приложение завершено")
    # Гарантированное завершение процесса: мост pywebview использует
    # не-daemon потоки, которые после закрытия окна могут навсегда остаться
    # на семафоре evaluate_js (GTK) и не давать интерпретатору выйти.
    # Все данные (настройки, журнал) к этому моменту уже сохранены.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
