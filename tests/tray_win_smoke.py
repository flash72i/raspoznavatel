# -*- coding: utf-8 -*-
"""
tests/tray_win_smoke.py — дымовой тест трея НА НАСТОЯЩЕМ Win32 (запуск в Wine).

Зачем: юнит-тесты на Linux проверяют чистые функции и структуру кода, но
«живой» насос сообщений, WM_TIMER, доставку WM_TRAYICON и перезапуск трея
watchdog'ом можно проверить только настоящими вызовами user32/shell32.

Запуск (из папки проекта, в Wine):
    xvfb-run -a wine64 "C:\\winpy\\python.exe" "Z:\\...\\tests\\tray_win_smoke.py"

Проверяется (баг v2.6.0 «иконка видна, но не нажимается»):
  1. окно трея и значок создаются В ПОТОКЕ НАСОСА, насос качает сообщения;
  2. WM_TIMER даёт пульс раз в секунду (watchdog меряет именно его);
  3. WM_TRAYICON + WM_LBUTTONUP → вызывается on_show (левый клик = показать);
  4. ЗАВИСШИЙ колбэк (on_show спит вечно) НЕ останавливает насос: следующие
     клики обрабатываются, пульс свежий — ровно то, что ломалось в v2.6.0;
  5. WM_RASPOZ_SHOW → on_show + подтверждение второму экземпляру (SetEvent);
  6. watchdog перезапускает трей, если пульс пропал, и трей снова здоров;
  7. NIM_MODIFY-проверка значка выполняется (значок не «теряется» молча).
"""

import ctypes
import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import log     # noqa: E402
import tray    # noqa: E402

# консоль Wine по умолчанию cp1252 — кириллица роняет print в самом конце
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = FAIL = 0
RESULTS = []
REPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tray_smoke_report.txt")


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append("OK   " + name)
    else:
        FAIL += 1
        RESULTS.append("FAIL " + name + ("  | " + str(extra) if extra else ""))


def pump_ok(t, limit=4.0):
    return not tray.pump_is_stale(t._last_beat, limit=limit, in_menu=t._in_menu)


def wait_for(cond, timeout=12.0, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def main():
    tray._setup_prototypes()          # явные argtypes/restype (урок L6)
    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.CreateEventW.restype = ctypes.c_void_p
    k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_wchar_p]
    k32.ResetEvent.argtypes = [ctypes.c_void_p]
    k32.WaitForSingleObject.restype = ctypes.c_uint
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]

    calls = {"show": 0, "toggle": 0, "exit": 0}
    hang = threading.Event()

    def on_show():
        calls["show"] += 1
        if hang.is_set():
            time.sleep(3600)      # «GUI-поток занят»: колбэк виснет намертво

    t = tray.TrayIcon(on_show=on_show,
                      on_exit=lambda: calls.__setitem__("exit", calls["exit"] + 1),
                      on_toggle=lambda: calls.__setitem__("toggle", calls["toggle"] + 1),
                      is_visible=lambda: False,
                      tooltip="Smoke test")

    # Wine может не иметь настоящей области уведомлений: если значок не создался,
    # подменяем Shell_NotifyIconW заглушкой — логика окна/насоса/watchdog та же.
    t.start()
    real_notify = ctypes.windll.shell32.Shell_NotifyIconW
    if not wait_for(lambda: t.icon_shown, timeout=8.0):
        RESULTS.append("INFO Shell_NotifyIcon в Wine не сработал — ставлю заглушку "
                       "(проверяем окно, насос, таймер, watchdog)")
        counter = {"add": 0, "modify": 0, "delete": 0}

        def fake_notify(code, data):
            key = {tray.NIM_ADD: "add", tray.NIM_MODIFY: "modify",
                   tray.NIM_DELETE: "delete"}.get(code, "add")
            counter[key] += 1
            return 1

        ctypes.windll.shell32.Shell_NotifyIconW = fake_notify
        t.icon_shown = False
        t._restart("smoke: заглушка Shell_NotifyIcon")
        wait_for(lambda: t.icon_shown, timeout=8.0)
    else:
        counter = None

    # --- 1-2: окно трея, поток, пульс -----------------------------------------
    check("поток трея жив", t._thread is not None and t._thread.is_alive())
    check("скрытое окно трея создано", bool(t._hwnd), t._hwnd)
    check("класс окна — RaspoznavatelTrayClass (его ищет второй экземпляр)",
          bool(user32.FindWindowW(tray.TRAY_CLASS, None)))
    check("значок в трее есть (icon_shown)", t.icon_shown is True)
    check("таймер-пульс поставлен (SetTimer)", bool(t._timer_id), t._timer_id)
    beat0 = t._last_beat
    check("насос качает сообщения: пульс обновляется", wait_for(lambda: t._last_beat > beat0, 6.0))
    check("healthy() = True (значок + поток + свежий пульс)", t.healthy() is True)
    _tid = user32.GetWindowThreadProcessId(t._hwnd, None)
    check("окно трея принадлежит ПОТОКУ НАСОСА (а не watchdog'у) — иначе значок «муляж»",
          _tid == t._thread.ident, "win tid=%s, thread.ident=%s" % (_tid, t._thread.ident))

    # --- 3: клик по значку ----------------------------------------------------
    calls["show"] = 0
    user32.PostMessageW(t._hwnd, tray.WM_TRAYICON, 0, tray.WM_LBUTTONUP)
    check("левый клик по значку → окно показывают (on_show)",
          wait_for(lambda: calls["show"] >= 1, 5.0), calls)

    # --- 4: зависший колбэк не останавливает насос (баг v2.6.0) ---------------
    hang.set()
    calls["show"] = 0
    user32.PostMessageW(t._hwnd, tray.WM_TRAYICON, 0, tray.WM_LBUTTONUP)
    wait_for(lambda: calls["show"] >= 1, 5.0)          # этот поток зависнет навсегда
    beat_hang = t._last_beat
    time.sleep(2.0)
    check("насос ЖИВ, пока колбэк висит (пульс свежий)", pump_ok(t),
          "возраст пульса %.1f с" % (time.time() - t._last_beat))
    user32.PostMessageW(t._hwnd, tray.WM_TRAYICON, 0, tray.WM_LBUTTONUP)
    check("следующий клик обрабатывается, хотя предыдущий колбэк завис навсегда",
          wait_for(lambda: calls["show"] >= 2, 6.0), calls)
    check("healthy() остаётся True при зависшем колбэке", t.healthy() is True)
    hang.clear()

    # --- 5: WM_RASPOZ_SHOW (повторный запуск exe) -----------------------------
    ack = k32.CreateEventW(None, False, False, tray.SHOW_ACK_EVENT)
    k32.ResetEvent(ack)
    calls["show"] = 0
    user32.PostMessageW(t._hwnd, tray.WM_RASPOZ_SHOW, 0, 0)
    got_ack = (k32.WaitForSingleObject(ack, 5000) == tray.WAIT_OBJECT_0)
    check("WM_RASPOZ_SHOW → окно показывают", wait_for(lambda: calls["show"] >= 1, 5.0), calls)
    check("WM_RASPOZ_SHOW → второй экземпляр получает подтверждение (ack-событие)",
          got_ack)

    # --- 6: watchdog чинит «мёртвый» насос ------------------------------------
    old_thread = t._thread
    old_hwnd = t._hwnd
    old_gen = t._generation
    # ЧЕСТНАЯ имитация залипшего насоса: обработчик WM_TIMER засыпает,
    # сообщения перестают доставляться, пульс стареет → watchdog обязан
    # перезапустить ВЕСЬ поток трея (новое окно + новый значок).
    t._probe_icon = lambda: time.sleep(25)
    user32.PostMessageW(t._hwnd, tray.WM_TIMER, tray.TRAY_TIMER_ID, 0)
    check("watchdog замечает мёртвый насос и перезапускает трей",
          wait_for(lambda: t._restarts >= 1 and t._thread is not old_thread, 25.0),
          "restarts=%s" % t._restarts)
    check("после перезапуска трей снова здоров", wait_for(lambda: t.healthy(), 25.0))
    check("новое окно трея найдено вторым экземпляром",
          bool(user32.FindWindowW(tray.TRAY_CLASS, None)) and t._hwnd != old_hwnd)
    check("поколение потока выросло: старый поток больше не главный",
          t._generation > old_gen)
    del t._probe_icon                 # возвращаем штатную проверку значка
    calls["show"] = 0
    user32.PostMessageW(t._hwnd, tray.WM_TRAYICON, 0, tray.WM_LBUTTONUP)
    check("клик по НОВОМУ значку работает", wait_for(lambda: calls["show"] >= 1, 6.0), calls)

    # --- 7: проверка значка NIM_MODIFY ----------------------------------------
    if counter is not None:
        t._last_probe = 0
        user32.PostMessageW(t._hwnd, tray.WM_TIMER, tray.TRAY_TIMER_ID, 0)
        check("WM_TIMER запускает проверку значка (NIM_MODIFY)",
              wait_for(lambda: counter["modify"] >= 1, 6.0), counter)

    t.stop()
    time.sleep(1.0)
    check("stop(): поток трея остановлен", t._thread is None or not t._thread.is_alive()
          or not t._running)
    if counter is not None:
        ctypes.windll.shell32.Shell_NotifyIconW = real_notify

    text = "\n".join(RESULTS) + "\n\nSMOKE TRAY: %d пройдено, %d провалено\n" % (PASS, FAIL)
    try:
        io.open(REPORT, "w", encoding="utf-8").write(text)   # отчёт в файл (L15)
    except Exception:
        pass
    print("")
    print(text)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
