import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  Table,
  Button,
  Space,
  Input,
  Empty,
  Tag,
  Breadcrumb,
  Segmented,
  Dropdown,
  Checkbox,
  Tooltip,
  Modal,
  Select,
  App as AntApp
} from 'antd'
import {
  FolderOpenOutlined,
  ReloadOutlined,
  ExportOutlined,
  EyeOutlined,
  SearchOutlined,
  FolderFilled,
  FileExcelFilled,
  FileFilled,
  FolderAddOutlined,
  EditOutlined,
  DeleteOutlined,
  AppstoreOutlined,
  UnorderedListOutlined,
  PartitionOutlined,
  HomeOutlined,
  RightOutlined,
  ArrowLeftOutlined,
  FileZipOutlined,
  CloudUploadOutlined,
  HistoryOutlined
} from '@ant-design/icons'
import { settingsStore, api, type UploadProfile } from '../services/api'
import { startDataUpload } from '../components/UploadProgress'

// ============================================================
// Types & helpers
// ============================================================

interface DirEntry {
  name: string; path: string; isDir: boolean
  type: string; ext: string; size: number; mtime: number
}

function fmtSize(n: number, isDir: boolean): string {
  if (isDir) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function fmtDate(ms: number): string {
  const d = new Date(ms)
  const today = new Date()
  if (d.toDateString() === today.toDateString())
    return `今天 ${d.toTimeString().slice(0, 5)}`
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function sep(p: string): string { return p.includes('\\') ? '\\' : '/' }

function entryIcon(e: DirEntry, size = 16): JSX.Element {
  if (e.isDir) return <FolderFilled style={{ color: '#F59E0B', fontSize: size }} />
  if (['xlsx', 'xls', 'csv'].includes(e.ext))
    return <FileExcelFilled style={{ color: '#10B981', fontSize: size }} />
  return <FileFilled style={{ color: '#6B7280', fontSize: size }} />
}

// ============================================================
// macOS Finder-style tree component
// ============================================================

function FileTree({
  entries,
  onEnter,
  paddingLeft,
  rowMenu,
  onMenuClick,
  selected,
  onToggleSelect,
  childrenMap,
  onLoadChildren
}: {
  entries: DirEntry[]
  onEnter: (e: DirEntry) => void
  paddingLeft: number
  rowMenu: (e: DirEntry) => MenuProps['items']
  onMenuClick: (e: DirEntry, key: string) => void
  selected: Set<string>
  onToggleSelect: (path: string, isDir: boolean) => void
  childrenMap: Record<string, DirEntry[]>
  onLoadChildren: (path: string) => void
}): JSX.Element {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Sort: folders first, then files, alphabetically
  const sorted = [...entries].sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
    return a.name.localeCompare(b.name)
  })

  // Collect all descendant paths recursively
  const collectDescendants = (dirPath: string): string[] => {
    const kids = childrenMap[dirPath]
    if (!kids || !kids.length) return []
    const paths: string[] = []
    for (const k of kids) {
      paths.push(k.path)
      if (k.isDir) paths.push(...collectDescendants(k.path))
    }
    return paths
  }

  // Compute indeterminate state for folders
  const isIndeterminate = (dir: DirEntry): boolean => {
    if (!dir.isDir) return false
    if (selected.has(dir.path)) return false
    const kids = childrenMap[dir.path]
    if (!kids || !kids.length) return false
    return kids.some((k) => selected.has(k.path) || isIndeterminate(k))
  }

  return (
    <div style={{
      fontSize: 13,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif',
      userSelect: 'none'
    }}>
      {sorted.map((e) => {
        const isOpen = expanded.has(e.path)
        const isSel = selected.has(e.path)
        const childEntries = childrenMap[e.path]
        return (
          <div key={e.path}>
            <div
              onClick={() => {
                if (e.isDir) {
                  if (!expanded.has(e.path)) onLoadChildren(e.path)
                  setExpanded((prev) => {
                    const next = new Set(prev)
                    if (next.has(e.path)) next.delete(e.path)
                    else next.add(e.path)
                    return next
                  })
                }
              }}
              onDoubleClick={() => onEnter(e)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: `4px 12px 4px ${paddingLeft + 8}px`,
                cursor: 'default',
                borderRadius: 6,
                margin: '1px 4px',
                background: isSel
                  ? 'linear-gradient(135deg, rgba(50, 120, 246, 0.08), rgba(50, 120, 246, 0.04))'
                  : 'transparent',
                border: isSel ? '1px solid rgba(50, 120, 246, 0.15)' : '1px solid transparent',
                transition: 'background .1s, border-color .1s',
              }}
              onMouseEnter={(ev) => {
                if (!isSel) ev.currentTarget.style.background = 'rgba(0,0,0,.03)'
              }}
              onMouseLeave={(ev) => {
                if (!isSel) { ev.currentTarget.style.background = 'transparent' }
              }}
            >
              {/* Checkbox for multi-select */}
              <Checkbox
                checked={isSel}
                indeterminate={isIndeterminate(e)}
                style={{ flexShrink: 0 }}
                onClick={(ev) => {
                  ev.stopPropagation()
                  onToggleSelect(e.path, e.isDir)
                }}
              />

              {/* Disclosure chevron — Finder-style ›/∨ */}
              <span style={{
                width: 16, flexShrink: 0, textAlign: 'center',
                color: e.isDir ? '#999' : 'transparent',
                fontSize: 12, lineHeight: '22px',
                transition: 'transform .15s',
                transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center'
              }}>
                {e.isDir ? '›' : ''}
              </span>

              {/* Icon */}
              {e.isDir
                ? <FolderFilled style={{
                  color: isOpen ? '#4A8FE7' : '#6AAEF0',
                  fontSize: 15, flexShrink: 0,
                  filter: 'drop-shadow(0 1px 1px rgba(0,0,0,.06))'
                }} />
                : entryIcon(e, 15)
              }

              {/* Name */}
              <span style={{
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                flex: 1, lineHeight: '22px',
                color: isSel ? '#1D4ED8' : '#1F2937',
                fontWeight: isSel ? 500 : 400
              }}>
                {e.name}
              </span>

              {/* Size / meta */}
              <span style={{ color: '#B0B0B0', fontSize: 12, flexShrink: 0, marginLeft: 8 }}>
                {e.isDir ? '—' : fmtSize(e.size, false)}
              </span>
              <span style={{ color: '#C0C0C0', fontSize: 11, flexShrink: 0, width: 80, textAlign: 'right' }}>
                {fmtDate(e.mtime)}
              </span>

              {/* Operations */}
              <Dropdown menu={{ items: rowMenu(e), onClick: ({ key }) => onMenuClick(e, key) }} trigger={['click']}>
                <Button size="small" type="text"
                  style={{ color: '#B0B0B0', flexShrink: 0, marginLeft: 4, minWidth: 28, height: 22, borderRadius: 5 }}
                  onClick={(ev) => ev.stopPropagation()}
                >···</Button>
              </Dropdown>
            </div>

            {/* Children (recursive) */}
            {e.isDir && isOpen && childEntries && (
              <FileTree
                entries={childEntries}
                onEnter={onEnter}
                paddingLeft={paddingLeft + 18}
                rowMenu={rowMenu}
                onMenuClick={onMenuClick}
                selected={selected}
                onToggleSelect={onToggleSelect}
                childrenMap={childrenMap}
                onLoadChildren={onLoadChildren}
              />
            )}
          </div>
        )
      })}
    </div>
  )
}

// ============================================================
// Component
// ============================================================

export default function Files(): JSX.Element {
  const { message, modal } = AntApp.useApp()
  const [root, setRoot] = useState('')
  const [settingsReady, setSettingsReady] = useState(false)
  const [cwd, setCwd] = useState<string>('')
  const [entries, setEntries] = useState<DirEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [view, setView] = useState<'list' | 'grid' | 'tree'>('list')
  const [selected, setSelected] = useState<React.Key[]>([])
  const [gridSelected, setGridSelected] = useState<Set<string>>(new Set())
  const [treeSelected, setTreeSelected] = useState<Set<string>>(new Set())
  const [childrenMap, setChildrenMap] = useState<Record<string, DirEntry[]>>({})
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadProfiles, setUploadProfiles] = useState<UploadProfile[]>([])
  const [uploadProfileId, setUploadProfileId] = useState('')

  // ---- init：先等设置加载完，失败时不要用空目录覆盖已保存的下载路径 ----
  useEffect(() => {
    let alive = true
    settingsStore.load().then((settings) => {
      if (!alive) return
      const stored = settings.downloadDir || ''
      setSettingsReady(true)
      if (stored) { setRoot(stored); setCwd(stored) }
    }).catch(() => {
      if (alive) setSettingsReady(true)
    })
    return () => { alive = false }
  }, [])
  useEffect(() => {
    if (!settingsReady || root) return
    window.api.getDataDir().then((dir) => {
      if (dir) { setRoot(dir); setCwd(dir); void settingsStore.save({ downloadDir: dir }) }
    })
  }, [settingsReady, root])
  useEffect(() => { if (root && !cwd) setCwd(root) }, [root, cwd])

  const syncRoot = useCallback(() => {
    const stored = settingsStore.get('downloadDir', '')
    if (stored && stored !== root) { setRoot(stored); setCwd(stored); return true }
    return false
  }, [root])

  const load = useCallback(async () => {
    if (syncRoot()) return
    if (!cwd) { setEntries([]); return }
    setLoading(true); setSelected([]); setGridSelected(new Set()); setTreeSelected(new Set()); setChildrenMap({})
    try { setEntries((await window.api.listDir(cwd)) as DirEntry[]) }
    finally { setLoading(false) }
  }, [cwd, syncRoot])

  useEffect(() => { void load() }, [load])

  const pickRoot = useCallback(async () => {
    const picked = await window.api.pickDir()
    if (picked) { setRoot(picked); setCwd(picked); settingsStore.save({ downloadDir: picked }) }
  }, [])

  const enter = useCallback((e: DirEntry) => {
    if (e.isDir) setCwd(e.path)
    else window.api.openFile(e.path)
  }, [])

  const isRoot = cwd === root

  const goUp = useCallback(() => {
    if (isRoot) return
    const s = sep(cwd)
    const parent = cwd.split(s).slice(0, -1).join(s) || root
    if (parent) setCwd(parent)
  }, [cwd, root, isRoot])

  const loadChildren = useCallback(async (path: string) => {
    if (childrenMap[path]) return
    try {
      const list = await window.api.listDir(path) as DirEntry[]
      setChildrenMap((prev) => ({ ...prev, [path]: list }))
    } catch { /* folder may be inaccessible */ }
  }, [childrenMap])

  // ---- breadcrumb ----
  const crumbs = useMemo(() => {
    if (!root || !cwd) return []
    const s = sep(cwd)
    const rel = cwd.startsWith(root) ? cwd.slice(root.length).replace(/^[\\/]+/, '') : ''
    const segs = rel ? rel.split(/[\\/]+/).filter(Boolean) : []
    const rootEl = (
      <a
        onClick={() => setCwd(root)}
        style={{ color: cwd === root ? '#1F2937' : '#6B7280', fontWeight: cwd === root ? 600 : 400, cursor: 'pointer' }}
      >
        <HomeOutlined style={{ marginRight: 6 }} />
        {root.split(/[\\/]+/).pop() || root}
      </a>
    )
    const items: { title: JSX.Element }[] = [{ title: rootEl }]
    let acc = root
    for (const seg of segs) {
      acc = acc + s + seg
      const target = acc
      const isLast = acc === cwd
      const el = (
        <a
          onClick={() => setCwd(target)}
          style={{ color: isLast ? '#1F2937' : '#6B7280', fontWeight: isLast ? 600 : 400, cursor: 'pointer' }}
        >{seg}</a>
      )
      items.push({ title: el })
    }
    return items
  }, [root, cwd])

  const filtered = useMemo(
    () => entries.filter((e) => !keyword || e.name.toLowerCase().includes(keyword.toLowerCase())),
    [entries, keyword]
  )

  // ---- unified selection helpers ----
  const selectedPaths = useMemo((): string[] => {
    if (view === 'grid') return entries.filter((e) => gridSelected.has(e.path)).map((e) => e.path)
    if (view === 'tree') return [...treeSelected]   // 树形可能包含子目录中的项
    return entries.filter((e) => selected.includes(e.path)).map((e) => e.path)
  }, [view, entries, gridSelected, treeSelected, selected])

  const selCount = selectedPaths.length

  // Helper: collect all descendant paths from childrenMap (recursive)
  const collectDescendants = useCallback((dirPath: string): string[] => {
    const kids = childrenMap[dirPath]
    if (!kids || !kids.length) return []
    const paths: string[] = []
    for (const k of kids) {
      paths.push(k.path)
      if (k.isDir) paths.push(...collectDescendants(k.path))
    }
    return paths
  }, [childrenMap])

  // ---- actions ----
  const createFolder = useCallback(() => {
    let name = ''
    modal.confirm({
      title: '新建文件夹', icon: <FolderAddOutlined />,
      content: <Input placeholder="文件夹名称" onChange={(e) => (name = e.target.value)} />,
      onOk: async () => {
        if (!name.trim()) return message.warning('请输入名称')
        const r = await window.api.createFolder(cwd, name.trim())
        r.ok ? message.success('已创建') : message.error('创建失败：' + r.error)
        if (r.ok) load()
      }
    })
  }, [cwd, load, message, modal])

  const renameEntry = useCallback((e: DirEntry) => {
    let name = e.name
    modal.confirm({
      title: '重命名', icon: <EditOutlined />,
      content: <Input defaultValue={e.name} onChange={(ev) => (name = ev.target.value)} />,
      onOk: async () => {
        if (!name.trim() || name === e.name) return
        const r = await window.api.rename(e.path, name.trim())
        r.ok ? message.success('已重命名') : message.error('重命名失败：' + r.error)
        if (r.ok) load()
      }
    })
  }, [load, message, modal])

  const deleteEntry = useCallback((e: DirEntry) => {
    modal.confirm({
      title: `删除「${e.name}」？`, icon: <DeleteOutlined />, okType: 'danger',
      content: e.isDir ? '将删除该文件夹及其全部内容，不可恢复。' : '该文件将被删除，不可恢复。',
      onOk: async () => {
        const r = await window.api.deletePath(e.path)
        r.ok ? message.success('已删除') : message.error('删除失败：' + r.error)
        if (r.ok) load()
      }
    })
  }, [load, message])

  const compressEntry = useCallback((e: DirEntry) => {
    const targetZip = e.path + '.zip'
    modal.confirm({
      title: `压缩「${e.name}」？`, icon: <FileZipOutlined />,
      content: `将压缩为 ${e.name}.zip，保存在同级目录。`,
      onOk: async () => {
        const r = await window.api.compressFiles([e.path], targetZip)
        r.ok ? message.success(`压缩完成 (${((r.size || 0) / 1024).toFixed(0)} KB)`) : message.error('压缩失败：' + r.error)
        load()
      }
    })
  }, [load, message, modal])

  const uploadEntry = useCallback((e: DirEntry) => {
    modal.confirm({
      title: `发布「${e.name}」到云端？`, icon: <CloudUploadOutlined />,
      content: `将通过上传接口发布到服务器 /upload/publicData/ 目录。`,
      onOk: async () => {
        message.loading({ content: '正在上传...', key: 'upload' })
        try {
          const r = await api.uploadFile({ localPath: e.path })
          if (r.status === 'ok') message.success({ content: '发布成功！', key: 'upload' })
          else message.error({ content: '发布失败：' + r.message, key: 'upload' })
        } catch (err) {
          message.error({ content: '上传异常：' + String(err), key: 'upload' })
        }
      }
    })
  }, [message, modal])

  const showVersionInfo = useCallback(async (e: DirEntry) => {
    try {
      const result = await window.api.fileVersionInfo(e.path) as {
        ok: boolean
        error?: string
        info?: {
          name: string
          sha256: string
          manifestSha256?: string
          updatedAt?: string
          versions: Array<{ path: string; name: string; sha256: string; archivedAt?: string; size: number; mtime: number }>
        }
      }
      if (!result.ok || !result.info) {
        message.error('读取版本信息失败：' + (result.error || '未知错误'))
        return
      }
      const info = result.info
      modal.info({
        title: `版本信息 - ${info.name}`,
        width: 720,
        content: (
          <div style={{ fontSize: 13, lineHeight: 1.8 }}>
            <div><b>当前 SHA256：</b><code style={{ wordBreak: 'break-all' }}>{info.sha256}</code></div>
            {info.updatedAt && <div><b>记录时间：</b>{info.updatedAt}</div>}
            {info.manifestSha256 && info.manifestSha256 !== info.sha256 && (
              <div style={{ color: '#D97706' }}>清单 hash 与当前文件不一致，文件可能被手动替换。</div>
            )}
            <div style={{ marginTop: 12, fontWeight: 600 }}>历史版本</div>
            {info.versions.length === 0 ? (
              <div style={{ color: '#8A8F95' }}>暂无历史版本。</div>
            ) : (
              <div style={{ maxHeight: 260, overflow: 'auto', marginTop: 6 }}>
                {info.versions.map((v) => (
                  <div key={v.path} style={{ padding: '8px 0', borderTop: '1px solid #F0F0F0' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v.name}</span>
                      <Button size="small" onClick={() => window.api.openFile(v.path)}>打开</Button>
                    </div>
                    <div style={{ color: '#6B7280' }}>{v.archivedAt || fmtDate(v.mtime)} · {fmtSize(v.size, false)}</div>
                    <code style={{ wordBreak: 'break-all', color: '#6B7280' }}>{v.sha256}</code>
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })
    } catch (err) {
      message.error('读取版本信息失败：' + String(err))
    }
  }, [message, modal])

  const rowMenu = (e: DirEntry): MenuProps['items'] => {
    const items: MenuProps['items'] = [
      { key: 'open', label: e.isDir ? '打开文件夹' : '打开文件', icon: <EyeOutlined /> },
      { key: 'reveal', label: '在文件管理器中定位', icon: <ExportOutlined /> },
      { key: 'rename', label: '重命名', icon: <EditOutlined /> }
    ]
    if (e.isDir) {
      items.push({ key: 'compress', label: '压缩为 ZIP', icon: <FileZipOutlined /> })
    }
    if (e.ext === 'zip') {
      items.push({ key: 'upload', label: '发布到云端', icon: <CloudUploadOutlined /> })
    }
    if (!e.isDir) {
      items.push({ key: 'versions', label: '版本信息', icon: <HistoryOutlined /> })
    }
    items.push({ key: 'delete', label: '删除', icon: <DeleteOutlined />, danger: true })
    return items
  }

  const onMenuClick = (e: DirEntry, key: string): void => {
    if (key === 'open') enter(e)
    else if (key === 'reveal') window.api.revealFile(e.path)
    else if (key === 'rename') renameEntry(e)
    else if (key === 'compress') compressEntry(e)
    else if (key === 'upload') uploadEntry(e)
    else if (key === 'versions') void showVersionInfo(e)
    else if (key === 'delete') deleteEntry(e)
  }

  const handleBatchDelete = useCallback(() => {
    const paths = selectedPaths
    const names = paths.map((p) => p.split(/[\\/]+/).pop() || p)
    modal.confirm({
      title: `确认删除 ${paths.length} 项？`,
      icon: <DeleteOutlined />,
      okType: 'danger',
      content: (
        <div style={{ maxHeight: 200, overflow: 'auto', fontSize: 13, lineHeight: 1.8 }}>
          {names.map((n) => <div key={n}>• {n}</div>)}
        </div>
      ),
      onOk: async () => {
        const r = await window.api.batchDelete(paths)
        if (r.errors.length) {
          message.warning(`已删除 ${r.deleted}/${r.total}，${r.errors.length} 项失败`)
        } else {
          message.success(`已删除 ${r.deleted} 项`)
        }
        load()
      }
    })
  }, [selectedPaths, modal, message, load])

  const openUpload = useCallback(async () => {
    if (!selectedPaths.length) {
      message.warning('请先选择文件或文件夹')
      return
    }
    try {
      const res = await api.listUploadProfiles()
      const profiles = res.profiles || []
      setUploadProfiles(profiles)
      setUploadProfileId(profiles[0]?.id || '')
      if (!profiles.length) {
        message.warning('请先在设置 → 数据上传里添加配置')
        return
      }
      setUploadOpen(true)
    } catch (error: unknown) {
      message.error('读取上传配置失败: ' + (error instanceof Error ? error.message : String(error)))
    }
  }, [message, selectedPaths.length])

  const confirmUpload = (): void => {
    if (!uploadProfileId) {
      message.warning('请选择上传配置')
      return
    }
    const profile = uploadProfiles.find((item) => item.id === uploadProfileId)
    startDataUpload({
      profileId: uploadProfileId,
      paths: selectedPaths,
      title: profile ? `上传 · ${profile.name}` : '文件中心上传'
    })
    setUploadOpen(false)
  }

  const handleCompress = useCallback(async () => {
    const paths = selectedPaths
    if (!paths.length) { message.warning('请先选择文件或文件夹'); return }
    const defaultName = `export_${new Date().toISOString().slice(0, 10)}.zip`
    try {
      const target = await window.api.pickSaveFile(defaultName)
      if (!target) return
      const r = await window.api.compressFiles(paths, target)
      if (r.ok) {
        message.success(`已压缩 ${((r.size || 0) / 1024).toFixed(0)} KB 到 ${target.split(/[\\/]+/).pop()}`)
      } else {
        message.error('压缩失败：' + (r.error || '未知错误'))
      }
    } catch (err) {
      message.error('压缩失败：' + String(err))
    }
  }, [selectedPaths, message])

  // ---- table columns ----
  const columns: ColumnsType<DirEntry> = [
    {
      title: '名称', dataIndex: 'name', ellipsis: true,
      render: (v: string, r) => (
        <Space>
          {entryIcon(r)}
          <a onClick={() => enter(r)} style={{ color: r.isDir ? '#3566F5' : '#1F2937' }}>{v}</a>
        </Space>
      )
    },
    { title: '类型', dataIndex: 'type', width: 110, render: (v: string) => <Tag>{v}</Tag> },
    { title: '修改日期', dataIndex: 'mtime', width: 150, sorter: (a, b) => a.mtime - b.mtime, render: (v: number) => fmtDate(v) },
    { title: '大小', dataIndex: 'size', width: 100, render: (v: number, r) => fmtSize(v, r.isDir) },
    {
      title: '', width: 48,
      render: (_, r) => (
        <Dropdown menu={{ items: rowMenu(r), onClick: ({ key }) => onMenuClick(r, key) }} trigger={['click']}>
          <Button size="small" type="text" style={{ color: '#9CA3AF', borderRadius: 5 }}>···</Button>
        </Dropdown>
      )
    }
  ]

  // ---- empty state ----
  if (!root) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 360, gap: 16 }}>
        <FolderOpenOutlined style={{ fontSize: 56, color: '#D1D5DB' }} />
        <div style={{ fontSize: 16, color: '#6B7280' }}>尚未设置下载目录</div>
        <div style={{ fontSize: 13, color: '#9CA3AF' }}>请选择数据采集文件所在的文件夹</div>
        <Button type="primary" size="large" icon={<FolderOpenOutlined />} onClick={pickRoot}>
          选择下载目录
        </Button>
      </div>
    )
  }

  // ============================================================
  // Render
  // ============================================================
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, height: '100%' }}>
      {/* ── Toolbar ── */}
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
          padding: '10px 14px', background: '#fff',
          border: '1px solid #E5E7EB', borderRadius: 10,
          boxShadow: '0 1px 2px rgba(0,0,0,.04)'
        }}
      >
        {/* Left: actions */}
        <Space.Compact>
          <Tooltip title="更换下载目录"><Button icon={<FolderOpenOutlined />} onClick={pickRoot}>目录</Button></Tooltip>
          <Tooltip title="新建文件夹"><Button icon={<FolderAddOutlined />} onClick={createFolder} /></Tooltip>
          <Tooltip title="在系统中打开当前文件夹"><Button icon={<ExportOutlined />} onClick={() => window.api.openDir(cwd)} /></Tooltip>
        </Space.Compact>

        <div style={{ width: 1, height: 24, background: '#E5E7EB', margin: '0 4px' }} />

        <Tooltip title="刷新"><Button icon={<ReloadOutlined />} onClick={load} loading={loading} /></Tooltip>

        <div style={{ flex: 1 }} />

        {/* Batch actions */}
        {selCount > 0 && (
          <>
            <Button type="primary" icon={<DeleteOutlined />} danger onClick={handleBatchDelete} style={{ borderRadius: 6 }}>
              批量删除 ({selCount})
            </Button>
            <Button icon={<ExportOutlined />} onClick={handleCompress} style={{ borderRadius: 6 }}>
              压缩下载 ({selCount})
            </Button>
            <Button icon={<CloudUploadOutlined />} onClick={() => { void openUpload() }} style={{ borderRadius: 6 }}>
              上传 ({selCount})
            </Button>
          </>
        )}

        {/* Search */}
        <Input
          prefix={<SearchOutlined style={{ color: '#9CA3AF' }} />}
          placeholder="搜索..."
          allowClear size="middle"
          style={{ width: 200, borderRadius: 6 }}
          onChange={(e) => setKeyword(e.target.value)}
        />

        {/* View switch */}
        <Segmented
          value={view}
          size="small"
          onChange={(v) => {
            setView(v as 'list' | 'grid' | 'tree')
            setSelected([]); setGridSelected(new Set()); setTreeSelected(new Set())
          }}
          options={[
            { value: 'list', icon: <UnorderedListOutlined /> },
            { value: 'grid', icon: <AppstoreOutlined /> },
            { value: 'tree', icon: <PartitionOutlined /> }
          ]}
          style={{ background: '#F3F4F6' }}
        />

        {/* Item count */}
        <span style={{ fontSize: 12, color: '#9CA3AF', whiteSpace: 'nowrap', marginLeft: 4 }}>
          {entries.length} 项{selCount > 0 ? `，已选 ${selCount}` : ''}
        </span>
      </div>

      {/* ── Breadcrumb ── */}
      <div
        style={{
          padding: '8px 14px', background: '#F9FAFB',
          border: '1px solid #E5E7EB', borderRadius: 8,
          display: 'flex', alignItems: 'center', gap: 6
        }}
      >
        {/* Finder-style back button */}
        <Tooltip title={isRoot ? '已是根目录' : '返回上一级'}>
          <Button
            size="small"
            type="text"
            icon={<ArrowLeftOutlined />}
            disabled={isRoot}
            onClick={goUp}
            style={{
              minWidth: 28, height: 28, borderRadius: 6,
              color: isRoot ? '#D1D5DB' : '#4B5563',
              fontSize: 14
            }}
          />
        </Tooltip>
        <Breadcrumb separator={<RightOutlined style={{ fontSize: 10, color: '#D1D5DB' }} />} items={crumbs} />
      </div>

      {/* ── Content ── */}
      <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
        {view === 'list' ? (
          <Table
            rowKey="path" size="middle" columns={columns} dataSource={filtered} loading={loading}
            rowSelection={{
              selectedRowKeys: selected, onChange: setSelected
            }}
            onRow={(r) => ({
              onDoubleClick: () => enter(r),
              style: { cursor: 'pointer' }
            })}
            pagination={{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
            locale={{ emptyText: <Empty description="该目录为空" /> }}
            style={{ background: '#fff', borderRadius: 8, border: '1px solid #E5E7EB' }}
          />
        ) : view === 'tree' ? (
          filtered.length ? (
            <div style={{
              background: '#fff', borderRadius: 10, border: '1px solid #E5E7EB',
              overflow: 'auto', padding: '6px 0'
            }}>
              <FileTree
                entries={filtered}
                onEnter={enter}
                paddingLeft={0}
                rowMenu={rowMenu}
                onMenuClick={onMenuClick}
                selected={treeSelected}
                onToggleSelect={(path, isDir) => {
                  setTreeSelected((prev) => {
                    const next = new Set(prev)
                    if (isDir) {
                      // Folder: toggle self + all descendants
                      const allPaths = [path, ...collectDescendants(path)]
                      const allSelected = allPaths.every((p) => prev.has(p))
                      if (allSelected) {
                        allPaths.forEach((p) => next.delete(p))
                      } else {
                        allPaths.forEach((p) => next.add(p))
                      }
                    } else {
                      // File: simple toggle
                      if (next.has(path)) { next.delete(path) } else { next.add(path) }
                    }
                    return next
                  })
                }}
                childrenMap={childrenMap}
                onLoadChildren={loadChildren}
              />
            </div>
          ) : (
            <Empty description="该目录为空" />
          )
        ) : filtered.length ? (
          /* ── Grid (macOS Finder-style icon view) ── */
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 12 }}>
            {filtered.map((e) => {
              const sel = gridSelected.has(e.path)
              return (
                <Dropdown key={e.path} menu={{ items: rowMenu(e), onClick: ({ key }) => onMenuClick(e, key) }} trigger={['contextMenu']}>
                  <div
                    onDoubleClick={() => enter(e)}
                    onClick={(ev) => {
                      // Toggle selection (classic checkable behavior)
                      if (ev.shiftKey || ev.metaKey || ev.ctrlKey) {
                        // Modifier click → additive toggle
                        setGridSelected((prev) => {
                          const next = new Set(prev)
                          if (next.has(e.path)) { next.delete(e.path) } else { next.add(e.path) }
                          return next
                        })
                      } else {
                        // Plain click → select only this, or deselect if already selected
                        if (sel) {
                          setGridSelected(new Set())
                        } else {
                          setGridSelected(new Set([e.path]))
                        }
                      }
                    }}
                    style={{
                      background: sel
                        ? 'linear-gradient(135deg, rgba(50, 120, 246, 0.08), rgba(50, 120, 246, 0.03))'
                        : 'rgba(255,255,255,.7)',
                      backdropFilter: 'blur(8px)',
                      border: sel ? '1.5px solid rgba(50, 120, 246, 0.35)' : '1px solid rgba(0,0,0,.06)',
                      borderRadius: 14,
                      padding: '18px 12px 14px',
                      textAlign: 'center',
                      cursor: 'default',
                      transition: 'all .18s cubic-bezier(.4,0,.2,1)',
                      position: 'relative',
                      boxShadow: sel
                        ? '0 2px 12px rgba(50, 120, 246, 0.10), 0 0 0 4px rgba(50, 120, 246, 0.06)'
                        : '0 1px 3px rgba(0,0,0,.03)'
                    }}
                    onMouseEnter={(ev) => {
                      if (!sel) {
                        ev.currentTarget.style.background = 'rgba(0,0,0,.02)'
                        ev.currentTarget.style.borderColor = 'rgba(0,0,0,.10)'
                        ev.currentTarget.style.boxShadow = '0 3px 10px rgba(0,0,0,.05)'
                      }
                    }}
                    onMouseLeave={(ev) => {
                      if (!sel) {
                        ev.currentTarget.style.background = 'rgba(255,255,255,.7)'
                        ev.currentTarget.style.borderColor = 'rgba(0,0,0,.06)'
                        ev.currentTarget.style.boxShadow = '0 1px 3px rgba(0,0,0,.03)'
                      }
                    }}
                  >
                    {/* Selection badge */}
                    {sel && (
                      <div style={{
                        position: 'absolute', top: 8, right: 8,
                        width: 20, height: 20, borderRadius: '50%',
                        background: '#3874F6',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        boxShadow: '0 1px 4px rgba(56,116,246,.3)'
                      }}>
                        <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <path d="M3 8l3.5 3.5L13 5" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </div>
                    )}
                    <div style={{ fontSize: 44, lineHeight: 1, marginBottom: 10, marginTop: 2 }}>
                      {entryIcon(e, 44)}
                    </div>
                    <div style={{
                      fontSize: 12, lineHeight: '1.4',
                      wordBreak: 'break-word',
                      color: sel ? '#1D4ED8' : '#1F2937',
                      fontWeight: sel ? 600 : 500,
                      display: '-webkit-box',
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden'
                    }}>
                      {e.name}
                    </div>
                    <div style={{ fontSize: 11, color: '#A0A0A0', marginTop: 5 }}>
                      {e.isDir ? '文件夹' : fmtSize(e.size, false)}
                    </div>
                  </div>
                </Dropdown>
              )
            })}
          </div>
        ) : (
          <Empty description="该目录为空" />
        )}
      </div>
      <Modal
        title="选择上传配置"
        open={uploadOpen}
        okText="开始上传"
        cancelText="取消"
        onOk={confirmUpload}
        onCancel={() => setUploadOpen(false)}
      >
        <div style={{ marginBottom: 8 }}>文件夹会先压缩成 zip，再和文件一起上传。</div>
        <Select
          style={{ width: '100%' }}
          value={uploadProfileId || undefined}
          placeholder="选择上传配置"
          options={uploadProfiles.map((item) => ({ label: item.name, value: item.id }))}
          onChange={setUploadProfileId}
        />
      </Modal>
    </div>
  )
}
