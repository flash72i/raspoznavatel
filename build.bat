@echo off
REM ==========================================================================
REM  build.bat - sborka "Raspoznavatel" v odin portativnyj .EXE
REM
REM  VAZHNO: fajl soderzhit tolko ASCII-simvoly - on korrektno rabotaet
REM  v lyuboy kodirovke komandnoj stroki Windows, "ieroglifov" ne budet.
REM
REM  Trebovaniya: Windows 10/11 x64, Python 3.11 (64 bit) v PATH.
REM  Internet nuzhen TOLKO na vremya sborki.
REM  Zapusk: dvojnoj klik po build.bat
REM ==========================================================================

setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ============================================================
echo   "Raspoznavatel" - sborka portativnogo EXE
echo ============================================================
echo.

REM --- 1. Proverka Python ---------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [OSHIBKA] Python ne najden v PATH.
    echo Ustanovite Python 3.11 x64: https://www.python.org/downloads/release/python-3119/
    echo Pri ustanovke OBYAZATELNO postavte galochku "Add python.exe to PATH".
    pause
    exit /b 1
)
python --version

REM --- 2. Virtualnoe okruzhenie ----------------------------------------------
if not exist ".venv\Scripts\activate.bat" (
    echo [1/6] Sozdayu virtualnoe okruzhenie .venv ...
    python -m venv .venv
    if errorlevel 1 ( echo [OSHIBKA] Ne udalos sozdat okruzhenie & pause & exit /b 1 )
) else (
    echo [1/6] Okruzhenie .venv uzhe est - propuskayu.
)
call ".venv\Scripts\activate.bat"

REM --- 3. Zavisimosti ----------------------------------------------------------
echo [2/6] Ustanavlivayu zavisimosti iz requirements.txt ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [OSHIBKA] Ne udalos ustanovit zavisimosti. Smotrite log vyshe.
    pause
    exit /b 1
)

REM --- 4. Resursy: tessdata, model perevoda, shrifty, dvizhok Tesseract -------
echo [3/6] Gotovlyu resursy (skachivayu ODIN raz, nuzhen internet) ...
python tools\download_resources.py
if errorlevel 1 (
    echo [VNIMANIE] Ne vse resursy podgotovleny. Smotrite soobshcheniya vyshe.
)

REM --- 5. Kontrolnaya proverka resursov ----------------------------------------
echo [4/6] Proveryayu resursy pered sborkoj ...
set RES_OK=1
if not exist "resources\tessdata\rus.traineddata" ( echo   [NET] rus.traineddata & set RES_OK=0 )
if not exist "resources\tessdata\eng.traineddata" ( echo   [NET] eng.traineddata & set RES_OK=0 )
if not exist "resources\tesseract\tesseract.exe"  ( echo   [NET] Tesseract-OCR v resources\tesseract & set RES_OK=0 )
dir /b "resources\argos\*.argosmodel" >nul 2>nul
if errorlevel 1 ( echo   [NET] model perevoda Argos EN-RU & set RES_OK=0 )
if "%RES_OK%"=="0" (
    echo.
    echo [SBORKA PREKRASHCHENA] Resursy gotovy ne polnostyu.
    echo Reshenie po Tesseract: ustanovite UB-Mannheim Tesseract-OCR
    echo   https://github.com/UB-Mannheim/tesseract/wiki
    echo i zapustite build.bat povtorno - dvizhok skopiruetsya avtomaticheski.
    pause
    exit /b 1
)
echo   Vse resursy na meste.

REM --- 6. Ochistka proshlyh sborok ----------------------------------------------
echo [5/6] Ochishchayu papki build\ i dist\ ...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

REM --- 7. Sborka PyInstaller (ONEDIR: portativnaya PAPKA - bystryj zapusk!) ---
echo [6/6] Sobirayu portativnuyu papku. Zaymyot 5-20 minut, zhdite...
python -m PyInstaller build.spec --noconfirm --clean
if errorlevel 1 (
    echo [OSHIBKA] Sborka PyInstaller ne udalas. Smotrite log vyshe.
    pause
    exit /b 1
)

if not exist "dist\Raspoznavatel\Raspoznavatel.exe" (
    echo [OSHIBKA] Sborka zakonchilas, no dist\Raspoznavatel\Raspoznavatel.exe ne najden.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   GOTOVO!
echo.
echo   Portativnaya PAPKA:  dist\Raspoznavatel\
echo   Zapusk programmy:    dist\Raspoznavatel\Raspoznavatel.exe
echo.
echo   Kopirujte papku TSELIKOM kuda ugodno (naprimer na fleshku):
echo     - ustanovka NE nuzhna
echo     - prava administratora NE nuzhny
echo     - internet NE nuzhen (vse modeli vnutri)
echo     - zapusk bystryj: nichego ne raspakovivaetsya
echo.
echo   Proverka posle sborki:
echo     set RASPOZ_SELFTEST=1  i zapustit exe - otchet o komponentah
echo     set RASPOZ_E2E=1       i zapustit exe - avtoproverka interfeysa
echo ============================================================
echo.
dir "dist\Raspoznavatel\Raspoznavatel.exe"
pause
endlocal
