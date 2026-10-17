; installer.nsi — установщик «Распознаватель» (NSIS 3, Unicode)
; Сборка:  makensis installer.nsi   (или build_installer.sh)
; Результат: dist/Raspoznavatel-2.4.0-Setup.exe
Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"

Name "Распознаватель"
OutFile "dist\Raspoznavatel-2.4.0-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Raspoznavatel"
InstallDirRegKey HKCU "Software\Raspoznavatel" "InstallDir"
RequestExecutionLevel user          ; БЕЗ прав администратора
SetCompressor /SOLID lzma

; --- метаданные установщика ---
VIProductVersion "2.4.0.0"
VIAddVersionKey "ProductName"     "Распознаватель"
VIAddVersionKey "FileDescription" "Установщик «Распознаватель» 2.4.0"
VIAddVersionKey "FileVersion"     "2.4.0"
VIAddVersionKey "ProductVersion"  "2.4.0"
VIAddVersionKey "LegalCopyright"  "Open source (flash72i)"
VIAddVersionKey "Comments"        "QR, штрих-коды, OCR, скриншоты, офлайн-перевод EN->RU"

; --- оформление ---
!define MUI_ABORTWARNING
!define MUI_ICON   "ui\assets\icon.ico"
!define MUI_UNICON "ui\assets\icon.ico"
!define MUI_WELCOMEPAGE_TITLE "Установка программы «Распознаватель»"
!define MUI_WELCOMEPAGE_TEXT "QR-коды и штрих-коды, текст с картинок и с экрана, офлайн-перевод с английского на русский — всё без интернета, телеметрии и прав администратора.$\r$\n$\r$\nПрограмма будет установлена для текущего пользователя в:$\r$\n$INSTDIR$\r$\n$\r$\nНажмите «Далее», чтобы продолжить."
!define MUI_FINISHPAGE_RUN "$INSTDIR\Raspoznavatel.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить «Распознаватель»"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"

; ============================ УСТАНОВКА ============================
Section "Программа (обязательно)" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "dist\Raspoznavatel\*.*"

  ; ярлыки в меню «Пуск»
  CreateDirectory "$SMPROGRAMS\Распознаватель"
  CreateShortcut "$SMPROGRAMS\Распознаватель\Распознаватель.lnk" "$INSTDIR\Raspoznavatel.exe"
  CreateShortcut "$SMPROGRAMS\Распознаватель\Удаление.lnk" "$INSTDIR\Uninstall.exe"

  ; запись для «Установка и удаление программ» (только HKCU — без админ-прав)
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "DisplayName"     "Распознаватель 2.4.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "DisplayVersion"  "2.4.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "DisplayIcon"     "$INSTDIR\Raspoznavatel.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "Publisher"       "flash72i"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel" "NoRepair" 1
  WriteRegStr HKCU "Software\Raspoznavatel" "InstallDir" "$INSTDIR"
SectionEnd

Section "Ярлык на рабочем столе" SecDesktop
  CreateShortcut "$DESKTOP\Распознаватель.lnk" "$INSTDIR\Raspoznavatel.exe"
SectionEnd

Section /o "Запускать при включении Windows (свёрнутой в трей)" SecAuto
  CreateShortcut "$SMSTARTUP\Распознаватель.lnk" "$INSTDIR\Raspoznavatel.exe" "--minimized"
SectionEnd

; описания компонентов
LangString DESC_MAIN ${LANG_RUSSIAN} "Все файлы программы: интерфейс, движок Tesseract, языковые модели (14 языков), модель офлайн-перевода EN->RU."
LangString DESC_DESK ${LANG_RUSSIAN} "Создать ярлык «Распознаватель» на рабочем столе."
LangString DESC_AUTO ${LANG_RUSSIAN} "Программа будет запускаться вместе с Windows и работать свёрнутой в системном трее; горячие клавиши действуют сразу."
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}   $(DESC_MAIN)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_DESK)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAuto}    $(DESC_AUTO)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ============================ УДАЛЕНИЕ ============================
Section Uninstall
  Delete "$DESKTOP\Распознаватель.lnk"
  Delete "$SMSTARTUP\Распознаватель.lnk"
  RMDir /r "$SMPROGRAMS\Распознаватель"

  ; спросить про пользовательские данные (скриншоты, журнал, настройки)
  MessageBox MB_YESNO|MB_ICONQUESTION "Удалить также пользовательские данные?$\r$\n(скриншоты, журнал, настройки в папке Raspoznavatel_data и Screenshots)" IDNO +2
  RMDir /r "$INSTDIR\Raspoznavatel_data"
  RMDir /r "$INSTDIR\Screenshots"

  RMDir /r "$INSTDIR\App"
  Delete "$INSTDIR\Raspoznavatel.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Raspoznavatel"
  DeleteRegKey HKCU "Software\Raspoznavatel"
SectionEnd

Function .onInit
  ${If} ${Silent}
    !insertmacro UnselectSection ${SecDesktop}
    !insertmacro UnselectSection ${SecAuto}
  ${EndIf}
FunctionEnd
