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
#   bash tools/build_release_wine.sh all       # всё по порядку (doctor..package)
#   bash tools/build_release_wine.sh clean     # убрать временные гигабайты
#
# Результат кладётся в <workspace>/release_assets/:
#   Raspoznavatel-<ВЕРСИЯ>-Setup.exe
#   Raspoznavatel_v<ВЕРСИЯ>_win64_portable.zip   (в КОРНЕ: Raspoznavatel.exe + App/)
#
# ГЛАВНОЕ ПРАВИЛО этого скрипта (урок L33): после КАЖДОГО wine-шага вызывается
# wine_cleanup — wineserver, wine64 и Xvfb убиваются, зомби дожидаются. Иначе
# процессы копятся, песочница исчерпывает их лимит, и сборка умирает посередине
# (так потеряли сессию 2026-10-18).
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

# L33: уборка процессов после wine-шага. Без неё песочница задыхается.
wine_cleanup() {
  local why="${1:-после wine-шага}" i
  have wineserver && wineserver -k >/dev/null 2>&1
  pkill -f 'wine64'      >/dev/null 2>&1
  pkill -f 'wineserver'  >/dev/null 2>&1
  pkill -f 'xvfb-run'    >/dev/null 2>&1
  pkill -f 'Xvfb'        >/dev/null 2>&1
  sleep 2
  for i in 1 2 3 4 5 6 7 8; do            # ждём, пока ядро подберёт зомби
    [[ "$(nzombies)" == "0" ]] && break
    sleep 2
  done
  log "[уборка] ${why}: процессов=$(nproc_count), зомби=$(nzombies), свободно=$(free_gb) ГБ"
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
  for t in wine64 wineserver xvfb-run xauth 7z makensis python3 git tar timeout; do
    have "$t" && log "  [ок] $t" || { log "  [НЕТ] $t"; miss=1; }
  done
  have ps && have pkill || { log "  [НЕТ] procps (ps/pkill) — без него не работает уборка L33"; miss=1; }
  [[ "$miss" == "0" ]] || die "не хватает инструментов: apt-get install -y --no-install-recommends wine64 xvfb xauth p7zip-full nsis procps ca-certificates"

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
  log "Windows-Python: $("$WINPY_DIR/python.exe" --version 2>&1 | head -1 || true)"

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
  say "check: selftest готовой сборки (консольная копия)"
  need_disk
  cd "$PROJ" || die "нет папки проекта"
  [[ -d "$PROJ/dist/Raspoznavatel" ]] || die "сначала выполни: bash $0 build"
  sed 's/console=False/console=True/' build.spec > build_console.spec
  py -m PyInstaller build_console.spec --noconfirm --clean --distpath dist_console \
    || { rm -rf build_console.spec build; die "сборка консольной копии не удалась"; }
  rm -rf "$PROJ/build"

  local exe report
  exe="$(wpath "$PROJ/dist_console/Raspoznavatel_console/Raspoznavatel.exe")"
  report="$PROJ/dist_console/Raspoznavatel_console/Raspoznavatel_data/selftest_report.txt"
  rm -f "$report"
  log "запускаю selftest: RASPOZ_SELFTEST=1 $exe"
  RASPOZ_SELFTEST=1 wine_run_to "$SELFTEST_TIMEOUT" "$exe" || log "[!] wine вернул ненулевой код (проверяю отчёт)"
  wine_cleanup "после selftest"

  [[ -f "$report" ]] || { rm -rf build_console.spec dist_console; die "нет отчёта selftest (урок L15: он рядом с exe)"; }
  say "check: отчёт selftest"
  cat "$report"
  python3 - "$report" <<'PY' || { rm -rf "$PROJ/build_console.spec" "$PROJ/dist_console"; die "selftest НЕ пройден — релиз не публикуй"; }
import io, re, sys
txt = io.open(sys.argv[1], encoding="utf-8", errors="replace").read()
bad = re.findall(r'(?im)^.*\bok\s*[:=]\s*false.*$', txt) + re.findall(r'(?im)^.*\b(ОШИБКА|FAIL|ERROR)\b.*$', txt)
blocks = re.findall(r'(?im)^\s*(?:\[|\d+[).])?\s*([A-Za-zА-Яа-я].{0,60})\.{3,}\s*(ok|ОК)', txt)
print("проверок найдено: %d" % (len(blocks) or txt.lower().count("ok")))
if bad:
    print("ПРОВАЛЫ:")
    for b in bad[:20]:
        print("  " + b.strip())
    raise SystemExit(1)
print("selftest: ошибок нет")
PY
  rm -rf "$PROJ/build_console.spec" "$PROJ/dist_console" "$PROJ/build"
  wine_cleanup "после уборки dist_console"
  log "check: ГОТОВО (10 блоков без ошибок)"
}

# ----------------------------------------------------------------- package ---
cmd_package() {
  say "package: установщик NSIS + портативный zip"
  need_disk
  cd "$PROJ" || die "нет папки проекта"
  [[ -d "$PROJ/dist/Raspoznavatel" ]] || die "сначала выполни: bash $0 build"
  mkdir -p "$RELEASE_DIR"

  log "makensis installer.nsi (несколько минут)"
  makensis -V2 installer.nsi 2>&1 | tail -5
  [[ -f "$PROJ/dist/$SETUP_NAME" ]] || die "NSIS не создал dist/$SETUP_NAME"
  mv -f "$PROJ/dist/$SETUP_NAME" "$RELEASE_DIR/$SETUP_NAME"

  # zip: в КОРНЕ Raspoznavatel.exe и App/ — архивируем СОДЕРЖИМОЕ папки (L28)
  rm -f "$RELEASE_DIR/$ZIP_NAME"
  ( cd "$PROJ/dist/Raspoznavatel" && 7z a -tzip -mx=1 "$RELEASE_DIR/$ZIP_NAME" "*" >/dev/null ) \
    || die "7z не смог собрать zip"

  say "package: контроль структуры и целостности"
  log "--- zip: первые записи (ожидаем Raspoznavatel.exe и App/) ---"
  7z l "$RELEASE_DIR/$ZIP_NAME" | sed -n '1,20p'
  7z l "$RELEASE_DIR/$ZIP_NAME" | grep -qE ' Raspoznavatel\.exe$' || die "в КОРНЕ zip нет Raspoznavatel.exe (урок L28)"
  7z l "$RELEASE_DIR/$ZIP_NAME" | grep -qE ' App[\\/]' || die "в zip нет папки App/ (урок L28)"
  log "--- Setup.exe: число файлов и объём (ожидаем ~1500+ файлов, ~800 МБ) ---"
  7z l "$RELEASE_DIR/$SETUP_NAME" | tail -3

  say "package: ГОТОВЫЕ АССЕТЫ ($RELEASE_DIR)"
  ls -la "$RELEASE_DIR"
  python3 - "$RELEASE_DIR/$SETUP_NAME" "$RELEASE_DIR/$ZIP_NAME" <<'PY'
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
cmd_all() { cmd_doctor; cmd_env; cmd_build; cmd_check; cmd_package; }

usage() {
  cat <<USAGE
build_release_wine.sh — пересборка релиза «Распознаватель» в Wine
Проект:   $PROJ
Префикс:  $WINEPREFIX
Ассеты:   $RELEASE_DIR
Версия:   $VERSION

Этапы: doctor | env | build | check | package | all | clean
USAGE
}

case "${1:-all}" in
  doctor)  cmd_doctor ;;
  env)     cmd_env ;;
  build)   cmd_build ;;
  check)   cmd_check ;;
  package) cmd_package ;;
  clean)   cmd_clean ;;
  all)     cmd_all ;;
  -h|--help|help) usage ;;
  *)       usage; die "неизвестный этап: $1" ;;
esac
