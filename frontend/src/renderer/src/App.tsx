import { useEffect, useRef, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { App as AntApp, Button, Progress } from 'antd'
import AppLayout from './layouts/AppLayout'
import Files from './pages/Files'
import Tasks from './pages/Tasks'
import Indicators from './pages/Indicators'
import ExecutionLogs from './pages/ExecutionLogs'
import RecordWorkshop from './pages/RecordWorkshop'
import ExecutionResultModal from './components/ExecutionResultModal'
import { UploadProgressHost } from './components/UploadProgress'
import { api, settingsStore } from './services/api'
import { filesFromLog, showExecutionResult, uploadOfferFromLog } from './services/executionResult'

export default function App(): JSX.Element {
  const { notification: notif } = AntApp.useApp()
  const [downloadState, setDownloadState] = useState<{
    percent: number
    version: string
  } | null>(null)

  // 启动时从 SQLite 加载设置到内存缓存
  useEffect(() => { void settingsStore.load() }, [])

  // 调度器在后台执行时没有进度弹窗，轮询只监听 scheduled_auto，避免和手动执行重复通知。
  const seenScheduledRef = useRef<Set<string>>(new Set())
  const scheduledBaselineRef = useRef(false)
  useEffect(() => {
    let disposed = false
    const poll = async (): Promise<void> => {
      try {
        const result = await api.listExecutionLogs(1, 20, 'scheduled_auto')
        if (disposed) return
        const terminal = result.items.filter((item) => item.status === 'success' || item.status === 'failed')
        if (!scheduledBaselineRef.current) {
          terminal.forEach((item) => seenScheduledRef.current.add(item.exec_id))
          scheduledBaselineRef.current = true
          return
        }
        for (const item of terminal) {
          if (seenScheduledRef.current.has(item.exec_id)) continue
          seenScheduledRef.current.add(item.exec_id)
          let files = [] as ReturnType<typeof filesFromLog>
          let upload: ReturnType<typeof uploadOfferFromLog>
          try {
            const detail = await api.getExecutionLog(item.exec_id)
            files = filesFromLog(detail.detail_log || '')
            upload = uploadOfferFromLog(detail.detail_log || '')
          } catch {
            // 详情取不到时仍弹出结果，树为空。
          }
          showExecutionResult({
            title: item.status === 'success' ? '定时任务执行完成' : '定时任务执行失败',
            status: item.status === 'success' ? 'success' : 'error',
            name: item.name,
            message: item.status === 'success' ? '任务已成功完成' : (item.reason || '任务执行失败'),
            files,
            upload
          })
        }
      } catch {
        // 后端暂不可用时静默重试，不干扰主界面。
      }
    }
    void poll()
    const timer = window.setInterval(() => { void poll() }, 5000)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    const offAvail = window.api.onUpdateAvailable((info) => {
      const key = `update-${info.version}`
      notif.info({
        key,
        message: `发现新版本 v${info.version}`,
        description: info.notes ? info.notes.slice(0, 120) : '点击下载并安装更新',
        duration: 0,
        btn: (
          <div style={{ display: 'flex', gap: 8 }}>
            <Button
              type="primary"
              size="small"
              onClick={async () => {
                notif.destroy(key)
                setDownloadState({ percent: 0, version: info.version })
                try {
                  const result = await window.api.downloadUpdate()
                  if (!result.ok) {
                    setDownloadState(null)
                    notif.error({ message: '下载失败', description: result.error })
                  }
                } catch {
                  setDownloadState(null)
                  notif.error({ message: '下载失败，请检查网络' })
                }
              }}
            >
              下载更新
            </Button>
            <Button
              size="small"
              onClick={() => notif.destroy(key)}
            >
              稍后
            </Button>
          </div>
        )
      })
    })

    const offNone = window.api.onUpdateNone(() => {
      // 手动检查时由 AppLayout 处理通知
    })

    const offProgress = window.api.onDownloadProgress((info) => {
      setDownloadState((prev) =>
        prev ? { ...prev, percent: info.percent } : prev
      )
    })

    const offComplete = window.api.onDownloadComplete(async () => {
      setDownloadState(null)
      // electron-updater 下载完成后可以直接安装
      const result = await window.api.installUpdate()
      if (!result.ok) {
        notif.error({ message: '安装启动失败', description: result.error })
      }
    })

    const offError = window.api.onUpdateError((info) => {
      setDownloadState(null)
      notif.error({ message: '更新失败', description: info.message })
    })

    return () => {
      offAvail()
      offNone()
      offProgress()
      offComplete()
      offError()
    }
  }, [notif])

  return (
    <>
      <ExecutionResultModal />
      <UploadProgressHost />
      {downloadState && (
        <div
          style={{
            position: 'fixed',
            bottom: 16,
            right: 16,
            zIndex: 9999,
            background: '#fff',
            borderRadius: 12,
            padding: '16px 20px',
            boxShadow: '0 4px 20px rgba(0,0,0,0.15)',
            minWidth: 280
          }}
        >
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>
            正在下载 v{downloadState.version}...
          </div>
          <Progress percent={downloadState.percent} status="active" size="small" />
        </div>
      )}
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Tasks />} />
          <Route path="/tasks" element={<Navigate to="/" replace />} />
          <Route path="/files" element={<Files />} />
          <Route path="/indicators" element={<Indicators />} />
          <Route path="/execution-logs" element={<ExecutionLogs />} />
          <Route path="/record-workshop" element={<RecordWorkshop />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </>
  )
}
