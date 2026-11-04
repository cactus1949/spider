import { useMemo, useState } from 'react'
import { Button, DatePicker, Modal, Segmented, Select, Space, Tag, Typography } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import type { WorkflowInfo, WorkflowGroup, WorkflowOutput } from '../services/api'
import IndicatorTreeSelect from './IndicatorTreeSelect'
import { workflowPath } from './workflowTree'
import { formatDOffset, formatMOffset, formatWOffset } from './DateWindowSlider'
import { OutputName, OutputPanel } from './OutputPanel'

const { Text } = Typography
const MAX_DAYS = 62
const MAX_MONTHS = 24

export interface CatchupSelection {
  login: string
  indicators: string[]
  indicatorDates: Record<string, string[]>
  indicatorMonths: Record<string, string[]>
  /** 单个导出另选的日期，键是 指标::输出key；空列表 = 不补 */
  outputDates: Record<string, string[]>
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

interface Props {
  open: boolean
  workflows: WorkflowInfo[]
  groups: WorkflowGroup[]
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

export default function CatchupModal({ open, workflows, groups, defaultLogin, onCancel, onRun }: Props): JSX.Element {
  const [login, setLogin] = useState(defaultLogin)
  const [indicators, setIndicators] = useState<string[]>([])
  const [datesByIndicator, setDatesByIndicator] = useState<Record<string, Dayjs[]>>({})
  const [monthsByIndicator, setMonthsByIndicator] = useState<Record<string, Dayjs[]>>({})
  const [outputModes, setOutputModes] = useState<Record<string, OutputMode>>({})
  const [outputPicks, setOutputPicks] = useState<Record<string, Dayjs[]>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [referenceDate] = useState(() => dayjs().startOf('day'))

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
  const canRun = indicators.length > 0 && daysReady && monthsReady && outputsReady
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

  const submit = (): void => {
    if (!canRun) return
    const dates: Record<string, string[]> = {}
    const months: Record<string, string[]> = {}
    for (const item of dayIndicators) dates[item.name] = indicatorDates[item.name]
    for (const item of monthIndicators) months[item.name] = indicatorMonths[item.name]
    onRun({ login, indicators, indicatorDates: dates, indicatorMonths: months, outputDates })
  }

  const renderOutputRows = (item: WorkflowInfo): JSX.Element | null => {
    const outputs = ownableOutputs(item)
    if (outputs.length === 0) return null
    const keys = outputs.map((output) => `${item.name}::${output.key}`)
    const changed = keys.filter((key) => (outputModes[key] || 'follow') !== 'follow').length
    const open = expanded[item.name] ?? changed > 0
    const allSkipped = keys.every((key) => outputModes[key] === 'skip')
    return (
      <OutputPanel
        title="按导出另选"
        count={changed}
        total={outputs.length}
        open={open}
        onToggle={() => setExpanded((prev) => ({ ...prev, [item.name]: !open }))}
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

  return (
    <Modal
      title="补数据"
      open={open}
      onCancel={onCancel}
      width={900}
      destroyOnClose
      maskClosable={false}
      styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
      footer={
        <Space>
          <Button onClick={onCancel}>取消</Button>
          <Button type="primary" disabled={!canRun} onClick={submit}>开始补数</Button>
        </Space>
      }
    >
      <div className="catchup-editor">
        <div className="catchup-intro">仅执行一次，不会修改定时任务。时间留空时使用该指标的默认采集范围，具体日期见下方预览。</div>
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
            <Text type="secondary">为每个指标选好时间后即可执行。</Text>
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
      </div>
    </Modal>
  )
}
