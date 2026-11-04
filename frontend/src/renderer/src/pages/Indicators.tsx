import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Card,
  Button,
  Dropdown,
  Input,
  Select,
  Modal,
  Empty,
  Popconfirm,
  Radio,
  Space,
  Tag,
  Tooltip,
  Typography,
  App as AntApp
} from 'antd'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  PauseCircleOutlined,
  SaveOutlined,
  CodeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  EyeOutlined,
  ImportOutlined,
  ExportOutlined,
  LoginOutlined,
  ExperimentOutlined,
  ReloadOutlined,
  UnorderedListOutlined,
  CaretRightOutlined,
  DoubleRightOutlined,
  CopyOutlined,
  EyeInvisibleOutlined,
  CalendarOutlined
} from '@ant-design/icons'
import { api, settingsStore, type WorkflowInfo, type WorkflowDetail, type WorkflowGroup, type WorkflowOutput } from '../services/api'
import { createDownloadLedger, showExecutionResult } from '../services/executionResult'
import { filterWorkflowTree, workflowTree, workflowPath, type WorkflowNode } from '../components/workflowTree'
import { formatDOffset } from '../components/DateWindowSlider'
import DateProfileModal, { describeProfile, type DateProfileChoice } from '../components/DateProfileModal'

const { TextArea } = Input
const { Text } = Typography

// ── 步骤 YAML 工具 ──
function extractStepYaml(yamlText: string, stepIndex: number): string {
  const lines = yamlText.split('\n')
  let count = 0
  let start = -1
  let end = lines.length
  for (let i = 0; i < lines.length; i++) {
    if (/^- action:/.test(lines[i])) {
      if (count === stepIndex) start = i
      else if (count === stepIndex + 1) { end = i; break }
      count++
    }
  }
  if (start === -1) return ''
  return lines.slice(start, end).join('\n')
}

/** 这些动作会产出文件，和后端 OUTPUT_ACTIONS 保持一致 */
const OUTPUT_ACTIONS = new Set([
  'export', 'extract', 'extract_table', 'extract_echarts', 'extract_calendar',
  'extract_calendar_month_weeks',
  'download_notice_reports'
])

function readStepName(stepYaml: string): string {
  const line = stepYaml.split('\n').find((item) => /^  name:/.test(item))
  const raw = line ? line.replace(/^  name:\s*/, '').replace(/\s+#.*$/, '').trim() : ''
  if (raw.startsWith('"')) {
    try { return String(JSON.parse(raw)) } catch { return raw.slice(1, -1) }
  }
  return raw.replace(/^'(.*)'$/, '$1')
}

function writeStepName(stepYaml: string, name: string): string {
  const lines = stepYaml.split('\n')
  const index = lines.findIndex((item) => /^  name:/.test(item))
  const value = name.trim()
  if (!value) {
    if (index >= 0) lines.splice(index, 1)
  } else if (index >= 0) {
    lines[index] = `  name: ${JSON.stringify(value)}`
  } else {
    lines.splice(1, 0, `  name: ${JSON.stringify(value)}`)
  }
  return lines.join('\n')
}

function replaceStepYaml(yamlText: string, stepIndex: number, newYaml: string): string {
  const lines = yamlText.split('\n')
  let count = 0
  let start = -1
  let end = lines.length
  for (let i = 0; i < lines.length; i++) {
    if (/^- action:/.test(lines[i])) {
      if (count === stepIndex) start = i
      else if (count === stepIndex + 1) { end = i; break }
      count++
    }
  }
  if (start === -1) return yamlText
  const before = lines.slice(0, start)
  const after = lines.slice(end)
  return [...before, ...newYaml.split('\n'), ...after].join('\n')
}

function parseStepYaml(yaml: string): Record<string, unknown> {
  const lines = yaml.split('\n')
  const step: Record<string, unknown> = {}
  for (const line of lines) {
    const leadMatch = line.match(/^\s*-\s+(\w+):\s*(.*)/)
    if (leadMatch) {
      step[leadMatch[1]] = leadMatch[2].trim().replace(/^"(.*)"$/, '$1')
    } else {
      const m = line.match(/^\s+(\w+):\s*(.*)/)
      if (m) {
        const raw = m[2].trim()
        let v: unknown = raw.replace(/^"(.*)"$/, '$1')
        if (/^\d+$/.test(raw)) v = parseInt(raw)
        else if (raw === 'true') v = true
        else if (raw === 'false') v = false
        step[m[1]] = v
      }
    }
  }
  return step
}

function readProfile(yaml: string): { kind: 'day' | 'week' | 'month'; start: number; end: number } {
  const kindLine = yaml.match(/^date_kind:\s*(day|week|month)\s*$/m)
  let kind: 'day' | 'week' | 'month' = kindLine?.[1] === 'week' ? 'week' : kindLine?.[1] === 'month' ? 'month' : 'day'
  if (!kindLine && /previous_month_start/.test(yaml) && /previous_month_end/.test(yaml)) {
    kind = 'month'
  }
  const startText = yaml.match(/date_window:[\s\S]{0,120}?start:\s*(-?\d+)/)?.[1]
    ?? yaml.match(/action:\s*repeat[\s\S]{0,160}?start:\s*(-?\d+)/)?.[1]
  const endText = yaml.match(/date_window:[\s\S]{0,160}?end:\s*(-?\d+)/)?.[1]
    ?? yaml.match(/action:\s*repeat[\s\S]{0,200}?end:\s*(-?\d+)/)?.[1]
  return {
    kind,
    start: startText === undefined ? (kind === 'week' ? 0 : -1) : Number(startText),
    end: endText === undefined ? (kind === 'week' ? 0 : -1) : Number(endText)
  }
}

const ALL_ACTIONS = [
  'ensure_login', 'login', 'navigate', 'click', 'wait', 'pick_date', 'pick_date_range', 'repeat',
  'export', 'extract', 'extract_table', 'extract_echarts', 'extract_calendar', 'select_calendar_week',
  'extract_calendar_month_weeks',
  'switch_tab', 'close_tab', 'close_other_tabs', 'pause', 'screenshot',
  'fill', 'check', 'captcha', 'dismiss_modal', 'hover', 'select',
  'intercept_api', 'collect_api', 'api_call'
]

const ACTION_LABELS: Record<string, string> = {
  ensure_login: '检查手工登录', login: '登录', navigate: '跳转', click: '点击', wait: '等待',
  pick_date: '选择日期', pick_date_range: '日期范围', repeat: '按日期循环', export: '导出',
  extract: '提取', extract_table: '提取表格', extract_echarts: '提取图表', extract_calendar: '提取交易日历',
  extract_calendar_month_weeks: '按月逐周提取交易日历',
  select_calendar_week: '选择交易日历周',
  switch_tab: '切换标签', close_tab: '关闭标签', close_other_tabs: '关闭其他标签',
  pause: '暂停', screenshot: '截图', fill: '填入', check: '勾选',
  captcha: '验证码', dismiss_modal: '关闭弹窗', hover: '悬停', select: '选择',
  intercept_api: '拦截API', collect_api: '收集API', api_call: 'API调用'
}

export default function Indicators(): JSX.Element {
  const { message } = AntApp.useApp()
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([])
  const [groups, setGroups] = useState<WorkflowGroup[]>([])
  const [treeSearch, setTreeSearch] = useState('')
  const [openGroups, setOpenGroups] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem('workflow-open-groups') || '[]') } catch { return [] }
  })
  const [selected, setSelected] = useState<string | null>(null)
  // 拖拽移动：当前悬停的分组 id（'__root__' 表示树的空白区域，即移出分组）
  const [dragOverGroup, setDragOverGroup] = useState<string | null>(null)
  const [detail, setDetail] = useState<WorkflowDetail | null>(null)
  const [yamlText, setYamlText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [dateKind, setDateKind] = useState<'day' | 'week' | 'month'>('day')
  const [windowSpan, setWindowSpan] = useState<[number, number]>([-1, -1])
  const [profileBusy, setProfileBusy] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const [profileOutputs, setProfileOutputs] = useState<WorkflowOutput[]>([])

  // 浏览器共享模式
  const [browserRunning, setBrowserRunning] = useState(false)
  const [browserUrl, setBrowserUrl] = useState('')
  const [stepView, setStepView] = useState(false)  // true=步骤卡片视图, false=YAML视图
  const [outputLines, setOutputLines] = useState<string[]>([])
  const [expandedKeys, setExpandedKeys] = useState<Set<number>>(new Set())
  const [editBuffers, setEditBuffers] = useState<Record<number, string>>({})
  const [playing, setPlaying] = useState(false)
  const [playingStep, setPlayingStep] = useState(0)
  const [stepResults, setStepResults] = useState<Record<number, 'done' | 'error'>>({})
  const [disabledSteps, setDisabledSteps] = useState<Set<number>>(new Set())  // 被禁用的步骤索引（1-based）
  const [outputTotal, setOutputTotal] = useState(0)
  const outputRef = useRef<HTMLDivElement>(null)
  const stepRunRef = useRef<string | null>(null)
  const stepAbortRef = useRef<AbortController | null>(null)  // 用于取消正在执行的步骤

  // 重命名弹窗
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameTarget, setRenameTarget] = useState<string | null>(null)
  const [renameIsGroup, setRenameIsGroup] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [renameParent, setRenameParent] = useState<string | null>(null)

  // 新建弹窗
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createType, setCreateType] = useState<'login' | 'indicator' | 'group'>('indicator')
  const [createParent, setCreateParent] = useState<string | null>(null)
  const archiveInput = useRef<HTMLInputElement>(null)

  // ---- 加载工作流列表 ----
  const loadList = useCallback(async () => {
    try {
      const [list, groupList] = await Promise.all([api.listWorkflows(), api.listWorkflowGroups()])
      setWorkflows(list)
      setGroups(groupList)
      if (localStorage.getItem('workflow-open-groups') === null) setOpenGroups(groupList.filter((group) => !group.parent_id).map((group) => group.id))
    } catch {
      message.error('加载工作流列表失败')
    }
  }, [message])

  useEffect(() => {
    loadList()
  }, [loadList])

  useEffect(() => { if (groups.length || workflows.length) localStorage.setItem('workflow-open-groups', JSON.stringify(openGroups)) }, [openGroups, groups, workflows])

  // ---- 加载单个工作流详情 ----
  const loadDetail = useCallback(async (name: string) => {
    try {
      const d = await api.getWorkflow(name)
      setDetail(d)
      // 直接显示原始 YAML 文本
      const text = d.yaml_text || ''
      const profile = readProfile(text)
      setYamlText(text)
      setDateKind(profile.kind)
      setWindowSpan([profile.start, profile.end])
      setDirty(false)
    } catch {
      message.error('加载工作流失败')
    }
  }, [message])

  const applyProfile = useCallback(async ({ kind, start, end, outputWindows }: DateProfileChoice): Promise<boolean> => {
    if (!yamlText || detail?.type === 'login') return false
    setProfileBusy(true)
    try {
      const result = await api.applyDateProfile({
        yaml_text: yamlText, date_kind: kind, start, end, output_windows: outputWindows
      })
      if (result.kind !== kind || result.start !== start || result.end !== end || !result.outputs) {
        message.error('后端未应用所选日期窗口，请重启后端后重试')
        return false
      }
      setYamlText(result.yaml_text)
      setDateKind(result.kind)
      setWindowSpan([result.start, result.end])
      setProfileOutputs(result.outputs)
      setDetail((current) => current ? { ...current, yaml_text: result.yaml_text, steps: result.steps } : current)
      setDirty(true)
      return true
    } catch {
      message.error('日期窗口没有写进配置')
      return false
    } finally {
      setProfileBusy(false)
    }
  }, [yamlText, detail?.type, message])

  // YAML 改动后刷新按钮上的摘要（各导出是否单独配了范围）；编辑中 YAML 不合法时保留上一次
  useEffect(() => {
    if (!yamlText || detail?.type === 'login') {
      setProfileOutputs([])
      return
    }
    let alive = true
    const timer = window.setTimeout(() => {
      api.readDateProfile(yamlText)
        .then((profile) => { if (alive) setProfileOutputs(profile.outputs || []) })
        .catch(() => undefined)
    }, 400)
    return () => { alive = false; window.clearTimeout(timer) }
  }, [yamlText, detail?.type])


  // ---- 手风琴步骤编辑（多展开） ----
  const commitStep = useCallback((idx: number) => {
    if (!detail || editBuffers[idx] === undefined) return
    const curYaml = extractStepYaml(yamlText, idx)
    if (editBuffers[idx] === curYaml) return
    const newYamlText = replaceStepYaml(yamlText, idx, editBuffers[idx])
    setYamlText(newYamlText)
    const parsed = parseStepYaml(editBuffers[idx])
    if (Object.keys(parsed).length > 0) {
      const newSteps = [...detail.steps]
      newSteps[idx] = parsed
      setDetail({ ...detail, steps: newSteps })
    }
    setDirty(true)
  }, [editBuffers, yamlText, detail])

  const commitAllExpanded = useCallback(() => {
    for (const idx of expandedKeys) commitStep(idx)
  }, [expandedKeys, commitStep])

  const handleToggleStep = useCallback((i: number) => {
    setExpandedKeys(prev => {
      const next = new Set(prev)
      if (next.has(i)) {
        commitStep(i)
        next.delete(i)
        setEditBuffers(p => { const n = { ...p }; delete n[i]; return n })
      } else {
        next.add(i)
        setEditBuffers(p => ({ ...p, [i]: extractStepYaml(yamlText, i) }))
      }
      return next
    })
  }, [yamlText, commitStep])

  // ---- 选中工作流 ----
  const handleSelect = useCallback((name: string) => {
    // 取消正在执行的步骤流
    if (stepAbortRef.current) {
      stepAbortRef.current.abort()
      stepAbortRef.current = null
    }
    commitAllExpanded()
    setExpandedKeys(new Set())
    setEditBuffers({})
    setSelected(name)
    // 重置执行状态
    setPlaying(false)
    setPlayingStep(0)
    setStepResults({})
    setDisabledSteps(new Set())
    setOutputLines([])
    setOutputTotal(0)
    loadDetail(name)
  }, [loadDetail, commitAllExpanded])

  // ---- 保存 ----
  const handleSave = useCallback(async () => {
    if (!selected) return
    try {
      await api.saveWorkflow(selected, { name: detail?.label || selected, yaml_text: yamlText })
      setDirty(false)
      message.success('已保存')
    } catch (e: unknown) {
      message.error('保存失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [selected, detail, yamlText, message])

  // ---- 删除 ----
  const handleDelete = useCallback(async (name: string) => {
    try {
      await api.deleteWorkflow(name)
      if (selected === name) {
        setSelected(null)
        setDetail(null)
        setYamlText('')
      }
      message.success('已删除')
      loadList()
    } catch (e: unknown) {
      message.error('删除失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [selected, message, loadList])

  // ---- 新建 ----
  const handleCreate = useCallback(async () => {
    if (!createName.trim()) return
    if (createType === 'group') {
      try {
        const group = await api.createWorkflowGroup({ name: createName.trim(), parent_id: createParent })
        setCreateOpen(false)
        setCreateName('')
        if (createParent) setOpenGroups((prev) => [...new Set([...prev, createParent])])
        setOpenGroups((prev) => [...new Set([...prev, group.id])])
        await loadList()
        message.success('已创建分组')
      } catch (e: unknown) { message.error('创建失败: ' + (e instanceof Error ? e.message : String(e))) }
      return
    }
    const workflowId = `wf_${crypto.randomUUID()}`
    const isLogin = createType === 'login'
    const defaultYaml = isLogin ? `name: "${createName.trim()}"
type: login
start_from: main
description: ""
steps:
  - action: login
    url: "https://pm.gd.csg.cn/portal/#/home"
  - action: fill
    selector: "input[placeholder*='证书密码']"
    value: "\${CERT_PASSWORD}"
  - action: captcha
    captcha_img: "img[src^='data:image']"
    captcha_input: "#validateOnly_verifyCode"
    retry: 5
  - action: check
    selector: "input[type='checkbox']"
  - action: click
    selector: "button:has-text('登陆')"
  - action: wait
    selector: ".workspace, .el-menu"
    timeout: 30000
` : `name: "${createName.trim()}"
type: indicator
needs_login: true
start_from: dashboard
description: ""
steps:
  - action: click
    selector: "text=请替换"
  - action: export
    selector: "button:has-text('导出')"
    save_as: "{date}.xlsx"
    # name: 日前   # 选填：同一指标里多个导出文件名相同时用来区分，只用于任务的存储结构和采集范围
`
    try {
      await api.saveWorkflow(workflowId, { name: createName.trim(), yaml_text: defaultYaml, group_id: createParent })
      setCreateOpen(false)
      setCreateName('')
      message.success('已创建')
      await loadList()
      if (createParent) setOpenGroups((prev) => [...new Set([...prev, createParent])])
      handleSelect(workflowId)
    } catch (e: unknown) {
      message.error('创建失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [createName, createType, createParent, message, loadList, handleSelect])

  // ---- 导入 YAML（多文件） ----
  const handleImport = useCallback(async () => {
    const results = await window.api.pickFiles([{ name: 'YAML', extensions: ['yml', 'yaml'] }])
    if (!results || results.length === 0) return

    const files: { name: string; yaml_text: string }[] = []
    let hasError = false
    for (const r of results) {
      if (r.error) {
        message.error(`读取文件失败: ${r.error}`)
        hasError = true
        continue
      }
      // 从 YAML 中提取 name 字段作为工作流名
      let workflowName = ''
      try {
        const match = r.content.match(/^name:\s*["']?(.+?)["']?\s*$/m)
        workflowName = match ? match[1].trim() : ''
      } catch { /* ignore */ }
      if (!workflowName) {
        workflowName = r.path.split(/[\\/]/).pop()?.replace(/\.(yml|yaml)$/, '') || 'imported'
      }
      workflowName = workflowName.split('\n')[0].slice(0, 64)
      files.push({ name: workflowName, yaml_text: r.content })
    }

    if (files.length === 0) {
      if (!hasError) message.warning('没有可导入的文件')
      return
    }

    try {
      const res = await api.importWorkflows(files)
      const msg = res.imported.length > 0 ? `已导入: ${res.imported.join(', ')}` : ''
      const err = res.errors.length > 0 ? ` | 失败: ${res.errors.join('; ')}` : ''
      message.success(msg + err)
      loadList()
      if (res.imported.length > 0) handleSelect(res.imported[res.imported.length - 1])
    } catch (e: unknown) {
      message.error('导入失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [message, loadList, handleSelect])

  // ---- 导出全部 ----
  const handleExportAll = useCallback(() => {
    window.open(api.exportAllWorkflows())
  }, [])

  // ---- 重命名 ----
  const handleRename = useCallback(async () => {
    if (!renameTarget || !renameValue.trim()) {
      setRenameOpen(false)
      return
    }
    try {
      if (renameIsGroup) await api.updateWorkflowGroup(renameTarget, { name: renameValue.trim(), parent_id: renameParent })
      else await api.updateWorkflowMeta(renameTarget, { label: renameValue.trim(), group_id: renameParent })
      setRenameOpen(false)
      message.success('已更新')
      await loadList()
      if (!renameIsGroup && selected === renameTarget) setDetail((prev) => prev ? { ...prev, label: renameValue.trim(), group_id: renameParent } : prev)
    } catch (e: unknown) {
      message.error('编辑失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [renameTarget, renameValue, renameParent, renameIsGroup, selected, message, loadList])


  // ---- 共享浏览器 ----
  const BASE = 'http://localhost:8800'

  const checkBrowserStatus = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}/api/browser/status`)
      const data = await res.json()
      setBrowserRunning(data.running)
      if (data.current_url) setBrowserUrl(data.current_url)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    checkBrowserStatus()
    const timer = setInterval(checkBrowserStatus, 5000)
    return () => clearInterval(timer)
  }, [checkBrowserStatus])

  // ---- 步骤播放 ----
  const addOutput = (line: string) => {
    setOutputLines((prev) => [...prev, line])
    setOutputTotal((prev) => prev + 1)
    // 使用 requestAnimationFrame 确保 DOM 更新后再滚动
    requestAnimationFrame(() => {
      const el = outputRef.current
      if (!el) return
      // 如果用户在底部附近（50px以内），自动跟随滚动；否则用户在看历史，不打断
      const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50
      if (isNearBottom) {
        el.scrollTop = el.scrollHeight
      }
    })
  }

  const handlePlayStep = useCallback(async (stepIndex: number, mode: 'single' | 'from_here' | 'to_here') => {
    if (!selected) return
    // 取消之前的步骤执行
    if (stepAbortRef.current) {
      stepAbortRef.current.abort()
    }
    const abortController = new AbortController()
    stepAbortRef.current = abortController

    stepRunRef.current = null
    setPlaying(true)
    setOutputLines([])
    setOutputTotal(1)
    const modeLabel = mode === 'single' ? '执行此步' : mode === 'to_here' ? '执行到此' : '从此开始'
    addOutput(`▶ ${modeLabel} (步骤 ${stepIndex})...`)
    let notificationSent = false
    let completedSteps = 0
    let failedSteps = 0
    const ledger = createDownloadLedger()
    const indicatorLabel = detail?.label || selected
    const notifyOnce = (result: { title: string; status: 'success' | 'warning' | 'error'; message: string }): void => {
      if (notificationSent) return
      notificationSent = true
      showExecutionResult({ ...result, name: indicatorLabel, files: ledger.finish() })
    }

    try {
      const certPwd = settingsStore.get('certPassword', '')
      const downloadDir = settingsStore.get('downloadDir', '')
      const params = new URLSearchParams({ mode })
      if (certPwd) params.set('cert_password', certPwd)
      if (downloadDir) params.set('download_dir', downloadDir)
      // 全量执行时传递被禁用的步骤（单步执行忽略禁用标记）
      if (mode !== 'single' && disabledSteps.size > 0) {
        params.set('skip_steps', Array.from(disabledSteps).sort((a, b) => a - b).join(','))
      }
      const url = `${BASE}/api/workflows/${encodeURIComponent(selected)}/step/${stepIndex}/run/stream`
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(params)), signal: abortController.signal })
      if (!res.ok || !res.body) {
        addOutput(`❌ 请求失败: HTTP ${res.status}`)
        notifyOnce({
          title: '指标播放失败',
          status: 'error',
          message: `请求失败：HTTP ${res.status}`
        })
        setPlaying(false)
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))
              if (data.run_id) stepRunRef.current = data.run_id
              const evt = data.event
              ledger.note(String(evt || ''), data, { [selected]: indicatorLabel, [indicatorLabel]: indicatorLabel })
              if (evt === 'step_start') {
                setPlayingStep(data.step)
                setStepResults(prev => { const n = { ...prev }; delete n[data.step]; return n })
                addOutput(`⏳ ${data.step}/${data.total}: ${data.summary || data.action}`)
              } else if (evt === 'step_done') {
                completedSteps += 1
                setStepResults(prev => ({ ...prev, [data.step]: 'done' }))
                addOutput(`✅ 步骤 ${data.step} 完成`)
              } else if (evt === 'export_saved') {
                addOutput(`📁 已保存：${data.path}（${data.size} 字节；浏览器原始文件名：${data.original_name || '未提供'}）`)
              } else if (evt === 'calendar_saved') {
                addOutput(`📅 交易日历已保存：${data.path}（${data.month || '当月'}，${data.rows ?? '?'} 行）`)
              } else if (evt === 'error_pause' || evt === 'waiting_input') {
                addOutput(`⏸ ${data.message || data.error}；请使用顶部浏览器控制栏处理`)
              } else if (evt === 'stale_tabs_warning' || evt === 'gateway_rejected' || evt === 'page_recovery') {
                addOutput(`⚠ ${data.message || data.error || data.reason || evt}`)
              } else if (evt === 'resumed') {
                addOutput('▶ 正在重新检查并恢复')
              } else if (evt === 'step_error') {
                failedSteps += 1
                setStepResults(prev => ({ ...prev, [data.step]: 'error' }))
                addOutput(`❌ 步骤 ${data.step}: ${data.error}`)
              } else if (evt === 'workflow_done') {
                addOutput('🏁 完成')
                notifyOnce({
                  title: '指标播放完成',
                  status: failedSteps > 0 ? 'warning' : 'success',
                  message: `${modeLabel}已完成：成功 ${completedSteps} 步，失败 ${failedSteps} 步`
                })
              } else if (evt === 'error' || evt === 'workflow_error') {
                addOutput(`💥 ${data.error || data.message || '未知错误'}`)
                notifyOnce({
                  title: '指标播放失败',
                  status: 'error',
                  message: String(data.error || data.message || '未知错误')
                })
              } else if (evt === 'aborted') {
                addOutput(`⛔ ${data.message || '中止'}`)
                notifyOnce({
                  title: '指标播放已停止',
                  status: 'warning',
                  message: String(data.message || '执行已中止')
                })
              }
            } catch { /* skip */ }
          }
        }
      }
      checkBrowserStatus()
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        addOutput('⏹ 已取消')
      } else {
        addOutput('❌ ' + (e instanceof Error ? e.message : String(e)))
        notifyOnce({
          title: '指标播放失败',
          status: 'error',
          message: e instanceof Error ? e.message : String(e)
        })
      }
    } finally {
      setPlaying(false)
      setPlayingStep(0)
      stepAbortRef.current = null
    }
  }, [selected, checkBrowserStatus])

  // ---- Ctrl+S 快捷键 ----
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        if (dirty) handleSave()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [dirty, handleSave])

  const beginCreate = (parent: string | null): void => {
    setCreateParent(parent)
    setCreateName('')
    setCreateType('indicator')
    setCreateOpen(true)
  }

  const beginEdit = (id: string, isGroup: boolean): void => {
    const item = isGroup ? groups.find((group) => group.id === id) : workflows.find((workflow) => workflow.name === id)
    if (!item) return
    setRenameTarget(id)
    setRenameIsGroup(isGroup)
    setRenameValue(isGroup ? (item as WorkflowGroup).name : (item as WorkflowInfo).label)
    setRenameParent(isGroup ? (item as WorkflowGroup).parent_id : (item as WorkflowInfo).group_id || null)
    setRenameOpen(true)
  }

  // ---- 拖拽移动指标/登录/分组 ----
  const DRAG_MIME = 'application/x-spider-workflow'
  type DragPayload = { kind: 'group' | 'workflow'; id: string }
  const writeDrag = (dataTransfer: DataTransfer, payload: DragPayload): void => {
    dataTransfer.setData(DRAG_MIME, JSON.stringify(payload))
    dataTransfer.effectAllowed = 'move'
  }
  const readDrag = (dataTransfer: DataTransfer): DragPayload | null => {
    const raw = dataTransfer.getData(DRAG_MIME)
    if (!raw) return null
    try {
      const parsed = JSON.parse(raw) as DragPayload
      return parsed && (parsed.kind === 'group' || parsed.kind === 'workflow') && parsed.id ? parsed : null
    } catch {
      return null
    }
  }
  const handleMoveWorkflow = async (name: string, groupId: string | null): Promise<void> => {
    setDragOverGroup(null)
    const workflow = workflows.find((item) => item.name === name)
    if (!workflow || (workflow.group_id || null) === groupId) return
    try {
      // 后端 label 缺省会回退成 name，必须带上当前显示名
      await api.updateWorkflowMeta(name, { label: workflow.label || name, group_id: groupId })
      message.success(groupId ? '已移入分组' : '已移出分组')
      await loadList()
      if (groupId) setOpenGroups((prev) => [...new Set([...prev, groupId])])
      if (selected === name) setDetail((prev) => prev ? { ...prev, group_id: groupId } : prev)
    } catch (e: unknown) {
      message.error('移动失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }

  // 分组拖拽改上级：复用 PUT workflow-groups，后端自带环校验与同级同名校验
  const handleMoveGroup = async (id: string, parentId: string | null): Promise<void> => {
    setDragOverGroup(null)
    const group = groups.find((item) => item.id === id)
    if (!group || parentId === id || (group.parent_id || null) === parentId) return
    try {
      await api.updateWorkflowGroup(id, { name: group.name, parent_id: parentId })
      message.success(parentId ? '已移入分组' : '已移出分组')
      await loadList()
      if (parentId) setOpenGroups((prev) => [...new Set([...prev, parentId])])
    } catch (e: unknown) {
      message.error('移动失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }

  const handleDeleteGroup = async (id: string): Promise<void> => {
    try { await api.deleteWorkflowGroup(id); await loadList(); message.success('已删除分组') }
    catch (e: unknown) { message.error('删除失败: ' + (e instanceof Error ? e.message : String(e))) }
  }

  const importArchive = async (file?: File): Promise<void> => {
    if (!file) return
    try {
      const bytes = new Uint8Array(await file.arrayBuffer())
      let binary = ''
      for (const byte of bytes) binary += String.fromCharCode(byte)
      const result = await api.importWorkflowArchive(btoa(binary))
      await loadList()
      message.success(`已导入 ${result.imported.length} 项${result.errors.length ? `，失败 ${result.errors.length} 项：${result.errors.join('；')}` : ''}`)
    } catch (e: unknown) { message.error('导入 ZIP 失败: ' + (e instanceof Error ? e.message : String(e))) }
  }

  const tree = filterWorkflowTree(workflowTree(workflows, groups), treeSearch)
  const groupPath = (id: string): string => {
    const parts: string[] = []
    const seen = new Set<string>()
    let current: string | null = id
    while (current && !seen.has(current)) {
      seen.add(current)
      const group: WorkflowGroup | undefined = groups.find((item) => item.id === current)
      if (!group) break
      parts.unshift(group.name)
      current = group.parent_id
    }
    return parts.join(' / ')
  }
  const groupOptions = groups.map((group) => ({ value: group.id, label: groupPath(group.id) }))
  const renderNode = (node: WorkflowNode, depth = 0): JSX.Element => {
    const isGroup = !node.selectable
    const id = isGroup ? node.key.slice(6) : node.value
    const expanded = Boolean(treeSearch) || openGroups.includes(id)
    const workflow = isGroup ? undefined : workflows.find((item) => item.name === id)
    return <div key={node.key}>
      <div onClick={() => {
        if (isGroup) setOpenGroups((prev) => prev.includes(id) ? prev.filter((key) => key !== id) : [...prev, id])
        else handleSelect(id)
      }}
        draggable
        onDragStart={(event) => {
          event.stopPropagation()
          writeDrag(event.dataTransfer, { kind: isGroup ? 'group' : 'workflow', id })
        }}
        onDragEnd={() => setDragOverGroup(null)}
        onDragOver={isGroup ? (event) => {
          if (!event.dataTransfer.types.includes(DRAG_MIME)) return
          event.preventDefault()
          event.stopPropagation()
          event.dataTransfer.dropEffect = 'move'
          setDragOverGroup(id)
        } : undefined}
        onDragLeave={isGroup ? (event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragOverGroup(null)
        } : undefined}
        onDrop={isGroup ? (event) => {
          event.preventDefault()
          event.stopPropagation()
          const payload = readDrag(event.dataTransfer)
          if (!payload) return
          if (payload.kind === 'group') void handleMoveGroup(payload.id, id)
          else void handleMoveWorkflow(payload.id, id)
        } : undefined}
        style={{ display: 'flex', alignItems: 'center', minHeight: isGroup ? 40 : 36, paddingLeft: 8 + depth * 14,
        paddingRight: 4, cursor: 'pointer', background: isGroup ? (dragOverGroup === id ? '#e6f4ff' : '#f5f7fa') : selected === id ? '#e6f4ff' : undefined,
        borderBottom: isGroup ? '1px solid #e9edf2' : undefined,
        boxShadow: isGroup && dragOverGroup === id ? 'inset 0 0 0 1px #1677ff' : undefined,
        borderLeft: selected === id && !isGroup ? '3px solid #1677ff' : '3px solid transparent' }}>
        <span style={{ width: 17, flexShrink: 0 }}>{isGroup ? (expanded ? '▾' : '▸') : ''}</span>
        {isGroup && <span style={{ color: '#667085', marginRight: 6, display: 'inline-flex' }}>{expanded ? <FolderOpenOutlined /> : <FolderOutlined />}</span>}
        {!isGroup && <Tag color={workflow?.type === 'login' ? 'orange' : 'blue'} style={{ margin: '0 6px 0 0', padding: '0 4px', fontSize: 10, lineHeight: '17px', flexShrink: 0 }}>{workflow?.type === 'login' ? '登录' : '指标'}</Tag>}
        <span title={node.title} style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13,
          fontWeight: isGroup ? 600 : 400, color: isGroup ? '#344054' : undefined }}>
          {isGroup ? node.label : workflow?.label || id}
        </span>
        {isGroup && <Button type="text" size="small" icon={<PlusOutlined />} title="在此分组下增加" onClick={(event) => { event.stopPropagation(); beginCreate(id) }} />}
        <Button type="text" size="small" icon={<EditOutlined />} title="编辑" onClick={(event) => { event.stopPropagation(); beginEdit(id, isGroup) }} />
        <Popconfirm title="确定删除？" onConfirm={(event) => { event?.stopPropagation(); if (isGroup) void handleDeleteGroup(id); else void handleDelete(id) }}>
          <Button type="text" size="small" danger icon={<DeleteOutlined />} title="删除" onClick={(event) => event.stopPropagation()} />
        </Popconfirm>
      </div>
      {isGroup && expanded && node.children?.map((child) => renderNode(child, depth + 1))}
    </div>
  }

  // ================================================================
  return (
    <div style={{ display: 'flex', flex: 1, minHeight: 0, gap: 12, overflow: 'hidden', minWidth: 0 }}>
      {/* ====== 左侧：指标列表 ====== */}
      <Card
        size="small"
        title={
          <Space>
            <FileTextOutlined />
            <span>指标配置</span>
            <Tooltip title="刷新列表"><Button type="text" size="small" icon={<ReloadOutlined />} onClick={loadList} aria-label="刷新列表" /></Tooltip>
          </Space>
        }
        extra={
          <Tooltip title="增加分组、指标或登录">
            <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => beginCreate(null)} aria-label="增加" />
          </Tooltip>
        }
        style={{ width: 350, maxWidth: '40vw', flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}
        styles={{ body: { flex: 1, overflow: 'hidden', padding: 0, display: 'flex', flexDirection: 'column', minHeight: 0 } }}
      >
        <div style={{ padding: 8 }}><Input.Search placeholder="搜索名称或分组路径" value={treeSearch} onChange={(event) => setTreeSearch(event.target.value)} allowClear /></div>
        <input ref={archiveInput} type="file" accept=".zip" style={{ display: 'none' }} onChange={(event) => { void importArchive(event.target.files?.[0]); event.target.value = '' }} />
        <div
          style={{ flex: 1, minHeight: 0, overflowY: 'auto', background: dragOverGroup === '__root__' ? '#f0f7ff' : undefined }}
          onDragOver={(event) => {
            // 只响应空白区域：落在行/子容器上的事件不在此处理
            if (event.target !== event.currentTarget || !event.dataTransfer.types.includes(DRAG_MIME)) return
            event.preventDefault()
            event.dataTransfer.dropEffect = 'move'
            setDragOverGroup('__root__')
          }}
          onDragLeave={(event) => {
            if (event.target === event.currentTarget) setDragOverGroup(null)
          }}
          onDrop={(event) => {
            if (event.target !== event.currentTarget) return
            event.preventDefault()
            const payload = readDrag(event.dataTransfer)
            if (!payload) return
            if (payload.kind === 'group') void handleMoveGroup(payload.id, null)
            else void handleMoveWorkflow(payload.id, null)
          }}
        >
          {tree.length === 0 ? (
            <Empty description={treeSearch ? "没有匹配项目" : "暂无指标"} image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ marginTop: 40 }} />
          ) : tree.map((node) => renderNode(node))}
        </div>
        <div style={{ borderTop: '1px solid #f0f0f0', padding: '8px 12px', display: 'flex', gap: 8, flexShrink: 0 }}>
          <Dropdown menu={{ items: [
            { key: 'yaml', label: '导入 YAML', onClick: handleImport },
            { key: 'zip', label: '导入 ZIP', onClick: () => archiveInput.current?.click() }
          ] }}>
            <Button size="small" icon={<ImportOutlined />}>导入</Button>
          </Dropdown>
          <Button size="small" icon={<ExportOutlined />} onClick={handleExportAll}>导出全部</Button>
        </div>
      </Card>

      {/* ====== 右侧：YAML 编辑区 ====== */}
      <Card
        size="small"
        title={
          <Space>
            <CodeOutlined />
            <span>{detail ? workflowPath({ name: detail.name, label: detail.label, group_id: detail.group_id, type: detail.type, needs_login: detail.needs_login, start_from: detail.start_from, description: detail.description, steps_count: detail.steps.length }, groups) : '请选择一个指标'}</span>
            {dirty && <Text type="warning" style={{ fontSize: 12 }}>● 未保存</Text>}
          </Space>
        }
        extra={
          selected && (
            <Space>
              <Text type="secondary" style={{ fontSize: 11 }}>Ctrl+S 保存</Text>
              <Button type="primary" size="small" icon={<SaveOutlined />} disabled={!dirty} onClick={handleSave}>保存</Button>
            </Space>
          )
        }
        style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}
        styles={{ body: { flex: 1, minHeight: 0, padding: 8, overflow: 'hidden', display: 'flex', flexDirection: 'column' } }}
      >
        {!selected ? (
          <Empty description="← 从左侧列表选择一个指标" style={{ marginTop: 80 }} />
        ) : (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {/* 浏览器工具栏 */}
            <div style={{
              display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', flexShrink: 0,
              padding: '4px 8px',
              background: browserRunning ? '#f6ffed' : '#fafafa',
              borderRadius: 4, border: '1px solid #f0f0f0'
            }}>
              <Tag color={browserRunning ? 'green' : 'default'}>{browserRunning ? '使用顶部已连接会话' : '请先在顶部连接浏览器'}</Tag>
              {browserRunning && browserUrl && (
                <Text type="secondary" style={{ fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{browserUrl}</Text>
              )}
              <Button size="small" type="text" icon={stepView ? <EyeOutlined /> : <UnorderedListOutlined />}
                onClick={() => {
                  if (stepView) commitAllExpanded()
                  setExpandedKeys(new Set())
                  setEditBuffers({})
                  setStepView(!stepView)
                }}>{stepView ? 'YAML' : '步骤'}</Button>
              {stepView && (
                <>
                  {playing ? (
                    <Button size="small" type="text" danger
                      icon={<PauseCircleOutlined />}
                      onClick={() => {
                        if (stepRunRef.current) void fetch(`${BASE}/api/browser/cancel`, { method: 'POST',
                          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: stepRunRef.current }) })
                        stepAbortRef.current?.abort()
                        setPlaying(false)
                        addOutput('⏹ 用户手动停止')
                      }}
                    >停止</Button>
                  ) : (
                    <Button size="small" type="text" icon={<PlayCircleOutlined style={{ color: '#52c41a' }} />}
                      onClick={() => handlePlayStep(1, 'from_here')}>播放全部</Button>
                  )}
                </>
              )}
            </div>

            {detail?.type !== 'login' && (() => {
              const looped = profileOutputs.filter((output) => output.in_loop)
              const own = dateKind === 'day' ? looped.filter((output) => output.window).length : 0
              return (
                <div className="indicator-profile-bar">
                  <span className="indicator-profile-label"><CalendarOutlined />日期范围</span>
                  <Button size="small" loading={profileBusy} onClick={() => setProfileOpen(true)}>
                    {describeProfile(dateKind, windowSpan[0], windowSpan[1])}
                    <EditOutlined style={{ color: '#a0a6ad' }} />
                  </Button>
                  {own > 0 && <Tag color="blue" bordered={false}>{own}/{looped.length} 个导出单独配</Tag>}
                  <span className="indicator-profile-hint">直接执行时的默认范围，改完记得保存</span>
                </div>
              )
            })()}
            <DateProfileModal
              open={profileOpen}
              yamlText={yamlText}
              onCancel={() => setProfileOpen(false)}
              onOk={applyProfile}
            />

            {/* 主内容区 */}
            {stepView && detail ? (
              <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
                {detail.steps?.length === 0 ? (
                  <Empty description="暂无步骤，请切换到 YAML 编辑" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ marginTop: 40 }} />
                ) : (
                  detail.steps?.map((step, i) => {
                    const action = (step.action as string) || '?'
                    const label = action === 'repeat'
                      ? `${formatDOffset(Number(step.start ?? -1))} 至 ${formatDOffset(Number(step.end ?? -1))}`
                      : `${OUTPUT_ACTIONS.has(action) && step.name ? `[${String(step.name)}] ` : ''}${((step.selector || step.value || step.save_as || step.url || step.message || '') as string)}`.slice(0, 60)
                    const active = playing && playingStep === i + 1
                    const result = stepResults[i + 1]
                    const expanded = expandedKeys.has(i)
                    const bgColor = active ? '#e6f4ff' : result === 'done' ? '#f6ffed' : result === 'error' ? '#fff2f0' : expanded ? '#e6f4ff' : '#fafafa'
                    const borderColor = active ? '#91caff' : result === 'done' ? '#b7eb8f' : result === 'error' ? '#ffccc7' : expanded ? '#1677ff' : '#f0f0f0'
                    const c: Record<string, string> = { click: 'blue', export: 'red', pick_date: 'purple', repeat: 'geekblue', wait: 'orange', login: 'green', captcha: 'volcano', pause: 'gold' }
                    return (
                      <div key={i} style={{
                        marginBottom: 3,
                        borderRadius: 4,
                        border: `1px solid ${borderColor}`,
                        overflow: 'hidden'
                      }}>
                        {/* 头部：点击展开/收起 */}
                        <div
                          onClick={() => { if (!playing) handleToggleStep(i) }}
                          style={{
                            display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px',
                            background: bgColor,
                            cursor: playing ? 'default' : 'pointer',
                          }}>
                          {/* 眼睛图标：关闭则跳过（仅全量执行时生效，单步执行忽略） */}
                          <span style={{ flexShrink: 0, cursor: 'pointer' }}
                            onClick={(e) => {
                              e.stopPropagation()
                              setDisabledSteps(prev => {
                                const next = new Set(prev)
                                if (next.has(i + 1)) next.delete(i + 1)
                                else next.add(i + 1)
                                return next
                              })
                            }}
                            title={disabledSteps.has(i + 1) ? '已禁用（全量执行时跳过）' : '已启用（点击禁用）'}>
                            {disabledSteps.has(i + 1)
                              ? <EyeInvisibleOutlined style={{ fontSize: 13, color: '#ccc' }} />
                              : <EyeOutlined style={{ fontSize: 13, color: '#1677ff' }} />
                            }
                          </span>
                          <span style={{ width: 18, fontSize: 11, color: '#999', textAlign: 'center', flexShrink: 0 }}>{i + 1}</span>
                          <Tag color={c[action] || 'default'} style={{ fontSize: 10, lineHeight: '16px', padding: '0 4px', margin: 0, flexShrink: 0 }}>{action}</Tag>
                          <span style={{ flex: 1, minWidth: 0, fontSize: 11, color: '#333', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
                          <span style={{ fontSize: 10, color: '#bbb', flexShrink: 0, transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform .2s' }}>▶</span>
                          {!playing && (
                            <Space.Compact style={{ flexShrink: 0, marginLeft: 2 }}>
                              <Button type="text" size="small" style={{ padding: '0 2px', height: 22 }}
                                icon={<CaretRightOutlined style={{ fontSize: 11 }} />}
                                onClick={(e) => { e.stopPropagation(); handlePlayStep(i + 1, 'single') }} title="执行此步" />
                              <Button type="text" size="small" style={{ padding: '0 2px', height: 22 }}
                                icon={<DoubleRightOutlined style={{ fontSize: 11 }} />}
                                onClick={(e) => { e.stopPropagation(); handlePlayStep(i + 1, 'to_here') }} title="执行到此" />
                            </Space.Compact>
                          )}
                        </div>
                        {/* 展开的 YAML 编辑区 */}
                        {expanded && (
                          <div style={{ padding: '6px 8px', background: '#fff', borderTop: '1px solid #f0f0f0' }}>
                            <div style={{ marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                              <span style={{ fontSize: 11, color: '#888' }}>动作:</span>
                              <select
                                value={action}
                                onChange={(e) => {
                                  const newAction = e.target.value
                                  const buf = editBuffers[i] || ''
                                  const lines = buf.split('\n')
                                  for (let li = 0; li < lines.length; li++) {
                                    if (/^- action:/.test(lines[li])) {
                                      lines[li] = lines[li].replace(/action:\s*\S+/, `action: ${newAction}`)
                                      break
                                    }
                                  }
                                  setEditBuffers(p => ({ ...p, [i]: lines.join('\n') }))
                                }}
                                style={{ padding: '2px 6px', fontSize: 11, border: '1px solid #d9d9d9', borderRadius: 4 }}
                              >
                                {ALL_ACTIONS.map(a => <option key={a} value={a}>{ACTION_LABELS[a] ? `${a} (${ACTION_LABELS[a]})` : a}</option>)}
                              </select>
                              <div style={{ flex: 1 }} />
                              <Button type="primary" size="small" style={{ fontSize: 10, padding: '0 8px', height: 22 }}
                                onClick={(e) => { e.stopPropagation(); handleToggleStep(i) }}>
                                完成
                              </Button>
                            </div>
                            {OUTPUT_ACTIONS.has(action) && (
                              <div style={{ marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                                <span style={{ fontSize: 11, color: '#888', flexShrink: 0 }}>下载名:</span>
                                <Input
                                  size="small"
                                  allowClear
                                  value={readStepName(editBuffers[i] || '')}
                                  onChange={(e) => setEditBuffers(p => ({ ...p, [i]: writeStepName(p[i] || '', e.target.value) }))}
                                  placeholder="选填，如 日前；同一指标多个导出文件名相同时用来区分"
                                  style={{ fontSize: 11 }}
                                />
                              </div>
                            )}
                            <TextArea
                              value={editBuffers[i] || ''}
                              onChange={(e) => setEditBuffers(p => ({ ...p, [i]: e.target.value }))}
                              style={{
                                fontFamily: '"JetBrains Mono", "Fira Code", "Consolas", monospace',
                                fontSize: 11, lineHeight: 1.5, minHeight: 60, resize: 'vertical'
                              }}
                              spellCheck={false}
                            />
                          </div>
                        )}
                      </div>
                    )
                  })
                )}
              </div>
            ) : selected ? (
              <TextArea
                value={yamlText}
                onChange={(e) => { setYamlText(e.target.value); setDirty(true) }}
                style={{
                  flex: 1, minHeight: 0,
                  fontFamily: '"JetBrains Mono", "Fira Code", "Consolas", monospace',
                  fontSize: 13, lineHeight: 1.6, resize: 'none', border: 'none', background: '#fafafa', borderRadius: 4
                }}
                placeholder="- action: click&#10;  selector: text=现货交易&#10;  expect: new_tab"
                spellCheck={false}
              />
            ) : null}

            {/* 输出面板 */}
            {outputLines.length > 0 && (
              <div style={{ flexShrink: 0, height: 140, display: 'flex', flexDirection: 'column', borderTop: '1px solid #f0f0f0', paddingTop: 4 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2, flexShrink: 0 }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>输出 ({outputTotal} 行)</Text>
                  <Space size={4}>
                    <Button type="text" size="small" style={{ fontSize: 11 }}
                      icon={<CopyOutlined />}
                      onClick={() => {
                        const text = outputLines.join('\n')
                        navigator.clipboard.writeText(text).then(() => message.success('已复制'))
                          .catch(() => message.error('复制失败'))
                      }}>复制</Button>
                    <Button type="text" size="small" style={{ fontSize: 11 }} onClick={() => { setOutputLines([]); setOutputTotal(0) }}>清空</Button>
                  </Space>
                </div>
                <div ref={outputRef} style={{
                  flex: 1, overflow: 'auto', fontFamily: '"JetBrains Mono", monospace', fontSize: 11, lineHeight: 1.5,
                  background: '#1e1e1e', color: '#d4d4d4', borderRadius: 4, padding: '4px 8px'
                }}>
                  {outputLines.map((line, i) => (
                    <div key={i} style={{ color: line.startsWith('✅') ? '#4ec9b0' : line.startsWith('❌') || line.startsWith('💥') ? '#f14c4c' : line.startsWith('⏳') ? '#569cd6' : line.startsWith('⛔') ? '#ce9178' : line.startsWith('▶') || line.startsWith('🏁') ? '#dcdcaa' : '#d4d4d4' }}>{line}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ====== 重命名弹窗 ====== */}
      <Modal
        title={renameIsGroup ? '编辑分组' : '编辑指标/登录'}
        open={renameOpen}
        onOk={handleRename}
        onCancel={() => setRenameOpen(false)}
        okText="确定"
        cancelText="取消"
      >
        <Input
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          placeholder="输入新名称"
          onPressEnter={handleRename}
        />
        <div style={{ marginTop: 12, marginBottom: 5 }}>上级分组</div>
        <Select style={{ width: '100%' }} allowClear showSearch optionFilterProp="label" placeholder="根层" value={renameParent || undefined}
          onChange={(value) => setRenameParent(value || null)}
          options={groupOptions.filter((option) => !renameIsGroup || (option.value !== renameTarget && !groupPath(option.value).startsWith(`${groupPath(renameTarget || '')} /`)))} />
      </Modal>

      {/* ====== 新建弹窗 ====== */}
      <Modal
        title="新增"
        open={createOpen}
        onOk={handleCreate}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
        cancelText="取消"
      >
        <div style={{ marginBottom: 12 }}>
          <div style={{ marginBottom: 6, fontSize: 13, color: '#555' }}>类型</div>
          <Radio.Group
            value={createType}
            onChange={(e) => setCreateType(e.target.value)}
            buttonStyle="solid"
          >
            <Radio.Button value="group">分组</Radio.Button>
            <Radio.Button value="indicator">
              <ExperimentOutlined /> 指标
            </Radio.Button>
            <Radio.Button value="login">
              <LoginOutlined /> 登录
            </Radio.Button>
          </Radio.Group>
          <div style={{ fontSize: 11, color: '#999', marginTop: 4 }}>
            {createType === 'group' ? '分组可以包含子分组、指标和登录流程' : createType === 'login'
              ? '登录流程：从主界面 → 证书登录 → 工作台'
              : '指标流程：从已登录状态出发的数据采集步骤'}
          </div>
        </div>
        <Input
          value={createName}
          onChange={(e) => setCreateName(e.target.value)}
          placeholder="名称"
          onPressEnter={handleCreate}
        />
        <div style={{ marginTop: 12, marginBottom: 5 }}>上级分组</div>
        <Select style={{ width: '100%' }} allowClear showSearch optionFilterProp="label" placeholder="根层" value={createParent || undefined}
          onChange={(value) => setCreateParent(value || null)} options={groupOptions} />
      </Modal>
    </div>
  )
}
