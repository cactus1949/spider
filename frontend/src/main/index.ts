import { app, shell, BrowserWindow, ipcMain } from 'electron'
import path, { join } from 'path'
import { createTray, destroyTray } from './tray'
import { registerFileIpc } from './files'
import { startUpdater, manualCheck } from './updater'
import { startBackend, stopBackend } from './backend'

let mainWindow: BrowserWindow | null = null
let isQuitting = false

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1180,
    height: 760,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    autoHideMenuBar: true,
    title: '广东电力数据采集',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  win.on('ready-to-show', () => win.show())

  // 关闭按钮 → 隐藏到托盘，不退出
  win.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault()
      win.hide()
    }
  })

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  if (process.env['ELECTRON_RENDERER_URL']) {
    win.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }

  return win
}

function showMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  // 采集时 Playwright 会把 Chrome 带到前台。直接 show/focus 在 macOS 和 Windows 上
  // 经常只闪一下任务栏，窗口仍留在 Chrome 后面。先短暂置顶再取消，把前台抢回来。
  mainWindow.setAlwaysOnTop(true)
  mainWindow.show()
  mainWindow.moveTop()
  if (process.platform === 'darwin') app.focus({ steal: true })
  mainWindow.focus()
  setTimeout(() => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setAlwaysOnTop(false)
  }, 200)
}

// 单实例锁：二次启动聚焦已有窗口
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => showMainWindow())

  app.whenReady().then(() => {
    if (process.platform === 'win32') {
      // Windows 桌面通知需要稳定的 AppUserModelId 才能正常归属到本应用。
      app.setAppUserModelId('com.spider-gd.desktop')
    }
    mainWindow = createWindow()
    registerFileIpc()

    // 启动采集后端服务
    startBackend(mainWindow)

    createTray({
      onShow: showMainWindow,
      onCheckUpdate: () => {
        showMainWindow()
        manualCheck(() => mainWindow)
      },
      onQuit: () => {
        isQuitting = true
        app.quit()
      }
    })

    ipcMain.handle('app:get-version', () => app.getVersion())
    ipcMain.handle('app:get-data-dir', () => {
      // 开发模式：app.getAppPath() 指向 frontend/ 根
      // 打包模式：app.getAppPath() 指向 app.asar 内
      const isDev = !app.isPackaged
      if (isDev) {
        return path.resolve(app.getAppPath(), '../data')
      }
      return path.resolve(process.resourcesPath, '../../data')
    })
    ipcMain.handle('window:raise', () => {
      showMainWindow()
      return true
    })
    ipcMain.on('window:minimize', () => mainWindow?.minimize())
    ipcMain.on('window:hide', () => mainWindow?.hide())

    startUpdater(() => mainWindow)

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        mainWindow = createWindow()
      } else {
        showMainWindow()
      }
    })
  })

  app.on('before-quit', () => {
    isQuitting = true
    stopBackend()
    destroyTray()
  })

  // 托盘常驻：所有窗口关闭也不退出（macOS 默认如此，Windows 需显式保留）
  app.on('window-all-closed', () => {
    // no-op：保持后台运行，由托盘菜单退出
  })
}
