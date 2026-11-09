import { useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, DatePicker, Input, Modal, Segmented, Select, Space, Steps, Tag, Typography } from 'antd'
import { FolderOpenOutlined } from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import { settingsStore, type WorkflowInfo, type WorkflowGroup, type WorkflowOutput } from '../services/api'
import IndicatorTreeSelect from './IndicatorTreeSelect'
import { workflowPath } from './workflowTree'
import { formatDOffset, formatMOffset, formatWOffset } from './DateWindowSlider'
import { OutputName, OutputPanel } from './OutputPanel'
import StoragePathModal, { type PathFolder } from './StoragePathModal'
import { TASK_EDITOR_STEPS } from './taskEditorSteps'
import TaskUploadFields from './TaskUploadFields'

const { Text } = Typography
const MAX_DAYS = 62
const MAX_MONTHS = 24

/** 用来给补数默认存储路径的任务摘要。字段与任务卡片一致，多出来的字段可以一起传入。 */
export interface CatchupTaskHint {
  name: string
  indicators: string[]
  enabled: boolean
  createdAt: number
  pathTemplate?: string
  pathFolders?: PathFolder[]
  pathStructure?: string
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
}

interface StorageOverride {
  /** 用户改过存储树。序列化结果和任务原文不同也算，这样提交的是树上看到的结构。 */
  structureEdited: boolean
  structure: string
  /** null 表示下载目录仍跟随任务 / 设置 */
  downloadDir: string | null
}

type OutputMode = 'follow' | 'own' | 'skip'

const OUTPUT_MODES: { label: string; value: OutputMode }[] = [
  { label: '跟随', value: 'follow' },
  { label: '另选', value: 'own' },
  { label: '不补', value: 'skip' }
]

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

function describeList(values: string[]): string {
  return values.join('、')
}

function weekMonday(value: Dayjs): Dayjs {
  return value.subtract((value.day() + 6) % 7, 'day').startOf('day')
}

function recentPeriods(count: number, endOffset: number, unit: 'day' | 'week' | 'month'): Dayjs[] {
  const end = (unit === 'week' ? weekMonday(dayjs()) : dayjs().startOf(unit)).subtract(endOffset, unit)
  return Array.from({ length: count }, (_, index) => end.subtract(count - index - 1, unit))
}

const DAY_PRESETS = [
  { label: '近三天', count: 3, endOffset: 0 },
  { label: '近七天', count: 7, endOffset: 0 },
  { label: '近一个月', count: 30, endOffset: 0 }
]

const MONTH_PRESETS = [
  { label: '上个月', count: 1, endOffset: 1 },
  { label: '近三月', count: 3, endOffset: 1 },
  { label: '近一年', count: 12, endOffset: 1 }
]

const WEEK_PRESETS = [
  { label: '本周', count: 1, endOffset: 0 },
  { label: '近四周', count: 4, endOffset: 0 },
  { label: '近十二周', count: 12, endOffset: 0 }
]

function toIso(values: Dayjs[] | undefined, pattern: string): string[] {
  return [...new Set((values || []).map((item) => item.format(pattern)))].sort()
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
  const month = item.date_kind === 'month'
  const week = item.date_kind === 'week'
  const [start, end] = defaultWindow(item)
  const format = month ? formatMOffset : week ? formatWOffset : formatDOffset
  const offset = start === end ? format(start) : `${format(start)} ～ ${format(end)}`
  const dates = values.length === 1 ? values[0] : `${values[0]} ～ ${values[values.length - 1]}`
  return `${offset}（${dates}，共 ${values.length} ${month ? '个月' : week ? '周' : '天'}）`
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
  const [datesByIndicator, setDatesByIndicator] = useState<Record<string, Dayjs[]>>({})
  const [monthsByIndicator, setMonthsByIndicator] = useState<Record<string, Dayjs[]>>({})
  const [outputModes, setOutputModes] = useState<Record<string, OutputMode>>({})
  const [outputPicks, setOutputPicks] = useState<Record<string, Dayjs[]>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
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
      downloadDir: (task?.downloadDir || '').trim() || settingsDir
    }
  }, [indicators, tasks, settingsDir])
  const downloadDir = override?.downloadDir ?? suggested.downloadDir
  const modalStructure = override?.structureEdited ? override.structure : suggested.structure

  useEffect(() => {
    if (step !== 2) structureBaseline.current = null
  }, [step])

  const selected = indicators
    .map((name) => workflows.find((item) => item.name === name))
    .filter(Boolean) as WorkflowInfo[]
  const dayIndicators = selected.filter((item) => item.date_kind !== 'month')
  const monthIndicators = selected.filter((item) => item.date_kind === 'month')
  const defaults = Object.fromEntries(selected.map((item) => [item.name, defaultPeriods(item, referenceDate)])) as Record<string, Dayjs[]>
  const indicatorDates = useMemo(() => {
    const next: Record<string, string[]> = {}
    for (const item of dayIndicators) {
      const values = datesByIndicator[item.name]?.length ? datesByIndicator[item.name] : defaults[item.name]
      next[item.name] = toIso(item.date_kind === 'week' ? values.map(weekMonday) : values, 'YYYY-MM-DD')
    }
    return next
  }, [dayIndicators, datesByIndicator, defaults])
  const indicatorMonths = useMemo(() => {
    const next: Record<string, string[]> = {}
    for (const item of monthIndicators) next[item.name] = toIso(monthsByIndicator[item.name]?.length ? monthsByIndicator[item.name] : defaults[item.name], 'YYYY-MM')
    return next
  }, [monthIndicators, monthsByIndicator, defaults])
  const daysReady = dayIndicators.every((item) => {
    const count = indicatorDates[item.name].length
    return count > 0 && count <= MAX_DAYS
  })
  const monthsReady = monthIndicators.every((item) => {
    const count = indicatorMonths[item.name].length
    return count > 0 && count <= MAX_MONTHS
  })
  const outputDates = useMemo(() => {
    const next: Record<string, string[]> = {}
    for (const item of dayIndicators) {
      for (const output of ownableOutputs(item)) {
        const key = `${item.name}::${output.key}`
        const mode = outputModes[key] || 'follow'
        if (mode === 'skip') next[key] = []
        else if (mode === 'own') next[key] = toIso(outputPicks[key], 'YYYY-MM-DD')
      }
    }
    return next
  }, [dayIndicators, outputModes, outputPicks])
  const outputsReady = Object.entries(outputDates).every(([key, values]) =>
    outputModes[key] === 'skip' || (values.length > 0 && values.length <= MAX_DAYS))
  const datesReady = indicators.length > 0 && daysReady && monthsReady && outputsReady
  const canRun = datesReady && !structureBlocked
  const loginOptions = workflows
    .filter((item) => item.type === 'login')
    .map((item) => ({ label: item.label || item.name, value: item.name }))

  const copyToSameKind = (source: WorkflowInfo): void => {
    const month = source.date_kind === 'month'
    const peers = month ? monthIndicators : dayIndicators
    if (month) {
      const values = monthsByIndicator[source.name] || []
      setMonthsByIndicator((prev) => {
        const next = { ...prev }
        for (const item of peers) next[item.name] = [...values]
        return next
      })
      return
    }
    const values = datesByIndicator[source.name] || []
    setDatesByIndicator((prev) => {
      const next = { ...prev }
      for (const item of peers) next[item.name] = [...values]
      return next
    })
  }

  const rememberDownload = (dir: string): void => {
    setOverride((prev) => ({
      structureEdited: prev?.structureEdited ?? false,
      structure: prev?.structure ?? '',
      downloadDir: dir
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

  const goNext = (): void => {
    if (step === 0 && indicators.length === 0) {
      message.warning('请选择要补的指标')
      return
    }
    if (step === 1 && !datesReady) {
      message.warning(!outputsReady
        ? '请为单独选择的导出补上日期，或改成跟随 / 不补'
        : '请检查每个指标的补数日期')
      return
    }
    setStep((current) => Math.min(current + 1, TASK_EDITOR_STEPS.length - 1))
  }

  const submit = (): void => {
    if (!datesReady) {
      message.warning(indicators.length === 0 ? '请选择要补的指标' : '请检查每个指标的补数日期')
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
    for (const item of dayIndicators) dates[item.name] = indicatorDates[item.name]
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
      pathStructure: structureEdited ? (override?.structure || '') : suggested.structure
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
          const picks = outputPicks[key] || []
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
                <div className="task-output-detail">
                  <DatePicker
                    multiple
                    size="small"
                    style={{ flex: 1, minWidth: 220 }}
                    value={picks}
                    status={picks.length === 0 || picks.length > MAX_DAYS ? 'error' : undefined}
                    placeholder="选择这个导出要补的日期"
                    maxTagCount="responsive"
                    onChange={(value) => setOutputPicks((prev) => ({ ...prev, [key]: (value as Dayjs[] | null) || [] }))}
                  />
                  {picks.length === 0 && <span className="task-output-error">请选择日期</span>}
                  {picks.length > MAX_DAYS && <span className="task-output-error">{`最多 ${MAX_DAYS} 天`}</span>}
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
                      setDatesByIndicator((prev) => Object.fromEntries(Object.entries(prev).filter(([key]) => names.includes(key))))
                      setMonthsByIndicator((prev) => Object.fromEntries(Object.entries(prev).filter(([key]) => names.includes(key))))
                      const kept = (key: string): boolean => names.includes(key.split('::')[0])
                      setOutputModes((prev) => Object.fromEntries(Object.entries(prev).filter(([key]) => kept(key))))
                      setOutputPicks((prev) => Object.fromEntries(Object.entries(prev).filter(([key]) => kept(key))))
                    }}
                  />
                </div>
              </div>
            </>
          )}

          {step === 1 && (
            <>
              <div className="catchup-step-note">时间留空时使用该指标的默认采集范围，具体日期见下方预览。这一步不会改任务卡片上的区间。</div>
              <div className="task-section-heading">
                <span>每个指标的补数时间</span>
                <span>可留空使用默认，也可点选多个日期或月份</span>
              </div>
              {indicators.length === 0 ? (
                <div className="task-window-empty">先选择要补数的指标，再设置每项的时间。</div>
              ) : (
                selected.map((item) => {
                  const month = item.date_kind === 'month'
                  const week = item.date_kind === 'week'
                  const peers = month ? monthIndicators : dayIndicators
                  const values = month ? (monthsByIndicator[item.name] || []) : (datesByIndicator[item.name] || [])
                  const tooMany = month ? values.length > MAX_MONTHS : values.length > MAX_DAYS
                  const canCopy = peers.length > 1 && values.length > 0
                  return (
                    <div key={item.name} className="task-window-block">
                      <div className="catchup-time-row">
                        <div className="task-window-label">
                          <Tag color={month ? 'cyan' : week ? 'purple' : 'blue'}>{month ? '按月' : week ? '按周' : '按日'}</Tag>
                          <span>{workflowPath(item, groups)}</span>
                        </div>
                        <div className="catchup-time-control">
                          <DatePicker
                            multiple
                            picker={month ? 'month' : week ? 'week' : 'date'}
                            style={{ width: '100%' }}
                            value={values}
                            placeholder={month ? '留空使用默认月份' : week ? '留空使用默认周' : '留空使用默认日期'}
                            maxTagCount="responsive"
                            renderExtraFooter={() => (
                              <div className="catchup-date-presets">
                                <span>快捷选择</span>
                                {(month ? MONTH_PRESETS : week ? WEEK_PRESETS : DAY_PRESETS).map((preset) => (
                                  <Button
                                    key={preset.label}
                                    type="link"
                                    size="small"
                                    onClick={() => {
                                      const next = recentPeriods(preset.count, preset.endOffset, month ? 'month' : week ? 'week' : 'day')
                                      if (month) setMonthsByIndicator((prev) => ({ ...prev, [item.name]: next }))
                                      else setDatesByIndicator((prev) => ({ ...prev, [item.name]: next }))
                                    }}
                                  >
                                    {preset.label}
                                  </Button>
                                ))}
                              </div>
                            )}
                            onChange={(value) => {
                              const next = (value as Dayjs[] | null) || []
                              if (month) setMonthsByIndicator((prev) => ({ ...prev, [item.name]: next }))
                              else setDatesByIndicator((prev) => ({ ...prev, [item.name]: next }))
                            }}
                          />
                          {values.length === 0 && (
                            <div className="catchup-default-hint">
                              默认：{describeDefault(item, toIso(defaults[item.name], month ? 'YYYY-MM' : 'YYYY-MM-DD'))}
                            </div>
                          )}
                          {tooMany ? <Text type="danger">{month ? `一次最多补 ${MAX_MONTHS} 个月。` : week ? `一次最多补 ${MAX_DAYS} 周。` : `一次最多补 ${MAX_DAYS} 天。`}</Text> : null}
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
              <div className="catchup-summary">
                <div className="catchup-summary-title">本次补数预览</div>
                {selected.length === 0 ? (
                  <Text type="secondary">为每个指标选好时间后即可继续。</Text>
                ) : selected.map((item) => {
                  const month = item.date_kind === 'month'
                  const values = month ? indicatorMonths[item.name] : indicatorDates[item.name]
                  const isDefault = !(month ? monthsByIndicator[item.name] : datesByIndicator[item.name])?.length
                  const own = month ? [] : ownableOutputs(item)
                    .filter((output) => `${item.name}::${output.key}` in outputDates)
                    .map((output) => {
                      const picked = outputDates[`${item.name}::${output.key}`]
                      return `${output.label}：${picked.length ? describeList(picked) : '不补'}`
                    })
                  return (
                    <div key={item.name} className="catchup-summary-row">
                      <span className="catchup-summary-name">{workflowPath(item, groups)}</span>
                      <span className="catchup-summary-value">
                        {isDefault && <span className="catchup-summary-default">默认</span>}
                        {describeList(values)}
                        {own.length > 0 && `（${own.join('；')}）`}
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
                    downloadDir: prev?.downloadDir ?? null
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
