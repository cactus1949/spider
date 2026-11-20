; Fail closed: identity is injected by applyInstallerIdentity for BOTH NSIS builds.
!ifndef SPIDER_CHANNEL
  !error "Missing SPIDER_CHANNEL"
!endif
!ifndef SPIDER_OWN_MAIN_EXE
  !error "Missing SPIDER_OWN_MAIN_EXE"
!endif
!ifndef SPIDER_OTHER_MAIN_EXE
  !error "Missing SPIDER_OTHER_MAIN_EXE"
!endif
!ifndef SPIDER_OWN_SERVICE_EXE
  !error "Missing SPIDER_OWN_SERVICE_EXE"
!endif
!ifndef SPIDER_OTHER_SERVICE_EXE
  !error "Missing SPIDER_OTHER_SERVICE_EXE"
!endif
!ifndef SPIDER_OWN_INSTALL_KEY
  !error "Missing SPIDER_OWN_INSTALL_KEY"
!endif
!ifndef SPIDER_OTHER_INSTALL_KEY
  !error "Missing SPIDER_OTHER_INSTALL_KEY"
!endif
!ifndef SPIDER_OWN_UNINSTALL_KEY
  !error "Missing SPIDER_OWN_UNINSTALL_KEY"
!endif
!ifndef SPIDER_OTHER_UNINSTALL_KEY
  !error "Missing SPIDER_OTHER_UNINSTALL_KEY"
!endif
!ifdef DELETE_APP_DATA_ON_UNINSTALL
  !error "Installer isolation must preserve user data"
!endif
!ifdef UNINSTALL_REGISTRY_KEY_2
  !error "Unverified legacy uninstall key is not supported"
!endif

Var spiderGuardReady

!macro spiderFail MESSAGE
  DetailPrint "${MESSAGE}"
  SetErrorLevel 2
  Quit
!macroend

!macro spiderSetEnv NAME VALUE
  !define _envId ${__COUNTER__}
  ; System::Call 的输出寄存器写法和 NSIS 变量名都区分大小写：
  ;   .r8 → $8（小写变量），.R8 → $R8。写错时调用不报错但返回值为空，
  ;   安装器会静默退出（exit 2）；写成 .s 从栈接收在旧版 System.dll 上还会崩溃。
  System::Call 'Kernel32::SetEnvironmentVariable(t, t) i ("${NAME}", "${VALUE}").R8'
  StrCmp $R8 "1" envOk_${_envId}
  !insertmacro spiderFail "Cannot set installer guard environment: ${NAME}"
  envOk_${_envId}:
  !undef _envId
!macroend

!macro spiderRunGuard ACTION TARGET
  !define _guardId ${__COUNTER__}
  ; Silent uninstall reaches customCheckAppRunning before customUnInit.
  StrCmp $spiderGuardReady "1" guardReady_${_guardId}
  InitPluginsDir
  ; Framework registry probes can leave Errors set when a key is absent.
  ; Reset only BEFORE measuring this new operation, never after a failure.
  ClearErrors
  File /oname=$PLUGINSDIR\installer-guard.ps1 "${BUILD_RESOURCES_DIR}\installer-guard.ps1"
  IfErrors 0 guardExtracted_${_guardId}
  !insertmacro spiderFail "Cannot extract installer isolation guard"
  guardExtracted_${_guardId}:
  StrCpy $spiderGuardReady "1"
  guardReady_${_guardId}:
  !insertmacro spiderSetEnv "SPIDER_CHANNEL" "${SPIDER_CHANNEL}"
  !insertmacro spiderSetEnv "SPIDER_OWN_MAIN_EXE" "${SPIDER_OWN_MAIN_EXE}"
  !insertmacro spiderSetEnv "SPIDER_OTHER_MAIN_EXE" "${SPIDER_OTHER_MAIN_EXE}"
  !insertmacro spiderSetEnv "SPIDER_OWN_SERVICE_EXE" "${SPIDER_OWN_SERVICE_EXE}"
  !insertmacro spiderSetEnv "SPIDER_OTHER_SERVICE_EXE" "${SPIDER_OTHER_SERVICE_EXE}"
  !insertmacro spiderSetEnv "SPIDER_OWN_INSTALL_KEY" "${SPIDER_OWN_INSTALL_KEY}"
  !insertmacro spiderSetEnv "SPIDER_OTHER_INSTALL_KEY" "${SPIDER_OTHER_INSTALL_KEY}"
  !insertmacro spiderSetEnv "SPIDER_OWN_UNINSTALL_KEY" "${SPIDER_OWN_UNINSTALL_KEY}"
  !insertmacro spiderSetEnv "SPIDER_OTHER_UNINSTALL_KEY" "${SPIDER_OTHER_UNINSTALL_KEY}"
  !insertmacro spiderSetEnv "SPIDER_UNINSTALL_FILENAME" "${UNINSTALL_FILENAME}"
  !insertmacro spiderSetEnv "SPIDER_INSTALL_DIR" "$INSTDIR"
  !insertmacro spiderSetEnv "SPIDER_GUARD_STATE" "$PLUGINSDIR\installer-guard-state.json"
  ; 不直接把 $CMDLINE 传进 System::Call：真实命令行里带引号、中文路径时会
  ; 破坏内联参数的字符串解析，变量设不上（guard 报 Missing guard arguments），
  ; 安装器静默退出 2。--delete-app-data 已在编译期用 !error 拦死，运行时标志
  ; 只剩纵深防御，可用固定串代替。
  !insertmacro spiderSetEnv "SPIDER_GUARD_ARGUMENTS" "installer"
  !insertmacro spiderSetEnv "SPIDER_GUARD_ACTION" "${ACTION}"
  !insertmacro spiderSetEnv "SPIDER_CLEANUP_TARGET" "${TARGET}"
  ; Dynamic paths/identity never become PowerShell source or command-line arguments.
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\installer-guard.ps1"'
  Pop $R8
  StrCmp $R8 "0" guardOk_${_guardId}
  !insertmacro spiderFail "Installer isolation guard ${ACTION} failed ($R8); nothing further will run"
  guardOk_${_guardId}:
  !undef _guardId
!macroend

!macro customHeader
  ; Defaults from common.nsh/multiUser.nsh now exist; validate both framework builds.
  !if "${APP_EXECUTABLE_FILENAME}" != "${SPIDER_OWN_MAIN_EXE}"
    !error "Framework executable identity mismatch"
  !endif
  !if "${INSTALL_REGISTRY_KEY}" != "${SPIDER_OWN_INSTALL_KEY}"
    !error "Framework install registry identity mismatch"
  !endif
  !if "${UNINSTALL_REGISTRY_KEY}" != "${SPIDER_OWN_UNINSTALL_KEY}"
    !error "Framework uninstall registry identity mismatch"
  !endif
  !ifndef BUILD_UNINSTALLER
    ; customHeader precedes the framework's default section, even in assisted inner instances.
    Section "-spiderIsolation"
      SectionIn RO
      !insertmacro spiderRunGuard "Check" ""
      !insertmacro spiderRunGuard "Stop" ""
      !insertmacro spiderRunGuard "Forget" ""
      !insertmacro spiderRemoveProgramFiles
    SectionEnd
  !endif
!macroend

; Read-only validation here; never kill or forget while the user is still choosing.
!macro customCheckAppRunning
  !insertmacro spiderRunGuard "Check" ""
!macroend

; Preserve the existing current-user install policy without changing permissions.
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
  StrCpy $isForceMachineInstall "0"
!macroend

!macro customInit
  !ifndef ONE_CLICK
    StrCpy $hasPerMachineInstallation "0"
    StrCpy $hasPerUserInstallation "1"
  !endif
  !insertmacro setInstallModePerUser
!macroend

!macro customUnInit
  !insertmacro spiderRunGuard "Check" ""
!macroend

; Forget must make legacy uninstall unreachable. Never suppress a framework failure.
!macro customUnInstallCheck
  !define _uncheckId ${__COUNTER__}
  ; 安装流程会先 Check/Stop/Forget 再清程序文件：旧卸载器可能已被 Forget 摘掉
  ; UninstallString（框架 copy 失败会置错误标志），因此这里不能把“继承的错误标志”
  ; 当成卸载失败——只看真实卸载退出码 $R0。升级 beta.1 -> beta.2 时曾因此 exit=2。
  ClearErrors
  StrCmp $R0 "0" uncheckOk_${_uncheckId}
  !insertmacro spiderFail "Unexpected legacy uninstall failure; refusing to overwrite"
  uncheckOk_${_uncheckId}:
  !undef _uncheckId
!macroend

!macro customUnInstallCheckCurrentUser
  !insertmacro customUnInstallCheck
!macroend

!macro spiderRemoveTree RELATIVE
  !define _treeId ${__COUNTER__}
  !insertmacro spiderRunGuard "CheckCleanup" "${RELATIVE}"
  IfFileExists "$INSTDIR\${RELATIVE}\*.*" 0 treeOk_${_treeId}
  ClearErrors
  RMDir /r "$INSTDIR\${RELATIVE}"
  IfErrors 0 treeOk_${_treeId}
  !insertmacro spiderFail "Cannot remove checked program directory; refusing to continue"
  treeOk_${_treeId}:
  !undef _treeId
!macroend

!macro spiderRemoveFile RELATIVE
  !define _fileId ${__COUNTER__}
  !insertmacro spiderRunGuard "CheckCleanup" "${RELATIVE}"
  IfFileExists "$INSTDIR\${RELATIVE}" 0 fileOk_${_fileId}
  ClearErrors
  Delete "$INSTDIR\${RELATIVE}"
  IfErrors 0 fileOk_${_fileId}
  !insertmacro spiderFail "Cannot remove checked program file; refusing to continue"
  fileOk_${_fileId}:
  !undef _fileId
!macroend

; Only program files. Never remove the root, userData, data/logs/tasks/workflows/
; downloads/archives/.env. No attrib, rename fallback, reboot deletion or permission repair.
!macro spiderRemoveProgramFiles
  !insertmacro spiderRemoveTree "resources"
  !insertmacro spiderRemoveTree "locales"
  !insertmacro spiderRemoveFile "${SPIDER_OWN_MAIN_EXE}"
  !insertmacro spiderRemoveFile "${UNINSTALL_FILENAME}"
  !insertmacro spiderRemoveFile "*.dll"
  !insertmacro spiderRemoveFile "*.pak"
  !insertmacro spiderRemoveFile "*.bin"
  !insertmacro spiderRemoveFile "icudtl.dat"
  !insertmacro spiderRemoveFile "LICENSE*"
  !insertmacro spiderRemoveFile "vk_swiftshader*"
!macroend

!macro customRemoveFiles
  !insertmacro spiderRunGuard "Check" ""
  !insertmacro spiderRunGuard "Stop" ""
  !insertmacro spiderRemoveProgramFiles
!macroend
