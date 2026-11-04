import { contextBridge, ipcRenderer } from 'electron'

const api = {
  getVersion: (): Promise<string> => ipcRenderer.invoke('app:get-version'),
  getDataDir: (): Promise<string> => ipcRenderer.invoke('app:get-data-dir'),

  // 窗口
  raiseMainWindow: (): Promise<boolean> => ipcRenderer.invoke('window:raise'),
  minimize: (): void => ipcRenderer.send('window:minimize'),
  hideToTray: (): void => ipcRenderer.send('window:hide'),

  // 本地文件
  listDir: (dir: string) => ipcRenderer.invoke('files:list-dir', dir),
  fileVersionInfo: (filePath: string) => ipcRenderer.invoke('files:version-info', filePath),
  createFolder: (dir: string, name: string) =>
    ipcRenderer.invoke('files:create-folder', dir, name),
  rename: (oldPath: string, newName: string) =>
    ipcRenderer.invoke('files:rename', oldPath, newName),
  deletePath: (targetPath: string) => ipcRenderer.invoke('files:delete', targetPath),
  openFile: (filePath: string) => ipcRenderer.invoke('files:open', filePath),
  revealFile: (filePath: string) => ipcRenderer.invoke('files:reveal', filePath),
  openDir: (dir: string) => ipcRenderer.invoke('files:open-dir', dir),
  pickDir: (): Promise<string | null> => ipcRenderer.invoke('dialog:pick-dir'),
  pickFile: (filters?: { name: string; extensions: string[] }[]) =>
    ipcRenderer.invoke('dialog:pick-file', filters) as Promise<{ path: string; content: string; error?: string } | null>,
  pickFiles: (filters?: { name: string; extensions: string[] }[]) =>
    ipcRenderer.invoke('dialog:pick-files', filters) as Promise<{ path: string; content: string; error?: string }[] | null>,
  pickSaveFile: (defaultName: string): Promise<string | null> =>
    ipcRenderer.invoke('dialog:pick-save', defaultName),
  batchDelete: (paths: string[]) =>
    ipcRenderer.invoke('files:batch-delete', paths),
  compressFiles: (paths: string[], targetZip: string) =>
    ipcRenderer.invoke('files:compress', { paths, targetZip }),

  // 更新 (electron-updater)
  checkUpdate: () => ipcRenderer.invoke('updater:check'),
  downloadUpdate: () => ipcRenderer.invoke('updater:download'),
  installUpdate: () => ipcRenderer.invoke('updater:install'),
  onUpdateAvailable: (cb: (info: { version: string; notes: string }) => void) => {
    const listener = (_e: unknown, info: { version: string; notes: string }) => cb(info)
    ipcRenderer.on('updater:available', listener)
    return () => ipcRenderer.removeListener('updater:available', listener)
  },
  onUpdateNone: (cb: () => void) => {
    const listener = (): void => cb()
    ipcRenderer.on('updater:none', listener)
    return () => ipcRenderer.removeListener('updater:none', listener)
  },
  onDownloadProgress: (cb: (info: { percent: number }) => void) => {
    const listener = (_e: unknown, info: { percent: number }) => cb(info)
    ipcRenderer.on('updater:progress', listener)
    return () => ipcRenderer.removeListener('updater:progress', listener)
  },
  onDownloadComplete: (cb: () => void) => {
    const listener = (): void => cb()
    ipcRenderer.on('updater:completed', listener)
    return () => ipcRenderer.removeListener('updater:completed', listener)
  },
  onUpdateError: (cb: (info: { message: string }) => void) => {
    const listener = (_e: unknown, info: { message: string }) => cb(info)
    ipcRenderer.on('updater:error', listener)
    return () => ipcRenderer.removeListener('updater:error', listener)
  },

  // 后端状态
  onBackendStatus: (cb: (info: { status: string }) => void) => {
    const listener = (_e: unknown, info: { status: string }) => cb(info)
    ipcRenderer.on('backend:status', listener)
    return () => ipcRenderer.removeListener('backend:status', listener)
  }
}

contextBridge.exposeInMainWorld('api', api)

export type DesktopApi = typeof api
