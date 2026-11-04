import { ipcMain, shell, dialog } from 'electron'
import { promises as fs, createWriteStream, createReadStream, statSync } from 'fs'
import { createHash } from 'crypto'
import { join, extname, basename, dirname } from 'path'
// @ts-ignore: @types/archiver 缺少 default export 声明
import archiver from 'archiver'

export interface DirEntry {
  name: string
  path: string
  isDir: boolean
  type: string
  ext: string
  size: number
  mtime: number
}

export interface FileVersionInfo {
  path: string
  name: string
  sha256: string
  manifestSha256?: string
  updatedAt?: string
  versions: Array<{
    path: string
    name: string
    sha256: string
    archivedAt?: string
    size: number
    mtime: number
  }>
}

const TYPE_BY_KEYWORD: Array<[string, string]> = [
  ['日前', '日前市场'],
  ['实时', '实时电价'],
  ['月度', '月度结算'],
  ['中长期', '中长期'],
  ['合同', '合同电量'],
  ['现货', '现货结算'],
  ['辅助', '辅助服务']
]

function inferType(name: string): string {
  for (const [kw, label] of TYPE_BY_KEYWORD) {
    if (name.includes(kw)) return label
  }
  return '其他'
}

function sha256File(filePath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256')
    const stream = createReadStream(filePath)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.on('error', reject)
    stream.on('end', () => resolve(hash.digest('hex')))
  })
}

async function versionInfo(filePath: string): Promise<FileVersionInfo> {
  const name = basename(filePath)
  const dir = dirname(filePath)
  const ext = extname(name)
  const stem = ext ? name.slice(0, -ext.length) : name
  const sha256 = await sha256File(filePath)
  let manifestItem: any = null
  try {
    const manifest = JSON.parse(await fs.readFile(join(dir, '.file_versions.json'), 'utf-8'))
    manifestItem = manifest?.files?.[name] || null
  } catch {
    manifestItem = null
  }

  const versionDir = join(dir, '_versions')
  const versionEntries: FileVersionInfo['versions'] = []
  const manifestVersions = Array.isArray(manifestItem?.versions) ? manifestItem.versions : []
  try {
    const names = await fs.readdir(versionDir)
    for (const itemName of names) {
      if (!itemName.startsWith(`${stem}_`) || (ext && !itemName.endsWith(ext))) continue
      const full = join(versionDir, itemName)
      try {
        const st = await fs.stat(full)
        if (!st.isFile()) continue
        const relPath = `_versions/${itemName}`
        const record = manifestVersions.find((v: any) => v?.path === relPath)
        versionEntries.push({
          path: full,
          name: itemName,
          sha256: String(record?.sha256 || await sha256File(full)),
          archivedAt: String(record?.archived_at || ''),
          size: st.size,
          mtime: st.mtimeMs
        })
      } catch {
        // Ignore stale version entries.
      }
    }
  } catch {
    // No versions yet.
  }
  versionEntries.sort((a, b) => b.mtime - a.mtime)
  return {
    path: filePath,
    name,
    sha256,
    manifestSha256: manifestItem?.current_sha256,
    updatedAt: manifestItem?.updated_at,
    versions: versionEntries
  }
}

// 只列出当前目录下一级的文件夹与文件
async function listLevel(dir: string): Promise<DirEntry[]> {
  const out: DirEntry[] = []
  let entries
  try {
    entries = await fs.readdir(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue
    const full = join(dir, entry.name)
    let size = 0
    let mtime = 0
    try {
      const st = await fs.stat(full)
      size = st.size
      mtime = st.mtimeMs
    } catch {
      continue
    }
    const ext = entry.isDirectory() ? '' : extname(entry.name).toLowerCase().replace('.', '')
    out.push({
      name: entry.name,
      path: full,
      isDir: entry.isDirectory(),
      type: entry.isDirectory() ? '文件夹' : inferType(entry.name),
      ext,
      size,
      mtime
    })
  }
  // 文件夹在前，再按修改时间倒序
  return out.sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
    return b.mtime - a.mtime
  })
}

export function registerFileIpc(): void {
  ipcMain.handle('files:list-dir', (_e, dir: string) => (dir ? listLevel(dir) : []))

  ipcMain.handle('files:version-info', async (_e, filePath: string) => {
    try {
      return { ok: true, info: await versionInfo(filePath) }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle('files:create-folder', async (_e, dir: string, name: string) => {
    try {
      const target = join(dir, name)
      await fs.mkdir(target, { recursive: false })
      return { ok: true, path: target }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle('files:rename', async (_e, oldPath: string, newName: string) => {
    try {
      const target = join(dirname(oldPath), newName)
      await fs.rename(oldPath, target)
      return { ok: true, path: target }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle('files:delete', async (_e, targetPath: string) => {
    try {
      await fs.rm(targetPath, { recursive: true, force: true })
      return { ok: true }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle('files:open', async (_e, filePath: string) => {
    const err = await shell.openPath(filePath)
    return { ok: !err, error: err || undefined }
  })

  ipcMain.handle('files:reveal', async (_e, filePath: string) => {
    shell.showItemInFolder(filePath)
    return { ok: true }
  })

  ipcMain.handle('files:open-dir', async (_e, dir: string) => {
    const err = await shell.openPath(dir)
    return { ok: !err, error: err || undefined }
  })

  ipcMain.handle('dialog:pick-dir', async () => {
    const r = await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] })
    return r.canceled ? null : r.filePaths[0]
  })

  ipcMain.handle('dialog:pick-file', async (_e, filters: { name: string; extensions: string[] }[]) => {
    const r = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: filters || [{ name: 'YAML', extensions: ['yml', 'yaml'] }]
    })
    if (r.canceled || r.filePaths.length === 0) return null
    const filePath = r.filePaths[0]
    try {
      const content = await fs.readFile(filePath, 'utf-8')
      return { path: filePath, content }
    } catch (err) {
      return { path: filePath, content: '', error: String(err) }
    }
  })

  ipcMain.handle('dialog:pick-files', async (_e, filters: { name: string; extensions: string[] }[]) => {
    const r = await dialog.showOpenDialog({
      properties: ['openFile', 'multiSelections'],
      filters: filters || [{ name: 'YAML', extensions: ['yml', 'yaml'] }]
    })
    if (r.canceled || r.filePaths.length === 0) return null
    const results: { path: string; content: string; error?: string }[] = []
    for (const filePath of r.filePaths) {
      try {
        const content = await fs.readFile(filePath, 'utf-8')
        results.push({ path: filePath, content })
      } catch (err) {
        results.push({ path: filePath, content: '', error: String(err) })
      }
    }
    return results
  })

  ipcMain.handle('dialog:pick-save', async (_e, defaultName: string) => {
    const r = await dialog.showSaveDialog({
      defaultPath: defaultName,
      filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }]
    })
    return r.canceled ? null : r.filePath
  })

  ipcMain.handle('files:batch-delete', async (_e, paths: string[]) => {
    let deleted = 0
    const errors: string[] = []
    for (const p of paths) {
      try {
        await fs.rm(p, { recursive: true, force: true })
        deleted++
      } catch (err) {
        errors.push(`${basename(p)}: ${String(err)}`)
      }
    }
    return { deleted, total: paths.length, errors }
  })

  ipcMain.handle('files:compress', async (_e, payload: { paths: string[]; targetZip: string }) => {
    const { paths, targetZip } = payload
    return new Promise((resolve) => {
      try {
        const output = createWriteStream(targetZip)
        const archive = archiver('zip', { zlib: { level: 9 } })

        const onError = (err: Error) => {
          output.destroy()
          resolve({ ok: false, error: String(err) })
        }

        output.on('close', () => resolve({ ok: true, size: archive.pointer() }))
        output.on('error', onError)
        archive.on('error', onError)

        archive.pipe(output)

        for (const p of paths) {
          try {
            const st = statSync(p)
            if (st.isDirectory()) {
              archive.directory(p, basename(p))
            } else {
              archive.file(p, { name: basename(p) })
            }
          } catch (err) {
            output.destroy()
            resolve({ ok: false, error: `无法读取 ${basename(p)}: ${String(err)}` })
            return
          }
        }

        void archive.finalize()
      } catch (err) {
        resolve({ ok: false, error: String(err) })
      }
    })
  })
}
