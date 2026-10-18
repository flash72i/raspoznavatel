# INSTRUCTIONS.md — инструкция ИИ-агенту по работе с репозиторием «Распознаватель»

> **Кто читает этот файл:** ИИ-агент (Qwen, Claude, ChatGPT, Cursor и т.п.), которому
> дали доступ к репозиторию `flash72i/raspoznavatel` для анализа кода, внесения
> изменений, добавления функций и пересборки программы.
> **Правило нуля:** прочитай этот файл ЦЕЛИКОМ до первых действий. Здесь собраны
> окружение, проверенные команды и уроки прошлых сессий — чтобы ты не тратил время
> на поиски и не наступал на известные грабли.
> **Владелец:** flash72i. Владелец НЕ программист: не задавай ему технических
> вопросов, результат объясняй одной человеческой фразой, всё делай сам.

---

## 1. Проект за 30 секунд

* **«Распознаватель»** — портативная офлайн-программа для **Windows 10/11 x64**:
  OCR текстов с экрана и изображений (Tesseract, 15 языков), QR/штрих-коды
  (zxing-cpp + pyzbar), скриншоты области экрана, офлайн-перевод EN↔RU
  (ctranslate2 + sentencepiece, модели Argos), история распознаваний, трей,
  автозапуск, настраиваемые горячие клавиши.
* **Интерфейс:** HTML/CSS/JS внутри нативного окна **pywebview** (WebView2 через
  pythonnet). Мост «JS ↔ Python» — `api.py` (класс `Api`, методы вызываются из JS).
* **Сборка:** PyInstaller → портативная папка `dist/Raspoznavatel/`
  (`build.spec`, onedir, `console=False`), установщик — NSIS (`installer.nsi`).
  Официальные релизы собраны **в Linux через Wine** — и ты можешь так же.
* **Ключевые модули:** `main.py` (точка входа, selftest), `api.py`, `ocr.py`,
  `qr_reader.py`, `translator.py`, `screen_capture.py`, `tray.py` (трей, ctypes),
  `proc.py` (запуск процессов БЕЗ консоли), `config.py` (пути, настройки,
  `APP_VERSION`), `log.py`.
* **Версия живёт в ТРЁХ местах** (синхронизируй обязательно):
  `config.py:APP_VERSION`, `installer.nsi` (OutFile, VIProductVersion, FileVersion,
  ProductVersion, DisplayName, DisplayVersion), `README.md`.
* **Ресурсы:** `resources/tessdata` (15 языков, в git), `resources/tesseract`
  (движок, в git), `ui/assets/fonts` (в git), `resources/argos/*.argosmodel`
  (модели перевода, **в git НЕ входят** — качаются, лежат в `.gitignore`).

---

## 2. Инструменты и как их ставить

В свежей песочнице нет НИЧЕГО из списка ниже, кроме `git`, `python3` (Linux),
`7z` (может не быть) и бэкап-скриптов. Ставь одним заходом:

```bash
# 1) базовый набор (wine64 БЕЗ мета-пакета wine — см. урок L11;
#    procps ОБЯЗАТЕЛЕН: без ps/pkill не работает уборка процессов, урок L33)
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    wine64 xvfb xauth p7zip-full nsis procps ca-certificates
ln -sf ../lib/wine/wine64 /usr/bin/wine64          # apt не создаёт врапперы
ln -sf ../lib/wine/wineserver64 /usr/bin/wineserver

# 2) бэкап-система (если папки tools/ нет): приватный репо flash72i/agent-tools
#    (gh-backup.sh + safe-edit.sh) или файл BACKUP-BUNDLE.md от владельца
git clone https://<ТОКЕН-НЕ-В-URL>@github.com/flash72i/agent-tools.git tools   # токен клади в .git-credentials, см. §7

# 3) токен GitHub — ОДИН РАЗ проси у владельца (scope repo), храни по §7
bash tools/gh-backup.sh status        # проверка: токен, аккаунт, привязки

# 4) проект
git clone https://github.com/flash72i/raspoznavatel.git
```

**Windows-Python для сборки (проверенный источник — урок L12):**

```bash
# python-build-standalone install_only: есть tkinter и pip, ничего не надо дошивать
python3 -c "import urllib.request as u; u.urlretrieve(
 'https://github.com/indygreg/python-build-standalone/releases/download/20240415/cpython-3.11.9%2B20240415-x86_64-pc-windows-msvc-install_only.tar.gz','cpython-win.tar.gz')"
mkdir -p /tmp/wp "$WINEPREFIX/drive_c/winpy"
tar xzf cpython-win.tar.gz -C /tmp/wp && mv /tmp/wp/python "$WINEPREFIX/drive_c/winpy"
export WINEDEBUG=-all
xvfb-run -a wine64 wineboot -u
xvfb-run -a wine64 "C:\\winpy\\python.exe" -m pip install -r requirements.txt
```

**Что чем делается (шпаргалка):**

| Задача | Инструмент | Команда-образец |
|---|---|---|
| Юнит-тесты логики | python3 (Linux) | `python3 tests/test_logic.py` |
| Зависимости для exe | pip **в Wine** | `xvfb-run -a wine64 C:\winpy\python.exe -m pip install -r requirements.txt` |
| Ресурсы (модели/шрифты) | python3 (Linux) | `python3 tools/download_resources.py` |
| Сборка exe | PyInstaller **в Wine** | `xvfb-run -a wine64 C:\winpy\python.exe -m PyInstaller build.spec --noconfirm --clean` |
| Самодиагностика exe | Wine + `RASPOZ_SELFTEST=1` | отчёт в `Raspoznavatel_data/selftest_report.txt` |
| Установщик | makensis (Linux) | `makensis -V2 installer.nsi` (~6 мин) |
| Портативный zip | 7z | `7z a -tzip -mx=1 out.zip dist/Raspoznavatel` |
| Релизы/теги/видимость | GitHub API | python3 urllib (curl в песочнице обычно НЕТ) |
| Бэкапы | tools/gh-backup.sh | `snapshot` → правки → `push` |

---

## 3. С чего начинать (первые 10 минут сессии)

1. Прочитай этот файл и `README.md`; посмотри `git log --oneline | head` —
   последняя версия и стиль сообщений коммитов.
2. Проверь бэкап-систему: `bash tools/gh-backup.sh status`. Нет токена → проси
   у владельца ОДИН раз, положи по §7. Не продолжай без токена.
3. Склонируй проект (или убедись, что он есть и на `main`, `git pull`).
4. Прогони быстрые тесты БЕЗ сборки: `python3 tests/test_logic.py`
   (должно быть «0 провалено»; если падают ДО твоих правок — скажи владельцу и
   разберись, не строй поверх сломанного).
5. Осмотрись по задаче: для трея/окна/горячих клавиш — `tray.py`, `main.py`;
   для всего, что жмётся в интерфейсе — `api.py` + `ui/`; для запусков внешних
   процессов — только `proc.py`.
6. **Снимок ДО правок:** `bash tools/gh-backup.sh snapshot <папка> "зачем"` —
   точка отката. Без снимка правки не начинай.
7. Проверь диск: `df -h /` (свободно ≥ 5 ГБ, иначе сначала убери мусор по L17).

---

## 4. Пошаговый рабочий цикл

### 4.1 Изменения
1. Снимок (п.6 раздела 3).
2. Правь код. Новые запуски внешних процессов — только через `proc.run/proc.kwargs`
   (иначе пользователь увидит мигание консоли, см. L8–L9).
3. Win32/ctypes-код — по L6–L7 (64-битные типы, фолбэки).
4. Добавь/обнови проверки в `tests/test_logic.py` (чистые функции тестируются на
   Linux; поведение Windows эмулируй, см. L21).
5. `python3 tests/test_logic.py` → «0 провалено».

### 4.2 Версия (если правка меняет поведение программы)
Подними версию в трёх местах: `config.py`, `installer.nsi`, `README.md` (L26).

### 4.3 Сборка exe (Wine)

**Полная пересборка релиза одной командой (рекомендуется):**
```bash
bash tools/build_release_wine.sh all        # doctor -> env -> build -> check -> package
# по этапам: doctor | env | build | check | package | clean
# готовые ассеты: <workspace>/release_assets/ (Setup.exe + portable.zip)
```
Скрипт сам ставит проверки (диск ≥5 ГБ, ресурсы, тесты), сам собирает, гоняет
selftest и ОБЯЗАТЕЛЬНО убирает wine-процессы после каждого шага (урок L33).
Ниже — те же шаги вручную, если нужно отладить отдельный этап.

```bash
export WINEDEBUG=-all WINEPREFIX=<workspace>/wineprefix
cd <workspace>/raspoznavatel
rm -rf build dist
xvfb-run -a wine64 "C:\\winpy\\python.exe" -m PyInstaller build.spec \
    --noconfirm --clean 2>&1 | tee /tmp/pyi.log | tail -5   # НЕ редирект в файл (L11)
ls dist/Raspoznavatel/Raspoznavatel.exe
```
Сборка занимает ~3 минуты. Предупреждения «Scripts not on PATH» от pip и
«OpenBLAS WARNING» при запуске — безвредны.

### 4.4 Проверка собранного (обязательно)
```bash
# консольная копия для отчёта на stdout не нужна: отчёт пишется в файл (L15)
sed 's/console=False/console=True/' build.spec > build_console.spec
xvfb-run -a wine64 "C:\\winpy\\python.exe" -m PyInstaller build_console.spec \
    --noconfirm --clean --distpath dist_console 2>&1 | tail -2
RASPOZ_SELFTEST=1 xvfb-run -a timeout 400 wine64 "<Z-путь>\\dist_console\\Raspoznavatel_console\\Raspoznavatel.exe"
cat dist_console/Raspoznavatel_console/Raspoznavatel_data/selftest_report.txt
```
Ожидай 10 блоков без ошибок: Tesseract (15 языков), OCR-проба, движки QR,
QR-цикл, перевод EN→RU живой строкой, tkinter, PIL/mss, папка скриншотов,
keyboard, превью. Любое `ok: false` — не публикуй релиз, чини.
После проверки удали `build_console.spec` и `dist_console`.

### 4.5 Установщик и портативный zip
```bash
makensis -V2 installer.nsi          # → dist/Raspoznavatel-<ВЕРСИЯ>-Setup.exe (~455 МБ, ~6 мин)
# ТРЕБОВАНИЕ ВЛАДЕЛЬЦА (2026-10-18): в КОРНЕ zip лежат Raspoznavatel.exe и App/,
# БЕЗ вложенных dist/Raspoznavatel/ — архивируем СОДЕРЖИМОЕ папки:
cd dist/Raspoznavatel
7z a -tzip -mx=1 "$WS/Raspoznavatel_v<ВЕРСИЯ>_win64_portable.zip" "*"
```
Проверь структуру до публикации: `7z l …zip | head` — первые записи
`Raspoznavatel.exe`, `App/…`. Иначе владелец получит лишних два уровня папок.
Тихую установку NSIS в Wine НЕ проверяй — не работает ни один NSIS (L14).
Целостность установщика подтверждай так: `7z l dist/*-Setup.exe | tail`
(ожидай ~1500+ файлов, ~800 МБ распаковано) + selftest портативной сборки (4.4).

### 4.6 Код и бэкап
```bash
git add -A && git commit -m "v<ВЕРСИЯ>: что сделано по-человечески"
bash tools/gh-backup.sh push <папка> -m "то же сообщение"
```

### 4.7 Релиз на GitHub (правило «ОДИН чистый релиз»)
Владелец требует: на странице Releases всегда **один** релиз — актуальная
версия, описание = «что за программа, для чего, как поставить», БЕЗ «что нового»
и истории. Порядок через API (python3 urllib, токен в заголовке):
1. `GET /repos/{o}/{r}/releases` → `DELETE` все релизы кроме целевого тега
   (ассеты удалятся вместе с релизом);
2. `DELETE /repos/{o}/{r}/git/refs/tags/<старые теги>`;
3. запушь новый тег `v<ВЕРСИЯ>` (`git tag -a … && git push origin v<ВЕРСИЯ>`);
4. `POST /releases` (`tag_name`, `name`, `body` — чистое описание);
5. загрузи оба артефакта на `upload_url` релиза (стриминг: `urlopen(req, fileobj)`,
   `Content-Type: application/octet-stream`, L25);
6. при необходимости `PATCH /repos/{o}/{r}` — описание репозитория и видимость
   (в теле ОБЯЗАТЕЛЬНО `name`, L24).

### 4.8 Финал сессии
1. `bash tools/gh-backup.sh push <папка> -m "итог"` — бэкап ДО отчёта владельцу.
2. Допиши новые уроки в раздел 8 этого файла и запушь его вместе с проектом (§9).
3. Убери мусор: `rm -rf wineprefix dist_console build_console.spec` и прочие
   временные гигабайты.
4. Скажи владельцу ОДНУ фразу: что исправлено/добавлено + ссылка на релиз.
   Напомни отозвать токен, если он присылал его в чат.

---

## 5. Тесты: что где запускается

| Тест | Где | Команда | Примечание |
|---|---|---|---|
| `tests/test_logic.py` | Linux, без зависимостей Windows | `python3 tests/test_logic.py` | ~110+ проверок, ~8 c; гоняй ПОСЛЕ каждых правок логики |
| `tests/ui_test.py` | headless Chromium | — | в чистой песочнице обычно недоступен — не застревай |
| `tests/capture_e2e.py` | pywebview + X | — | то же |
| selftest exe | Wine | см. 4.4 | главная проверка ГОТОВОЙ сборки |

---

## 6. Правила репозитория, релизов и описаний

* Репозиторий **публичный**; описание (About) — одна строка: что за программа
  и главные возможности. Без «auto-backup» и технического мусора.
* Releases: один релиз = одна актуальная версия = установщик + портативный zip;
  тело релиза — описание программы и установка, НЕ changelog.
* `README.md` — описание программы и способов сборки; журналы «что было в 2.x»
  не вести (история живёт в коммитах).
* Ветки `savepoint-*` и `pre-rollback-*` — точки отката бэкап-системы:
  не удаляй их молча (чистятся скриптом сами, держится 10 последних).
* Большие бинарники (модели, dist) в git НЕ коммитятся — только ассетами релиза.

---

## 7. Безопасность

* Токен GitHub: только в `<workspace>/.git-credentials` (chmod 600) +
  `credential.helper store --file …`; НИКОГДА в чате, логах, коммитах, URL
  remote'а. Засветился → напомни владельцу отозвать.
* Ничего не удаляй в проекте и на GitHub без явной просьбы владельца
  (бэкап-скрипт по умолчанию отменяет пуш с удалениями — это защита, не помеха).
* Перед пушем нового кода проверь, что не тащишь секреты (`.env`, ключи).

---

## 8. Уроки (Lessons Learned)

Формат: **если X → делай Y, иначе получишь Z.** Новые уроки дописывай в конец
своего подраздела (§9); старые не удаляй.

### Песочница, пути, git
* **L1.** Если скрипту (python/heredoc) нужен путь workspace → передавай его
  через `sys.argv`/переменную окружения (`$ARENA_WORKSPACE`), а не литералом
  `/home/user` в тексте → иначе песочница перепишет литерал в строку
  `"$ARENA_WORKSPACE"`, и скрипт упадёт с FileNotFoundError.
* **L2.** Не полагайся на `~` и `$HOME`: в песочнице это `/tmp`, он не переживает
  сессию → иначе потеряешь файлы (токен, конфиги) между сессиями.
* **L3.** Нет токена → проси у владельца один раз (scope `repo`) и клади в
  `.git-credentials` → иначе «No such device or address» при clone/push.
* **L4.** Собираешься править проект → сначала `gh-backup.sh snapshot` →
  иначе при поломке некуда откатиться.
* **L5.** `curl` в песочнице обычно нет → GitHub API и скачивания делай
  через `python3 urllib` → иначе «command not found» и потерянное время.

### Windows-код (ctypes, трей, процессы)
* **L6.** Пишешь Win32-колбэк на ctypes → `WPARAM/LPARAM/LRESULT` только
  `c_size_t/c_ssize_t` и явные `argtypes/restype` у ВСЕХ вызовов → иначе на x64
  указатели обрежутся до 32 бит: обработчики получат мусор (так родилось
  «белое меню с невидимыми надписями» v2.5.0).
* **L7.** Рисуешь пункты меню сам (`MF_OWNERDRAW`) → держи фолбэк на системное
  меню `MF_STRING` (флаг `_ownerdraw_failed` + try/except вокруг отрисовки) →
  иначе любой сбой отрисовки = пустое белое меню у пользователя.
* **L8.** Запускаешь внешний процесс из программы → только `proc.run/proc.kwargs`
  (`CREATE_NO_WINDOW` + `STARTUPINFO` c `SW_HIDE`) → иначе у пользователя на
  мгновение мелькает чёрное окно командной строки.
* **L9.** Сторонняя библиотека сама дёргает subprocess (pytesseract→tesseract.exe)
  → прокинь флаги через `proc.patch_module(внутренний_модуль)` → иначе мигание
  консоли останется, хотя «свой» код чист.

### Сборка в Wine
* **L10.** Ставишь `wine64` без мета-пакета `wine` → создай симлинки
  `/usr/bin/wine64 → ../lib/wine/wine64` и `wineserver`, и поставь `xauth` →
  иначе «command not found» и «xvfb-run: xauth not found».
* **L11.** Запускаешь Windows-Python в Wine → НЕ оборачивай в `cmd /c` и НЕ
  редиректь вывод в файл (`> log`) → иначе `Fatal Python error: init_sys_streams`
  / `OSError WinError 6`; запускай `wine64 python.exe …` напрямую, вывод через
  pipe (`| tee log`).
* **L12.** Нужен Windows-Python с tkinter → бери python-build-standalone
  (install_only tar.gz) → иначе: в NuGet-пакете нет tkinter, а официальный
  MSI-бандл 7z распаковывает частично — часы возни впустую.
* **L13.** Проверяешь зависимости в Wine → НЕ импортируй `clr`/pythonnet
  (Wine-mscoree падает с assert); анализ PyInstaller модули не исполняет, сборке
  не мешает. И помни: пакет `pywebview` импортируется как `webview` →
  иначе ложный ModuleNotFoundError.
* **L14.** Хочешь проверить NSIS-установщик тихой установкой в Wine → не трать
  время: там не запускается НИ ОДИН NSIS-установщик (проверено мини-примером);
  проверяй `7z l` (число файлов/объём) + selftest портативной сборки.
* **L15.** Ищешь отчёт selftest → он в `Raspoznavatel_data/selftest_report.txt`
  рядом с exe, не в stdout → иначе решишь, что проверка «ничего не вывела».
* **L16.** Передаёшь пути в Wine → собирай строку `Z:\…` из ФАКТИЧЕСКОГО пути
  workspace (env), а вывод Wine грепь с `grep -a` (в нём UTF-16 с нулями) →
  иначе «file not found» и «binary file matches».
* **L17.** Свободного места < 5 ГБ → снеси `wineprefix`, `dist_console`,
  `build_src` ДО новой сборки → иначе сборка умрёт посередине на полном диске.
* **L33.** Wine-шаг завершился (wineboot, pip, PyInstaller, selftest) → СРАЗУ
  убей Wine и дождись пропадания зомби: `wineserver -k`, `pkill -f wine64`,
  `pkill -f Xvfb`, `pkill -f xvfb-run` + несколько секунд ожидания в цикле
  (нужен пакет `procps`, иначе `ps`/`pkill` просто нет), и только потом
  начинай следующий шаг; всю пересборку гони одним
  `bash tools/build_release_wine.sh all`, где уборка встроена после каждого
  wine-вызова и в `trap EXIT` → иначе зомби копятся, песочница исчерпывает
  лимит процессов, и сборка с уже готовым exe умирает на последнем шаге
  (потерянная сессия 2026-10-18).

### Ресурсы
* **L18.** `download_resources.py` сказал «готово» про модели перевода → всё
  равно проверь `zipfile.ZipFile(...).testzip()` и размер (~196 / ~156 МБ):
  зеркало argos-net.com иногда отдаёт обрубленные файлы; рабочее зеркало —
  HuggingFace `TiberiuCristianLeon/Argostranslate` (файлы `en_ru.argosmodel`,
  `ru_en.argosmodel`) → иначе релиз выйдет с мёртвым переводом.
* **L19.** Тебя тянет закоммитить модели перевода или `dist/` → остановись:
  они в `.gitignore`, пуш сотен МБ всё равно не пройдёт; бинарники живут
  ассетами релиза → иначе сломанный пуш и переполненный репозиторий.

### Тесты и проверка себя
* **L20.** Правка логики → сначала `tests/test_logic.py` на Linux (8 секунд),
  ПОТОМ сборка (~3 мин) → иначе ловишь простые ошибки дорогой ценой.
* **L21.** Тестируешь Windows-поведение на Linux → эмулируй платформу
  (`proc.inject(..., force=True)`, подмена `proc.os`) → иначе тест
  «зелёный везде, кроме Windows» или красный везде, кроме Windows.
* **L22.** Проверка упала по причине окружения (Wine, зеркало) → не объявляй
  сборку сломанной: найди независимое подтверждение (мини-репродуктор, `7z l`,
  selftest) → иначе выбросишь рабочую сборку из-за кривого стенда.

### GitHub, релизы, версии
* **L23.** Обновляешь релиз → сначала удали старые релизы и их теги, ПОТОМ
  создавай один новый с ассетами; тело — описание программы, не changelog →
  иначе владелец снова увидит «свалку версий».
* **L24.** Меняешь видимость/описание репо через API → в теле PATCH обязаны быть
  `name` и `private` → иначе 422 или молчаливый игнор.
* **L25.** Заливаешь ассеты >100 МБ → стримингом на `upload_url` релиза
  (`urlopen(req, file_object)`) И с явным заголовком `Content-Length`
  (размер файла) → иначе GitHub вернёт 422 Validation Failed (field: size).
* **L26.** Поднимаешь версию → синхронно правь `config.py`, `installer.nsi`
  (6 мест), `README` → иначе установщик и программа спорят о версии.

### Процесс
* **L27.** Закончил работу → пуш на GitHub ДО того, как показать файлы владельцу,
  и новые уроки — в раздел 8 → иначе следующая сессия повторит твои грабли.

### Архивы и трей
* **L28.** Собираешь портативный zip → архивируй СОДЕРЖИМОЕ `dist/Raspoznavatel`
  (`cd dist/Raspoznavatel && 7z a -tzip … "*"`), чтобы в корне zip были
  `Raspoznavatel.exe` и `App/` → иначе при распаковке получатся вложенные
  `dist/Raspoznavatel/…` — владелец просил такую структуру НЕ делать (2026-10-18).
* **L29.** Публикуешь сборку, которой ещё не было на машине пользователя →
  Windows спрячет НОВЫЙ значок трея под стрелку «^» у часов: при первом
  сворачивании показывай balloon-подсказку (`TrayIcon.notify`), а здоровье трея
  проверяй `ensure_icon()` перед скрытием окна → иначе пользователь получит
  «процесс висит, окна и значка нет» и посчитает программу сломанной.
* **L30.** Пользователь может кликнуть exe повторно, когда окно спрятано →
  держи single-instance (мьютекс + `claim_single_instance()`, показ окна первого
  экземпляра через `WM_RASPOZ_SHOW`) → иначе в диспетчере задач висят два
  процесса одной программы и два трей-значка воюют за один uID.
* **L31.** Значок трея может быть снят системой в любой момент (explorer
  перезапустился, поток погиб) → держи watchdog-поток, который раз в 5 с
  проверяет `icon_shown` и пересоздаёт значок, а цикл сообщений трея оберни
  в retry (3 попытки) → иначе «значок появился на секунду и пропал».
* **L32.** Кнопка «Проверить звук» в настройках УДАЛЕНА по прямой просьбе
  владельца (2026-10-18) — не возвращай её; звуки уведомлений работают сами.

---

## 9. Как поддерживать эту инструкцию

* Столкнулся с новой ошибкой/неочевидностью, которой нет в разделе 8 → после
  решения допиши урок в конец нужного подраздела: одной строкой
  `* **Lnn.** если X → делай Y, иначе Z` (нумерацию продолжай).
* Устаревший урок не удаляй — зачеркни и припиши `(неактуально с <дата>: …)`:
  история граблей ценнее чистоты списка.
* Изменил процесс сборки/инструменты → обнови разделы 2 и 4 в том же коммите.
* Коммит с правкой инструкции пушится вместе с проектом
  (`gh-backup.sh push`), владелец видит актуальную версию на GitHub.

---

*Создано 2026-10-18 по итогам сессии v2.6.0 (сборка в Wine, починка трея,
скрытие консолей, чистые релизы). Владелец: flash72i.*
