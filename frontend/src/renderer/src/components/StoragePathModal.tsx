import { useEffect, useMemo, useState, type DragEvent } from 'react'
import {
  Alert,
  App as AntApp,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  Tooltip
} from 'antd'
import {
  CalendarOutlined,
  CheckOutlined,
  CloseOutlined,
  EditOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  PlusOutlined,
  WifiOutlined
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { api, type WorkflowInfo, type WorkflowOutput } from '../services/api'

export const DEFAULT_PATH_TEMPLATE = ''
export const LEGACY_PATH_TEMPLATE = '{YYYY-MM}/{YYYY-MM-DD}'

/** 旧版平铺文件夹配置，仅用于把旧任务迁移成结构树。 */
export interface PathFolder {
  name: string
  indicators: string[]
}

type NodeKind = 'wifi' | 'date' | 'folder' | 'file'

interface PathNode {
  id: string
  kind: NodeKind
  name: string
  token?: string
  indicator?: string
  outputKey?: string
  outputLabel?: string
  children: PathNode[]
}

interface Assignment {
  indicator: string
  output_key: string
  output_label: string
  folder: string
  path: string
}

interface UnassignedOutput {
  indicator: string
  output_key: string
  label: string
}

interface StructurePreview {
  error: string
  base: string
  assignments: Assignment[]
  unassigned: UnassignedOutput[]
  invalid: string[]
  conflicts: string[]
}

type PalettePayload = {
  source: 'palette'
  kind: NodeKind
  token?: string
  indicator?: string
  outputKey?: string
  outputLabel?: string
}

type TreePayload = { source: 'tree'; id: string; ids?: string[] }
type DragPayload = PalettePayload | TreePayload

interface StoragePathModalProps {
  open: boolean
  root: string
  structure: string
  template: string
  folders: PathFolder[]
  indicators: string[]
  workflows: WorkflowInfo[]
  indicatorWindows: Record<string, [number, number]>
  /** 嵌在任务编辑步骤里：不套弹窗，改动实时回传，保存由外层负责。 */
  embedded?: boolean
  onChange?: (structure: string, blocked: boolean) => void
  onCancel?: () => void
  onSave?: (structure: string) => void
}

const ROOT_ID = 'root'
const SITE_NAME_LABEL = '网站原名'
const DATE_TOKENS = ['{YYYY}', '{YYYY-MM}', '{YYYY-MM-DD}']
const TOKEN_HELP: Record<string, string> = {
  '{YYYY}': '按执行日期生成年份，如 2026',
  '{YYYY-MM}': '按执行日期生成年月，如 2026-10',
  '{YYYY-MM-DD}': '按执行日期生成年月日，如 2026-10-30'
}

let nodeSeq = 0
function nextId(): string {
  nodeSeq += 1
  return `n_${Date.now().toString(36)}_${nodeSeq}`
}

function makeWifi(): PathNode {
  return { id: nextId(), kind: 'wifi', name: 'WiFi 名称', token: '{wifi}', children: [] }
}

function makeDate(token: string): PathNode {
  return { id: nextId(), kind: 'date', name: token, token, children: [] }
}

function makeFolder(name = '新文件夹'): PathNode {
  return { id: nextId(), kind: 'folder', name, children: [] }
}

function makeFile(indicator: string, outputKey = '', outputLabel = ''): PathNode {
  return {
    id: nextId(),
    kind: 'file',
    name: outputLabel || indicator,
    indicator,
    outputKey,
    outputLabel,
    children: []
  }
}

function previewFileName(name: string, dateLabel = ''): string {
  const now = dayjs()
  return String(name || '')
    .replace(/\{date\}/g, dateLabel || now.format('YYYY-MM-DD'))
    .replace(/\{date_compact\}/g, dateLabel || now.format('YYYYMMDD'))
    .replace(/\{month_iso\}/g, now.format('YYYY-MM'))
    .replace(/\{previous_month_iso\}/g, now.subtract(1, 'month').format('YYYY-MM'))
    .replace(/\{previous_month\}/g, now.subtract(1, 'month').format('YYYYMM'))
    .replace(/\{month\}/g, now.format('YYYYMM'))
    .replace(/\{year\}/g, now.format('YYYY'))
}

function outputsFor(workflow: WorkflowInfo | undefined): WorkflowOutput[] {
  if (workflow?.outputs?.length) return workflow.outputs
  const fallback = String(workflow?.output_name || '').trim()
  return [{
    key: '',
    label: fallback || SITE_NAME_LABEL,
    save_as: fallback,
    action: 'export',
    step_index: 0
  }]
}

function findOutput(
  workflows: WorkflowInfo[],
  indicator: string,
  outputKey: string
): WorkflowOutput | undefined {
  const workflow = workflows.find((item) => item.name === indicator)
  return workflow?.outputs?.find((item) => item.key === outputKey)
}

/** 结构里的标签已对不上任务里的文件（指标移除、save_as 改名）。 */
function isInvalidFile(node: PathNode, invalid: string[]): boolean {
  if (node.kind !== 'file' || !node.indicator) return false
  const tag = node.outputKey ? outputCanonical(node.indicator, node.outputKey) : node.indicator
  return invalid.includes(tag) || invalid.includes(node.indicator)
}

function collectInvalidIds(nodes: PathNode[], invalid: string[], result: string[] = []): string[] {
  for (const node of nodes) {
    if (isInvalidFile(node, invalid)) result.push(node.id)
    collectInvalidIds(node.children, invalid, result)
  }
  return result
}

function indicatorLabel(indicator: string, workflows: WorkflowInfo[]): string {
  return workflows.find((item) => item.name === indicator)?.label || indicator
}

function outputBaseName(label: string, dateLabel = ''): string {
  const rendered = previewFileName(label, dateLabel).replace(/[\\/]+$/, '')
  return rendered.split(/[\\/]/).pop() || rendered
}

function outputRelative(label: string, dateLabel = ''): string {
  return previewFileName(label, dateLabel).replace(/^[\\/]+/, '')
}

function outputParent(label: string, dateLabel = ''): string {
  const parts = previewFileName(label, dateLabel).split(/[\\/]/).filter(Boolean)
  return parts.length > 1 ? parts[parts.length - 2] : ''
}

function nodeOutputLabel(node: PathNode, workflows: WorkflowInfo[]): string {
  if (node.outputLabel) return node.outputLabel
  const output = findOutput(workflows, node.indicator || '', node.outputKey || '')
  return output?.label || node.outputKey || SITE_NAME_LABEL
}

function findFileNode(nodes: PathNode[], canonical: string): PathNode | null {
  for (const node of nodes) {
    if (node.kind === 'file' && outputCanonical(node.indicator || '', node.outputKey || '') === canonical) return node
    const child = findFileNode(node.children, canonical)
    if (child) return child
  }
  return null
}

function dedupeFileNodes(nodes: PathNode[]): PathNode[] {
  const seen = new Set<string>()
  const walk = (items: PathNode[]): PathNode[] => {
    const result: PathNode[] = []
    for (const node of items) {
      if (node.kind === 'file') {
        const key = outputCanonical(node.indicator || '', node.outputKey || '')
        if (seen.has(key)) continue
        seen.add(key)
        result.push(node)
        continue
      }
      result.push({ ...node, children: walk(node.children) })
    }
    return result
  }
  return walk(nodes)
}

function outputCanonical(indicator: string, outputKey: string): string {
  return `${indicator}::${outputKey}`
}

function parseStructure(text: string, workflows: WorkflowInfo[]): PathNode[] {
  const roots: PathNode[] = []
  const stack: { indent: number; node: PathNode }[] = []
  for (const raw of String(text || '').split(/\r?\n/)) {
    if (!raw.trim() || raw.trim().startsWith('#')) continue
    const indent = raw.length - raw.trimStart().length
    const line = raw.trim()
    if (!line.startsWith('-')) continue
    const token = line.slice(1).trim()
    if (!token) continue

    const created: PathNode[] = []
    if (/^\{\s*(wifi|ssid|WIFI)\s*\}$/i.test(token)) {
      created.push(makeWifi())
    } else if (/^\{[^{}]+\}$/.test(token)) {
      created.push(makeDate(token))
    } else if (token.startsWith('[') && token.endsWith(']')) {
      const content = token.slice(1, -1).trim()
      const split = content.lastIndexOf('::')
      const indicator = split >= 0 ? content.slice(0, split) : content
      const key = split >= 0 ? content.slice(split + 2) : ''
      const workflow = workflows.find((item) => item.name === indicator)
      const outputs = outputsFor(workflow)
      if (split >= 0) {
        const output = outputs.find((item) => item.key === key)
        created.push(makeFile(indicator, key, output?.label || key))
      } else if (outputs.length > 1) {
        outputs.forEach((output) => created.push(makeFile(indicator, output.key, output.label)))
      } else {
        const output = outputs[0]
        created.push(makeFile(indicator, output?.key || '', output?.label || SITE_NAME_LABEL))
      }
    } else {
      created.push(makeFolder(token))
    }
    if (created.length === 0) continue

    while (stack.length && indent <= stack[stack.length - 1].indent) stack.pop()
    if (stack.length) {
      const parent = stack[stack.length - 1].node
      if (parent.kind !== 'file') parent.children.push(...created)
    } else {
      roots.push(...created)
    }
    const last = created[created.length - 1]
    if (last.kind !== 'file') stack.push({ indent, node: last })
  }
  return roots
}

function serializeStructure(nodes: PathNode[], depth = 0): string[] {
  const lines: string[] = []
  for (const node of nodes) {
    const token = node.kind === 'wifi'
      ? '{wifi}'
      : node.kind === 'date'
        ? node.token || node.name
        : node.kind === 'file'
          ? node.outputKey
            ? `[${node.indicator}::${node.outputKey}]`
            : `[${node.indicator}]`
          : node.name
    lines.push(`${'  '.repeat(depth)}- ${token}`)
    if (node.children.length > 0) {
      lines.push(...serializeStructure(node.children, depth + 1))
    }
  }
  return lines
}

function flattenLegacyFolders(raw: unknown, prefix = ''): PathFolder[] {
  if (!Array.isArray(raw)) return []
  const result: PathFolder[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const folder = item as { name?: string; indicators?: string[]; children?: unknown }
    const name = String(folder.name || '').trim()
    if (!name) continue
    const full = prefix ? `${prefix}/${name}` : name
    result.push({
      name: full,
      indicators: Array.isArray(folder.indicators)
        ? folder.indicators.filter((value): value is string => typeof value === 'string')
        : []
    })
    result.push(...flattenLegacyFolders(folder.children, full))
  }
  return result
}

interface LegacyNode {
  name: string
  files: string[]
  children: Map<string, LegacyNode>
}

function buildLegacyStructure(
  template: string,
  folders: PathFolder[],
  indicators: string[],
  workflows: WorkflowInfo[]
): string {
  const base = String(template || '')
    .split('/')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => (part === '{SSID}' || part === '{WIFI}' ? '{wifi}' : part))

  const tree = new Map<string, LegacyNode>()
  const assigned = new Map<string, string[]>()
  for (const folder of flattenLegacyFolders(folders)) {
    const parts = folder.name.split('/').map((part) => part.trim()).filter(Boolean)
    if (parts.length === 0) continue
    folder.indicators.filter((indicator) => indicators.includes(indicator))
      .forEach((indicator) => assigned.set(indicator, parts))
  }

  const addFile = (parts: string[], file: string): void => {
    let level = tree
    let node: LegacyNode | null = null
    for (const part of parts) {
      if (!level.has(part)) level.set(part, { name: part, files: [], children: new Map() })
      node = level.get(part) as LegacyNode
      level = node.children
    }
    if (node) node.files.push(file)
    else rootFiles.push(file)
  }
  const rootFiles: string[] = []
  for (const indicator of indicators) {
    const workflow = workflows.find((item) => item.name === indicator)
    for (const output of outputsFor(workflow)) {
      const yamlParts = String(output.save_as || '').replace(/^[\\/]+/, '').split(/[\\/]/).filter(Boolean)
      const yamlFolders = yamlParts.slice(0, -1)
      const parts = assigned.get(indicator) || yamlFolders
      const file = output.key ? `[${indicator}::${output.key}]` : `[${indicator}]`
      addFile(parts, file)
    }
  }

  const lines: string[] = []
  base.forEach((part, index) => lines.push(`${'  '.repeat(index)}- ${part}`))
  const render = (nodes: Map<string, LegacyNode>, depth: number): void => {
    nodes.forEach((node) => {
      lines.push(`${'  '.repeat(depth)}- ${node.name}`)
      node.files.forEach((file) => lines.push(`${'  '.repeat(depth + 1)}- ${file}`))
      render(node.children, depth + 1)
    })
  }
  render(tree, base.length)
  rootFiles.forEach((file) => lines.push(`${'  '.repeat(base.length)}- ${file}`))
  return lines.join('\n')
}

function defaultNodes(indicators: string[], workflows: WorkflowInfo[]): PathNode[] {
  const nodes: PathNode[] = []
  for (const indicator of indicators) {
    const outputs = outputsFor(workflows.find((item) => item.name === indicator))
    if (outputs.length > 1) {
      outputs.forEach((output) => nodes.push(makeFile(indicator, output.key, output.label)))
    } else {
      const output = outputs[0]
      nodes.push(makeFile(indicator, output?.key || '', output?.label || SITE_NAME_LABEL))
    }
  }
  return nodes
}

function findNode(nodes: PathNode[], id: string): PathNode | null {
  for (const node of nodes) {
    if (node.id === id) return node
    const child = findNode(node.children, id)
    if (child) return child
  }
  return null
}

function containsNode(node: PathNode, id: string): boolean {
  if (node.id === id) return true
  return node.children.some((child) => containsNode(child, id))
}

function updateNode(nodes: PathNode[], id: string, updater: (node: PathNode) => PathNode): PathNode[] {
  return nodes.map((node) => {
    if (node.id === id) return updater(node)
    if (node.children.length === 0) return node
    return { ...node, children: updateNode(node.children, id, updater) }
  })
}

function removeNode(nodes: PathNode[], id: string): PathNode[] {
  return nodes
    .filter((node) => node.id !== id)
    .map((node) => ({ ...node, children: removeNode(node.children, id) }))
}

function insertChild(nodes: PathNode[], parentId: string, node: PathNode): PathNode[] {
  if (parentId === ROOT_ID) return [...nodes, node]
  return updateNode(nodes, parentId, (parent) => ({
    ...parent,
    children: [...parent.children, node]
  }))
}

function moveNode(nodes: PathNode[], id: string, targetId: string): PathNode[] {
  if (id === targetId) return nodes
  const node = findNode(nodes, id)
  if (!node) return nodes
  if (containsNode(node, targetId)) return nodes
  if (targetId !== ROOT_ID) {
    const target = findNode(nodes, targetId)
    if (!target || target.kind === 'file') return nodes
  }
  return insertChild(removeNode(nodes, id), targetId, node)
}

/** 勾选里父子都选中时只留父节点，子节点跟着走 */
function topLevelIds(nodes: PathNode[], ids: string[]): string[] {
  const wanted = new Set(ids)
  const result: string[] = []
  const walk = (items: PathNode[]): void => {
    for (const node of items) {
      if (wanted.has(node.id)) result.push(node.id)
      else walk(node.children)
    }
  }
  walk(nodes)
  return result
}

function moveNodes(nodes: PathNode[], ids: string[], targetId: string): PathNode[] {
  return topLevelIds(nodes, ids).reduce((items, id) => moveNode(items, id, targetId), nodes)
}

function parentIdOf(nodes: PathNode[], id: string, parentId = ROOT_ID): string | null {
  for (const node of nodes) {
    if (node.id === id) return parentId
    const found = parentIdOf(node.children, id, node.id)
    if (found) return found
  }
  return null
}

function containerLabel(node: PathNode): string {
  if (node.kind === 'wifi') return 'WiFi 名称'
  if (node.kind === 'date') return node.token || node.name
  return node.name
}

/** 批量移动的目标：所有容器，去掉被选中的及其下级（不能把自己挪进自己） */
function containerOptions(
  nodes: PathNode[],
  exclude: Set<string>,
  prefix = ''
): { value: string; label: string }[] {
  const result: { value: string; label: string }[] = []
  for (const node of nodes) {
    if (node.kind === 'file' || exclude.has(node.id)) continue
    const label = prefix ? `${prefix} / ${containerLabel(node)}` : containerLabel(node)
    result.push({ value: node.id, label })
    result.push(...containerOptions(node.children, exclude, label))
  }
  return result
}

function collectFiles(nodes: PathNode[], result: string[] = []): string[] {
  for (const node of nodes) {
    if (node.kind === 'file' && node.indicator) {
      result.push(outputCanonical(node.indicator, node.outputKey || ''))
    }
    collectFiles(node.children, result)
  }
  return result
}

function hasKind(nodes: PathNode[], kind: NodeKind): boolean {
  return nodes.some((node) => node.kind === kind || hasKind(node.children, kind))
}

function countFiles(nodes: PathNode[]): number {
  return nodes.reduce((sum, node) => (
    sum + (node.kind === 'file' ? 1 : countFiles(node.children))
  ), 0)
}

function writeDragData(event: DragEvent, payload: DragPayload): void {
  event.dataTransfer.effectAllowed = 'copyMove'
  const text = JSON.stringify(payload)
  event.dataTransfer.setData('application/x-spider-path', text)
  event.dataTransfer.setData('text/plain', text)
}

function readDragData(event: DragEvent): DragPayload | null {
  const raw = event.dataTransfer.getData('application/x-spider-path')
    || event.dataTransfer.getData('text/plain')
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as DragPayload
    return parsed && (parsed.source === 'palette' || parsed.source === 'tree') ? parsed : null
  } catch {
    return null
  }
}

export default function StoragePathModal({
  open,
  root,
  structure,
  template,
  folders,
  indicators,
  workflows,
  indicatorWindows,
  embedded = false,
  onChange,
  onCancel,
  onSave
}: StoragePathModalProps): JSX.Element {
  const { message, modal } = AntApp.useApp()
  const [nodes, setNodes] = useState<PathNode[]>([])
  const [selectedId, setSelectedId] = useState<string>(ROOT_ID)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftName, setDraftName] = useState('')
  const [ssid, setSsid] = useState('')
  const [dragOver, setDragOver] = useState<string | null>(null)
  const [preview, setPreview] = useState<StructurePreview>({
    error: '',
    base: '',
    assignments: [],
    unassigned: [],
    invalid: [],
    conflicts: []
  })
  const [checking, setChecking] = useState(false)
  // 嵌入时所见即所得：初始化完就回传。旧任务的空结构是按 save_as 目录回显的，而运行时已忽略这些目录
  const [ready, setReady] = useState(false)
  const [startedEmpty, setStartedEmpty] = useState(false)
  const [flashId, setFlashId] = useState<string | null>(null)
  // 批量移动：勾选的文件/文件夹
  const [checked, setChecked] = useState<string[]>([])

  const windowLabelFor = (indicator: string): string => {
    const workflow = workflows.find((item) => item.name === indicator)
    const span = indicatorWindows[indicator]
      || [workflow?.window_start ?? -1, workflow?.window_end ?? -1]
    const unit = workflow?.date_kind === 'month' ? 'M' : workflow?.date_kind === 'week' ? 'W' : 'D'
    const format = (value: number): string => (
      value === 0 ? unit : `${unit}${value > 0 ? '+' : ''}${value}`
    )
    return span[0] === span[1]
      ? format(span[0])
      : `${format(span[0])}~${format(span[1])}`
  }

  const describeOutput = (indicator: string, rawLabel: string): string => {
    const windowLabel = windowLabelFor(indicator)
    const parent = outputParent(rawLabel, windowLabel)
    const base = outputBaseName(rawLabel, windowLabel)
    return `${indicatorLabel(indicator, workflows)}：${parent ? `[${parent}] ` : ''}${base}`
  }

  useEffect(() => {
    if (!open) return
    const initial = String(structure || '').trim()
    let parsed = initial ? parseStructure(initial, workflows) : []
    if (parsed.length === 0) {
      parsed = parseStructure(buildLegacyStructure(template, folders, indicators, workflows), workflows)
    }
    if (parsed.length === 0 && indicators.length > 0) {
      parsed = defaultNodes(indicators, workflows)
    }
    setNodes(dedupeFileNodes(parsed))
    setReady(true)
    setStartedEmpty(!initial)
    setSelectedId(ROOT_ID)
    setChecked([])
    setEditingId(null)
    setDragOver(null)
    Promise.all([
      api.currentWifi().then((result) => result.ssid || '').catch(() => ''),
      api.getSettings().then((result) => String(result.settings?.wifiSsid || '')).catch(() => '')
    ]).then(([current, target]) => setSsid((target || current).trim()))
    // 嵌入时 structure 是自己回传的，跟着它重建会打断正在改名的文件夹
  }, [open, embedded ? '' : structure, template, folders, indicators, workflows])

  const structureText = useMemo(() => serializeStructure(nodes).join('\n'), [nodes])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setChecking(true)
    const timer = window.setTimeout(() => {
      api.previewTaskStructure({
        structure: structureText,
        indicators,
        root,
        ssid
      })
        .then((result) => {
          if (!cancelled) setPreview({ ...result, invalid: result.invalid || [], conflicts: result.conflicts || [] })
        })
        .catch((error) => {
          if (cancelled) return
          setPreview({
            error: error instanceof Error ? error.message : String(error),
            base: '',
            assignments: [],
            unassigned: [],
            invalid: [],
            conflicts: []
          })
        })
        .finally(() => {
          if (!cancelled) setChecking(false)
        })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [open, structureText, indicators, root, ssid])

  useEffect(() => {
    if (!embedded || !open || !ready) return
    onChange?.(structureText, Boolean(preview.error))
  }, [embedded, open, ready, structureText, preview.error])

  const usedFiles = useMemo(() => new Set(collectFiles(nodes)), [nodes])
  const wifiUsed = useMemo(() => hasKind(nodes, 'wifi'), [nodes])
  const fileCount = useMemo(() => countFiles(nodes), [nodes])
  const invalidIds = useMemo(() => collectInvalidIds(nodes, preview.invalid), [nodes, preview.invalid])
  const outputIssues = useMemo(() => {
    const duplicated: string[] = []
    const unnamed: string[] = []
    const needName: string[] = []
    const conflicts = new Set(preview.conflicts)
    for (const indicator of indicators) {
      const workflow = workflows.find((item) => item.name === indicator)
      const source = workflow?.label || indicator
      const seen = new Set<string>()
      // 重名的按文件名合并成一条，带上各自的步骤位置
      const positions = new Map<string, string[]>()
      for (const output of workflow?.outputs || []) {
        if (conflicts.has(outputCanonical(indicator, output.key)) && !seen.has(output.label)) {
          seen.add(output.label)
          duplicated.push(`${source}：${output.label}`)
        }
        if (!String(output.save_as || '').trim()) unnamed.push(`${source}：${output.label}`)
        if (output.duplicate) {
          positions.set(output.label, [...(positions.get(output.label) || []), output.position || output.key])
        }
      }
      positions.forEach((items, label) => needName.push(`${source}：${label}（${items.join('、')}）`))
    }
    return { duplicated, unnamed, needName }
  }, [indicators, workflows, preview.conflicts])

  const removeInvalid = (): void => {
    setNodes((prev) => invalidIds.reduce((items, id) => removeNode(items, id), prev))
    if (invalidIds.includes(selectedId)) setSelectedId(ROOT_ID)
  }

  const selectedContainerId = useMemo(() => {
    if (selectedId === ROOT_ID) return ROOT_ID
    const selected = findNode(nodes, selectedId)
    return selected && selected.kind !== 'file' ? selected.id : ROOT_ID
  }, [selectedId, nodes])

  const targetName = useMemo(() => {
    if (selectedContainerId === ROOT_ID) return '下载根目录'
    const node = findNode(nodes, selectedContainerId)
    if (node?.kind === 'wifi') return 'WiFi 名称'
    if (node?.kind === 'date') return node.token || node.name
    return node?.name || '下载根目录'
  }, [selectedContainerId, nodes])

  const pending = indicators.flatMap((indicator) => outputsFor(workflows.find((item) => item.name === indicator))
    .filter((output) => !usedFiles.has(outputCanonical(indicator, output.key)))
    .map((output) => ({ indicator, output })))

  const addAllPending = (): void => {
    setNodes((prev) => pending.reduce(
      (items, { indicator, output }) => insertChild(items, selectedContainerId, makeFile(indicator, output.key, output.label)),
      prev
    ))
  }

  /** 左边已放入的文件点一下，在右边树里滚过去并闪一下 */
  const locateFile = (canonical: string): void => {
    const target = findFileNode(nodes, canonical)
    if (!target) return
    setFlashId(target.id)
    document.querySelector(`[data-node-id="${target.id}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    window.setTimeout(() => setFlashId((current) => (current === target.id ? null : current)), 1400)
  }

  const addNode = (parentId: string, node: PathNode): void => {
    if (node.kind === 'file') {
      if (!node.indicator) return
      const canonical = outputCanonical(node.indicator, node.outputKey || '')
      if (usedFiles.has(canonical)) {
        message.warning('这个文件已经在结构里了')
        return
      }
    }
    if (node.kind === 'wifi' && wifiUsed) {
      message.warning('WiFi 标签只需要一个；要换位置直接拖它')
      return
    }
    setNodes((prev) => insertChild(prev, parentId, node))
    if (node.kind !== 'file') {
      setSelectedId(node.id)
    }
    if (node.kind === 'folder') {
      setEditingId(node.id)
      setDraftName(node.name)
    }
  }

  const uniqueFolderName = (parentId: string): string => {
    const siblings = parentId === ROOT_ID
      ? nodes
      : findNode(nodes, parentId)?.children || []
    const names = new Set(
      siblings.filter((item) => item.kind === 'folder').map((item) => item.name)
    )
    let name = '新文件夹'
    let index = 2
    while (names.has(name)) {
      name = `新文件夹${index}`
      index += 1
    }
    return name
  }

  const addChildFolder = (parentId: string): void => {
    addNode(parentId, makeFolder(uniqueFolderName(parentId)))
  }

  const batchIds = useMemo(() => topLevelIds(nodes, checked), [nodes, checked])
  const moveTargets = useMemo(() => [
    { value: ROOT_ID, label: '下载根目录' },
    ...containerOptions(nodes, new Set(batchIds))
  ], [nodes, batchIds])

  const toggleChecked = (id: string): void => {
    setChecked((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))
  }

  const moveChecked = (targetId: string): void => {
    setNodes((prev) => moveNodes(prev, batchIds, targetId))
    setChecked([])
    setSelectedId(targetId)
  }

  /** 把勾选的打包进一个新文件夹，新文件夹放在第一项原来的位置下 */
  const wrapChecked = (): void => {
    if (batchIds.length === 0) return
    const parentId = parentIdOf(nodes, batchIds[0]) || ROOT_ID
    const folder = makeFolder(uniqueFolderName(parentId))
    setNodes((prev) => moveNodes(insertChild(prev, parentId, folder), batchIds, folder.id))
    setChecked([])
    setSelectedId(folder.id)
    setEditingId(folder.id)
    setDraftName(folder.name)
  }

  const removeChecked = (): void => {
    const targets = batchIds.map((id) => findNode(nodes, id)).filter((node): node is PathNode => Boolean(node))
    const remove = (): void => {
      setNodes((prev) => batchIds.reduce((items, id) => removeNode(items, id), prev))
      if (targets.some((node) => containsNode(node, selectedId))) setSelectedId(ROOT_ID)
      setChecked([])
    }
    if (targets.every((node) => node.kind === 'file' || node.children.length === 0)) {
      remove()
      return
    }
    modal.confirm({
      title: `移出选中的 ${targets.length} 项？`,
      content: '文件夹下面的标签也会一起移出，文件会回到左边「可用标签」里。',
      okText: '移出',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: remove
    })
  }

  /** 拖的是勾选中的一项时，整批一起拖 */
  const treePayload = (id: string): TreePayload => (
    checked.includes(id) && batchIds.length > 1 ? { source: 'tree', id, ids: batchIds } : { source: 'tree', id }
  )

  const renderCheck = (node: PathNode): JSX.Element => (
    // 包一层拦住点击，勾选不顺带「选中文件夹」
    <span className="sp-check" onClick={(event) => event.stopPropagation()}>
      <Checkbox checked={checked.includes(node.id)} onChange={() => toggleChecked(node.id)} />
    </span>
  )

  const makeFromPayload = (payload: PalettePayload): PathNode | null => {
    if (payload.kind === 'wifi') return makeWifi()
    if (payload.kind === 'date' && payload.token) return makeDate(payload.token)
    if (payload.kind === 'folder') return makeFolder()
    if (payload.kind === 'file' && payload.indicator) {
      return makeFile(payload.indicator, payload.outputKey || '', payload.outputLabel || '')
    }
    return null
  }

  const addFromPayload = (payload: PalettePayload): void => {
    const node = makeFromPayload(payload)
    if (node) addNode(selectedContainerId, node)
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>, targetId: string): void => {
    event.preventDefault()
    event.stopPropagation()
    const payload = readDragData(event)
    setDragOver(null)
    if (!payload) return
    if (payload.source === 'tree') {
      if (payload.ids?.length) {
        setNodes((prev) => moveNodes(prev, payload.ids || [], targetId))
        setChecked([])
      } else {
        setNodes((prev) => moveNode(prev, payload.id, targetId))
      }
      return
    }
    const node = makeFromPayload(payload)
    if (node) addNode(targetId, node)
  }

  const handleDelete = (node: PathNode): void => {
    const remove = (): void => {
      setNodes((prev) => removeNode(prev, node.id))
      if (selectedId === node.id || containsNode(node, selectedId)) setSelectedId(ROOT_ID)
    }
    if (node.children.length === 0) {
      remove()
      return
    }
    modal.confirm({
      title: `删除“${node.kind === 'folder'
        ? node.name
        : node.kind === 'wifi'
          ? 'WiFi 名称'
          : node.token}”？`,
      content: '它下面的标签也会一起删除，之后可以重新从左边拖进来。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: remove
    })
  }

  const startRename = (node: PathNode): void => {
    if (node.kind !== 'folder') return
    setEditingId(node.id)
    setDraftName(node.name)
  }

  const finishRename = (save: boolean): void => {
    if (!editingId) return
    if (!save) {
      setEditingId(null)
      return
    }
    const name = draftName.trim()
    if (!name || /[\[\]{}\/\\\n]/.test(name)) {
      message.error('文件夹名不能为空，不能含 [ ] { } / \\')
      return
    }
    setNodes((prev) => updateNode(prev, editingId, (node) => ({ ...node, name })))
    setEditingId(null)
  }

  const renderPaletteOutput = (
    indicator: string,
    output: WorkflowOutput
  ): JSX.Element => {
    const canonical = outputCanonical(indicator, output.key)
    const used = usedFiles.has(canonical)
    const windowLabel = windowLabelFor(indicator)
    const label = outputBaseName(output.label, windowLabel)
    const fullLabel = outputRelative(output.label, windowLabel)
    const parent = outputParent(output.label, windowLabel)
    const source = indicatorLabel(indicator, workflows)
    const duplicate = preview.conflicts.includes(canonical)
    const payload: PalettePayload = {
      source: 'palette',
      kind: 'file',
      indicator,
      outputKey: output.key,
      outputLabel: output.label
    }
    return (
      <Tooltip
        key={canonical}
        placement="right"
        title={used
          ? '已在结构里，点击定位'
          : duplicate
            ? `同名文件 ${output.label} 会和另一个落到同一目录、互相覆盖；把它们放进不同文件夹`
            : `${source}：${fullLabel}`}
      >
        <div
          className={`sp-chip${used ? ' is-used' : ''}${duplicate ? ' is-conflict' : ''}`}
          draggable={!used}
          onDragStart={(event) => {
            if (used) return
            event.stopPropagation()
            writeDragData(event, payload)
          }}
          onClick={() => (used ? locateFile(canonical) : addFromPayload(payload))}
        >
          {used ? <CheckOutlined className="sp-chip-icon" /> : <PlusOutlined className="sp-chip-icon" />}
          <span className="sp-chip-text">{parent ? `[${parent}] ` : ''}{label}</span>
          {output.duplicate && <span className="sp-pos">{output.position || output.key}</span>}
        </div>
      </Tooltip>
    )
  }

  const renderPaletteNode = (
    payload: PalettePayload,
    label: string,
    color: string,
    disabled = false,
    tooltip?: string
  ): JSX.Element => {
    const tag = (
      <span
        key={label}
        className={`sp-token${disabled ? ' is-disabled' : ''}`}
        draggable={!disabled}
        onDragStart={(event) => {
          if (disabled) return
          event.stopPropagation()
          writeDragData(event, payload)
        }}
        onClick={() => {
          if (!disabled) addFromPayload(payload)
        }}
      >
        <Tag color={color} bordered={false}>{label}</Tag>
      </span>
    )
    return tooltip ? <Tooltip key={label} title={tooltip}>{tag}</Tooltip> : tag
  }

  const renderFileNode = (node: PathNode): JSX.Element => {
    const source = indicatorLabel(node.indicator || '', workflows)
    const windowLabel = windowLabelFor(node.indicator || '')
    const rawLabel = nodeOutputLabel(node, workflows)
    const label = outputBaseName(rawLabel, windowLabel)
    const fullLabel = previewFileName(rawLabel, windowLabel)
    const parent = outputParent(rawLabel, windowLabel)
    const output = findOutput(workflows, node.indicator || '', node.outputKey || '')
    const position = output?.duplicate ? output.position || output.key : ''
    const invalid = isInvalidFile(node, preview.invalid)
    const duplicate = preview.conflicts.includes(outputCanonical(node.indicator || '', node.outputKey || ''))
    const problem = invalid
      ? '已失效：任务里找不到这个文件（指标已移除或 save_as 已改名），运行时会落到基础目录；删掉后重新从左边拖'
      : duplicate
        ? '和同名文件落在同一个目录，会互相覆盖；把它们拖到不同文件夹，或在工作流里改 save_as'
        : ''
    return (
      <div
        key={node.id}
        data-node-id={node.id}
        className={`sp-file${problem ? ' is-problem' : ''}${flashId === node.id ? ' is-flash' : ''}${checked.includes(node.id) ? ' is-checked' : ''}`}
        draggable
        onDragStart={(event) => {
          event.stopPropagation()
          writeDragData(event, treePayload(node.id))
        }}
        onClick={(event) => {
          event.stopPropagation()
          if (event.metaKey || event.ctrlKey) toggleChecked(node.id)
        }}
      >
        {renderCheck(node)}
        <Tooltip title={problem || `${source}：${fullLabel}；拖动可换位置`} placement="topLeft">
          <span className="sp-file-main">
            <FileTextOutlined className="sp-file-icon" />
            <span className="sp-file-name">{parent ? `[${parent}] ` : ''}{label}</span>
            <span className="sp-file-source">{source}</span>
            {position && <span className="sp-pos">{position}</span>}
            {invalid && <Tag color="error" bordered={false}>已失效</Tag>}
            {!invalid && duplicate && <Tag color="error" bordered={false}>同目录重名</Tag>}
          </span>
        </Tooltip>
        <span className="sp-actions">
          <Tooltip title="移出结构">
            <Button
              size="small"
              type="text"
              icon={<CloseOutlined />}
              onClick={(event) => {
                event.stopPropagation()
                handleDelete(node)
              }}
            />
          </Tooltip>
        </span>
      </div>
    )
  }

  const renderNode = (node: PathNode): JSX.Element => {
    if (node.kind === 'file') {
      return renderFileNode(node)
    }

    const isSelected = selectedId === node.id
    const isOver = dragOver === node.id
    const color = node.kind === 'wifi' ? 'green' : 'purple'
    const files = countFiles(node.children)
    const tooltip = node.kind === 'wifi'
      ? `执行时换成当前 WiFi：${ssid || '未连接WiFi'}；可以删掉，也可以拖到任意位置`
      : node.kind === 'date'
        ? TOKEN_HELP[node.token || node.name] || '按执行日期生成'
        : '双击改名；可以拖到别的文件夹里'

    return (
      <div
        key={node.id}
        data-node-id={node.id}
        className={`sp-node${isOver ? ' is-over' : ''}`}
        onDragOver={(event) => {
          event.preventDefault()
          event.stopPropagation()
          setDragOver(node.id)
        }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragOver(null)
        }}
        onDrop={(event) => handleDrop(event, node.id)}
      >
        <div
          className={`sp-row${isSelected ? ' is-selected' : ''}${checked.includes(node.id) ? ' is-checked' : ''}`}
          onClick={(event) => {
            event.stopPropagation()
            if (event.metaKey || event.ctrlKey) {
              toggleChecked(node.id)
              return
            }
            setSelectedId(node.id)
          }}
        >
          {renderCheck(node)}
          {editingId === node.id ? (
            <Input
              size="small"
              autoFocus
              value={draftName}
              style={{ width: 180 }}
              onClick={(event) => event.stopPropagation()}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setDraftName(event.target.value)}
              onPressEnter={() => finishRename(true)}
              onBlur={() => finishRename(true)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') finishRename(false)
              }}
            />
          ) : (
            <Tooltip title={tooltip} mouseEnterDelay={0.6}>
              <span
                className="sp-row-main"
                draggable
                onDragStart={(event) => {
                  event.stopPropagation()
                  writeDragData(event, treePayload(node.id))
                }}
                onDoubleClick={() => startRename(node)}
              >
                {node.kind === 'folder' ? (
                  <>
                    {isSelected ? <FolderOpenOutlined className="sp-folder-icon" /> : <FolderOutlined className="sp-folder-icon" />}
                    <span className="sp-folder-name">{node.name}</span>
                  </>
                ) : (
                  <Tag
                    color={color}
                    bordered={false}
                    icon={node.kind === 'wifi' ? <WifiOutlined /> : <CalendarOutlined />}
                  >
                    {node.kind === 'wifi' ? 'WiFi 名称' : node.token || node.name}
                    {node.kind === 'wifi' && <span className="sp-ssid">{ssid || '未连接WiFi'}</span>}
                  </Tag>
                )}
              </span>
            </Tooltip>
          )}
          {files > 0 && <span className="sp-count">{files}</span>}
          <span className="sp-actions">
            {node.kind === 'folder' && (
              <Tooltip title="改名">
                <Button
                  size="small"
                  type="text"
                  icon={<EditOutlined />}
                  onClick={(event) => {
                    event.stopPropagation()
                    startRename(node)
                  }}
                />
              </Tooltip>
            )}
            <Tooltip title="新建子文件夹">
              <Button
                size="small"
                type="text"
                icon={<FolderAddOutlined />}
                onClick={(event) => {
                  event.stopPropagation()
                  addChildFolder(node.id)
                }}
              />
            </Tooltip>
            <Tooltip title="删除">
              <Button
                size="small"
                type="text"
                icon={<CloseOutlined />}
                onClick={(event) => {
                  event.stopPropagation()
                  handleDelete(node)
                }}
              />
            </Tooltip>
          </span>
        </div>
        <div className="sp-children">
          {node.children.map((child) => renderNode(child))}
          {node.children.length === 0 && <div className="sp-empty">拖文件或标签到这里</div>}
        </div>
      </div>
    )
  }

  const saveDisabled = checking || Boolean(preview.error) || fileCount === 0

  const body = (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {startedEmpty && indicators.some((indicator) => outputsFor(workflows.find((item) => item.name === indicator))
          .some((output) => /[\\/]/.test(output.save_as))) && (
          <Alert type="info" showIcon message="工作流 save_as 里写了目录（运行时已忽略），已按它回显成文件夹结构。保存后由这里控制落盘，再把工作流里的 save_as 改成纯文件名。" />
        )}
        <div className="sp-root">
          <FolderOpenOutlined />
          <span>根目录</span>
          <span className="sp-root-path" title={root}>{root || '系统默认下载目录'}</span>
          <span className="sp-root-hint">在设置里修改</span>
        </div>

        <div className="sp-layout">
          <div className="sp-palette">
            <div className="sp-panel-head">
              <span>可用标签</span>
              <span className="sp-panel-hint">点击放进「{targetName}」，或拖到任意文件夹</span>
            </div>

            <div className="sp-section">位置</div>
            <div className="sp-tokens">
              {renderPaletteNode(
                { source: 'palette', kind: 'wifi' },
                'WiFi 名称',
                'green',
                wifiUsed,
                wifiUsed ? '结构里已经有 WiFi 了，要换位置直接拖它' : `执行时换成当前 WiFi：${ssid || '未连接WiFi'}；不需要可以不加`
              )}
              {DATE_TOKENS.map((token) => renderPaletteNode(
                { source: 'palette', kind: 'date', token },
                token,
                'purple',
                false,
                TOKEN_HELP[token]
              ))}
              {renderPaletteNode(
                { source: 'palette', kind: 'folder' },
                '新文件夹',
                'blue',
                false,
                '放进结构后双击改名；文件夹可以套文件夹'
              )}
            </div>

            <div className="sp-section">
              <span>文件</span>
              {pending.length > 0 ? (
                <Button type="link" size="small" onClick={addAllPending}>
                  剩下 {pending.length} 个全部放入
                </Button>
              ) : indicators.length > 0 && <span className="sp-section-done"><CheckOutlined /> 都已放入</span>}
            </div>
            {indicators.map((indicator) => {
              const workflow = workflows.find((item) => item.name === indicator)
              const outputs = outputsFor(workflow)
              const placed = outputs.filter((output) => usedFiles.has(outputCanonical(indicator, output.key))).length
              return (
                <div key={indicator} className="sp-group">
                  <div className="sp-group-title" title={workflow?.label || indicator}>
                    <span>{workflow?.label || indicator}</span>
                    <span className={placed === outputs.length ? 'is-done' : ''}>{placed}/{outputs.length}</span>
                  </div>
                  {outputs.map((output) => renderPaletteOutput(indicator, output))}
                </div>
              )
            })}
            {indicators.length === 0 && (
              <div className="sp-empty">先在「基本信息」里选择采集指标</div>
            )}
          </div>

          <div className={`sp-tree${batchIds.length > 0 ? ' is-picking' : ''}`}>
            {batchIds.length > 0 && (
              <div className="sp-batch">
                <span className="sp-batch-count">已选 {batchIds.length} 项</span>
                <Select
                  size="small"
                  showSearch
                  value={null}
                  placeholder="移到文件夹…"
                  optionFilterProp="label"
                  options={moveTargets}
                  style={{ width: 220 }}
                  popupMatchSelectWidth={false}
                  onChange={(value: string) => moveChecked(value)}
                />
                <Button size="small" icon={<FolderAddOutlined />} onClick={wrapChecked}>放进新文件夹</Button>
                <Button size="small" type="text" danger onClick={removeChecked}>移出结构</Button>
                <Button size="small" type="text" className="sp-batch-cancel" onClick={() => setChecked([])}>取消</Button>
              </div>
            )}
            <div
              className={`sp-tree-root${dragOver === ROOT_ID ? ' is-over' : ''}`}
              onClick={() => setSelectedId(ROOT_ID)}
              onDragOver={(event) => {
                event.preventDefault()
                setDragOver(ROOT_ID)
              }}
              onDragLeave={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragOver(null)
              }}
              onDrop={(event) => handleDrop(event, ROOT_ID)}
            >
              <div className={`sp-row is-root${selectedId === ROOT_ID ? ' is-selected' : ''}`}>
                <span className="sp-row-main">
                  <FolderOpenOutlined className="sp-folder-icon" />
                  <span className="sp-folder-name">下载根目录</span>
                </span>
                <span className="sp-count">{fileCount} 个文件</span>
                {batchIds.length === 0 && <span className="sp-root-tip">勾选或 Ctrl/⌘ 点击可多选，批量移动</span>}
                <Button
                  size="small"
                  type="text"
                  icon={<FolderAddOutlined />}
                  className="sp-root-add"
                  onClick={(event) => {
                    event.stopPropagation()
                    addChildFolder(ROOT_ID)
                  }}
                >
                  新建文件夹
                </Button>
              </div>
              <div className="sp-children">
                {nodes.map((node) => renderNode(node))}
                {nodes.length === 0 && (
                  <div className="sp-empty">
                    从左边把 WiFi、日期、文件夹或文件拖进来，或点上面的「新建文件夹」
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>

        {preview.error && <Alert type="error" showIcon message={preview.error} />}
        {invalidIds.length > 0 && (
          <Alert
            type="error"
            showIcon
            message={`有 ${invalidIds.length} 个文件标签已失效（指标已移除或 save_as 已改名），运行时这些文件会落到基础目录。`}
            action={<Button size="small" danger onClick={removeInvalid}>一键删除</Button>}
          />
        )}
        {outputIssues.duplicated.length > 0 && (
          <Alert
            type="error"
            showIcon
            message={`同名文件落在同一个目录，会互相覆盖：${outputIssues.duplicated.join('、')}。把它们放进不同文件夹，或在工作流里改 save_as。`}
          />
        )}
        {outputIssues.needName.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="有重名导出，只能靠步骤位置区分"
            description={`${outputIssues.needName.join('；')}。建议在工作流的导出步骤上补 name（如 name: 日前），增删步骤后才不会对不上。`}
          />
        )}
        {outputIssues.unnamed.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`未写 save_as：${outputIssues.unnamed.join('、')}。这些文件按步骤位置识别，工作流增删步骤后可能对不上；建议在工作流里补上文件名。`}
          />
        )}

        {preview.unassigned.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`未放进结构：${preview.unassigned
              .map((item) => describeOutput(item.indicator, item.label))
              .join('、')}。这些文件会直接放在基础目录。`}
          />
        )}
      </Space>
  )

  if (embedded) return body

  return (
    <Modal
      title="存储结构"
      open={open}
      onCancel={onCancel}
      width={1060}
      destroyOnClose
      styles={{ body: { maxHeight: '80vh', overflowY: 'auto' } }}
      okText="保存"
      cancelText="取消"
      okButtonProps={{ disabled: saveDisabled }}
      onOk={() => onSave?.(structureText)}
    >
      {body}
    </Modal>
  )
}
