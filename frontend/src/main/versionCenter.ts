import { ipcMain, net, shell } from 'electron'
import { releaseIdentity } from './release'
import { historicalInstallerUrl, readVersionCatalog, type VersionCatalog } from './versionCatalog.mjs'

export function registerVersionCenter(): void {
  let catalog: VersionCatalog | null = null
  let requestId = 0

  ipcMain.handle('versions:list', async () => {
    const id = ++requestId
    catalog = null
    const loaded = await readVersionCatalog(releaseIdentity, (url, options) => net.fetch(url, options))
    if (id === requestId) catalog = loaded
    return structuredClone(loaded)
  })

  ipcMain.handle('versions:download', async (_event, version: unknown) => {
    if (!catalog) throw new Error('请先读取或刷新版本记录')
    if (typeof version !== 'string' || !catalog.releases.some(item => item.version === version)) {
      throw new Error('该版本不在当前渠道的版本记录中，请刷新后重试')
    }
    await shell.openExternal(historicalInstallerUrl(releaseIdentity, version))
    return { ok: true }
  })
}
