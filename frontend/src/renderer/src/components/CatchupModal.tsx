import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, App as AntApp, Button, Input, Modal, Segmented, Select, Space, Steps, Tag, Tooltip, Typography } from 'antd'
import { FolderOpenOutlined } from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import { settingsStore, type WorkflowInfo, type WorkflowGroup, type WorkflowOutput } from '../services/api'
import IndicatorTreeSelect from './IndicatorTreeSelect'
import { workflowPath } from './workflowTree'
import { formatDOffset, formatMOffset, formatWOffset } from './DateWindowSlider'
import { OutputName, OutputPanel } from './OutputPanel'
import StoragePathModal, { type PathFolder } from './StoragePathModal'
import type { CustomFileNames } from './customFilename'
import { TASK_EDITOR_STEPS } from './taskEditorSteps'
import TaskUploadFields from './TaskUploadFields'
import CatchupPeriodPicker from './CatchupPeriodPicker'
import {
  CATCHUP_LIMITS,
  CATCHUP_SOFT_WARN_DAYS,
  DEFAULT_PREVIEW_SEGMENTS,
  clonePeriodSelection,
  emptyPeriodSelection,
  formatPeriodSegment,
  formatPeriodSummary,
  formatRangeLabel,
  groupConsecutivePeriods,
  groupOutputPreviews,
  groupPreviewRows,
  periodKindLabel,
  periodUnitLabel,
  resolveCustomIso,
  selectionCount,
  selectionExceedsLimit,
  toIsoList,
  weekMonday,
  type OutputPreviewEntry,
  type PeriodKind,
  type PeriodSegment,
  type PeriodSelection,
  type PreviewItemInput
} from './catchupDates'

const { Text } = Typography
const EMPTY_SELECTION: PeriodSelection = emptyPeriodSelection('range')
const PERIOD_KINDS: PeriodKind[] = ['day', 'week', 'month']

/** 用来给补数默认存储路径的任务摘要。字段与任务卡片一致，多出来的字段可以一起传入。 */
export interface CatchupTaskHint {
  name: string
  indicators: string[]
  enabled: boolean
  createdAt: number
  pathTemplate?: string
  pathFolders?: PathFolder[]
  pathStructure?: string
  customFilenames?: CustomFileNames
  /** 任务单独指定的下载目录；空表示用全局设置 */
  downloadDir?: string
}

export interface CatchupSelection {
  login: string
  indicators: string[]
  indicatorDates: Record<string, string[]>
  indicatorMonths: Record<string, string[]>
  /** 单个导出另选的日期，键是 指标::输出key；空列表 = 不补 */
  outputDates: Record<string, string[]>
  uploadProfileId?: string
  autoUpload?: boolean
  /** 这次补数的下载根目录 */
  downloadDir: string
  pathTemplate: string
  pathFolders: PathFolder[]
  pathStructure: string
  customFilenames: CustomFileNames
}

interface StorageOverride {
  /** 用户改过存储树。序列化结果和任务原文不同也算，这样提交的是树上看到的结构。 */
  structureEdited: boolean
  structure: string
  /** null 表示下载目录仍跟随任务 / 设置 */
  downloadDir: string | null
  namesEdited?: boolean
  customFilenames?: CustomFileNames
}

type OutputMode = 'follow' | 'own' | 'skip'

const OUTPUT_MODES: { label: string; value: OutputMode }[] = [
  { label: '跟随', value: 'follow' },
  { label: '另选', value: 'own' },
  { label: '不补', value: 'skip' }
]

function periodKindOf(item: WorkflowInfo): PeriodKind {
  if (item.date_kind === 'month') return 'month'
  if (item.date_kind === 'week') return 'week'
  return 'day'
}

/** 按日指标里落在日期循环里、能单独选日期的导出；只有一个时不必另选 */
function ownableOutputs(item: WorkflowInfo): WorkflowOutput[] {
  if (item.date_kind === 'month' || item.date_kind === 'week') return []
  const outputs = item.outputs || []
  return outputs.length > 1 ? outputs.filter((output) => output.in_loop) : []
}

/**
 * 与后端 match_task_storage 同一套优先级：覆盖的指标越多越优先，其次已启用，再其次最近创建。
 */
export function matchCatchupTask(indicators: string[], tasks: CatchupTaskHint[]): CatchupTaskHint | null {
  const wanted = indicators.map((item) => item.trim()).filter(Boolean)
  if (!wanted.length) return null
  let best: CatchupTaskHint | null = null
  let bestKey: [number, number, number] | null = null
  for (const task of tasks) {
    const owned = new Set((task.indicators || []).map((item) => item.trim()).filter(Boolean))
    const covered = wanted.filter((name) => owned.has(name)).length
    if (covered <= 0) continue
    const key: [number, number, number] = [covered, task.enabled ? 1 : 0, task.createdAt || 0]
    const better = !bestKey
      || key[0] > bestKey[0]
      || (key[0] === bestKey[0] && key[1] > bestKey[1])
      || (key[0] === bestKey[0] && key[1] === bestKey[1] && key[2] > bestKey[2])
    if (better) {
      best = task
      bestKey = key
    }
  }
  return best
}

interface Props {
  open: boolean
  workflows: WorkflowInfo[]
  groups: WorkflowGroup[]
  /** 已保存的任务，用来默认下载路径和存储结构 */
  tasks?: CatchupTaskHint[]
  defaultLogin: string
  onCancel: () => void
  onRun: (selection: CatchupSelection) => void
}

function defaultWindow(item: WorkflowInfo): [number, number] {
  const month = item.date_kind === 'month'
  const week = item.date_kind === 'week'
  const min = month ? -12 : week ? -52 : -31
  const max = month ? 0 : week ? 12 : 14
  const normalize = (value: number | undefined): number =>
    Number.isFinite(value) ? Math.max(min, Math.min(max, Math.trunc(value!))) : week ? 0 : -1
  const left = normalize(item.window_start)
  const right = normalize(item.window_end)
  return [Math.min(left, right), Math.max(left, right)]
}

function defaultPeriods(item: WorkflowInfo, reference: Dayjs): Dayjs[] {
  const month = item.date_kind === 'month'
  const week = item.date_kind === 'week'
  const [start, end] = defaultWindow(item)
  const unit = month ? 'month' : week ? 'week' : 'day'
  const anchor = month ? reference.startOf('month') : week ? weekMonday(reference) : reference
  return Array.from({ length: end - start + 1 }, (_, index) => anchor.add(start + index, unit))
}

function describeDefault(item: WorkflowInfo, values: string[]): string {
  const kind = periodKindOf(item)
  const [start, end] = defaultWindow(item)
  const format = kind === 'month' ? formatMOffset : kind === 'week' ? formatWOffset : formatDOffset
  const offset = start === end ? format(start) : `${format(start)} ～ ${format(end)}`
  const dates = values.length === 1 ? values[0] : `${values[0]} ～ ${values[values.length - 1]}`
  return `${offset}（${dates}，共 ${values.length} ${periodUnitLabel(kind)}）`
}

function describeOverflow(sel: PeriodSelection, kind: PeriodKind): string {
  const count = selectionCount(sel, kind)
  const limit = CATCHUP_LIMITS[kind]
  const unit = periodUnitLabel(kind)
  const span = sel.mode === 'range' ? formatRangeLabel(sel.range, kind) : ''
  return span
    ? `${span}（已选 ${count} ${unit}，超过上限 ${limit} ${unit}）`
    : `已选 ${count} ${unit}，超过上限 ${limit} ${unit}`
}

export default function CatchupModal({
  open,
  workflows,
  groups,
  tasks = [],
  defaultLogin,
  onCancel,
  onRun
}: Props): JSX.Element {
  const { message } = AntApp.useApp()
  const [step, setStep] = useState(0)
  const [login, setLogin] = useState(defaultLogin)
  const [indicators, setIndicators] = useState<string[]>([])
  const [selectionsByIndicator, setSelectionsByIndicator] = useState<Record<string, PeriodSelection>>({})
  const [outputModes, setOutputModes] = useState<Record<string, OutputMode>>({})
  const [outputSelections, setOutputSelections] = useState<Record<string, PeriodSelection>>({})
  const [batchDrafts, setBatchDrafts] = useState<Record<PeriodKind, PeriodSelection>>(() => ({
    day: emptyPeriodSelection('range'),
    week: emptyPeriodSelection('range'),
    month: emptyPeriodSelection('range')
  }))
  const [batchKind, setBatchKind] = useState<PeriodKind>('day')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [mergeSamePreview, setMergeSamePreview] = useState(true)
  const [expandedPreviewRows, setExpandedPreviewRows] = useState<Record<string, boolean>>({})
  const [referenceDate] = useState(() => dayjs().startOf('day'))
  const [uploadProfileId, setUploadProfileId] = useState('')
  const [autoUpload, setAutoUpload] = useState(false)
  const [settingsDir, setSettingsDir] = useState(() => settingsStore.get('downloadDir', ''))
  const [override, setOverride] = useState<StorageOverride | null>(null)
  const [structureBlocked, setStructureBlocked] = useState(false)
  const [storageNonce, setStorageNonce] = useState(0)
  // 打开存储步骤时的第一帧是组件自己的回显，不能当成用户改过
  const structureBaseline = useRef<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    settingsStore.load().then((all) => {
      if (!cancelled) setSettingsDir(String(all.downloadDir || ''))
    }).catch(() => {})
    return () => {
      cancelled = true
    }
  }, [open])

  const suggested = useMemo(() => {
    const task = matchCatchupTask(indicators, tasks)
    return {
      task,
      template: task?.pathTemplate || '',
      folders: task?.pathFolders || [],
      structure: task?.pathStructure || '',
      customFilenames: task?.customFilenames || {},
      downloadDir: (task?.downloadDir || '').trim() || settingsDir
    }
  }, [indicators, tasks, settingsDir])
  const downloadDir = override?.downloadDir ?? suggested.downloadDir
  const modalStructure = override?.structureEdited ? override.structure : suggested.structure
  const customFilenames = override?.namesEdited
    ? (override.customFilenames || {})
    : suggested.customFilenames

  useEffect(() => {
    if (step !== 2) structureBaseline.current = null
  }, [step])

  const selected = useMemo(
    () => indicators
      .map((name) => workflows.find((item) => item.name === name))
      .filter(Boolean) as WorkflowInfo[],
    [indicators, workflows]
  )

  const byKind = useMemo(() => {
    const grouped: Record<PeriodKind, WorkflowInfo[]> = { day: [], week: [], month: [] }
    for (const item of selected) grouped[periodKindOf(item)].push(item)
    return grouped
  }, [selected])

  const dayAndWeekIndicators = useMemo(
    () => selected.filter((item) => item.date_kind !== 'month'),
    [selected]
  )
  const monthIndicators = byKind.month
  const batchKinds = useMemo(
    () => PERIOD_KINDS.filter((kind) => byKind[kind].length >= 2),
    [byKind]
  )
  const activeBatchKind: PeriodKind | null = batchKinds.includes(batchKind)
    ? batchKind
    : (batchKinds[0] ?? null)

  const defaults = useMemo(
    () => Object.fromEntries(
      selected.map((item) => [item.name, defaultPeriods(item, referenceDate)])
    ) as Record<string, Dayjs[]>,
    [selected, referenceDate]
  )

  const indicatorDates = useMemo(() => {
    const next: Record<string, string[]> = {}
    for (const item of dayAndWeekIndicators) {
      const kind = periodKindOf(item)
      const sel = selectionsByIndicator[item.name]
      const custom = selectionCount(sel, kind) > 0
      next[item.name] = custom
        ? resolveCustomIso(sel, kind)
        : toIsoList(defaults[item.name], kind)
    }
    return next
  }, [dayAndWeekIndicators, selectionsByIndicator, defaults])

  const indicatorMonths = useMemo(() => {
    const next: Record<string, string[]> = {}
    for (const item of monthIndicators) {
      const sel = selectionsByIndicator[item.name]
      const custom = selectionCount(sel, 'month') > 0
      next[item.name] = custom
        ? resolveCustomIso(sel, 'month')
        : toIsoList(defaults[item.name], 'month')
    }
    return next
  }, [monthIndicators, selectionsByIndicator, defaults])

  const outputDates = useMemo(() => {
    const next: Record<string, string[]> = {}
    for (const item of byKind.day) {
      for (const output of ownableOutputs(item)) {
        const key = `${item.name}::${output.key}`
        const mode = outputModes[key] || 'follow'
        if (mode === 'skip') next[key] = []
        else if (mode === 'own') next[key] = resolveCustomIso(outputSelections[key], 'day')
      }
    }
    return next
  }, [byKind.day, outputModes, outputSelections])

  const hasOverLimit = useMemo(() => {
    for (const item of selected) {
      const kind = periodKindOf(item)
      if (selectionExceedsLimit(selectionsByIndicator[item.name], kind)) return true
    }
    for (const item of byKind.day) {
      for (const output of ownableOutputs(item)) {
        const key = `${item.name}::${output.key}`
        if (outputModes[key] === 'own' && selectionExceedsLimit(outputSelections[key], 'day')) {
          return true
        }
      }
    }
    return false
  }, [selected, byKind.day, selectionsByIndicator, outputModes, outputSelections])

  const indicatorsReady = selected.every((item) => {
    const kind = periodKindOf(item)
    const sel = selectionsByIndicator[item.name]
    if (selectionExceedsLimit(sel, kind)) return false
    const values = kind === 'month' ? indicatorMonths[item.name] : indicatorDates[item.name]
    return values.length > 0 && values.length <= CATCHUP_LIMITS[kind]
  })

  const outputsReady = useMemo(() => {
    for (const item of byKind.day) {
      for (const output of ownableOutputs(item)) {
        const key = `${item.name}::${output.key}`
        if (outputModes[key] !== 'own') continue
        const count = selectionCount(outputSelections[key], 'day')
        if (count === 0 || count > CATCHUP_LIMITS.day) return false
      }
    }
    return true
  }, [byKind.day, outputModes, outputSelections])

  const longSpanStats = useMemo(() => {
    let maxDays = 0
    let warnItems = 0
    let totalDayRuns = 0
    for (const item of byKind.day) {
      const sel = selectionsByIndicator[item.name]
      const indOver = selectionExceedsLimit(sel, 'day')
      const indDates = indOver ? [] : (indicatorDates[item.name] || [])
      const outputs = ownableOutputs(item)
      let effectiveDays = indDates.length
      if (outputs.length > 0) {
        const union = new Set<string>()
        for (const output of outputs) {
          const key = `${item.name}::${output.key}`
          const mode = outputModes[key] || 'follow'
          if (mode === 'skip') continue
          if (mode === 'own') {
            if (!selectionExceedsLimit(outputSelections[key], 'day')) {
              const outList = outputDates[key] || []
              for (const day of outList) union.add(day)
              if (outList.length > CATCHUP_SOFT_WARN_DAYS) {
                warnItems += 1
                maxDays = Math.max(maxDays, outList.length)
              }
            }
          } else {
            for (const day of indDates) union.add(day)
          }
        }
        effectiveDays = union.size
      }
      totalDayRuns += effectiveDays
      if (effectiveDays > CATCHUP_SOFT_WARN_DAYS) {
        warnItems += 1
        maxDays = Math.max(maxDays, effectiveDays)
      }
    }
    return { maxDays, warnItems, totalDayRuns }
  }, [byKind.day, selectionsByIndicator, indicatorDates, outputModes, outputSelections, outputDates])

  const previewItems = useMemo(() => {
    interface RichPreviewItem extends PreviewItemInput {
      item: WorkflowInfo
      selection: PeriodSelection
      segments: PeriodSegment[]
      ownTexts: string[]
    }
    return selected.map((item): RichPreviewItem => {
      const kind = periodKindOf(item)
      const sel = selectionsByIndicator[item.name] || EMPTY_SELECTION
      const isDefault = selectionCount(sel, kind) === 0
      const overLimit = selectionExceedsLimit(sel, kind)
      const values = overLimit
        ? []
        : (kind === 'month' ? indicatorMonths[item.name] : indicatorDates[item.name]) || []
      const segments = overLimit ? [] : groupConsecutivePeriods(values, kind)
      const ownEntries: OutputPreviewEntry[] = kind !== 'day'
        ? []
        : ownableOutputs(item)
          .filter((output) => (outputModes[`${item.name}::${output.key}`] || 'follow') !== 'follow')
          .map((output) => {
            const key = `${item.name}::${output.key}`
            const mode = outputModes[key]
            if (mode === 'skip') {
              return { label: output.label, summary: '不补', signature: 'skip' }
            }
            const outSel = outputSelections[key] || EMPTY_SELECTION
            const outCount = selectionCount(outSel, 'day')
            if (outCount === 0) {
              return { label: output.label, summary: '未选日期', signature: 'empty' }
            }
            if (outCount > CATCHUP_LIMITS.day) {
              return {
                label: output.label,
                summary: `超出上限（${outCount}/${CATCHUP_LIMITS.day} 天）`,
                signature: `over:${outCount}`
              }
            }
            const outDates = outputDates[key] || []
            return {
              label: output.label,
              summary: formatPeriodSummary(outDates, 'day'),
              signature: `own:${outDates.join(',')}`
            }
          })
      const groupedOwn = groupOutputPreviews(ownEntries)
      const ownTexts = groupedOwn.map((group) => `${group.labels.join('、')}：${group.summary}`)
      const ownSignature = ownEntries.map((entry) => `${entry.label}=${entry.signature}`).join(';')
      return {
        id: item.name,
        name: workflowPath(item, groups),
        kind,
        isDefault,
        overLimit,
        values,
        ownSignature,
        item,
        selection: sel,
        segments,
        ownTexts
      }
    })
  }, [selected, groups, selectionsByIndicator, indicatorDates, indicatorMonths, outputModes, outputSelections, outputDates])

  const groupedPreviewRows = useMemo(() => groupPreviewRows(previewItems), [previewItems])
  const canMergePreview = groupedPreviewRows.length < previewItems.length
  const visiblePreviewRows = canMergePreview && mergeSamePreview
    ? groupedPreviewRows
    : previewItems.map((entry) => ({ key: entry.id, items: [entry] }))

  const previewMetaText = useMemo(() => {
    if (selected.length === 0) return ''
    const parts: string[] = [`共 ${selected.length} 个指标`]
    if (longSpanStats.totalDayRuns > 0) parts.push(`按日 ${longSpanStats.totalDayRuns} 指标日`)
    const weekRuns = byKind.week.reduce((sum, item) => sum + (indicatorDates[item.name]?.length || 0), 0)
    if (weekRuns > 0) parts.push(`按周 ${weekRuns} 指标周`)
    const monthRuns = byKind.month.reduce((sum, item) => sum + (indicatorMonths[item.name]?.length || 0), 0)
    if (monthRuns > 0) parts.push(`按月 ${monthRuns} 指标月`)
    return parts.join(' · ')
  }, [selected.length, longSpanStats.totalDayRuns, byKind.week, byKind.month, indicatorDates, indicatorMonths])

  const datesReady = indicators.length > 0 && !hasOverLimit && indicatorsReady && outputsReady
  const canRun = datesReady && !structureBlocked
  const loginOptions = workflows
    .filter((item) => item.type === 'login')
    .map((item) => ({ label: item.label || item.name, value: item.name }))

  const copyToSameKind = (source: WorkflowInfo): void => {
    const kind = periodKindOf(source)
    const peers = byKind[kind]
    const current = selectionsByIndicator[source.name] || EMPTY_SELECTION
    setSelectionsByIndicator((prev) => {
      const next = { ...prev }
      for (const item of peers) next[item.name] = clonePeriodSelection(current)
      return next
    })
    message.success(`已复制到全部 ${peers.length} 个${periodKindLabel(kind)}指标`)
  }

  const applyBatch = (kind: PeriodKind): void => {
    const draft = batchDrafts[kind]
    const peers = byKind[kind]
    if (!peers.length || selectionCount(draft, kind) === 0 || selectionExceedsLimit(draft, kind)) return
    setSelectionsByIndicator((prev) => {
      const next = { ...prev }
      for (const item of peers) next[item.name] = clonePeriodSelection(draft)
      return next
    })
    message.success(`已应用到 ${peers.length} 个${periodKindLabel(kind)}指标`)
  }

  const clearBatch = (kind: PeriodKind): void => {
    const peers = byKind[kind]
    setSelectionsByIndicator((prev) => {
      const next = { ...prev }
      for (const item of peers) delete next[item.name]
      return next
    })
    setBatchDrafts((prev) => ({ ...prev, [kind]: emptyPeriodSelection(prev[kind].mode) }))
    message.info(`已恢复 ${peers.length} 个${periodKindLabel(kind)}指标为默认时间`)
  }

  const rememberDownload = (dir: string): void => {
    setOverride((prev) => ({
      structureEdited: prev?.structureEdited ?? false,
      structure: prev?.structure ?? '',
      downloadDir: dir,
      namesEdited: prev?.namesEdited,
      customFilenames: prev?.customFilenames
    }))
  }

  const pickDownloadDir = async (): Promise<void> => {
    if (!window.api?.pickDir) {
      message.warning('当前环境不能选择文件夹，请直接填写路径')
      return
    }
    try {
      const picked = await window.api.pickDir()
      if (picked) rememberDownload(picked)
    } catch {
      message.error('选择文件夹失败')
    }
  }

  const restoreStorage = (): void => {
    structureBaseline.current = null
    setOverride(null)
    setStructureBlocked(false)
    setStorageNonce((value) => value + 1)
  }

  const dateValidationMessage = (): string => {
    if (hasOverLimit) return '部分指标或导出超过了单次补数上限，请缩小时间范围'
    if (!outputsReady) return '请为单独选择的导出补上日期，或改成跟随 / 不补'
    return '请检查每个指标的补数日期'
  }

  const goNext = (): void => {
    if (step === 0 && indicators.length === 0) {
      message.warning('请选择要补的指标')
      return
    }
    if (step === 1 && !datesReady) {
      message.warning(dateValidationMessage())
      return
    }
    setStep((current) => Math.min(current + 1, TASK_EDITOR_STEPS.length - 1))
  }

  const submit = (): void => {
    if (!datesReady) {
      message.warning(indicators.length === 0 ? '请选择要补的指标' : dateValidationMessage())
      setStep(indicators.length === 0 ? 0 : 1)
      return
    }
    if (structureBlocked) {
      message.error('存储结构有错误，先在「存储结构」里处理')
      setStep(2)
      return
    }
    const dates: Record<string, string[]> = {}
    const months: Record<string, string[]> = {}
    for (const item of dayAndWeekIndicators) dates[item.name] = indicatorDates[item.name]
    for (const item of monthIndicators) months[item.name] = indicatorMonths[item.name]
    const structureEdited = Boolean(override?.structureEdited)
    onRun({
      login,
      indicators,
      indicatorDates: dates,
      indicatorMonths: months,
      outputDates,
      uploadProfileId,
      autoUpload,
      downloadDir,
      pathTemplate: structureEdited ? '' : suggested.template,
      pathFolders: structureEdited ? [] : suggested.folders,
      pathStructure: structureEdited ? (override?.structure || '') : suggested.structure,
      customFilenames
    })
  }

  const renderOutputRows = (item: WorkflowInfo): JSX.Element | null => {
    const outputs = ownableOutputs(item)
    if (outputs.length === 0) return null
    const keys = outputs.map((output) => `${item.name}::${output.key}`)
    const changed = keys.filter((key) => (outputModes[key] || 'follow') !== 'follow').length
    const openPanel = expanded[item.name] ?? changed > 0
    const allSkipped = keys.every((key) => outputModes[key] === 'skip')
    return (
      <OutputPanel
        title="按导出另选"
        count={changed}
        total={outputs.length}
        open={openPanel}
        onToggle={() => setExpanded((prev) => ({ ...prev, [item.name]: !openPanel }))}
        extra={allSkipped && <Text type="warning" style={{ fontSize: 12 }}>全部不补，只会跑循环外的步骤</Text>}
      >
        {outputs.map((output, index) => {
          const key = keys[index]
          const mode = outputModes[key] || 'follow'
          const sel = outputSelections[key] || EMPTY_SELECTION
          const count = selectionCount(sel, 'day')
          const overLimit = count > CATCHUP_LIMITS.day
          return (
            <div key={key} className={`task-output-item${mode === 'skip' ? ' is-muted' : ''}`}>
              <OutputName output={output} duplicateHint="同一工作流里有重名导出，去工作流给这一步补上 name 才能稳定区分" />
              <Segmented
                size="small"
                value={mode}
                options={OUTPUT_MODES}
                onChange={(value) => setOutputModes((prev) => ({ ...prev, [key]: value as OutputMode }))}
              />
              {mode === 'own' ? (
                <div className="task-output-detail catchup-output-detail">
                  <CatchupPeriodPicker
                    kind="day"
                    size="small"
                    value={sel}
                    referenceDate={referenceDate}
                    status={count === 0 || overLimit ? 'error' : undefined}
                    multiplePlaceholder="选择这个导出要补的日期"
                    onChange={(next) => setOutputSelections((prev) => ({ ...prev, [key]: next }))}
                  />
                  {count === 0 && <span className="task-output-error">请选择日期</span>}
                  {overLimit && <span className="task-output-error">{`最多 ${CATCHUP_LIMITS.day} 天`}</span>}
                </div>
              ) : (
                <span className="task-output-follow">
                  {mode === 'skip' ? '这次不导出' : <>同指标：<strong>{indicatorDates[item.name]?.length || 0} 天</strong></>}
                </span>
              )}
            </div>
          )
        })}
      </OutputPanel>
    )
  }

  const renderBatchBar = (): JSX.Element | null => {
    if (!activeBatchKind) return null
    const peers = byKind[activeBatchKind]
    const draft = batchDrafts[activeBatchKind]
    const draftCount = selectionCount(draft, activeBatchKind)
    const draftOver = selectionExceedsLimit(draft, activeBatchKind)
    const anyCustom = peers.some((item) => selectionCount(selectionsByIndicator[item.name], activeBatchKind) > 0)
    const unit = periodUnitLabel(activeBatchKind)
    return (
      <div className="catchup-batch-bar">
        <div className="catchup-batch-head">
          <span className="catchup-batch-title">
            {batchKinds.length === 1
              ? `批量设置（${peers.length} 个${periodKindLabel(activeBatchKind)}指标）`
              : '批量设置时间'}
          </span>
          {batchKinds.length > 1 && (
            <Segmented
              size="small"
              value={activeBatchKind}
              options={batchKinds.map((kind) => ({
                label: `${periodKindLabel(kind)} (${byKind[kind].length})`,
                value: kind
              }))}
              onChange={(value) => setBatchKind(value as PeriodKind)}
            />
          )}
          <span className="catchup-batch-tip">选好后点右侧「应用到全部」，个别指标仍可单独修改</span>
        </div>
        <div className="catchup-batch-controls">
          <CatchupPeriodPicker
            kind={activeBatchKind}
            value={draft}
            referenceDate={referenceDate}
            multiplePlaceholder={`批量选择${periodKindLabel(activeBatchKind)}时间`}
            onChange={(next) => setBatchDrafts((prev) => ({ ...prev, [activeBatchKind]: next }))}
          />
          <Space size={6} className="catchup-batch-actions">
            <Button
              type="primary"
              ghost
              disabled={draftCount === 0 || draftOver}
              onClick={() => applyBatch(activeBatchKind)}
            >
              应用到全部
            </Button>
            <Button
              type="link"
              size="small"
              disabled={!anyCustom && draftCount === 0}
              onClick={() => clearBatch(activeBatchKind)}
            >
              清空已选
            </Button>
          </Space>
        </div>
        {draftOver && (
          <Text type="danger" style={{ fontSize: 12 }}>
            {`已选 ${draftCount} ${unit}，一次最多补 ${CATCHUP_LIMITS[activeBatchKind]} ${unit}。`}
          </Text>
        )}
      </div>
    )
  }

  const storageHint = override
    ? '已按上面的路径执行这一次，不会写回定时任务。'
    : suggested.task
      ? `默认沿用任务「${suggested.task.name}」的下载路径和存储结构，只影响这次补数。`
      : '没有匹配到任务的存储配置，文件放在这个下载路径下。只影响这次补数。'

  return (
    <Modal
      title="补数据"
      open={open}
      onCancel={onCancel}
      width={1100}
      destroyOnClose
      maskClosable={false}
      styles={{ body: { display: 'flex', flexDirection: 'column', maxHeight: '72vh', minHeight: 0 } }}
      footer={[
        <Button key="cancel" onClick={onCancel}>取消</Button>,
        step > 0 && <Button key="prev" onClick={() => setStep(step - 1)}>上一步</Button>,
        step < TASK_EDITOR_STEPS.length - 1 && (
          <Button key="next" type="primary" onClick={goNext}>下一步</Button>
        ),
        step === TASK_EDITOR_STEPS.length - 1 && (
          <Button key="ok" type="primary" disabled={!canRun} onClick={submit}>开始补数</Button>
        )
      ]}
    >
      <Steps
        size="small"
        current={step}
        style={{ flexShrink: 0, margin: '8px 0 16px' }}
        onChange={(next) => {
          if (next < step) setStep(next)
        }}
        items={TASK_EDITOR_STEPS.map((title, index) => ({ title, disabled: index > step }))}
      />
      <div className="task-editor-scroll">
        <div className="catchup-editor">
          {step === 0 && (
            <>
              <div className="catchup-intro">仅执行一次，不会修改定时任务。接下来再选补数时间和下载路径。</div>
              <TaskUploadFields
                active={open}
                profileId={uploadProfileId}
                autoUpload={autoUpload}
                onChange={(next) => {
                  setUploadProfileId(next.profileId)
                  setAutoUpload(next.autoUpload)
                }}
              />
              <div className="task-form-grid">
                <div>
                  <div className="catchup-field-label">登录方式</div>
                  <Select
                    allowClear
                    placeholder="选择登录工作流"
                    style={{ width: '100%' }}
                    value={login || undefined}
                    options={loginOptions}
                    onChange={(value) => setLogin(value || '')}
                  />
                </div>
                <div>
                  <div className="catchup-field-label">补数指标</div>
                  <IndicatorTreeSelect
                    workflows={workflows}
                    groups={groups}
                    placeholder="选择要补的指标"
                    value={indicators}
                    onChange={(names: string[]) => {
                      setIndicators(names)
                      setSelectionsByIndicator((prev) =>
                        Object.fromEntries(Object.entries(prev).filter(([key]) => names.includes(key)))
                      )
                      const kept = (key: string): boolean => names.includes(key.split('::')[0])
                      setOutputModes((prev) => Object.fromEntries(Object.entries(prev).filter(([key]) => kept(key))))
                      setOutputSelections((prev) =>
                        Object.fromEntries(Object.entries(prev).filter(([key]) => kept(key)))
                      )
                    }}
                  />
                </div>
              </div>
            </>
          )}

          {step === 1 && (
            <>
              <div className="catchup-step-note">默认按起止「时间段」快捷选择，也可切换到「指定日期」点选零散日期；留空则使用该指标默认采集范围，不会改任务卡片。</div>
              {renderBatchBar()}
              <div className="task-section-heading">
                <span>每个指标的补数时间</span>
                <span>{`单次上限：按日 ${CATCHUP_LIMITS.day} 天 · 按周 ${CATCHUP_LIMITS.week} 周 · 按月 ${CATCHUP_LIMITS.month} 个月`}</span>
              </div>
              {indicators.length === 0 ? (
                <div className="task-window-empty">先选择要补数的指标，再设置每项的时间。</div>
              ) : (
                selected.map((item) => {
                  const kind = periodKindOf(item)
                  const peers = byKind[kind]
                  const sel = selectionsByIndicator[item.name] || EMPTY_SELECTION
                  const count = selectionCount(sel, kind)
                  const overLimit = selectionExceedsLimit(sel, kind)
                  const canCopy = peers.length > 1 && count > 0 && !overLimit
                  const unit = periodUnitLabel(kind)
                  return (
                    <div key={item.name} className="task-window-block">
                      <div className="catchup-time-row">
                        <div className="task-window-label">
                          <Tag color={kind === 'month' ? 'cyan' : kind === 'week' ? 'purple' : 'blue'}>
                            {periodKindLabel(kind)}
                          </Tag>
                          <span>{workflowPath(item, groups)}</span>
                        </div>
                        <div className="catchup-time-control">
                          <CatchupPeriodPicker
                            kind={kind}
                            value={sel}
                            referenceDate={referenceDate}
                            onChange={(next) => {
                              setSelectionsByIndicator((prev) => ({ ...prev, [item.name]: next }))
                            }}
                          />
                          {count === 0 && (
                            <div className="catchup-default-hint">
                              默认：{describeDefault(item, toIsoList(defaults[item.name], kind))}
                            </div>
                          )}
                          {overLimit && (
                            <Text type="danger" style={{ fontSize: 12 }}>
                              {`已选 ${count} ${unit}，一次最多补 ${CATCHUP_LIMITS[kind]} ${unit}。`}
                            </Text>
                          )}
                        </div>
                        {canCopy ? (
                          <Button type="link" size="small" className="catchup-copy" onClick={() => copyToSameKind(item)}>
                            复制到同类指标
                          </Button>
                        ) : <span />}
                      </div>
                      {renderOutputRows(item)}
                    </div>
                  )
                })
              )}
              {longSpanStats.warnItems > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  className="catchup-long-warn"
                  message={`本次包含较长时间跨度（最长 ${longSpanStats.maxDays} 天，按日合计 ${longSpanStats.totalDayRuns} 个指标日），执行耗时较久，请保持网络和数字证书连接稳定。`}
                />
              )}
              <div className="catchup-summary">
                <div className="catchup-summary-head">
                  <div className="catchup-summary-title">本次补数预览</div>
                  {previewMetaText && <span className="catchup-summary-meta">{previewMetaText}</span>}
                  {canMergePreview && (
                    <Button
                      type="link"
                      size="small"
                      className="catchup-summary-toggle"
                      onClick={() => setMergeSamePreview((prev) => !prev)}
                    >
                      {mergeSamePreview ? `展开逐项（${previewItems.length}）` : '合并相同项'}
                    </Button>
                  )}
                </div>
                {selected.length === 0 ? (
                  <Text type="secondary">为每个指标选好时间后即可继续。</Text>
                ) : visiblePreviewRows.map((row) => {
                  const first = row.items[0]
                  const names = row.items.map((entry) => entry.name)
                  const nameLabel = names.length <= 2
                    ? names.join('、')
                    : `${names.slice(0, 2).join('、')} 等 ${names.length} 个${periodKindLabel(first.kind)}指标`
                  const total = first.values.length
                  const unit = periodUnitLabel(first.kind)
                  const allSegmentTexts = first.segments.map(formatPeriodSegment)
                  const hasMoreSegments = allSegmentTexts.length > DEFAULT_PREVIEW_SEGMENTS
                  const isSegmentsExpanded = Boolean(expandedPreviewRows[row.key])
                  const shownSegments = hasMoreSegments && !isSegmentsExpanded
                    ? allSegmentTexts.slice(0, DEFAULT_PREVIEW_SEGMENTS)
                    : allSegmentTexts
                  return (
                    <div key={row.key} className="catchup-summary-row">
                      <span className="catchup-summary-name">
                        {names.length > 2 ? (
                          <Tooltip title={names.join('、')}>
                            <span className="catchup-summary-group-name">{nameLabel}</span>
                          </Tooltip>
                        ) : (
                          nameLabel
                        )}
                      </span>
                      <span className="catchup-summary-value">
                        {first.isDefault && <span className="catchup-summary-default">默认</span>}
                        {first.overLimit ? (
                          <Text type="danger">{describeOverflow(first.selection, first.kind)}</Text>
                        ) : (
                          <>
                            <span className="catchup-summary-segments">
                              {shownSegments.map((text, idx) => (
                                <span key={`${text}-${idx}`} className="catchup-summary-seg">{text}</span>
                              ))}
                              {hasMoreSegments && (
                                <Tooltip title={!isSegmentsExpanded ? allSegmentTexts.join('、') : undefined}>
                                  <button
                                    type="button"
                                    className="catchup-summary-more"
                                    onClick={() => setExpandedPreviewRows((prev) => ({
                                      ...prev,
                                      [row.key]: !prev[row.key]
                                    }))}
                                  >
                                    {isSegmentsExpanded ? '收起' : `+${allSegmentTexts.length - DEFAULT_PREVIEW_SEGMENTS} 段`}
                                  </button>
                                </Tooltip>
                              )}
                              {total > 1 && (
                                <span className="catchup-summary-total">{`共 ${total} ${unit}`}</span>
                              )}
                            </span>
                          </>
                        )}
                        {first.ownTexts.length > 0 && (
                          <span className="catchup-summary-own">{`（${first.ownTexts.join('；')}）`}</span>
                        )}
                      </span>
                    </div>
                  )
                })}
                {monthIndicators.length > 0 ? (
                  <div className="catchup-summary-help">
                    <Text type="secondary">按月指标会查询所选月份的整月数据。</Text>
                  </div>
                ) : null}
              </div>
            </>
          )}

          {step === 2 && (
            <>
              <div className="catchup-download">
                <div className="catchup-field-label">下载路径</div>
                <Space.Compact style={{ width: '100%' }}>
                  <Input
                    value={downloadDir}
                    placeholder="留空则使用设置里的下载目录"
                    onChange={(event) => rememberDownload(event.target.value)}
                    allowClear
                  />
                  <Button icon={<FolderOpenOutlined />} onClick={() => { void pickDownloadDir() }}>选择</Button>
                </Space.Compact>
                <div className="catchup-download-actions">
                  <Button type="link" size="small" disabled={!override} onClick={restoreStorage}>恢复默认</Button>
                </div>
                <div className="catchup-step-note">{storageHint}</div>
              </div>
              <StoragePathModal
                key={storageNonce}
                embedded
                open
                root={downloadDir || settingsDir}
                rootHint="仅本次补数"
                structure={modalStructure}
                template={suggested.template}
                folders={suggested.folders}
                indicators={indicators}
                workflows={workflows}
                indicatorWindows={{}}
                customNames={customFilenames}
                onCustomNamesChange={(names) => {
                  setOverride((prev) => ({
                    structureEdited: prev?.structureEdited ?? false,
                    structure: prev?.structure ?? modalStructure,
                    downloadDir: prev?.downloadDir ?? null,
                    namesEdited: true,
                    customFilenames: names
                  }))
                }}
                onChange={(next, blocked) => {
                  setStructureBlocked(blocked)
                  if (structureBaseline.current === null) {
                    structureBaseline.current = next
                    return
                  }
                  if (next === structureBaseline.current) return
                  if (override?.structureEdited && next === override.structure) return
                  structureBaseline.current = next
                  setOverride((prev) => ({
                    structureEdited: true,
                    structure: next,
                    downloadDir: prev?.downloadDir ?? null,
                    namesEdited: prev?.namesEdited,
                    customFilenames: prev?.customFilenames
                  }))
                }}
              />
            </>
          )}
        </div>
      </div>
    </Modal>
  )
}
