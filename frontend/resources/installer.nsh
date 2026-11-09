; 安装、升级时强制盖掉旧程序。
;
; 用户数据在 %APPDATA%，安装目录里的 data、logs、tasks、workflows、
; downloads、archives、.env 也不删除。这里只结束进程并替换程序文件。
;
; 三种常见失败都从这里绕开：
; 1. 旧卸载程序删不掉文件、或卸载登记已损坏。不再运行它，避免安装被它中止。
; 2. 窗口关掉后主程序仍在托盘，或 spider-gd-service.exe 还占着文件。
; 3. 任务管理器里已经看不到界面进程，但文件仍被后端或只读属性占着。

!define SPIDER_STOP_ENCODED "JABFAHIAcgBvAHIAQQBjAHQAaQBvAG4AUAByAGUAZgBlAHIAZQBuAGMAZQAgAD0AIAAnAFMAaQBsAGUAbgB0AGwAeQBDAG8AbgB0AGkAbgB1AGUAJwAKAEcAZQB0AC0AUAByAG8AYwBlAHMAcwAgAC0ATgBhAG0AZQAgACcAcwBwAGkAZABlAHIALQBnAGQALQBzAGUAcgB2AGkAYwBlACcAIAAtAEUAcgByAG8AcgBBAGMAdABpAG8AbgAgAFMAaQBsAGUAbgB0AGwAeQBDAG8AbgB0AGkAbgB1AGUAIAB8ACAAUwB0AG8AcAAtAFAAcgBvAGMAZQBzAHMAIAAtAEYAbwByAGMAZQAgAC0ARQByAHIAbwByAEEAYwB0AGkAbwBuACAAUwBpAGwAZQBuAHQAbAB5AEMAbwBuAHQAaQBuAHUAZQAKACQAZABpAHIAIAA9ACAAJABlAG4AdgA6AFMAUABJAEQARQBSAF8AUwBUAE8AUABfAEQASQBSAAoAJABzAGUAdAB1AHAAIAA9ACAAJABlAG4AdgA6AFMAUABJAEQARQBSAF8AUwBFAFQAVQBQAF8ARQBYAEUACgBpAGYAIAAoACQAZABpAHIAKQAgAHsAIAAkAGQAaQByACAAPQAgACQAZABpAHIALgBUAHIAaQBtAEUAbgBkACgAJwBcACcAKQAgAH0ACgBHAGUAdAAtAFAAcgBvAGMAZQBzAHMAIAB8ACAARgBvAHIARQBhAGMAaAAtAE8AYgBqAGUAYwB0ACAAewAKACAAIAAkAHAAYQB0AGgAIAA9ACAAJABuAHUAbABsAAoAIAAgAHQAcgB5ACAAewAgACQAcABhAHQAaAAgAD0AIABbAHMAdAByAGkAbgBnAF0AJABfAC4AUABhAHQAaAAgAH0AIABjAGEAdABjAGgAIAB7ACAAcgBlAHQAdQByAG4AIAB9AAoAIAAgAGkAZgAgACgALQBuAG8AdAAgACQAcABhAHQAaAApACAAewAgAHIAZQB0AHUAcgBuACAAfQAKACAAIAAkAG4AYQBtAGUAIAA9ACAAWwBzAHQAcgBpAG4AZwBdACQAXwAuAFAAcgBvAGMAZQBzAHMATgBhAG0AZQAKACAAIABpAGYAIAAoACQAcwBlAHQAdQBwACAALQBhAG4AZAAgACgAJABwAGEAdABoACAALQBlAHEAIAAkAHMAZQB0AHUAcAApACkAIAB7ACAAcgBlAHQAdQByAG4AIAB9AAoAIAAgAGkAZgAgACgAJABuAGEAbQBlACAALQBpAG4AIABAACgAJwBwAG8AdwBlAHIAcwBoAGUAbABsACcALAAnAHAAdwBzAGgAJwAsACcAYwBtAGQAJwAsACcAdABhAHMAawBrAGkAbABsACcALAAnAGMAbwBuAGgAbwBzAHQAJwApACkAIAB7ACAAcgBlAHQAdQByAG4AIAB9AAoAIAAgACQAaABpAHQAIAA9ACAAJABuAGEAbQBlACAALQBlAHEAIAAnAHMAcABpAGQAZQByAC0AZwBkAC0AcwBlAHIAdgBpAGMAZQAnAAoAIAAgAGkAZgAgACgAJABkAGkAcgAgAC0AYQBuAGQAIAAkAHAAYQB0AGgALgBTAHQAYQByAHQAcwBXAGkAdABoACgAJABkAGkAcgAgACsAIAAnAFwAJwAsACAAWwBTAHkAcwB0AGUAbQAuAFMAdAByAGkAbgBnAEMAbwBtAHAAYQByAGkAcwBvAG4AXQA6ADoATwByAGQAaQBuAGEAbABJAGcAbgBvAHIAZQBDAGEAcwBlACkAKQAgAHsAIAAkAGgAaQB0ACAAPQAgACQAdAByAHUAZQAgAH0ACgAgACAAaQBmACAAKAAkAGgAaQB0ACkAIAB7ACAAUwB0AG8AcAAtAFAAcgBvAGMAZQBzAHMAIAAtAEkAZAAgACQAXwAuAEkAZAAgAC0ARgBvAHIAYwBlACAALQBFAHIAcgBvAHIAQQBjAHQAaQBvAG4AIABTAGkAbABlAG4AdABsAHkAQwBvAG4AdABpAG4AdQBlACAAfQAKAH0ACgA="

; 跳过“程序正在运行”提示。真正开始复制文件时才结束进程。
; 放在向导一开始就杀，用户还没点安装就会被关掉。
!macro customCheckAppRunning
  !insertmacro spiderForceStop
  !insertmacro spiderForgetBrokenUninstall
!macroend

; 始终装到当前用户目录，避免去提权。这台机器经常关着 UAC。
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
  StrCpy $isForceMachineInstall "0"
!macroend

!macro spiderStopByDir DIR
  !define _stopId ${__COUNTER__}
  StrCmp "${DIR}" "" stopSkip_${_stopId}
  System::Call 'Kernel32::SetEnvironmentVariable(t "SPIDER_STOP_DIR", t "${DIR}")'
  System::Call 'Kernel32::SetEnvironmentVariable(t "SPIDER_SETUP_EXE", t "$EXEPATH")'
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "${SPIDER_STOP_ENCODED}"'
  Pop $R8
  stopSkip_${_stopId}:
  !undef _stopId
!macroend

!macro spiderForceStop
  DetailPrint "正在结束残留的采集程序和后端…"
  nsExec::ExecToLog '"$SYSDIR\taskkill.exe" /F /T /IM "spider-gd-service.exe"'
  Pop $R8
  nsExec::ExecToLog '"$SYSDIR\taskkill.exe" /F /T /IM "${APP_EXECUTABLE_FILENAME}"'
  Pop $R8
  ReadRegStr $R9 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
  !insertmacro spiderStopByDir "$R9"
  ReadRegStr $R9 HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation
  !insertmacro spiderStopByDir "$R9"
  !insertmacro spiderStopByDir "$INSTDIR"
  Sleep 800
  nsExec::ExecToLog '"$SYSDIR\taskkill.exe" /F /T /IM "spider-gd-service.exe"'
  Pop $R8
  nsExec::ExecToLog '"$SYSDIR\taskkill.exe" /F /T /IM "${APP_EXECUTABLE_FILENAME}"'
  Pop $R8
  ClearErrors
!macroend

; 旧卸载程序一旦返回失败，安装器会重试五次后直接退出。
; 先摘掉卸载命令，让它找不到旧卸载程序，后面改为覆盖程序文件。
!macro spiderForgetBrokenUninstall
  SetRegView 64
  DeleteRegValue HKCU "${UNINSTALL_REGISTRY_KEY}" UninstallString
  DeleteRegValue HKCU "${UNINSTALL_REGISTRY_KEY}" QuietUninstallString
  DeleteRegValue HKLM "${UNINSTALL_REGISTRY_KEY}" UninstallString
  DeleteRegValue HKLM "${UNINSTALL_REGISTRY_KEY}" QuietUninstallString
  SetRegView 32
  DeleteRegValue HKCU "${UNINSTALL_REGISTRY_KEY}" UninstallString
  DeleteRegValue HKCU "${UNINSTALL_REGISTRY_KEY}" QuietUninstallString
  DeleteRegValue HKLM "${UNINSTALL_REGISTRY_KEY}" UninstallString
  DeleteRegValue HKLM "${UNINSTALL_REGISTRY_KEY}" QuietUninstallString
  SetRegView 64
  ClearErrors
!macroend

!macro spiderTryReplace FILE
  !define _repId ${__COUNTER__}
  IfFileExists "${FILE}" 0 repSkip_${_repId}
  nsExec::ExecToLog '"$SYSDIR\attrib.exe" -R "${FILE}"'
  Pop $R8
  ClearErrors
  Delete "${FILE}"
  IfErrors 0 repSkip_${_repId}
  ClearErrors
  Delete "${FILE}.old"
  ClearErrors
  Rename "${FILE}" "${FILE}.old"
  ClearErrors
  repSkip_${_repId}:
  !undef _repId
!macroend

!macro spiderUnlockDir DIR
  !define _unlockId ${__COUNTER__}
  StrCmp "${DIR}" "" unlockSkip_${_unlockId}
  IfFileExists "${DIR}\${APP_EXECUTABLE_FILENAME}" unlockDo_${_unlockId} 0
  IfFileExists "${DIR}\resources\app.asar" unlockDo_${_unlockId} 0
  IfFileExists "${DIR}\resources\backend\spider-gd-service.exe" unlockDo_${_unlockId} unlockSkip_${_unlockId}
  unlockDo_${_unlockId}:
  !insertmacro spiderTryReplace "${DIR}\${APP_EXECUTABLE_FILENAME}"
  !insertmacro spiderTryReplace "${DIR}\${UNINSTALL_FILENAME}"
  !insertmacro spiderTryReplace "${DIR}\resources\app.asar"
  !insertmacro spiderTryReplace "${DIR}\resources\backend\spider-gd-service.exe"
  unlockSkip_${_unlockId}:
  ClearErrors
  !undef _unlockId
!macroend

!macro customInit
  !ifndef ONE_CLICK
    StrCpy $hasPerMachineInstallation "0"
    StrCpy $hasPerUserInstallation "1"
  !endif
  !insertmacro setInstallModePerUser
!macroend

; 旧卸载失败、或根本没跑起来时，安装器原逻辑会退出。这里改成继续覆盖。
!macro customUnInstallCheck
  ${if} $R0 != 0
    DetailPrint "旧版本卸载返回 $R0，改为强制覆盖程序文件。"
  ${endif}
  ClearErrors
  !insertmacro spiderForceStop
  !insertmacro spiderUnlockDir "$INSTDIR"
  ReadRegStr $R9 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
  !insertmacro spiderUnlockDir "$R9"
  ReadRegStr $R9 HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation
  !insertmacro spiderUnlockDir "$R9"
  ClearErrors
!macroend

!macro customUnInstallCheckCurrentUser
  !insertmacro customUnInstallCheck
!macroend

; 替换默认的整目录删除。程序文件删掉，用户数据目录留下。
!macro customRemoveFiles
  !insertmacro spiderForceStop
  !insertmacro spiderUnlockDir "$INSTDIR"
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  Delete "$INSTDIR\*.dll"
  Delete "$INSTDIR\*.pak"
  Delete "$INSTDIR\*.bin"
  Delete "$INSTDIR\icudtl.dat"
  Delete "$INSTDIR\LICENSE*"
  Delete "$INSTDIR\vk_swiftshader*"
  ClearErrors
!macroend
