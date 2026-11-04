import { Tray, Menu, nativeImage } from 'electron'
import { join } from 'path'

let tray: Tray | null = null

interface TrayHandlers {
  onShow: () => void
  onCheckUpdate: () => void
  onQuit: () => void
}

function loadTrayIcon(): Electron.NativeImage {
  // 优先用打包资源里的图标，缺失时退回一个 1x1 占位，保证不崩
  const iconPath = join(__dirname, '../../resources/tray.png')
  const img = nativeImage.createFromPath(iconPath)
  if (!img.isEmpty()) return img
  return nativeImage.createFromDataURL(
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC'
  )
}

export function createTray(handlers: TrayHandlers): Tray {
  tray = new Tray(loadTrayIcon())
  tray.setToolTip('广东电力数据采集 · 运行中')

  const menu = Menu.buildFromTemplate([
    { label: '显示主窗口', click: handlers.onShow },
    { label: '检查更新', click: handlers.onCheckUpdate },
    { type: 'separator' },
    { label: '退出', click: handlers.onQuit }
  ])
  tray.setContextMenu(menu)
  tray.on('double-click', handlers.onShow)
  return tray
}

export function destroyTray(): void {
  tray?.destroy()
  tray = null
}
