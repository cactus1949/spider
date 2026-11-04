/**
 * 自动更新：基于 electron-updater，支持 generic (HTTP) provider。
 *
 * 发布流程：
 *   1. pnpm build:win → dist/ 下生成 *.exe + latest.yml
 *   2. 将 exe + latest.yml 上传到更新服务器
 *   3. electron-builder.yml 中 publish.url 指向该服务器
 *
 * 更新事件通过 IPC 通知渲染进程：
 *   - updater:available  → { version }
 *   - updater:progress   → { percent }
 *   - updater:completed  → {}
 *   - updater:error      → { message }
 */

import { autoUpdater } from 'electron-updater'
import { BrowserWindow, ipcMain } from 'electron'

const UPDATE_FEED_URL = 'http://47.101.175.39:20152/releases'

export function startUpdater(getWindow: () => BrowserWindow | null): void {
  // 配置 autoUpdater
  autoUpdater.setFeedURL({ provider: 'generic', url: UPDATE_FEED_URL })
  autoUpdater.autoDownload = false        // 不自动下载，由用户触发
  autoUpdater.autoInstallOnAppQuit = false

  // ── 检查更新（自动定时检查，或用户手动触发） ──
  autoUpdater.on('checking-for-update', () => {
    console.log('[updater] 检查更新...')
  })

  autoUpdater.on('update-available', (info) => {
    console.log('[updater] 发现新版本:', info.version)
    getWindow()?.webContents.send('updater:available', {
      version: info.version,
      notes: (info as any).releaseNotes || '',
    })
  })

  autoUpdater.on('update-not-available', () => {
    console.log('[updater] 已是最新版本')
    getWindow()?.webContents.send('updater:none')
  })

  // ── 下载进度（由 IPC 触发） ──
  autoUpdater.on('download-progress', (progress) => {
    getWindow()?.webContents.send('updater:progress', {
      percent: Math.round(progress.percent),
    })
  })

  autoUpdater.on('update-downloaded', () => {
    console.log('[updater] 下载完成')
    getWindow()?.webContents.send('updater:completed')
  })

  autoUpdater.on('error', (err) => {
    console.error('[updater] 错误:', err.message)
    // 仅在用户手动检查时才通知错误
  })

  // ── 首次检查（5s 延迟） ──
  setTimeout(() => {
    autoUpdater.checkForUpdates().catch((err) => {
      console.log('[updater] 首次检查失败:', err.message)
    })
  }, 5000)

  // ── 定时检查（每 4 小时） ──
  setInterval(() => {
    autoUpdater.checkForUpdates().catch(() => {})
  }, 4 * 60 * 60 * 1000)

  // ── IPC：用户手动检查 ──
  ipcMain.handle('updater:check', async () => {
    try {
      const result = await autoUpdater.checkForUpdates()
      console.log('[updater] 检查结果:', JSON.stringify(result?.updateInfo))
      return { hasUpdate: !!result?.updateInfo, version: result?.updateInfo?.version }
    } catch (err: any) {
      console.error('[updater] 检查失败:', err.message, err.stack)
      return { error: err.message }
    }
  })

  // ── IPC：用户触发下载 ──
  ipcMain.handle('updater:download', async () => {
    try {
      await autoUpdater.downloadUpdate()
      return { ok: true }
    } catch (err: any) {
      return { ok: false, error: err.message }
    }
  })

  // ── IPC：用户触发安装 ──
  ipcMain.handle('updater:install', async () => {
    try {
      autoUpdater.quitAndInstall(false, true)
      return { ok: true }
    } catch (err: any) {
      return { ok: false, error: err.message }
    }
  })
}

export function manualCheck(getWindow: () => BrowserWindow | null): void {
  autoUpdater.checkForUpdates().catch((err) => {
    console.error('[updater] 手动检查失败:', err.message)
    getWindow()?.webContents.send('updater:error', { message: err.message })
  })
}
