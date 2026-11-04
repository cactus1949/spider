; NSIS 自定义脚本：跳过 electron-builder 的进程检测 + 强制杀残留

; 用 customCheckAppRunning 空宏跳过 "应用正在运行" 检测
; electron-builder 检测到此宏存在时会直接跳过 CHECK_APP_RUNNING
!macro customCheckAppRunning
  ; 什么都不做，跳过
!macroend

!macro customInit
  ; 先杀残留
  nsExec::Exec 'taskkill /F /IM spider-gd-service.exe /T'
  nsExec::Exec 'taskkill /F /IM 广东电力数据采集.exe /T'
  Sleep 1000
!macroend

!macro customRemoveFiles
  nsExec::Exec 'taskkill /F /IM spider-gd-service.exe /T'
  nsExec::Exec 'taskkill /F /IM 广东电力数据采集.exe /T'
  Sleep 1000
!macroend

; 强制当前用户级安装，跳过“为所有用户安装/仅为我安装”选择页：
; 后端运行时把 data/、logs/、configs/ 写在安装目录下，装进 Program Files 后
; 非提权启动会写不进去；而且该机器常关闭 UAC，提权分支会报 “Unable to elevate, error 6”。
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
!macroend
