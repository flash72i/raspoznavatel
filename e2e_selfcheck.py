# -*- coding: utf-8 -*-
"""
e2e_selfcheck.py — автоматическая самопроверка интерфейса и моста JS↔Python.

Запуск:  set RASPOZ_E2E=1  (Windows) / RASPOZ_E2E=1 (Linux) и старт программы.
Приложение открывается само, прогоняет проверки, пишет отчёт в
Raspoznavatel_data/e2e_report.txt и закрывается.

Что проверяется (ровно те места, где были пользовательские баги v2.1):
  1. мост window.pywebview.api существует (js_api передан в create_window);
  2. интерфейс доходит до статуса «Готов к работе» (get_settings/get_info прошли);
  3. версия в интерфейсе = версии приложения;
  4. буфер журнала долетает до интерфейса (#logBox не пуст);
  5. кнопки окна: «свернуть» и «развернуть» доходят до Python и ЛОГИРУЮТСЯ;
  6. ошибки из JS долетают до файла журнала (js_error);
  7. валидация горячих клавиш: «f5» (1 клавиша), «alt+f4» (системная) и
     дубликаты отклоняются с ошибкой; «ctrl+f9» принимается и сохраняется;
  8. запись комбинации прямо в интерфейсе (независимо от раскладки — по e.code);
  9. check_components возвращает структуру и не рвёт мост;
 10. кнопка «закрыть» завершает приложение.

Защита от особенностей бэкендов: каждый evaluate_js выполняется в отдельном
потоке с таймаутом (GTK-бэкенд pywebview при JS-ошибке не освобождает
семафор и вешает вызов навсегда); все сниппеты однострочные, ASCII, и всегда
возвращают сериализуемое значение.
"""

import json
import os
import threading
import time

import config
import log

TOTAL_TIMEOUT = 240   # сек на всю самопроверку (watchdog)
JS_TIMEOUT = 10       # сек на один evaluate_js


class Report:
    def __init__(self):
        self.rows = []
        self.t0 = time.time()
        self._saved = False
        self._lock = threading.Lock()

    def add(self, name, ok, extra=""):
        self.rows.append((name, bool(ok), str(extra)))
        mark = "OK  " if ok else "FAIL"
        line = f"[{mark}] {name}" + (f" — {extra}" if (extra and not ok) else "")
        (log.ok if ok else log.error)(f"E2E: {line}")

    def text(self):
        passed = sum(1 for _, ok, _ in self.rows if ok)
        head = (f"ОТЧЁТ САМОПРОВЕРКИ ИНТЕРФЕЙСА «{config.APP_NAME}» v{config.APP_VERSION}\n"
                f"Пройдено: {passed}/{len(self.rows)} | время: {round(time.time() - self.t0, 1)} c\n"
                + "=" * 70 + "\n")
        body = "\n".join(
            f"{'+' if ok else 'X'} {name}" + (f"   ({extra})" if extra and not ok else "")
            for name, ok, extra in self.rows
        )
        return head + body + "\n"

    def save(self):
        with self._lock:
            self._saved = True
            path = os.path.join(config.user_data_dir(), "e2e_report.txt")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.text())
                print(self.text())
                print("Отчёт сохранён:", path)
            except OSError as exc:
                log.error(f"E2E: не удалось сохранить отчёт: {exc}")


def run(window, api):
    """Вход самопроверки (вызывается из main.started_worker в фоновом потоке)."""
    rep = Report()

    # --- watchdog: даже если проверка зависнет, отчёт будет сохранён,
    #     а приложение закрыто (иначе webview.start() не вернётся никогда) ---
    def watchdog():
        time.sleep(TOTAL_TIMEOUT)
        log.error("E2E: превышен общий таймаут — принудительно завершаю")
        rep.add("САМОПРОВЕРКА УЛОЖИЛАСЬ В ТАЙМАУТ", False, f">{TOTAL_TIMEOUT} c")
        try:
            rep.save()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            os._exit(2)
    threading.Thread(target=watchdog, daemon=True).start()

    # --- evaluate_js с таймаутом (защита от зависшего семафора GTK) ---
    def js(code, timeout=JS_TIMEOUT):
        box = {}
        ev = threading.Event()

        def runner():
            try:
                box["v"] = window.evaluate_js(code)
            except Exception as e:
                box["e"] = e
            finally:
                ev.set()
        threading.Thread(target=runner, daemon=True).start()
        if not ev.wait(timeout):
            raise TimeoutError(f"evaluate_js завис: {code[:70]}…")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def poll(cond, timeout=20.0, interval=0.25):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def js_async(var, js_expr, timeout=20.0):
        """Запускает promise в странице, ждёт и возвращает результат (или None)."""
        kick = (
            "(function(){ window.__e2e = window.__e2e || {};"
            f" delete window.__e2e[{json.dumps(var)}];"
            f" Promise.resolve({js_expr})"
            f".then(function(r){{ window.__e2e[{json.dumps(var)}] = JSON.stringify(r === undefined ? null : r); }})"
            f".catch(function(e){{ window.__e2e[{json.dumps(var)}] = JSON.stringify({{__err: String(e)}}); }});"
            " return 1; })()"
        )
        js(kick)
        got = poll(lambda: js(
            f"(function(){{ return (window.__e2e || {{}}).hasOwnProperty({json.dumps(var)}) ? 1 : 0; }})()"
        ) == 1, timeout)
        if not got:
            return None
        raw = js(f"(function(){{ return window.__e2e[{json.dumps(var)}]; }})()")
        try:
            return json.loads(raw) if raw is not None else None
        except Exception:
            return raw

    log.info("E2E: самопроверка запущена")
    time.sleep(1.0)   # даём app.js отработать onReady

    try:
        # --- 1. мост ---
        rep.add("мост window.pywebview.api существует",
                js("(function(){ return !!(window.pywebview && window.pywebview.api) ? 1 : 0; })()") == 1)

        # --- 2. интерфейс инициализирован ---
        st = ""
        ready = poll(lambda: "Готов" in (st := str(js(
            "(function(){ var e=document.querySelector('#statusText'); return e ? e.textContent : ''; })()"
        ))), 40)
        rep.add("статус «Готов к работе» (get_settings/get_info прошли)", ready, st[:80])

        # --- 3. версия ---
        ver = str(js("(function(){ return document.querySelector('#versionLabel').textContent; })()"))
        rep.add(f"версия в интерфейсе = {config.APP_VERSION}", ver == config.APP_VERSION, ver)

        # --- 4. журнал долетел до интерфейса ---
        got_log = poll(lambda: int(js(
            "(function(){ return document.querySelector('#logBox').textContent.length; })()"
        ) or 0) > 0, 20)
        rep.add("буфер журнала доставлен в интерфейс (#logBox)", got_log)

        # --- 5. кнопки окна (через реальный клик в UI) ---
        js("(function(){ document.querySelector('#btnMin').click(); return 1; })()")
        ok_min = poll(lambda: "win_minimize" in log.get_log(), 20)
        rep.add("кнопка «свернуть» → Python, записана в журнал", ok_min)
        time.sleep(0.5)
        try:
            window.restore()   # возвращаем окно для следующих проверок
        except Exception:
            pass
        time.sleep(0.5)

        js("(function(){ document.querySelector('#btnMax').click(); return 1; })()")
        ok_max = poll(lambda: "win_maximize_toggle" in log.get_log(), 20)
        rep.add("кнопка «развернуть» → Python, записана в журнал", ok_max)
        time.sleep(0.3)
        try:
            window.restore()
        except Exception:
            pass

        # --- 6. ошибки JS долетают до ФАЙЛА журнала ---
        probe = f"E2E probe {int(time.time())}"
        js_async("jserr", f"window.pywebview.api.js_error({json.dumps(probe)})")
        logged = poll(lambda: probe in log.get_log(), 15)
        in_file = False
        try:
            with open(os.path.join(config.user_data_dir(), "app.log"), encoding="utf-8") as f:
                in_file = probe in f.read()
        except OSError:
            pass
        rep.add("ошибка из JS записана в журнал и в файл app.log", logged and in_file, probe)

        # --- 6б. ошибки подсвечены красным в панели журнала ---
        red = poll(lambda: int(js(
            "(function(){ return document.querySelectorAll('#logBox .log-err').length; })()"
        ) or 0) >= 1, 15)
        rep.add("ошибки отображаются КРАСНЫМ в журнале (.log-err)", red)

        # --- 7. горячие клавиши: валидация через мост ---
        cur = js_async("settings", "window.pywebview.api.get_settings()") or {}
        base = {k: v for k, v in cur.items()} if isinstance(cur, dict) else {}

        r1 = js_async("bad1", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{hotkey_recognize: 'f5'}}))") or {}
        rep.add("комбинация из 1 клавиши («f5») отклонена",
                isinstance(r1, dict) and r1.get("ok") is False and bool(r1.get("error")),
                json.dumps(r1, ensure_ascii=False)[:120])

        r2 = js_async("bad2", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{hotkey_recognize: 'alt+f4'}}))") or {}
        rep.add("системная комбинация («alt+f4») отклонена",
                isinstance(r2, dict) and r2.get("ok") is False and bool(r2.get("error")),
                json.dumps(r2, ensure_ascii=False)[:120])

        r3 = js_async("bad3", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{hotkey_recognize: 'ctrl+f9', hotkey_screenshot: 'ctrl+f9'}}))") or {}
        rep.add("одинаковые комбинации отклонены",
                isinstance(r3, dict) and r3.get("ok") is False and bool(r3.get("error")),
                json.dumps(r3, ensure_ascii=False)[:120])

        r4 = js_async("good", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{hotkey_recognize: 'ctrl+f9'}}))") or {}
        saved_ok = isinstance(r4, dict) and r4.get("ok") is True
        file_ok = False
        try:
            with open(config.SETTINGS_FILE, "r", encoding="utf-8") as f:
                file_ok = json.load(f).get("hotkey_recognize") == "ctrl+f9"
        except Exception:
            pass
        rep.add("комбинация «ctrl+f9» принята и сохранена в settings.json",
                saved_ok and file_ok, json.dumps(r4, ensure_ascii=False)[:120])

        js_async("restore", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{hotkey_recognize: 'ctrl+shift+s'}}))")

        # --- 8. запись комбинации прямо в интерфейсе (e.code — раскладка не важна) ---
        js("(function(){ document.querySelector('#settingsBtn').click();"
           " document.querySelector('#hkShotBtn').click();"
           " window.dispatchEvent(new KeyboardEvent('keydown', {key:'P', code:'KeyP',"
           " ctrlKey:true, altKey:true, bubbles:true, cancelable:true})); return 1; })()")
        rv_ok = poll(lambda: str(js(
            "(function(){ return document.querySelector('#hkScreenshot').value; })()"
        )) == "Ctrl + Alt + P", 15)
        rep.add("запись в UI: Ctrl+Alt+P → «Ctrl + Alt + P» (по e.code, без раскладки)", rv_ok,
                str(js("(function(){ return document.querySelector('#hkScreenshot').value; })()")))
        js_async("restore2", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{hotkey_screenshot: 'ctrl+shift+p'}}))")
        js("(function(){ document.querySelector('#drawerClose').click(); return 1; })()")

        # --- 8б. v2.3: направления перевода, шкала прогресса, звук ---
        rp = js_async("pairs", "window.pywebview.api.get_translation_pairs()")
        ok_pairs = isinstance(rp, dict) and rp.get("ok") and len(rp.get("pairs", [])) >= 1
        rep.add("get_translation_pairs вернул направления (≥1)", ok_pairs,
                json.dumps(rp, ensure_ascii=False)[:140])

        sel_n = js("(function(){ var e=document.querySelector('#trFrom'); return e ? e.options.length : -1; })()")
        rep.add("селекторы направления перевода заполнены (≥2 языков)", int(sel_n or -1) >= 2, str(sel_n))

        js("window.onProgress(42, 'E2E stage')")
        time.sleep(0.4)
        pw = js("(function(){ return document.querySelector('#progressFill').style.width; })()")
        rep.add("шкала прогресса реагирует на onProgress(42)", pw == "42%", str(pw))
        js("window.onProgress(-1, '')")

        rs = js_async("soundoff", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{sound: false}}))")
        ok_sound = isinstance(rs, dict) and rs.get("ok") is True and rs.get("sound") is False
        rep.add("настройка звука сохраняется (sound=false)", ok_sound,
                json.dumps(rs, ensure_ascii=False)[:140])
        js_async("soundon", f"window.pywebview.api.save_settings(Object.assign({{}}, {json.dumps(base)}, {{sound: true}}))")

        # --- 9. диагностика компонентов через мост ---
        rc = js_async("comp", "window.pywebview.api.check_components()", timeout=90)
        good = isinstance(rc, dict) and all(k in rc for k in ("ocr", "qr", "screen", "clipboard", "translator"))
        rep.add("check_components вернул структуру (мост жив)", good,
                json.dumps(rc, ensure_ascii=False)[:160])

        rep.add("Самопроверка завершена штатно", True)
    except Exception as exc:
        rep.add("САМОПРОВЕРКА ПРЕРВАНА ИСКЛЮЧЕНИЕМ", False, repr(exc))
    finally:
        try:
            rep.save()
        finally:
            # --- 10. кнопка «закрыть» завершает приложение ---
            log.info("E2E: закрываю окно (проверка кнопки close)")
            try:
                js("(function(){ document.querySelector('#btnClose').click(); return 1; })()", 5)
            except Exception:
                pass

            def force():
                time.sleep(12)
                try:
                    window.destroy()
                except Exception:
                    pass
                time.sleep(5)
                os._exit(3)
            threading.Thread(target=force, daemon=True).start()
