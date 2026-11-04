export type DownloadStatus = 'expected' | 'saved' | 'empty' | 'failed' | 'missed'

export interface ResultFile {
  id: string
  folders: string[]
  name: string
  source: string
  date: string
  status: DownloadStatus
  reason: string
}

export interface ExecutionUpload {
  profileId: string
  profileName?: string
  execId: string
  paths: string[]
  /** 已经按自动上传跑过 */
  auto: boolean
  /** 自动上传结果；还没传时为 null */
  ok: boolean | null
  message?: string
}

export interface ExecutionResult {
  /** 弹窗标题，如「执行完成」「指标播放失败」 */
  title: string
  status: 'success' | 'warning' | 'error'
  /** 任务或指标名称 */
  name: string
  /** 结果说明：成功/失败项、错误原因等 */
  message?: string
  /** 按存储目录展开的预期文件和实际结果 */
  files: ResultFile[]
  /** 选了上传配置时，结果里可以手动上传 */
  upload?: ExecutionUpload
}

export interface ResultTreeNode {
  key: string
  name: string
  kind: 'folder' | 'file'
  file?: ResultFile
  children: ResultTreeNode[]
}

const STATUS_RANK: Record<DownloadStatus, number> = {
  expected: 0,
  missed: 1,
  failed: 2,
  empty: 3,
  saved: 4
}

type Listener = (items: ExecutionResult[]) => void

let queue: ExecutionResult[] = []
const listeners = new Set<Listener>()

function publish(): void {
  const snapshot = queue
  listeners.forEach((listener) => listener(snapshot))
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function foldersOf(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => text(item)).filter(Boolean)
}

function fileKey(data: Record<string, unknown>): string {
  return [text(data.indicator), text(data.output_key), text(data.date)].join('::')
}

function displayName(file: Pick<ResultFile, 'name' | 'date'>): string {
  if (!file.date || file.name.includes(file.date)) return file.name || '未命名文件'
  return `${file.name || '未命名文件'} · ${file.date}`
}

/** 把一次执行里的下载事件收成文件清单。 */
export function createDownloadLedger(): {
  note: (event: string, data: Record<string, unknown>, labels?: Record<string, string>) => void
  finish: () => ResultFile[]
  reset: () => void
} {
  let files: ResultFile[] = []
  let sawPlan = false

  const sourceOf = (indicator: string, labels?: Record<string, string>): string =>
    (indicator && labels?.[indicator]) || indicator

  const upsert = (next: ResultFile, replaceName: boolean): void => {
    const index = files.findIndex((item) => item.id === next.id)
    if (index < 0) {
      if (!next.name && next.status !== 'saved') return
      files.push(next)
      return
    }
    const current = files[index]
    if (STATUS_RANK[current.status] > STATUS_RANK[next.status]) return
    files[index] = {
      ...current,
      status: next.status,
      reason: next.reason || current.reason,
      source: next.source || current.source,
      folders: next.folders.length > 0 ? next.folders : current.folders,
      name: replaceName && next.name ? next.name : current.name,
      date: next.date || current.date
    }
  }

  return {
    reset(): void {
      files = []
      sawPlan = false
    },
    note(event: string, data: Record<string, unknown>, labels: Record<string, string> = {}): void {
      if (event === 'file_expected') {
        sawPlan = true
        const id = fileKey(data)
        if (files.some((item) => item.id === id)) return
        files.push({
          id,
          folders: foldersOf(data.folders),
          name: text(data.name) || text(data.label) || '下载文件',
          source: sourceOf(text(data.indicator), labels),
          date: text(data.date),
          status: 'expected',
          reason: ''
        })
        return
      }
      if (event === 'file_result') {
        const status = text(data.status)
        if (status !== 'saved' && status !== 'empty' && status !== 'failed') return
        const name = text(data.name)
        const id = fileKey(data)
        const existing = files.find((item) => item.id === id)
        if (existing && existing.status === 'saved' && status === 'saved' && name && existing.name !== name) {
          files.push({
            id: `${id}#${name}`,
            folders: foldersOf(data.folders).length > 0 ? foldersOf(data.folders) : existing.folders,
            name,
            source: existing.source,
            date: text(data.date) || existing.date,
            status: 'saved',
            reason: ''
          })
          return
        }
        upsert({
          id,
          folders: foldersOf(data.folders),
          name,
          source: sourceOf(text(data.indicator), labels),
          date: text(data.date),
          status,
          reason: text(data.reason)
        }, status === 'saved')
        return
      }
      if (event === 'repeat_no_data') {
        const date = text(data.date)
        const indicator = text(data.indicator)
        const reason = text(data.message) || '没数据'
        let touched = false
        files = files.map((item) => {
          if (item.status !== 'expected' || item.date !== date) return item
          if (indicator && item.id.split('::')[0] !== indicator) return item
          touched = true
          return { ...item, status: 'empty', reason }
        })
        if (!touched) {
          files.push({
            id: `empty-${date}-${files.length}`,
            folders: [],
            name: date || '当日数据',
            source: sourceOf(indicator, labels),
            date,
            status: 'empty',
            reason
          })
        }
        return
      }
      if (sawPlan) return
      if (event === 'export_saved' || event === 'calendar_saved' || event === 'report_saved' || (event === 'api_fallback' && data.status === 'success')) {
        const parts = text(data.path).split(/[\\/]/).filter(Boolean)
        const name = parts.pop() || text(data.original_name) || '下载文件'
        files.push({
          id: text(data.path) || `${event}-${files.length}`,
          folders: parts.slice(-3),
          name,
          source: sourceOf(text(data.indicator), labels),
          date: text(data.date) || text(data.month),
          status: 'saved',
          reason: ''
        })
        return
      }
      if (event === 'report_not_found') {
        const date = text(data.date)
        files.push({
          id: `missing-${date}-${files.length}`,
          folders: [],
          name: date || '报告',
          source: sourceOf(text(data.indicator), labels),
          date,
          status: 'empty',
          reason: '没有找到报告'
        })
      }
    },
    finish(): ResultFile[] {
      return files.map((item) => item.status === 'expected'
        ? { ...item, status: 'missed', reason: item.reason || (sawPlan ? '未执行到' : '') }
        : { ...item })
    }
  }
}

/** 从执行日志里还原下载树。没有结构化事件时返回空列表。 */
export function filesFromLog(detail: string, labels: Record<string, string> = {}): ResultFile[] {
  const ledger = createDownloadLedger()
  for (const line of detail.split(/\r?\n/)) {
    const matched = line.match(/📌 (file_expected|file_result|repeat_no_data): (\{.*\})\s*$/)
    if (!matched) continue
    try {
      const data = JSON.parse(matched[2]) as Record<string, unknown>
      ledger.note(matched[1], data, labels)
    } catch {
      // 坏行跳过，不影响其余结果。
    }
  }
  return ledger.finish()
}

/** 从执行日志末尾还原上传提议。采集状态不变，上传成败写在详情里。 */
export function uploadOfferFromLog(detail: string): ExecutionUpload | undefined {
  const lines = detail.split(/\r?\n/)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const marker = lines[index].indexOf('upload_offer:')
    if (marker < 0) continue
    try {
      const data = JSON.parse(lines[index].slice(marker + 'upload_offer:'.length).trim()) as Record<string, unknown>
      const paths = Array.isArray(data.paths) ? data.paths.map((item) => text(item)).filter(Boolean) : []
      const profileId = text(data.profile_id)
      if (!profileId) continue
      return {
        profileId,
        profileName: text(data.profile_name),
        execId: text(data.exec_id),
        paths,
        auto: Boolean(data.auto),
        ok: data.ok == null ? null : Boolean(data.ok),
        message: text(data.message)
      }
    } catch {
      // 坏掉的一行不影响结果弹窗。
    }
  }
  return undefined
}

/** 按目录分段收成和存储结构一样的树。 */
export function buildResultTree(files: ResultFile[]): ResultTreeNode[] {
  const roots: ResultTreeNode[] = []
  files.forEach((file, index) => {
    let level = roots
    file.folders.forEach((folder, depth) => {
      const key = `${file.folders.slice(0, depth + 1).join('/')}`
      let node = level.find((item) => item.kind === 'folder' && item.name === folder)
      if (!node) {
        node = { key, name: folder, kind: 'folder', children: [] }
        level.push(node)
      }
      level = node.children
    })
    level.push({
      key: `${file.id}-${index}`,
      name: displayName(file),
      kind: 'file',
      file,
      children: []
    })
  })
  return roots
}

/** 数一棵树里的文件。 */
export function countTreeFiles(nodes: ResultTreeNode[]): number {
  return nodes.reduce((sum, node) => sum + (node.kind === 'file' ? 1 : countTreeFiles(node.children)), 0)
}

/** 打开全局执行结果弹窗，并把采集窗口带回前台。 */
export function showExecutionResult(result: ExecutionResult): void {
  const raise = window.api?.raiseMainWindow
  if (raise) void raise().catch(() => {})
  queue = [...queue, {
    title: result.title.trim() || '执行结果',
    status: result.status,
    name: result.name.trim() || '未命名',
    message: result.message?.trim() || undefined,
    files: result.files || [],
    upload: result.upload
  }]
  publish()
}

/** 关闭当前这条结果；队列里还有下一条时继续显示。 */
export function dismissExecutionResult(): void {
  queue = queue.slice(1)
  publish()
}

/** 订阅结果队列。挂载时会立刻收到当前队列。 */
export function subscribeExecutionResults(listener: Listener): () => void {
  listeners.add(listener)
  listener(queue)
  return () => { listeners.delete(listener) }
}
