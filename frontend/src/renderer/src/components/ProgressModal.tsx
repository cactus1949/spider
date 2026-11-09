import { useEffect, useState, useRef } from 'react'
import { Modal, Button, Space, Tag, Typography } from 'antd'
import {
  LoadingOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  PlayCircleOutlined,
  PauseCircleOutlined,
  ExclamationCircleOutlined
} from '@ant-design/icons'
import { settingsStore } from '../services/api'
import { createDownloadLedger, showExecutionResult } from '../services/executionResult'

const { Text } = Typography

export interface StepProgress {
  step: number
  total: number
  action: string
  summary: string
  status: 'pending' | 'running' | 'done' | 'error'
  error?: string
}

export interface CatchupRun {
  login: string
  indicators: string[]
  indicatorDates: Record<string, string[]>
  indicatorMonths: Record<string, string[]>
  /** 单个导出另选的日期，键是 指标::输出key；空列表 = 不补 */
  outputDates?: Record<string, string[]>
  /** 这次补数的下载根目录。空则由后端再用全局设置。 */
  downloadDir?: string
  pathTemplate?: string
  pathFolders?: { name: string; indicators: string[] }[]
  pathStructure?: string
}

interface Props {
  open: boolean
  workflowName: string
  headless: boolean
  minimized?: boolean
  isTask?: boolean
  catchup?: CatchupRun | null
  title?: string
  indicatorLabels?: Record<string, string>
  onClose: () => void
}

const BASE = 'http://localhost:8800'

export default function ProgressModal({ open, workflowName, headless, minimized = false, isTask, catchup = null, title, indicatorLabels = {}, onClose }: Props): JSX.Element {
  const [steps, setSteps] = useState<StepProgress[]>([])
  const [running, setRunning] = useState(false)
  const [needInput, setNeedInput] = useState(false)
  const [pauseMessage, setPauseMessage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const readerRef = useRef<ReadableStreamDefaultReader | null>(null)
  const abortedRef = useRef(false)
  const runRef = useRef<{ run_id: string; pause_version: number } | null>(null)
  const notifiedRef = useRef(false)
  const ledgerRef = useRef(createDownloadLedger())
  const terminalRef = useRef(false)
  const taskStatsRef = useRef<{
    done: number
    failed: number
    seen: Map<string, 'running' | 'done' | 'error' | 'pending'>
  }>({ done: 0, failed: 0, seen: new Map() })
  const indicatorLabelsRef = useRef(indicatorLabels)
  indicatorLabelsRef.current = indicatorLabels

  const notifyOnce = (result: { title: string; status: 'success' | 'warning' | 'error'; name: string; message?: string }): void => {
    if (notifiedRef.current) return
    notifiedRef.current = true
    showExecutionResult({ ...result, files: ledgerRef.current.finish() })
  }

  useEffect(() => {
    if (!open || !workflowName) return

    // Reset
    setSteps([])
    setRunning(true)
    setNeedInput(false)
    setPauseMessage('')
    setError(null)
    abortedRef.current = false
    runRef.current = null
    notifiedRef.current = false
    ledgerRef.current.reset()
    terminalRef.current = false
    taskStatsRef.current = { done: 0, failed: 0, seen: new Map() }

    const run = async (): Promise<void> => {
      try {
        // 读取设置中的证书密码等
        const settings = await settingsStore.load()
        const certPwd = settings.certPassword || ''
        const accountUser = settings.account || ''
        const accountPwd = settings.accountPassword || ''
        const dlDir = settings.downloadDir || ''
        const params = new URLSearchParams()
        if (certPwd) params.set('cert_password', certPwd)
        if (accountUser) params.set('account', accountUser)
        if (accountPwd) params.set('account_password', accountPwd)
        // Task runs resolve their own root first, then the saved global root.
        if (dlDir && !isTask && !catchup) params.set('download_dir', dlDir)
        params.set('headless', String(headless))
        params.set('minimized', String(minimized))

        const basePath = isTask ? '/api/tasks' : '/api/workflows'
        const url = catchup
          ? `${BASE}/api/catchup/run/stream`
          : `${BASE}${basePath}/${encodeURIComponent(workflowName)}/run/stream`
        const payload = catchup
          ? {
              ...Object.fromEntries(params),
              login: catchup.login,
              indicators: catchup.indicators,
              indicator_dates: catchup.indicatorDates,
              indicator_months: catchup.indicatorMonths,
              output_dates: catchup.outputDates || {},
              download_dir: catchup.downloadDir || '',
              path_template: catchup.pathTemplate || '',
              path_folders: catchup.pathFolders || [],
              path_structure: catchup.pathStructure || '',
              // 对话框里看到的路径就是这次要用的，空结构也不再回退到任务
              storage_overridden: true
            }
          : Object.fromEntries(params)
        const res = await fetch(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        })
        if (!res.ok || !res.body) {
          let detail = ''
          try {
            const body = await res.json() as { detail?: string }
            detail = body.detail || ''
          } catch { /* 非 JSON 错误页 */ }
          setError(detail || `请求失败: HTTP ${res.status}`)
          setRunning(false)
          return
        }

        const reader = res.body.getReader()
        readerRef.current = reader
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
                handleEvent(data)
              } catch { /* skip malformed */ }
            }
          }
        }
      } catch (err: unknown) {
        if (!abortedRef.current) {
          setError(`连接失败: ${err instanceof Error ? err.message : String(err)}`)
        }
      } finally {
        setRunning(false)
      }
    }

    run()

    return () => {
      abortedRef.current = true
      readerRef.current?.cancel()
    }
  }, [open, workflowName, headless, minimized, isTask, catchup])

  const handleEvent = (data: Record<string, unknown>): void => {
    if (data.run_id) runRef.current = { run_id: String(data.run_id), pause_version: Number(data.pause_version || 0) }
    const event = data.event as string
    ledgerRef.current.note(event, data, indicatorLabelsRef.current)

    switch (event) {
      case 'browser_start':
        // 任务模式下不重置步骤，追加显示
        if (!isTask) {
          setSteps([])
        }
        break

      case 'task_step': {
        // 任务级步骤（登录/指标切换）
        const phase = data.phase as string
        const name = data.name as string
        const status = data.status as string
        const message = data.message as string

        const statKey = `${phase}:${name}:${data.target_date || ''}:${data.step || ''}`
        const previousStatus = taskStatsRef.current.seen.get(statKey)
        const nextStatus = status === 'done' || status === 'error' || status === 'running' || status === 'skip'
          ? (status === 'skip' ? 'done' : status)
          : 'pending'
        if (previousStatus === 'done') taskStatsRef.current.done -= 1
        if (previousStatus === 'error') taskStatsRef.current.failed -= 1
        if (nextStatus === 'done') taskStatsRef.current.done += 1
        if (nextStatus === 'error') taskStatsRef.current.failed += 1
        taskStatsRef.current.seen.set(statKey, nextStatus)

        setSteps((prev) => {
          const icon = phase === 'login' ? '🔑' : '📊'
          const label = phase === 'login' ? '登录' : (indicatorLabelsRef.current[name] || name)
          const summary = `${icon} ${label}: ${message || ''}`

          // 更新或新增任务步骤
          const idx = prev.findIndex(
            (s) => s.action === `__task_${phase}_${name}`
          )
          if (idx >= 0) {
            const updated = [...prev]
            updated[idx] = {
              ...updated[idx],
              summary,
              status: status === 'running' ? 'running'
                    : status === 'done' ? 'done'
                    : status === 'error' ? 'error'
                    : status === 'skip' ? 'done'
                    : 'pending',
              error: status === 'error' ? message : undefined
            }
            return updated
          }
          return [...prev, {
            step: prev.length + 1,
            total: prev.length + 1,
            action: `__task_${phase}_${name}`,
            summary,
            status: status === 'running' ? 'running'
                  : status === 'done' ? 'done'
                  : status === 'error' ? 'error'
                  : status === 'skip' ? 'done'
                  : 'pending',
          }]
        })
        break
      }

      case 'repeat_date': {
        setSteps((prev) => [...prev, {
          step: prev.length + 1,
          total: prev.length + 1,
          action: 'repeat',
          summary: String(data.summary || data.date || ''),
          status: 'done' as const
        }])
        break
      }

      case 'step_start': {
        setSteps((prev) => {
          if (isTask) {
            // 任务模式：追加步骤（不清空，不按索引覆盖）
            const summary = `${data.summary || data.action || ''}`
            return [...prev, {
              step: prev.length + 1,
              total: prev.length + 1,
              action: data.action as string,
              summary,
              status: 'running' as const
            }]
          }
          // 单工作流模式：按索引更新
          const stepIdx = (data.step as number) - 1
          const updated = [...prev]
          while (updated.length <= stepIdx) {
            updated.push({
              step: updated.length + 1,
              total: data.total as number,
              action: '',
              summary: '',
              status: 'pending'
            })
          }
          updated[stepIdx] = {
            step: data.step as number,
            total: data.total as number,
            action: data.action as string,
            summary: data.summary as string,
            status: 'running'
          }
          return updated
        })
        break
      }

      case 'step_done': {
        setSteps((prev) => {
          if (isTask) {
            // 任务模式：最后一个 running 状态的步骤标记完成
            const updated = [...prev]
            for (let i = updated.length - 1; i >= 0; i--) {
              if (updated[i].status === 'running') {
                updated[i] = { ...updated[i], status: 'done' }
                break
              }
            }
            return updated
          }
          const stepIdx = (data.step as number) - 1
          const updated = [...prev]
          if (updated[stepIdx]) {
            updated[stepIdx] = { ...updated[stepIdx], status: 'done' }
          }
          return updated
        })
        break
      }

      case 'step_error': {
        setSteps((prev) => {
          if (isTask) {
            const updated = [...prev]
            for (let i = updated.length - 1; i >= 0; i--) {
              if (updated[i].status === 'running') {
                updated[i] = { ...updated[i], status: 'error', error: data.error as string }
                break
              }
            }
            return updated
          }
          const stepIdx = (data.step as number) - 1
          const updated = [...prev]
          if (updated[stepIdx]) {
            updated[stepIdx] = { ...updated[stepIdx], status: 'error', error: data.error as string }
          }
          return updated
        })
        break
      }

      case 'station_start':
      case 'station_done':
      case 'station_switch':
      case 'station_wifi':
        setPauseMessage(String(data.message || ''))
        break

      case 'resumed':
        setNeedInput(false)
        setError(null)
        setPauseMessage('')
        break

      case 'need_login':
      case 'waiting_input':
        setNeedInput(true)
        setPauseMessage((data.message as string) || '请手动操作后继续...')
        break

      case 'stale_tabs_warning':
      case 'gateway_rejected':
      case 'page_recovery': {
        const msg = ((data.message as string) || (data.error as string) || (data.reason as string) || event)
        if (event === 'page_recovery') {
          setPauseMessage(msg)
        } else {
          setError(msg)
          setPauseMessage(msg)
        }
        setSteps((prev) => {
          const updated = [...prev]
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].status === 'running' || updated[i].status === 'error') {
              updated[i] = { ...updated[i], error: msg }
              break
            }
          }
          return updated
        })
        break
      }

      case 'error_pause': {
        const msg = ((data.message as string) || (data.error as string) || '执行出现错误，已暂停')
        setNeedInput(true)
        setPauseMessage(msg)
        setError(msg)
        setSteps((prev) => {
          const updated = [...prev]
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].status === 'running') {
              updated[i] = { ...updated[i], status: 'error', error: msg }
              break
            }
          }
          return updated
        })
        break
      }

      case 'site_unreachable': {
        const msg = ((data.message as string) || (data.error as string) || '目标站点不可达，请检查网络或随身 WiFi')
        setNeedInput(false)
        setError(msg)
        setRunning(false)
        setSteps((prev) => {
          const updated = [...prev]
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].status === 'running') {
              updated[i] = { ...updated[i], status: 'error', error: msg }
              break
            }
          }
          return updated
        })
        break
      }

      case 'captcha_failed': {
        const msg = ((data.error as string) || '验证码识别失败')
        const shot = data.screenshot ? `\n截图：${data.screenshot as string}` : ''
        const fullMsg = `${msg}${shot}`
        setError(fullMsg)
        setSteps((prev) => {
          const updated = [...prev]
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].status === 'running') {
              updated[i] = { ...updated[i], status: 'error', error: fullMsg }
              break
            }
          }
          return updated
        })
        break
      }

      case 'aborted': {
        const msg = (data.message as string) || '执行已中止'
        setNeedInput(false)
        setError(msg)
        setRunning(false)
        terminalRef.current = true
        notifyOnce({
          title: '执行已停止',
          status: 'warning',
          name: title || workflowName,
          message: msg
        })
        setSteps((prev) => {
          const updated = [...prev]
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].status === 'running') {
              updated[i] = { ...updated[i], status: 'error', error: msg }
              break
            }
          }
          return updated
        })
        break
      }

      case 'workflow_done':
        terminalRef.current = true
        if (!isTask) {
          setRunning(false)
          notifyOnce({
            title: '指标执行完成',
            status: 'success',
            name: title || workflowName,
            message: '工作流已成功完成'
          })
        }
        break

      case 'workflow_error':
        if (!isTask) {
          terminalRef.current = true
          setError(data.error as string)
          setRunning(false)
          notifyOnce({
            title: '指标执行失败',
            status: 'error',
            name: title || workflowName,
            message: String(data.error || '工作流执行失败')
          })
        }
        break

      case 'task_done':
        terminalRef.current = true
        setRunning(false)
        notifyOnce({
          title: '执行完成',
          status: taskStatsRef.current.failed > 0 ? 'warning' : 'success',
          name: title || workflowName,
          message: `成功 ${taskStatsRef.current.done} 项，失败 ${taskStatsRef.current.failed} 项`
        })
        break

      case 'task_error':
        terminalRef.current = true
        setError(data.error as string)
        setRunning(false)
        notifyOnce({
          title: '执行失败',
          status: 'error',
          name: title || workflowName,
          message: String(data.error || '任务执行失败')
        })
        break
    }
  }

  const controlRun = async (action: 'resume' | 'cancel'): Promise<void> => {
    if (!runRef.current) return
    try {
      const res = await fetch(`${BASE}/api/browser/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(runRef.current)
      })
      const result = await res.json()
      if (!res.ok || result.status === 'error') throw new Error(result.message || result.detail || '操作失败')
      if (action === 'resume') setNeedInput(false)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }
  const handleContinue = (): void => { void controlRun('resume') }
  const handleStop = (): void => { void controlRun('cancel') }

  const statusIcon = (status: string): React.ReactNode => {
    switch (status) {
      case 'running': return <LoadingOutlined style={{ color: '#1677ff' }} />
      case 'done': return <CheckCircleOutlined style={{ color: '#52c41a' }} />
      case 'error': return <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
      default: return <span style={{ color: '#d9d9d9' }}>○</span>
    }
  }

  return (
    <Modal
      title={
        <Space>
          <PlayCircleOutlined />
          <span>{title || `执行${isTask ? '任务' : '工作流'}：${workflowName}`}</span>
          {running && <Tag color="processing">运行中</Tag>}
          {!running && steps.length > 0 && !error && <Tag color="success">完成</Tag>}
          {error && <Tag color="error">出错</Tag>}
        </Space>
      }
      open={open}
      onCancel={onClose}
      footer={
        <Space>
          {running && (
            <Button danger onClick={handleStop} icon={<PauseCircleOutlined />}>
              停止
            </Button>
          )}
          {needInput && (
            <Button type="primary" onClick={handleContinue} icon={<PlayCircleOutlined />}>
              继续
            </Button>
          )}
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
      width={560}
      maskClosable={false}
    >
      {/* Error */}
      {error && (
        <div style={{
          background: '#fff2f0', border: '1px solid #ffccc7', borderRadius: 6,
          padding: '10px 14px', marginBottom: 16
        }}>
          <Space>
            <ExclamationCircleOutlined style={{ color: '#ff4d4f' }} />
            <Text type="danger">{error}</Text>
          </Space>
        </div>
      )}

      {/* Login prompt */}
      {needInput && (
        <div style={{
          background: '#e6f4ff', border: '1px solid #91caff', borderRadius: 6,
          padding: '12px 16px', marginBottom: 16
        }}>
          <Text strong style={{ color: '#1677ff' }}>
            ⏸️ {pauseMessage}
          </Text>
        </div>
      )}

      {/* Step list */}
      {steps.length === 0 && running && (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <LoadingOutlined style={{ fontSize: 32, color: '#1677ff' }} />
          <div style={{ marginTop: 12, color: '#999' }}>正在启动浏览器...</div>
        </div>
      )}

      {steps.length > 0 && (
        <div style={{ maxHeight: 400, overflow: 'auto' }}>
          {steps.map((s, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
                padding: '8px 0',
                borderBottom: i < steps.length - 1 ? '1px solid #f5f5f5' : 'none',
                opacity: s.status === 'pending' ? 0.4 : 1
              }}
            >
              <div style={{ marginTop: 2 }}>{statusIcon(s.status)}</div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: s.status === 'running' ? 600 : 400 }}>
                  {s.step}. {s.summary || s.action || '...'}
                  {s.status === 'running' && (
                    <LoadingOutlined style={{ marginLeft: 8, color: '#1677ff' }} />
                  )}
                </div>
                {s.error && (
                  <Text type="danger" style={{ fontSize: 11 }}>{s.error}</Text>
                )}
              </div>
              <Tag style={{ fontSize: 10 }}>
                {s.status === 'running' ? '执行中' : s.status === 'done' ? '完成' : s.status === 'error' ? '失败' : '等待'}
              </Tag>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}
