Unicode True
RequestExecutionLevel user
ManifestDPIAware True
SetCompressor /SOLID lzma
SetCompressorDictSize 32
SetDatablockOptimize on

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!define APP_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\VTGUI"
Name "VTGUI ${APP_VERSION}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\VTGUI"
InstallDirRegKey HKCU "${APP_KEY}" "InstallLocation"
BrandingText "VTGUI · DRMLAB / unshackle 独立任务工作台"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "VTGUI 安装程序"
VIAddVersionKey /LANG=2052 "FileDescription" "VTGUI 离线安装包 (Windows x64)"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "VTGUI"
ShowInstDetails show
ShowUninstDetails show

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "欢迎安装 VTGUI ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "DRMLAB / unshackle 独立任务工作台。$\r$\n$\r$\n本安装包包含 GUI 及运行库，无需安装 Python。$\r$\n$\r$\n请先准备好 DRMLAB / unshackle 及其依赖，安装后在 GUI 中选择 DRMLAB / unshackle 所在目录。$\r$\n$\r$\n升级前请先正常退出 VTGUI。配置、任务和日志会保留。"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\VTGUI.exe"
!define MUI_FINISHPAGE_RUN_TEXT "启动 VTGUI"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\使用说明.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "查看使用说明"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!insertmacro MUI_PAGE_FINISH

!define MUI_UNCONFIRMPAGE_TEXT_TOP "卸载将删除程序及快捷方式。$\r$\n$\r$\n配置、定时任务、日志以及 DRMLAB / unshackle 和下载文件会保留。"
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"

Var ExistingDir

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "VTGUI 需要 64 位 Windows。" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP|MB_OK "VTGUI 需要 Windows 10 或更新版本。" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  SetRegView 64
  ReadRegStr $ExistingDir HKCU "${APP_KEY}" "InstallLocation"
  ${If} $ExistingDir != ""
    StrCpy $INSTDIR $ExistingDir
  ${EndIf}
FunctionEnd

; Never replace an unrelated non-empty folder. The marker identifies our own
; installation; user-added files survive upgrades and uninstall.
Function ValidateDirectory
  ${If} $ExistingDir != ""
    ${If} $INSTDIR != $ExistingDir
      MessageBox MB_ICONSTOP|MB_OK "已安装 VTGUI。升级请使用原安装目录；更换位置请先卸载旧版。" /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
  ${EndIf}
  ReadINIStr $0 "$INSTDIR\.vtgui-install.ini" "VTGUI" "Product"
  ${If} $0 == "VTGUI"
    Return
  ${EndIf}
  FindFirst $0 $1 "$INSTDIR\*.*"
  ${DoWhile} $1 != ""
    ${If} $1 != "."
    ${AndIf} $1 != ".."
      FindClose $0
      MessageBox MB_ICONSTOP|MB_OK "请选择一个空文件夹作为 VTGUI 安装目录。请勿选择 DRMLAB / unshackle 目录、数据目录或已有文件的其他目录。" /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
    FindNext $0 $1
  ${Loop}
  FindClose $0
FunctionEnd

Function .onVerifyInstDir
  ; Validation without a message box while the user types the directory.
  ${If} $INSTDIR == ""
    Abort
  ${EndIf}
FunctionEnd

!macro CheckAppClosed PREFIX
Function ${PREFIX}CheckAppClosed
  IfFileExists "$INSTDIR\VTGUI.exe" 0 done
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\VTGUI.exe", i 0xC0000000, i 0, p 0, i 3, i 0, p 0) p .r0'
  ${If} $0 == -1
    MessageBox MB_ICONSTOP|MB_OK "请先正常退出正在运行的 VTGUI，再进行安装或卸载。" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p r0)'
  done:
FunctionEnd
!macroend
!insertmacro CheckAppClosed ""
!insertmacro CheckAppClosed "un."

Section "VTGUI 程序及开始菜单入口（必选）" MainSection
  SectionIn RO
  Call ValidateDirectory
  Call CheckAppClosed
  SetOutPath "$INSTDIR"
  SetOverwrite on
  File /r "${PAYLOAD_DIR}\*.*"
  WriteINIStr "$INSTDIR\.vtgui-install.ini" "VTGUI" "Product" "VTGUI"
  WriteINIStr "$INSTDIR\.vtgui-install.ini" "VTGUI" "Version" "${APP_VERSION}"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\VTGUI"
  CreateShortcut "$SMPROGRAMS\VTGUI\VTGUI.lnk" "$INSTDIR\VTGUI.exe"
  CreateShortcut "$SMPROGRAMS\VTGUI\使用说明.lnk" "$INSTDIR\使用说明.txt"
  CreateShortcut "$SMPROGRAMS\VTGUI\卸载 VTGUI.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${APP_KEY}" "DisplayName" "VTGUI"
  WriteRegStr HKCU "${APP_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${APP_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APP_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "${APP_KEY}" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegStr HKCU "${APP_KEY}" "DisplayIcon" "$INSTDIR\VTGUI.exe,0"
  WriteRegDWORD HKCU "${APP_KEY}" "EstimatedSize" ${ESTIMATED_SIZE}
  WriteRegDWORD HKCU "${APP_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${APP_KEY}" "NoRepair" 1
SectionEnd

Section /o "创建桌面快捷方式" DesktopSection
  CreateShortcut "$DESKTOP\VTGUI.lnk" "$INSTDIR\VTGUI.exe"
  WriteRegDWORD HKCU "${APP_KEY}" "DesktopShortcut" 1
SectionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 64
  ReadINIStr $0 "$INSTDIR\.vtgui-install.ini" "VTGUI" "Product"
  ${If} $0 != "VTGUI"
    MessageBox MB_ICONSTOP|MB_OK "无法确认 VTGUI 安装目录，已停止卸载。" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.CheckAppClosed
  SetOutPath "$TEMP"
  !include "${UNINSTALL_MANIFEST}"
  Delete "$INSTDIR\.vtgui-install.ini"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\VTGUI\VTGUI.lnk"
  Delete "$SMPROGRAMS\VTGUI\使用说明.lnk"
  Delete "$SMPROGRAMS\VTGUI\卸载 VTGUI.lnk"
  RMDir "$SMPROGRAMS\VTGUI"
  ReadRegDWORD $0 HKCU "${APP_KEY}" "DesktopShortcut"
  ${If} $0 == 1
    Delete "$DESKTOP\VTGUI.lnk"
  ${EndIf}
  DeleteRegKey HKCU "${APP_KEY}"
SectionEnd
