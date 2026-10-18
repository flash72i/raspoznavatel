#!/usr/bin/env bash
# ==========================================================================
# tools/build_release_wine.sh — ОДНА команда полной пересборки релиза
# «Распознаватель» (Windows x64) в Linux-песочнице через Wine.
#
#   bash tools/build_release_wine.sh doctor    # среда, диск, ресурсы, тесты
#   bash tools/build_release_wine.sh env       # Wine-префикс + Windows-Python + pip
#   bash tools/build_release_wine.sh build     # PyInstaller -> dist/Raspoznavatel/
#   bash tools/build_release_wine.sh check     # selftest готового exe (10 блоков)
#   bash tools/build_release_wine.sh package   # Setup.exe + портативный zip
#   bash tools/build_release_wine.sh smoke     # дымовой тест трея в Wine (Win32)
#   bash tools/build_release_wine.sh verify    # проверить УЖЕ собранные ассеты
#   bash tools/build_release_wine.sh all       # всё по порядку (doctor..package)
#   bash tools/build_release_wine.sh clean     # убрать временные гигабайты
#
# Результат кладётся в <workspace>/release_assets/:
#   Raspoznavatel-<ВЕРСИЯ>-Setup.exe
#   Raspoznavatel_v<ВЕРСИЯ>_win64_portable.zip   (в КОРНЕ: Raspoznavatel.exe + App/)
#
# ГЛАВНОЕ ПРАВИЛО этого скрипта (урок L33):
#   1) скрипт ПЕРЕЗАПУСКАЕТ сам себя под `tini -s` (child subreaper): PID 1 в
#      песочнице — node, он чужих сирот не подбирает, поэтому без субрепера
#      каждый wine-процесс навечно остаётся зомби и таблица процессов пухнет;
#   2) после КАЖДОГО wine-шага вызывается wine_cleanup — wineserver, wine64 и
#      Xvfb убиваются, и зомби тут же подбираются субрепером.
# Без этого процессы копятся, песочница исчерпывает их лимит, и сборка умирает
# посередине (так потеряли сессию 2026-10-18).
#
# Пути НЕ хардкодятся: корень проекта и корень песочницы вычисляются из
# расположения скрипта (урок L1/L2). Временные гигабайты (wine-префикс, логи)
# живут в ${TMPDIR:-/tmp}/raspoz-build — вне снапшота песочницы.
# ==========================================================================
set -uo pipefail

SELF="${BASH_SOURCE[0]}"
TOOLS_DIR="$(cd "$(dirname "$SELF")" && pwd)"
PROJ="$(cd "$TOOLS_DIR/.." && pwd)"                     # корень проекта
WS_ROOT="$(cd "$PROJ/.." && pwd)"                       # корень песочницы

BUILD_TMP="${BUILD_TMP:-${TMPDIR:-/tmp}/raspoz-build}"  # wine-префикс, логи, winpy
LOG_DIR="$BUILD_TMP/logs"
RELEASE_DIR="${RELEASE_DIR:-$WS_ROOT/release_assets}"   # куда класть готовые ассеты

export WINEPREFIX="${WINEPREFIX:-$BUILD_TMP/wineprefix}"
export WINEDEBUG="${WINEDEBUG:--all}"
export WINEDLGCANCEL=1

WINPY_EXE='C:\winpy\python.exe'                         # Windows-Python внутри префикса
WINPY_DIR="$WINEPREFIX/drive_c/winpy"
WINPY_URL="${WINPY_URL:-https://github.com/indygreg/python-build-standalone/releases/download/20240415/cpython-3.11.9%2B20240415-x86_64-pc-windows-msvc-install_only.tar.gz}"
WINPY_TAR="$BUILD_TMP/cpython-win.tar.gz"

MIN_FREE_GB="${MIN_FREE_GB:-5}"                         # урок L17
SELFTEST_TIMEOUT="${SELFTEST_TIMEOUT:-600}"
PIP_TIMEOUT="${PIP_TIMEOUT:-2400}"
PYI_TIMEOUT="${PYI_TIMEOUT:-2400}"
NSIS_TIMEOUT="${NSIS_TIMEOUT:-1800}"

VERSION="$(python3 - "$PROJ" <<'PY'
import io, re, sys
src = io.open(sys.argv[1] + "/config.py", encoding="utf-8").read()
print(re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src).group(1))
PY
)"
SETUP_NAME="Raspoznavatel-${VERSION}-Setup.exe"
ZIP_NAME="Raspoznavatel_v${VERSION}_win64_portable.zip"

# ---------------------------------------------------------------- утилиты ---
say()  { printf '\n=== %s ===\n' "$*"; }
log()  { printf '%s\n' "$*"; }
die()  { printf '\n[ОШИБКА] %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

free_gb() { df -BG --output=avail "${TMPDIR:-/tmp}" | tail -1 | tr -dc '0-9'; }

wpath() {                     # /a/b/c -> Z:\a\b\c   (урок L16)
  local p="$1"; p="${p//\//\\}"; printf 'Z:%s' "$p"
}

nproc_count() { ps -e --no-headers 2>/dev/null | wc -l; }
nzombies()    { ps -eo stat= 2>/dev/null | grep -c '^Z' || true; }

# L33: уборка после wine-шага. Порядок ВАЖЕН:
#   1) wineserver -k  — штатно снять всё дерево Wine;
#   2) wineserver -w  — ДОЖДАТЬСЯ выхода (иначе убитые на лету процессы
#                       остаются сиротами и превращаются в вечных зомби,
#                       которых PID 1 песочницы (node) не подбирает);
#   3) только потом точечно добить Xvfb, если он всё ещё жив.
# pkill по дереву Wine — крайняя мера: он и плодит зомби, и сам им становится.
wine_cleanup() {
  local why="${1:-после wine-шага}" i
  if have wineserver; then
    wineserver -k >/dev/null 2>&1
    timeout 90 wineserver -w >/dev/null 2>&1
  fi
  for i in 1 2 3; do                       # Xvfb от xvfb-run обычно умирает сам
    pgrep -x Xvfb >/dev/null 2>&1 || break
    pkill -x Xvfb >/dev/null 2>&1
    sleep 2
  done
  sleep 1
  log "[уборка] ${why}: процессов=$(nproc_count), зомби=$(nzombies), свободно=$(free_gb) ГБ"
  local z; z="$(nzombies)"
  if [[ "$z" -gt "${ZOMBIE_WARN:-200}" ]]; then
    log "[!] зомби уже $z — песочница близка к лимиту процессов: доделывай этап и завершай сессию"
  fi
}

trap 'wine_cleanup "trap EXIT (прерывание скрипта)"' EXIT INT TERM

need_disk() {
  local f; f="$(free_gb)"
  [[ "$f" -ge "$MIN_FREE_GB" ]] || die "свободно ${f} ГБ < ${MIN_FREE_GB} ГБ: сначала bash $0 clean (урок L17)"
}

# wine_run <аргументы wine64...> — xvfb-run, вывод в лог И на экран (tee, урок L11:
# никакого «> файл»), затем обязательная уборка процессов.
wine_run() {
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local logfile="$LOG_DIR/wine-${stamp}.log"
  mkdir -p "$LOG_DIR"
  log "[wine] $* (лог: $logfile)"
  xvfb-run -a wine64 "$@" 2>&1 | tee -a "$logfile"
  local rc=${PIPESTATUS[0]}
  wine_cleanup "wine_run rc=$rc"
  return "$rc"
}

wine_run_to() {              # wine_run_to <секунды> <аргументы...>
  local t="$1"; shift
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local logfile="$LOG_DIR/wine-${stamp}.log"
  mkdir -p "$LOG_DIR"
  log "[wine] timeout $t: $* (лог: $logfile)"
  xvfb-run -a timeout "$t" wine64 "$@" 2>&1 | tee -a "$logfile"
  local rc=${PIPESTATUS[0]}
  wine_cleanup "wine_run_to rc=$rc"
  return "$rc"
}

py() { wine_run_to "$PYI_TIMEOUT" "$WINPY_EXE" "$@"; }

# ------------------------------------------------------------------ doctor ---
cmd_doctor() {
  say "doctor: среда, диск, ресурсы, тесты"
  local miss=0 t
  for t in tini wine64 wineserver xvfb-run xauth 7z makensis python3 git tar timeout; do
    have "$t" && log "  [ок] $t" || { log "  [НЕТ] $t"; miss=1; }
  done
  have ps && have pkill || { log "  [НЕТ] procps (ps/pkill) — без него не работает уборка L33"; miss=1; }
  [[ "$miss" == "0" ]] || die "не хватает инструментов: apt-get install -y --no-install-recommends wine64 xvfb xauth p7zip-full nsis tini procps ca-certificates"
  if [[ "${RASPOZ_REAPER:-0}" == "1" ]]; then
    log "  [ок] скрипт под субрепером tini: зомби подбираются (урок L33)"
  else
    log "  [!] скрипт НЕ под субрепером tini — зомби будут копиться (L33)"
    miss=1
  fi

  # v2.6.2: статическая проверка имён — Windows-ветки (ctypes) на Linux не
  # выполняются, и NameError в них уезжает прямо к владельцу (урок L48).
  if ! python3 -c "import pyflakes" 2>/dev/null; then
    log "  ставлю pyflakes для проверки имён…"
    python3 -m pip install -q pyflakes >/dev/null 2>&1 || true
  fi
  if python3 -c "import pyflakes" 2>/dev/null; then
    local undef
    undef="$(cd "$PROJ" && python3 - "$PROJ" <<'PY'
import ast, io, os, sys
from pyflakes import checker
root = sys.argv[1]
files = ["api.py","main.py","tray.py","translator.py","config.py","ocr.py","log.py",
         "proc.py","qr_reader.py","screen_capture.py","e2e_selfcheck.py",
         "tools/download_resources.py","tests/test_logic.py","tests/tray_win_smoke.py"]
bad = []
for f in files:
    p = os.path.join(root, f)
    if not os.path.isfile(p):
        continue
    try:
        tree = ast.parse(io.open(p, encoding="utf-8").read())
    except SyntaxError as e:
        bad.append("%s: SyntaxError %s" % (f, e)); continue
    for m in checker.Checker(tree, p).messages:
        if m.__class__.__name__ == "UndefinedName":
            bad.append("%s:%d: %s" % (f, m.lineno, m.message % m.message_args))
print("\n".join(bad))
PY
) || true"
    if [[ -n "$undef" ]]; then
      printf '%s\n' "$undef" | sed 's/^/    /'
      die "pyflakes нашёл неопределённые имена (урок L48) — на Windows это NameError"
    fi
    log "  [ок] pyflakes: неопределённых имён нет"
  else
    log "  [!] pyflakes недоступен — пропускаю проверку имён (урок L48)"
  fi

  log "  диск: свободно $(free_gb) ГБ (нужно ≥ ${MIN_FREE_GB})"
  need_disk
  log "  версия проекта: $VERSION (config.py)"
  grep -q "Raspoznavatel-${VERSION}-Setup.exe" "$PROJ/installer.nsi" \
    || die "installer.nsi не совпадает с config.py по версии (урок L26)"

  # ресурсы
  local n_tess; n_tess="$(ls "$PROJ/resources/tessdata"/*.traineddata 2>/dev/null | wc -l)"
  log "  tessdata: языков $n_tess"
  [[ "$n_tess" -ge 15 ]] || die "в resources/tessdata меньше 15 языков"
  [[ -f "$PROJ/resources/tesseract/tesseract.exe" ]] || die "нет resources/tesseract/tesseract.exe"
  [[ -f "$PROJ/ui/assets/icon.ico" ]] || die "нет ui/assets/icon.ico"
  python3 - "$PROJ" <<'PY' || die "модели перевода повреждены (урок L18)"
import os, sys, zipfile
d = os.path.join(sys.argv[1], "resources", "argos")
want = {"en_ru": 150, "ru_en": 120}
found = {}
for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
    if not n.endswith(".argosmodel"):
        continue
    p = os.path.join(d, n)
    mb = os.path.getsize(p) / 1048576
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None, "битый zip: " + n
        names = z.namelist()
        assert any(x.endswith("sentencepiece.model") for x in names), "нет sentencepiece.model в " + n
        assert any("/model/" in x for x in names), "нет папки model/ в " + n
    for k in want:
        if k in n:
            found[k] = mb
    print("  [ок] %s (%.0f МБ)" % (n, mb))
missing = [k for k, v in want.items() if k not in found or found[k] < v]
raise SystemExit(1 if missing else 0)
PY
  say "doctor: быстрые тесты логики (Linux)"
  (cd "$PROJ" && python3 tests/test_logic.py | tail -3) || die "тесты красные — не строй поверх сломанного"
  log "doctor: ГОТОВО к сборке"
}

# --------------------------------------------------------------------- env ---
cmd_env() {
  say "env: Wine-префикс + Windows-Python + зависимости"
  need_disk
  mkdir -p "$BUILD_TMP" "$LOG_DIR" "$WINPY_DIR/.." 2>/dev/null || true

  if have wine64 && have xvfb-run; then :; else die "нет wine64/xvfb-run"; fi

  if [[ ! -d "$WINEPREFIX/drive_c" ]]; then
    log "создаю префикс $WINEPREFIX"
    wine_run_to 900 wineboot -u || die "wineboot не удался"
  else
    log "префикс уже есть: $WINEPREFIX"
  fi

  if [[ ! -f "$WINPY_DIR/python.exe" ]]; then
    [[ -f "$WINPY_TAR" ]] || {
      log "качаю Windows-Python: $WINPY_URL"
      python3 -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
        "$WINPY_URL" "$WINPY_TAR" || die "не скачать python-build-standalone"
    }
    rm -rf "$BUILD_TMP/wp"; mkdir -p "$BUILD_TMP/wp"
    tar xzf "$WINPY_TAR" -C "$BUILD_TMP/wp" || die "не распаковался tar"
    rm -rf "$WINPY_DIR"
    mv "$BUILD_TMP/wp/python" "$WINPY_DIR" || die "не перенёс winpy в префикс"
    rm -rf "$BUILD_TMP/wp"
  fi
  [[ -f "$WINPY_DIR/python.exe" ]] || die "нет $WINPY_DIR/python.exe"
  log "Windows-Python на месте: $WINPY_DIR/python.exe ($(du -sh "$WINPY_DIR" | cut -f1))"

  say "env: pip install -r requirements.txt (в Wine)"
  py -m pip install --no-warn-script-location --disable-pip-version-check \
     -r "$(wpath "$PROJ/requirements.txt")" || die "pip install не удался"

  say "env: проверка импортов (БЕЗ clr/pythonnet — урок L13)"
  py -c "import numpy, PIL, cv2, mss, keyboard, pyperclip, pytesseract, ctranslate2, sentencepiece, zxingcpp, pyzbar, PyInstaller, webview.platforms as _p; import importlib.util as u; assert u.find_spec('webview'); print('deps ok')" \
    || die "какая-то зависимость не импортируется в Wine"
  log "env: ГОТОВО"
}

# ------------------------------------------------------------------- build ---
cmd_build() {
  say "build: PyInstaller (onedir, dist/Raspoznavatel)"
  need_disk
  [[ -f "$WINPY_DIR/python.exe" ]] || die "сначала выполни: bash $0 env"
  cd "$PROJ" || die "нет папки проекта"
  rm -rf build dist dist_console build_console.spec
  py -m PyInstaller build.spec --noconfirm --clean || die "PyInstaller упал"
  [[ -f "$PROJ/dist/Raspoznavatel/Raspoznavatel.exe" ]] || die "нет dist/Raspoznavatel/Raspoznavatel.exe"
  rm -rf "$PROJ/build"                       # экономим диск (урок L17)
  log "build: exe собран, размер папки $(du -sh "$PROJ/dist/Raspoznavatel" | cut -f1)"
  log "build: в корне dist/Raspoznavatel → $(ls "$PROJ/dist/Raspoznavatel" | tr '\n' ' ')"
}

# ------------------------------------------------------------------- check ---
cmd_check() {
  say "check: selftest готовой сборки"
  need_disk
  cd "$PROJ" || die "нет папки проекта"
  local exe="$PROJ/dist/Raspoznavatel/Raspoznavatel.exe"
  [[ -f "$exe" ]] || die "сначала выполни: bash $0 build"
  local bundle="$PROJ/dist/Raspoznavatel"
  local data="$bundle/Raspoznavatel_data"
  local report="$data/selftest_report.txt"

  # Консольная копия НЕ нужна: отчёт selftest пишется в файл рядом с exe (L15),
  # а в Wine она к тому же может не загрузиться (c0000135) — урок L34.
  rm -rf "$data" "$bundle/Screenshots"
  log "запускаю selftest на собранном exe: RASPOZ_SELFTEST=1"
  RASPOZ_SELFTEST=1 wine_run_to "$SELFTEST_TIMEOUT" "$(wpath "$exe")" \
    || log "[!] wine вернул ненулевой код — проверяю отчёт (урок L22)"
  wine_cleanup "после selftest"

  [[ -f "$report" ]] || die "нет отчёта selftest: $report (урок L15)"
  say "check: отчёт selftest"
  cat "$report"
  mkdir -p "$LOG_DIR"; cp -f "$report" "$LOG_DIR/selftest_report.txt"
  python3 - "$report" <<'PY' || die "selftest НЕ пройден — релиз не публикуй"
import io, re, sys
txt = io.open(sys.argv[1], encoding="utf-8", errors="replace").read()
bad = []
nums = sorted({int(n) for n in re.findall(r"(?m)^\[(\d+)\]", txt)})
if len(nums) < 10 or nums != list(range(1, len(nums) + 1)):
    bad.append("ожидалось 10 блоков [1]..[10], найдено: %s" % nums)
if "ЗАВЕРШЕНО" not in txt:
    bad.append("нет строки «=== ЗАВЕРШЕНО ===» — selftest оборвался")
if re.search(r'"ok"\s*:\s*false', txt, re.I):
    bad.append('в отчёте есть "ok": false')
if re.search(r'"error"\s*:\s*"(?!null)', txt):
    bad.append('в отчёте непустой "error"')
for pat in ("ОШИБКА", "Traceback", "FAIL", "WinError"):
    if re.search(pat, txt):
        bad.append("в отчёте встречается «%s»" % pat)
print("блоков проверки: %d" % len(nums))
if bad:
    print("ПРОВАЛЫ:")
    for b in bad:
        print("  - " + b)
    raise SystemExit(1)
print("selftest: 10/10, ошибок нет")
PY
  # L35: selftest создаёт в папке сборки рабочие данные (распакованная модель
  # перевода ~200 МБ, Screenshots/, логи) — до NSIS и zip их быть НЕ должно.
  rm -rf "$data" "$bundle/Screenshots"
  wine_cleanup "после уборки рабочих данных selftest"
  [[ -d "$data" ]] && die "не удалось вычистить $data — в установщик попадёт мусор"
  log "check: ГОТОВО (10/10), папка сборки чистая: $(du -sh "$bundle" | cut -f1)"
  log "check: в корне папки → $(ls "$bundle" | tr '\n' ' ')"
}

# ------------------------------------------------------------------- smoke ---
# Дымовой тест трея НА НАСТОЯЩЕМ Win32 (в Wine): значок создаётся в потоке
# насоса, клики работают, зависший колбэк не глушит насос, watchdog перезапускает
# «мёртвый» трей, повторный запуск exe открывает окно. Юнит-тесты на Linux этого
# не видят — именно так v2.6.0 уехала к владельцу с нерабочей иконкой.
cmd_smoke() {
  say "smoke: дымовой тест трея в Wine (tests/tray_win_smoke.py)"
  cd "$PROJ" || die "нет папки проекта"
  [[ -f "$WINPY_DIR/python.exe" ]] || die "сначала выполни: bash $0 env"
  local script="tests/tray_win_smoke.py"
  [[ -f "$PROJ/$script" ]] || die "нет $script — тест трея не написать нечем"
  rm -f "$PROJ/tray_smoke_report.txt"
  # L11: вывод ТОЛЬКО через pipe (tee), никакого «> файл» — иначе WinError 6
  # -u: вывод БЕЗ буферизации — если тест встанет, в логе будет видно, на чём
  PYTHONIOENCODING=utf-8 wine_run_to "${SMOKE_TIMEOUT:-300}" "$WINPY_EXE" -u \
      "$(wpath "$PROJ/$script")" || log "[!] wine вернул ненулевой код — смотрю отчёт"
  wine_cleanup "после smoke-теста трея"
  local rep="$PROJ/tray_smoke_report.txt"
  [[ -f "$rep" ]] || die "нет отчёта smoke-теста ($rep)"
  cat "$rep"
  mkdir -p "$LOG_DIR"; cp -f "$rep" "$LOG_DIR/" 2>/dev/null || true
  grep -qE "SMOKE TRAY: [0-9]+ пройдено, 0 провалено" "$rep" \
    || die "дымовой тест трея ПРОВАЛЕН — релиз не публикуй"
  log "smoke: иконка отвечает на клики, watchdog чинит насос, повторный запуск открывает окно"
}

# ----------------------------------------------------------------- package ---
cmd_package() {
  say "package: установщик NSIS + портативный zip"
  need_disk
  cd "$PROJ" || die "нет папки проекта"
  [[ -d "$PROJ/dist/Raspoznavatel" ]] || die "сначала выполни: bash $0 build"
  # L35: в установщик и zip не должно попасть ничего лишнего — только exe и App/
  rm -rf "$PROJ/dist/Raspoznavatel/Raspoznavatel_data" "$PROJ/dist/Raspoznavatel/Screenshots"
  local extra; extra="$(ls "$PROJ/dist/Raspoznavatel" | grep -vxE 'App|Raspoznavatel\.exe' || true)"
  [[ -z "$extra" ]] || die "в dist/Raspoznavatel лишнее: $extra (ждём только App и Raspoznavatel.exe)"
  mkdir -p "$RELEASE_DIR"

  log "makensis installer.nsi (несколько минут)"
  makensis -V2 installer.nsi 2>&1 | tail -5
  [[ -f "$PROJ/dist/$SETUP_NAME" ]] || die "NSIS не создал dist/$SETUP_NAME"
  mv -f "$PROJ/dist/$SETUP_NAME" "$RELEASE_DIR/$SETUP_NAME"

  # zip: в КОРНЕ Raspoznavatel.exe и App/ — архивируем СОДЕРЖИМОЕ папки (L28)
  rm -f "$RELEASE_DIR/$ZIP_NAME"
  ( cd "$PROJ/dist/Raspoznavatel" && 7z a -tzip -mx=1 "$RELEASE_DIR/$ZIP_NAME" "*" >/dev/null ) \
    || die "7z не смог собрать zip"

  say "package: контроль готовых ассетов"
  cmd_verify
}

# ------------------------------------------------------------------ verify ---
# Отдельный этап: проверить УЖЕ собранные ассеты, ничего не пересобирая.
# ВАЖНО: листинги 7z читаются в переменную, а НЕ в конвейер с `grep -q` —
# при set -o pipefail быстрый grep обрывает 7z (SIGPIPE) и проверка «падает»
# на полностью исправном архиве (урок L36).
cmd_verify() {
  say "verify: структура и целостность ассетов"
  local setup="$RELEASE_DIR/$SETUP_NAME" zipf="$RELEASE_DIR/$ZIP_NAME"
  [[ -f "$setup" ]] || die "нет $setup — сначала: bash $0 package"
  [[ -f "$zipf"  ]] || die "нет $zipf — сначала: bash $0 package"

  local listing roots nfiles
  listing="$(7z l "$zipf")" || die "7z не смог прочитать $ZIP_NAME"

  log "--- zip: первые записи ---"
  printf '%s\n' "$listing" | sed -n '9,16p'

  # пути в листинге 7z идут ПОСЛЕ пяти полей (дата, время, атрибуты, размер, сжатое)
  roots="$(printf '%s\n' "$listing" \
    | sed -nE 's#^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [A-Za-z.]{5} +[0-9]+ +[0-9]* +##p' \
    | sed -E 's#/.*##' | sort -u | tr '\n' ' ')"
  log "--- zip: записи В КОРНЕ: $roots"
  case " $roots" in
    *" App "*|*" App/"*) : ;;
    *) die "в КОРНЕ zip нет папки App/ (урок L28): $roots" ;;
  esac
  case " $roots" in
    *" Raspoznavatel.exe "*) : ;;
    *) die "в КОРНЕ zip нет Raspoznavatel.exe (урок L28): $roots" ;;
  esac
  nfiles="$(printf '%s\n' "$listing" | grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2} .*[^/]$' || true)"
  log "--- zip: файлов внутри $nfiles"
  [[ "$nfiles" -gt 1000 ]] || die "в zip подозрительно мало файлов: $nfiles"

  log "--- Setup.exe: листинг (ожидаем ~1500+ файлов, ~800 МБ распаковано) ---"
  listing="$(7z l "$setup" 2>/dev/null | tail -6)" || true
  printf '%s\n' "$listing"
  nfiles="$(printf '%s\n' "$listing" | sed -nE 's/.* ([0-9]+) files.*/\1/p' | tail -1)"
  [[ -n "$nfiles" && "$nfiles" -gt 1000 ]] || die "в установщике подозрительно мало файлов: '${nfiles:-?}'"

  log "--- размеры (ожидаем сотни МБ) ---"
  python3 - "$setup" "$zipf" <<'PY' || die "размеры ассетов вне разумных пределов"
import os, sys
for p in sys.argv[1:]:
    mb = os.path.getsize(p) / 1048576
    print("  %-45s %7.0f МБ" % (os.path.basename(p), mb))
    if not (200 < mb < 1500):
        raise SystemExit(1)
PY
  say "verify: ГОТОВЫЕ АССЕТЫ ($RELEASE_DIR)"
  ls -la "$RELEASE_DIR"
  python3 - "$setup" "$zipf" <<'PY'
import hashlib, os, sys
for p in sys.argv[1:]:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    print("%s  %.0f МБ  sha256=%s" % (os.path.basename(p), os.path.getsize(p) / 1048576, h.hexdigest()))
PY
}

# ------------------------------------------------------------------- clean ---
cmd_clean() {
  say "clean: убираю временные гигабайты"
  have wineserver && wineserver -k >/dev/null 2>&1
  pkill -f wine64 >/dev/null 2>&1; pkill -f Xvfb >/dev/null 2>&1; pkill -f xvfb-run >/dev/null 2>&1
  rm -rf "$BUILD_TMP" "$WINEPREFIX" \
         "$PROJ/dist_console" "$PROJ/build_console.spec" "$PROJ/build"
  log "удалено: $BUILD_TMP, $WINEPREFIX, dist_console, build_console.spec, build"
  log "сэкономлено: свободно $(free_gb) ГБ"
  log "готовые ассеты (если были) лежат в $RELEASE_DIR"
}

# --------------------------------------------------------------------- all ---
cmd_all() { cmd_doctor; cmd_env; cmd_build; cmd_check; cmd_smoke; cmd_package; cmd_verify; }

usage() {
  cat <<USAGE
build_release_wine.sh — пересборка релиза «Распознаватель» в Wine
Проект:   $PROJ
Префикс:  $WINEPREFIX
Ассеты:   $RELEASE_DIR
Версия:   $VERSION

Этапы: doctor | env | build | check | smoke | package | verify | all | clean
USAGE
}

# --- L33: ВЕСЬ скрипт работает под субрепером (tini -s). PID 1 в песочнице —
# --- node, чужих сирот он не подбирает, поэтому без субрепера каждый wine-процесс
# --- остаётся вечным зомби и таблица процессов пухнет до отказа.
if [[ -z "${RASPOZ_REAPER:-}" ]]; then
  if have tini; then
    export RASPOZ_REAPER=1
    exec tini -s -- bash "$TOOLS_DIR/$(basename "$SELF")" "$@"
  fi
  printf '[внимание] нет tini: зомби от Wine не подбираются автоматически (урок L33).\n' >&2
  printf '           поставь: apt-get install -y --no-install-recommends tini procps\n' >&2
fi

case "${1:-all}" in
  doctor)  cmd_doctor ;;
  env)     cmd_env ;;
  build)   cmd_build ;;
  check)   cmd_check ;;
  package) cmd_package ;;
  smoke)   cmd_smoke ;;
  verify)  cmd_verify ;;
  clean)   cmd_clean ;;
  all)     cmd_all ;;
  -h|--help|help) usage ;;
  *)       usage; die "неизвестный этап: $1" ;;
esac
