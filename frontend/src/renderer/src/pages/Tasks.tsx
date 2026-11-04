import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Card,
  Button,
  Input,
  Tag,
  Space,
  Empty,
  Modal,
  Form,
  Select,
  TimePicker,
  Switch,
  Checkbox,
  Badge,
  Tooltip,
  Steps,
  Segmented,
  App as AntApp
} from 'antd'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  ClockCircleOutlined,
  CalendarOutlined,
  ReloadOutlined,
  ImportOutlined,
  ExportOutlined,
  FolderOutlined
} from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import { api, settingsStore, type WorkflowInfo, type WorkflowGroup, type WorkflowOutput } from '../services/api'
import IndicatorTreeSelect from '../components/IndicatorTreeSelect'
import StoragePathModal, {
  DEFAULT_PATH_TEMPLATE,
  LEGACY_PATH_TEMPLATE,
  type PathFolder
} from '../components/StoragePathModal'
import { workflowPath } from '../components/workflowTree'
import ProgressModal, { type CatchupRun } from '../components/ProgressModal'
import CatchupModal from '../components/CatchupModal'
import { OutputName, OutputPanel } from '../components/OutputPanel'
import DateWindowSlider, {
  MonthWindowSlider,
  WeekWindowSlider,
  clampMonthSpan,
  clampWeekSpan,
  describeMonthWindow,
  describeWeekWindow,
  describeWindow,
  formatDOffset,
  formatMOffset,
  formatWOffset,
  legacyWindowText,
  offsetsFromBackfill
} from '../components/DateWindowSlider'

const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const EDITOR_STEPS = ['基本信息', '采集范围', '存储结构']

interface AutoTask {
  id: string
  name: string
  login: string
  indicators: string[]
  repeat: 'daily' | 'weekly'
  weekdays?: number[]
  time: string
  backfillDays: number
  coverDays: number
  indicatorWindows?: Record<string, { start: number; end: number }>
  /** 单个导出另配的范围，键是 指标::输出key；没配的跟随指标 */
  outputWindows?: Record<string, { start: number; end: number }>
  pathTemplate?: string
  pathFolders?: PathFolder[]
  pathStructure?: string
  downloadDir?: string
  enabled: boolean
  remark?: string
  createdAt: number
}

function parseWindows(raw: unknown): Record<string, { start: number; end: number }> {
  let value = raw
  if (typeof value === 'string' && value.trim()) {
    try {
      value = JSON.parse(value)
    } catch {
      return {}
    }
  }
  if (!value || typeof value !== 'object') return {}
  const parsed: Record<string, { start: number; end: number }> = {}
  for (const [key, item] of Object.entries(value as Record<string, { start?: number; end?: number }>)) {
    const start = Number(item?.start)
    const end = Number(item?.end)
    if (Number.isFinite(start) && Number.isFinite(end)) parsed[key] = { start, end }
  }
  return parsed
}

function describeStorage(template: string | undefined): string {
  const value = template || ''
  if (!value) return '下载根目录'
  if (value === '{SSID}') return 'WiFi 一级'
  if (value === '{SSID}/{YYYY-MM}') return 'WiFi + 月'
  if (value === '{SSID}/{YYYY-MM}/{YYYY-MM-DD}') return 'WiFi + 月 + 日'
  if (value === LEGACY_PATH_TEMPLATE) return '只按日期（旧）'
  return '自定义日期'
}

function describeTaskDateLines(task: AutoTask, workflowList: WorkflowInfo[], groups: WorkflowGroup[]): string[] {
  const saved = task.indicatorWindows || {}
  const names = task.indicators.length ? task.indicators : Object.keys(saved)
  if (!names.length) return [legacyWindowText(task.backfillDays, task.coverDays)]
  const legacy = Object.keys(saved).length === 0
  const [legacyStart, legacyEnd] = offsetsFromBackfill(task.backfillDays, task.coverDays)
  return names.map((name) => {
    const workflow = workflowList.find((item) => item.name === name)
    const month = workflow?.date_kind === 'month'
    const week = workflow?.date_kind === 'week'
    const window = saved[name] || (month
      ? legacy
        ? { start: -1, end: -1 }
        : { start: workflow?.window_start ?? -1, end: workflow?.window_end ?? -1 }
      : week
        ? { start: workflow?.window_start ?? 0, end: workflow?.window_end ?? 0 }
      : legacy
        ? { start: legacyStart, end: legacyEnd }
        : { start: workflow?.window_start ?? -1, end: workflow?.window_end ?? -1 })
    const format = month ? formatMOffset : week ? formatWOffset : formatDOffset
    const own = month || week ? [] : Object.entries(task.outputWindows || {})
      .filter(([key]) => key.startsWith(`${name}::`))
      .map(([key, item]) => {
        const output = workflow?.outputs?.find((entry) => `${name}::${entry.key}` === key)
        return `${output?.label || key.slice(name.length + 2)} ${format(item.start)} ～ ${format(item.end)}`
      })
    const extra = own.length ? `（${own.join('；')}）` : ''
    return `${workflow ? workflowPath(workflow, groups) : name}：${format(window.start)} ～ ${format(window.end)}${extra}`
  })
}

interface FormValues {
  name: string
  login: string
  indicators: string[]
  repeat: 'daily' | 'weekly'
  weekdays?: number[]
  time: Dayjs
  enabled: boolean
  remark?: string
}

async function loadTasksFromApi(): Promise<AutoTask[]> {
  try {
    const configs = await api.listTaskConfigs()
    if (configs.length > 0) {
      const tasks: AutoTask[] = configs.map((c: any) => ({
        id: c.id,
        name: c.name,
        login: c.login || '',
        indicators: c.indicators || [],
        repeat: c.repeat || 'daily',
        weekdays: c.weekdays || [],
        time: c.time || '09:00',
        backfillDays: c.backfill_days ?? 1,
        coverDays: c.cover_days ?? 0,
        indicatorWindows: parseWindows(c.indicator_windows),
        outputWindows: parseWindows(c.output_windows),
        pathTemplate: c.path_template || '',
        pathFolders: Array.isArray(c.path_folders) ? c.path_folders as PathFolder[] : [],
        pathStructure: typeof c.path_structure === 'string' ? c.path_structure : '',
        downloadDir: c.download_dir_custom ? (c.download_dir || '') : '',
        enabled: c.enabled ?? true,
        remark: c.remark || '',
        createdAt: c.created_at ? c.created_at * 1000 : Date.now()
      }))
      return tasks
    }
    return []
  } catch {
    return []
  }
}

async function saveTaskConfigs(tasks: AutoTask[]): Promise<void> {
  for (const t of tasks) {
    try {
      await api.saveTaskConfig(t.id, {
        name: t.name,
        login: t.login || '',
        indicators: t.indicators,
        repeat: t.repeat,
        time: t.time,
        weekdays: t.weekdays || [],
        backfill_days: t.backfillDays,
        cover_days: t.coverDays,
        indicator_windows: t.indicatorWindows || {},
        output_windows: t.outputWindows || {},
        path_template: t.pathTemplate || '',
        path_folders: t.pathFolders || [],
        path_structure: t.pathStructure || '',
        enabled: t.enabled,
        remark: t.remark || '',
        download_dir: t.downloadDir || '',
        download_dir_custom: Boolean(t.downloadDir?.trim())
      })
    } catch { /* ignore */ }
  }
}

async function deleteTaskConfig(id: string): Promise<void> {
  try {
    await api.deleteTaskConfig(id)
  } catch { /* ignore */ }
}

function uid(): string {
  return `t_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
}

function formatWeekdays(days: number[] | undefined): string {
  if (!days || days.length === 0) return ''
  const sorted = [...days].sort((a, b) => a - b)
  // 检测是否为周一到周五
  if (sorted.length === 5 && sorted.every((v, i) => v === i + 1)) {
    return '周一至周五'
  }
  if (sorted.length === 2 && sorted[0] === 0 && sorted[1] === 6) {
    return '周末'
  }
  if (sorted.length === 7) {
    return '每天'
  }
  return sorted.map((d) => WEEKDAY_LABELS[d]).join('、')
}

function formatSchedule(task: AutoTask): string {
  if (task.repeat === 'daily') return `每天 ${task.time}`
  return `每周 ${formatWeekdays(task.weekdays)} ${task.time}`
}

function nextRun(task: AutoTask, now: Dayjs = dayjs()): Dayjs | null {
  const [hh, mm] = task.time.split(':').map((s) => parseInt(s, 10))
  if (Number.isNaN(hh) || Number.isNaN(mm)) return null

  if (task.repeat === 'daily') {
    let target = now.hour(hh).minute(mm).second(0).millisecond(0)
    if (!target.isAfter(now)) target = target.add(1, 'day')
    return target
  }

  const days = task.weekdays || []
  if (days.length === 0) return null
  for (let offset = 0; offset < 14; offset++) {
    const candidate = now
      .add(offset, 'day')
      .hour(hh)
      .minute(mm)
      .second(0)
      .millisecond(0)
    if (days.includes(candidate.day()) && candidate.isAfter(now)) {
      return candidate
    }
  }
  return null
}

function defaultLoginOf(taskList: AutoTask[], workflowList: WorkflowInfo[]): string {
  const fromTasks = [...new Set(taskList.map((item) => item.login).filter(Boolean))]
  if (fromTasks.length === 1) return fromTasks[0]
  const logins = workflowList.filter((item) => item.type === 'login')
  return logins.length === 1 ? logins[0].name : ''
}

function summarizeForm(values: Partial<FormValues>): string {
  const time = values.time ? values.time.format('HH:mm') : '--:--'
  if (values.repeat === 'weekly') {
    const days = values.weekdays && values.weekdays.length ? formatWeekdays(values.weekdays) : '请选择'
    return `每周 ${days} ${time}`
  }
  return `每天 ${time}`
}

export default function Tasks(): JSX.Element {
  const { message, modal } = AntApp.useApp()
  const [tasks, setTasks] = useState<AutoTask[]>([])
  const [keyword, setKeyword] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<AutoTask | null>(null)
  const [form] = Form.useForm<FormValues>()
  const repeatWatch = Form.useWatch('repeat', form)
  const weekdaysWatch = Form.useWatch('weekdays', form)
  const timeWatch = Form.useWatch('time', form)
  const nameWatch = Form.useWatch('name', form)
  const indicatorsWatch = Form.useWatch('indicators', form)
  const selectedIndicators = useMemo(() => indicatorsWatch || [], [indicatorsWatch])
  const [windows, setWindows] = useState<Record<string, [number, number]>>({})
  const [outputWindows, setOutputWindows] = useState<Record<string, [number, number]>>({})
  const [pathTemplate, setPathTemplate] = useState(DEFAULT_PATH_TEMPLATE)
  const [pathFolders, setPathFolders] = useState<PathFolder[]>([])
  const [pathStructure, setPathStructure] = useState('')
  const [structureBlocked, setStructureBlocked] = useState(false)
  // 进过第 3 步才会把回显的文件夹结构提交上来
  const [structureSeen, setStructureSeen] = useState(false)
  const [step, setStep] = useState(0)
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([])
  const [groups, setGroups] = useState<WorkflowGroup[]>([])
  const [taskProgressOpen, setTaskProgressOpen] = useState(false)
  const [taskProgressName, setTaskProgressName] = useState('')
  const [taskProgressTitle, setTaskProgressTitle] = useState('')
  const [catchupOpen, setCatchupOpen] = useState(false)
  const [catchupSeed, setCatchupSeed] = useState(0)
  const [catchupProgressOpen, setCatchupProgressOpen] = useState(false)
  const [catchupRun, setCatchupRun] = useState<CatchupRun | null>(null)
  const [catchupTitle, setCatchupTitle] = useState('补数据')

  useEffect(() => {
    if (!modalOpen || !indicatorsWatch) return
    setWindows((prev) => {
      let changed = false
      const next = { ...prev }
      const saved = editing?.indicatorWindows || {}
      const legacy = Boolean(editing) && Object.keys(saved).length === 0
      const legacySpan = legacy ? offsetsFromBackfill(editing?.backfillDays ?? 1, editing?.coverDays ?? 0) : null
      for (const name of indicatorsWatch) {
        if (next[name]) continue
        const workflow = workflows.find((item) => item.name === name)
        if (!workflow) continue
        if (workflow.date_kind === 'month') {
          next[name] = legacy ? [-1, -1] : clampMonthSpan([workflow.window_start ?? -1, workflow.window_end ?? -1])
        } else if (workflow.date_kind === 'week') {
          next[name] = clampWeekSpan([workflow.window_start ?? 0, workflow.window_end ?? 0])
        } else if (legacySpan) {
          next[name] = legacySpan
        } else {
          next[name] = [workflow.window_start ?? -1, workflow.window_end ?? -1]
        }
        changed = true
      }
      for (const key of Object.keys(next)) {
        if (!indicatorsWatch.includes(key)) {
          delete next[key]
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [modalOpen, indicatorsWatch, workflows, editing])

  // 用于刷新"下次执行"显示
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 60_000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const init = async () => {
      const remote = await loadTasksFromApi()
      if (remote.length > 0) {
        setTasks(remote)
      }
    }
    init()
    api.listWorkflows().then(setWorkflows).catch(() => {})
    api.listWorkflowGroups().then(setGroups).catch(() => {})
  }, [])

  const persist = useCallback((next: AutoTask[]) => {
    setTasks(next)
    saveTaskConfigs(next)
  }, [])

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return tasks
    return tasks.filter(
      (t) =>
        t.name.toLowerCase().includes(kw) ||
        t.indicators.some((id) => {
          const item = workflows.find((workflow) => workflow.name === id)
          return (item ? workflowPath(item, groups) : id).toLowerCase().includes(kw)
        }) ||
        (t.remark || '').toLowerCase().includes(kw)
    )
  }, [tasks, keyword, workflows, groups])

  const goNext = async (): Promise<void> => {
    if (step === 0) {
      try {
        await form.validateFields()
      } catch {
        return
      }
    }
    setStep((current) => Math.min(current + 1, EDITOR_STEPS.length - 1))
  }

  const openCreate = (): void => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({
      name: '',
      login: '',
      indicators: [],
      repeat: 'daily',
      weekdays: [1, 2, 3, 4, 5],
      time: dayjs('08:00', 'HH:mm'),
      enabled: true,
      remark: ''
    })
    setWindows({})
    setOutputWindows({})
    setPathTemplate(DEFAULT_PATH_TEMPLATE)
    setPathFolders([])
    setPathStructure('')
    setStructureBlocked(false)
    setStructureSeen(false)
    setStep(0)
    setModalOpen(true)
  }

  const openEdit = (task: AutoTask): void => {
    setEditing(task)
    form.setFieldsValue({
      name: task.name,
      login: task.login || '',
      indicators: task.indicators,
      repeat: task.repeat,
      weekdays: task.weekdays || [1, 2, 3, 4, 5],
      time: dayjs(task.time, 'HH:mm'),
      enabled: task.enabled,
      remark: task.remark || ''
    })
    const saved = Object.fromEntries(
      Object.entries(task.indicatorWindows || {}).map(([name, window]) => [name, [window.start, window.end] as [number, number]])
    )
    setWindows(saved)
    setOutputWindows(Object.fromEntries(
      Object.entries(task.outputWindows || {}).map(([key, window]) => [key, [window.start, window.end] as [number, number]])
    ))
    setPathTemplate(task.pathTemplate || '')
    setPathFolders(task.pathFolders || [])
    setPathStructure(task.pathStructure || '')
    setStructureBlocked(false)
    setStructureSeen(false)
    setStep(0)
    setModalOpen(true)
  }

  const handleSubmit = async (): Promise<void> => {
    if (structureBlocked) {
      message.error('存储结构有错误，先在「存储结构」里处理')
      setStep(2)
      return
    }
    const savedAsFolders = !structureSeen && !pathStructure.trim() && selectedIndicators.some((name) =>
      (workflows.find((item) => item.name === name)?.outputs || []).some((output) => /[\\/]/.test(output.save_as || '')))
    if (savedAsFolders) {
      // 运行时已忽略 save_as 里的目录，不确认结构的话同名文件会落到根目录互相覆盖
      message.warning('工作流 save_as 里写了目录，先确认存储结构再保存')
      setStep(2)
      return
    }
    try {
      const values = await form.validateFields()
      const indicatorWindows: Record<string, { start: number; end: number }> = {}
      let dayWindow: { start: number; end: number } | undefined
      const legacy = Boolean(editing) && Object.keys(editing?.indicatorWindows || {}).length === 0
      for (const name of values.indicators || []) {
        const workflow = workflows.find((item) => item.name === name)
        const month = workflow?.date_kind === 'month'
        const week = workflow?.date_kind === 'week'
        const fallback: [number, number] = month
          ? (legacy ? [-1, -1] : [workflow?.window_start ?? -1, workflow?.window_end ?? -1])
          : week
            ? [workflow?.window_start ?? 0, workflow?.window_end ?? 0]
          : legacy
            ? offsetsFromBackfill(editing?.backfillDays ?? 1, editing?.coverDays ?? 0)
            : [workflow?.window_start ?? -1, workflow?.window_end ?? -1]
        const span = month ? clampMonthSpan(windows[name] || fallback)
          : week ? clampWeekSpan(windows[name] || fallback) : (windows[name] || fallback)
        indicatorWindows[name] = { start: span[0], end: span[1] }
        if (!month && !week && !dayWindow) dayWindow = indicatorWindows[name]
      }
      const savedOutputWindows: Record<string, { start: number; end: number }> = {}
      for (const [key, span] of Object.entries(outputWindows)) {
        if ((values.indicators || []).some((name) => key.startsWith(`${name}::`))) {
          savedOutputWindows[key] = { start: span[0], end: span[1] }
        }
      }
      const payload: AutoTask = {
        id: editing?.id || uid(),
        name: values.name.trim(),
        login: values.login || '',
        indicators: values.indicators,
        repeat: values.repeat,
        weekdays: values.repeat === 'weekly' ? values.weekdays : undefined,
        time: values.time.format('HH:mm'),
        backfillDays: dayWindow ? (dayWindow.start < 0 ? -dayWindow.start : 0) : (editing?.backfillDays ?? 1),
        coverDays: dayWindow ? (dayWindow.end >= 0 ? dayWindow.end + 1 : 0) : (editing?.coverDays ?? 0),
        indicatorWindows,
        outputWindows: savedOutputWindows,
        pathTemplate,
        // 下载根目录统一用设置里的全局目录
        downloadDir: '',
        pathFolders,
        pathStructure,
        enabled: values.enabled,
        remark: values.remark?.trim() || undefined,
        createdAt: editing?.createdAt || Date.now()
      }
      const next = editing
        ? tasks.map((t) => (t.id === editing.id ? payload : t))
        : [payload, ...tasks]
      persist(next)

      // 同时保存为后端 YAML 任务文件（含调度信息）
      try {
        const weekdaysStr = payload.weekdays?.length
          ? `\nweekdays:\n${payload.weekdays.map((d: number) => `  - ${d}`).join('\n')}`
          : ''
        const yamlText = [
          `name: "${payload.name}"`,
          `description: "${payload.remark || ''}"`,
          `login: ${payload.login || ''}`,
          `start_page: "https://pm.gd.csg.cn/portal/#/home"`,
          `stop_on_error: true`,
          `enabled: ${payload.enabled ? 'true' : 'false'}`,
          `repeat: ${payload.repeat}`,
          `time: "${payload.time}"`,
          `backfill_days: ${payload.backfillDays}`,
          `cover_days: ${payload.coverDays}`,
          weekdaysStr,
          'indicator_windows:',
          ...Object.entries(payload.indicatorWindows || {}).flatMap(([name, window]) => [
            `  ${JSON.stringify(name)}:`,
            `    start: ${window.start}`,
            `    end: ${window.end}`
          ]),
          ...(Object.keys(payload.outputWindows || {}).length ? ['output_windows:'] : []),
          ...Object.entries(payload.outputWindows || {}).flatMap(([key, window]) => [
            `  ${JSON.stringify(key)}:`,
            `    start: ${window.start}`,
            `    end: ${window.end}`
          ]),
          `indicators:`,
          ...payload.indicators.map((i: string) => `  - ${i}`),
          '',
        ].filter(l => l !== '').join('\n')
        await api.saveTask(payload.id, { name: payload.name, yaml_text: yamlText })
      } catch { /* ignore if backend not available */ }

      setModalOpen(false)
      message.success(editing ? '已保存任务' : '任务已创建')
    } catch {
      // 校验错误都在基本信息里，跳回去让人看到提示
      setStep(0)
    }
  }

  const handleDelete = (task: AutoTask): void => {
    modal.confirm({
      title: `删除任务"${task.name}"？`,
      content: '删除后将不再按计划执行，已生成的历史数据不受影响。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => {
        persist(tasks.filter((t) => t.id !== task.id))
        deleteTaskConfig(task.id)
        message.success('已删除')
      }
    })
  }

  const toggleEnabled = (task: AutoTask, enabled: boolean): void => {
    persist(tasks.map((t) => (t.id === task.id ? { ...t, enabled } : t)))
  }

  const runNow = (task: AutoTask): void => {
    // 用任务名作为 task 标识
    setTaskProgressName(task.id)
    setTaskProgressTitle(task.name)
    setTaskProgressOpen(true)
  }

  // ---- 导入任务配置（多文件 JSON） ----
  const handleImportTaskConfigs = useCallback(async () => {
    const results = await window.api.pickFiles([{ name: 'JSON', extensions: ['json'] }])
    if (!results || results.length === 0) return

    const files: Record<string, unknown>[] = []
    let hasError = false
    for (const r of results) {
      if (r.error) {
        message.error(`读取文件失败: ${r.error}`)
        hasError = true
        continue
      }
      try {
        const parsed = JSON.parse(r.content)
        if (!parsed.id) parsed.id = r.path.split(/[\\/]/).pop()?.replace(/\.json$/, '') || uid()
        files.push(parsed)
      } catch {
        message.error(`JSON 解析失败: ${r.path}`)
        hasError = true
      }
    }

    if (files.length === 0) {
      if (!hasError) message.warning('没有可导入的文件')
      return
    }

    try {
      const res = await api.importTaskConfigs(files)
      const msg = res.imported.length > 0 ? `已导入: ${res.imported.join(', ')}` : ''
      const err = res.errors.length > 0 ? ` | 失败: ${res.errors.join('; ')}` : ''
      message.success(msg + err)
      // 刷新列表
      const remote = await loadTasksFromApi()
      if (remote.length > 0) setTasks(remote)
    } catch (e: unknown) {
      message.error('导入失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [message])

  // ---- 导出全部任务配置 ----
  const handleExportAllTaskConfigs = useCallback(() => {
    window.open(api.exportAllTaskConfigs())
  }, [])

  return (
    <div style={{ padding: '0 16px' }}>
      {/* 顶部工具栏 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 16,
          gap: 12
        }}
      >
        <Input.Search
          placeholder="搜索任务名称、指标或备注"
          allowClear
          style={{ maxWidth: 360 }}
          onChange={(e) => setKeyword(e.target.value)}
          onSearch={setKeyword}
        />
        <Space>
          <Tooltip title="重新加载本地任务">
            <Button icon={<ReloadOutlined />} onClick={async () => {
              const remote = await loadTasksFromApi()
              if (remote.length > 0) setTasks(remote)
            }} />
          </Tooltip>
          <Button icon={<ImportOutlined />} onClick={handleImportTaskConfigs}>
            导入
          </Button>
          <Button icon={<ExportOutlined />} onClick={handleExportAllTaskConfigs}>
            导出全部
          </Button>
          <Button icon={<CalendarOutlined />} onClick={() => {
            setCatchupSeed((n) => n + 1)
            setCatchupOpen(true)
          }}>
            补数据
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增任务
          </Button>
        </Space>
      </div>

      {/* 卡片网格 */}
      {filtered.length === 0 ? (
        <Card style={{ textAlign: 'center', padding: '40px 0' }}>
          <Empty
            description={keyword ? '没有匹配的任务' : '暂无自动化任务，点击右上角"新增任务"开始'}
          />
        </Card>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 360px))',
            gap: 16
          }}
        >
          {filtered.map((task) => {
            const next = task.enabled ? nextRun(task) : null
            const dateLines = describeTaskDateLines(task, workflows, groups)
            return (
              <Card
                key={task.id}
                styles={{ body: { padding: 18 } }}
                style={{ borderRadius: 12 }}
                hoverable
              >
                {/* 右上角操作 */}
                <div
                  style={{
                    position: 'absolute',
                    top: 12,
                    right: 12,
                    display: 'flex',
                    gap: 4
                  }}
                >
                  <Tooltip title="编辑">
                    <Button
                      type="text"
                      size="small"
                      icon={<EditOutlined />}
                      onClick={() => openEdit(task)}
                    />
                  </Tooltip>
                  <Tooltip title="删除">
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() => handleDelete(task)}
                    />
                  </Tooltip>
                </div>

                {/* 标题 */}
                <div
                  style={{
                    fontSize: 16,
                    fontWeight: 600,
                    marginBottom: 12,
                    paddingRight: 64,
                    color: '#1A1D21'
                  }}
                >
                  {task.name}
                </div>

                {/* 信息行 */}
                <div style={{ fontSize: 13, color: '#5A6068', lineHeight: 1.9 }}>
                  <InfoLine
                    icon={<CalendarOutlined />}
                    label="调度"
                    value={formatSchedule(task)}
                  />
                  <InfoLine
                    icon={<ReloadOutlined />}
                    label="日期"
                    value={<Tooltip overlayStyle={{ maxWidth: 'min(80vw, 700px)' }} title={
                      <div>{dateLines.map((line, index) => <div key={index} style={{ whiteSpace: 'nowrap' }}>{line}</div>)}</div>
                    }>
                      <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {dateLines.join('；')}
                      </span>
                    </Tooltip>}
                  />
                  <InfoLine
                    icon={<FolderOutlined />}
                    label="存储"
                    value={task.pathStructure?.trim() ? '自定义结构（YAML）' : describeStorage(task.pathTemplate)}
                  />
                  <InfoLine
                    icon={<ClockCircleOutlined />}
                    label="下次执行"
                    value={
                      task.enabled && next ? next.format('YYYY-MM-DD HH:mm') : '已禁用'
                    }
                  />
                </div>

                {/* 状态 + 操作 */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginTop: 16,
                    paddingTop: 12,
                    borderTop: '1px solid #F0F1F2'
                  }}
                >
                  <Space size={8}>
                    <Switch
                      checked={task.enabled}
                      size="small"
                      onChange={(v) => toggleEnabled(task, v)}
                    />
                    <Badge
                      status={task.enabled ? 'success' : 'default'}
                      text={
                        <span style={{ fontSize: 13, color: task.enabled ? '#10B981' : '#8A8F95' }}>
                          {task.enabled ? '已启用' : '已禁用'}
                        </span>
                      }
                    />
                  </Space>
                  <Button type="primary" size="small" icon={<PlayCircleOutlined />}
                    onClick={() => runNow(task)}>
                    立即执行
                  </Button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {/* 新增/编辑 Modal */}
      <Modal
        title={editing ? '编辑任务' : '新建采集任务'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        width={1100}
        destroyOnClose
        maskClosable={false}
        styles={{ body: { display: 'flex', flexDirection: 'column', maxHeight: '72vh', minHeight: 0 } }}
        footer={[
          <Button key="cancel" onClick={() => setModalOpen(false)}>取消</Button>,
          step > 0 && <Button key="prev" onClick={() => setStep(step - 1)}>上一步</Button>,
          step < EDITOR_STEPS.length - 1 && (
            <Button key="next" type={editing ? 'default' : 'primary'} onClick={goNext}>下一步</Button>
          ),
          (editing || step === EDITOR_STEPS.length - 1) && (
            <Button key="ok" type="primary" onClick={handleSubmit}>{editing ? '保存' : '创建任务'}</Button>
          )
        ]}
      >
        <Steps
          size="small"
          current={step}
          style={{ flexShrink: 0, margin: '8px 0 16px' }}
          onChange={(next) => {
            // 新建只能回头改，往前走要经过「下一步」校验；编辑时随便跳
            if (editing || next < step) setStep(next)
          }}
          items={EDITOR_STEPS.map((title, index) => ({ title, disabled: !editing && index > step }))}
        />
        {/* 步骤条固定，只有下面的内容滚动 */}
        <div className="task-editor-scroll">
        <div style={{ display: step === 0 ? 'block' : 'none' }}>
        <Form
          form={form}
          layout="vertical"
          requiredMark="optional"
          className="task-editor"
          style={{ marginTop: 8 }}
          initialValues={{
            repeat: 'daily',
            enabled: true
          }}
        >
          <div className="task-form-grid">
            <Form.Item label="任务名称" name="name" rules={[{ required: true, message: '请输入任务名称' }]}>
              <Input placeholder="例如：现货出清电量采集" maxLength={64} showCount value={nameWatch} />
            </Form.Item>
            <Form.Item label="登录方式" name="login" tooltip="选择用于登录的工作流">
              <Select
                placeholder="选择登录工作流"
                allowClear
                options={workflows.filter(w => w.type === 'login').map((v) => ({ label: v.label || v.name, value: v.name }))}
              />
            </Form.Item>
          </div>

          <Form.Item
            label="采集指标"
            name="indicators"
            rules={[
              { required: true, message: '至少选择一个指标' },
              {
                validator: (_, val: string[]) =>
                  val && val.length > 0
                    ? Promise.resolve()
                    : Promise.reject(new Error('至少选择一个指标'))
              }
            ]}
          >
            <IndicatorTreeSelect workflows={workflows} groups={groups} placeholder="选择采集指标" />
          </Form.Item>

          <div className="task-form-grid task-schedule-grid">
          <Form.Item label="执行时间" required style={{ marginBottom: 8 }}>
            <Space.Compact style={{ width: '100%' }}>
              <Form.Item name="repeat" noStyle>
                <Select
                  style={{ width: 120 }}
                  options={[
                    { label: '每天', value: 'daily' },
                    { label: '每周', value: 'weekly' }
                  ]}
                />
              </Form.Item>
              <Form.Item
                name="time"
                noStyle
                rules={[{ required: true, message: '请选择时间' }]}
              >
                <TimePicker
                  format="HH:mm"
                  minuteStep={5}
                  style={{ flex: 1 }}
                  placeholder="选择时间"
                  needConfirm={false}
                />
              </Form.Item>
            </Space.Compact>
          </Form.Item>

          <Form.Item label="状态" name="enabled" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="关闭" />
          </Form.Item>
          </div>

          {repeatWatch === 'weekly' && (
            <Form.Item
              name="weekdays"
              rules={[
                {
                  validator: (_, val: number[]) =>
                    val && val.length > 0 ? Promise.resolve() : Promise.reject(new Error('请选择星期'))
                }
              ]}
              style={{ marginBottom: 12 }}
            >
              <Checkbox.Group
                options={WEEKDAY_LABELS.map((label, idx) => ({
                  label,
                  // 业务习惯：周一到周日的展示顺序，但 value 仍按 dayjs 习惯（0=周日）
                  value: idx
                })).sort((a, b) => {
                  const order = (v: number): number => (v === 0 ? 7 : v)
                  return order(a.value) - order(b.value)
                })}
              />
            </Form.Item>
          )}

          <div className="task-schedule-summary">
            执行计划：
            <strong>
              {summarizeForm({
                repeat: repeatWatch,
                weekdays: weekdaysWatch,
                time: timeWatch
              })}
            </strong>
          </div>

          <Form.Item label="备注" name="remark">
            <Input.TextArea
              rows={2}
              maxLength={200}
              showCount
              placeholder="可选：补充说明"
            />
          </Form.Item>
        </Form>
        </div>

        {step === 1 && (
          <>
            <div className="task-window-help" style={{ marginBottom: 12 }}>
              D = 运行当天，M = 运行当月。修改仅影响当前任务，不会更改指标配置；补数据时选择的日期会覆盖任务范围。
            </div>
            {selectedIndicators.length === 0 ? (
              <div className="task-window-empty">先在「基本信息」里选择采集指标，再设置各自的时间范围。</div>
            ) : (
              selectedIndicators.map((name) => {
                const workflow = workflows.find((item) => item.name === name)
                const month = workflow?.date_kind === 'month'
                const week = workflow?.date_kind === 'week'
                const fallback: [number, number] = [
                  workflow?.window_start ?? (week ? 0 : -1),
                  workflow?.window_end ?? (week ? 0 : -1)
                ]
                const span = windows[name] || fallback
                const update = (next: [number, number]): void => setWindows((prev) => ({ ...prev, [name]: next }))
                return (
                  <div key={name} className="task-window-block">
                    <div className="task-window-row">
                      <div className="task-window-label">
                        <Tag color={month ? 'cyan' : week ? 'purple' : 'blue'}>{month ? '按月' : week ? '按周' : '按日'}</Tag>
                        <span>{workflow ? workflowPath(workflow, groups) : name}</span>
                      </div>
                      {month ? (
                        <MonthWindowSlider compact value={span} onChange={update} />
                      ) : week ? (
                        <WeekWindowSlider compact value={span} onChange={update} />
                      ) : (
                        <DateWindowSlider compact value={span} onChange={update} />
                      )}
                      <span className="task-window-range">
                        {month
                          ? describeMonthWindow(...clampMonthSpan(span))
                          : week ? describeWeekWindow(...clampWeekSpan(span))
                          : describeWindow(span[0], span[1])}
                      </span>
                    </div>
                    {!month && !week && (
                      <OutputWindowList
                        indicator={name}
                        outputs={workflow?.outputs || []}
                        fallback={span}
                        value={outputWindows}
                        onChange={setOutputWindows}
                      />
                    )}
                  </div>
                )
              })
            )}
          </>
        )}

        {step === 2 && (
          <StoragePathModal
            embedded
            open
            root={settingsStore.get('downloadDir', '')}
            structure={pathStructure}
            template={pathTemplate}
            folders={pathFolders}
            indicators={selectedIndicators}
            workflows={workflows}
            indicatorWindows={windows}
            onChange={(nextStructure, blocked) => {
              setPathStructure(nextStructure)
              setStructureBlocked(blocked)
              setStructureSeen(true)
            }}
          />
        )}
        </div>
      </Modal>

      <CatchupModal
        key={catchupSeed}
        open={catchupOpen}
        workflows={workflows}
        groups={groups}
        defaultLogin={defaultLoginOf(tasks, workflows)}
        onCancel={() => setCatchupOpen(false)}
        onRun={(selection) => {
          const groups = [
            ...Object.values(selection.indicatorDates),
            ...Object.values(selection.indicatorMonths)
          ].filter((item) => item.length > 0)
          const same = groups.length > 0 && groups.every((item) => item.join() === groups[0].join())
          const title = same
            ? (groups[0].length <= 4 ? groups[0].join('、') : `${groups[0][0]} … ${groups[0][groups[0].length - 1]}`)
            : `${selection.indicators.length} 个指标分别补数`
          setCatchupTitle(`补数据：${title}`)
          setCatchupRun({
            login: selection.login,
            indicators: selection.indicators,
            indicatorDates: selection.indicatorDates,
            indicatorMonths: selection.indicatorMonths,
            outputDates: selection.outputDates
          })
          setCatchupOpen(false)
          setTaskProgressOpen(false)
          setCatchupProgressOpen(true)
        }}
      />

      <ProgressModal
        open={taskProgressOpen}
        indicatorLabels={Object.fromEntries(workflows.map((item) => [item.name, workflowPath(item, groups)]))}
        workflowName={taskProgressName}
        title={taskProgressTitle}
        headless={false}
        minimized={false}
        isTask={true}
        onClose={() => setTaskProgressOpen(false)}
      />
      <ProgressModal
        open={catchupProgressOpen}
        indicatorLabels={Object.fromEntries(workflows.map((item) => [item.name, workflowPath(item, groups)]))}
        workflowName="补数据"
        title={catchupTitle}
        headless={false}
        minimized={false}
        isTask={true}
        catchup={catchupRun}
        onClose={() => setCatchupProgressOpen(false)}
      />
    </div>
  )
}

interface OutputWindowListProps {
  indicator: string
  outputs: WorkflowOutput[]
  /** 指标自己的范围，导出没另配时跟随它 */
  fallback: [number, number]
  value: Record<string, [number, number]>
  onChange: React.Dispatch<React.SetStateAction<Record<string, [number, number]>>>
}

function OutputWindowList({ indicator, outputs, fallback, value, onChange }: OutputWindowListProps): JSX.Element | null {
  const prefix = `${indicator}::`
  const own = Object.keys(value).filter((key) => key.startsWith(prefix))
  const known = new Set(outputs.map((item) => item.key))
  const stale = own.filter((key) => !known.has(key.slice(prefix.length)))
  const [open, setOpen] = useState(own.length > 0)
  if (outputs.length < 2 && stale.length === 0) return null
  const remove = (key: string): void => onChange((prev) => {
    const next = { ...prev }
    delete next[key]
    return next
  })
  return (
    <OutputPanel
      title="按导出另配"
      count={own.length - stale.length}
      total={outputs.length}
      open={open}
      onToggle={() => setOpen((prev) => !prev)}
      extra={stale.length > 0 && <Tag color="red" bordered={false}>{stale.length} 项已失效</Tag>}
    >
      {outputs.map((output) => {
        const key = `${prefix}${output.key}`
        const span = value[key]
        // 跟随 = 指标里给这个导出写的范围，没写就是指标范围
        const follow: [number, number] = output.window ? [output.window[0], output.window[1]] : fallback
        return (
          <div key={key} className={`task-output-item${output.in_loop ? '' : ' is-muted'}`}>
            <OutputName output={output} duplicateHint="同一工作流里有重名导出，去工作流给这一步补上 name 才能稳定区分" />
            {output.in_loop ? (
              <>
                <Segmented
                  size="small"
                  value={span ? 'own' : 'follow'}
                  options={[{ label: '跟随', value: 'follow' }, { label: '单独', value: 'own' }]}
                  onChange={(mode: string) => mode === 'own'
                    ? onChange((prev) => ({ ...prev, [key]: [...follow] as [number, number] }))
                    : remove(key)}
                />
                {span ? (
                  <div className="task-output-detail">
                    <div className="task-output-slider">
                      <DateWindowSlider
                        compact
                        value={span}
                        onChange={(next) => onChange((prev) => ({ ...prev, [key]: next }))}
                      />
                    </div>
                    <span className="task-window-range">{describeWindow(span[0], span[1])}</span>
                  </div>
                ) : (
                  <span className="task-output-follow">
                    {output.window ? '指标里为它配的' : '同指标'}：<strong>{describeWindow(follow[0], follow[1])}</strong>
                  </span>
                )}
              </>
            ) : (
              <>
                <span />
                <span className="task-output-follow">
                  {output.in_loop === undefined ? '后端没返回循环信息，重启后端后再配' : '不在日期循环里，跟指标跑一次'}
                </span>
              </>
            )}
          </div>
        )
      })}
      {stale.map((key) => (
        <div key={key} className="task-output-item is-muted">
          <div className="task-output-name">
            <span className="task-output-label">{key.slice(prefix.length)}</span>
            <Tag color="red" bordered={false}>已失效</Tag>
          </div>
          <Button type="link" danger size="small" style={{ justifySelf: 'start', paddingLeft: 0 }} onClick={() => remove(key)}>删除</Button>
          <span className="task-output-follow">
            工作流里找不到这个导出，运行时按指标范围：{describeWindow(...value[key])}
          </span>
        </div>
      ))}
    </OutputPanel>
  )
}

interface InfoLineProps {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
}

function InfoLine({ icon, label, value }: InfoLineProps): JSX.Element {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
      <span style={{ color: '#8A8F95', fontSize: 12, flexShrink: 0 }}>{icon}</span>
      <span style={{ color: '#8A8F95', minWidth: 64, flexShrink: 0 }}>{label}</span>
      <span style={{ color: '#1A1D21', minWidth: 0, flex: 1 }}>{value}</span>
    </div>
  )
}
