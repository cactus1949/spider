import { useEffect, useState } from 'react'
import { Button, Modal, Progress, Tag } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudUploadOutlined,
  LoadingOutlined,
  ReloadOutlined
} from '@ant-design/icons'

const BASE = 'http://localhost:8800'

export interface UploadFileState {
  name: string
  source: string
  status: 'pending' | 'zipping' | 'ready' | 'uploading' | 'retrying' | 'success' | 'failed'
  message: string
  attempt: number
}

export interface UploadSnapshot {
  phase: string
  files: UploadFileState[]
  done: number
  total: number
  percent: number
  message: string
  ok: boolean | null
}

export interface UploadJob {
  profileId: string
  paths: string[]
  execId?: string
  title?: string
}

const STATUS_LABEL: Record<UploadFileState['status'], { color: string; label: string }> = {
  pending: { color: 'default', label: '等待' },
  zipping: { color: 'processing', label: '压缩中' },
  ready: { color: 'default', label: '就绪' },
  uploading: { color: 'processing', label: '上传中' },
  retrying: { color: 'warning', label: '重试中' },
  success: { color: 'success', label: '成功' },
  failed: { color: 'error', label: '失败' }
}

type Listener = (job: UploadJob | null) => void
let currentJob: UploadJob | null = null
const listeners = new Set<Listener>()

function publish(job: UploadJob | null): void {
  currentJob = job
  listeners.forEach((listener) => listener(job))
}

/** 打开共用的上传进度弹窗。文件中心和结果弹窗都走这里。 */
export function startDataUpload(job: UploadJob): void {
  publish(job)
}

export function subscribeDataUpload(listener: Listener): () => void {
  listeners.add(listener)
  listener(currentJob)
  return () => { listeners.delete(listener) }
}

export function UploadProgressView({
  snapshot,
  onRetry
}: {
  snapshot: UploadSnapshot | null
  onRetry?: () => void
}): JSX.Element {
  const files = snapshot?.files || []
  const percent = snapshot?.percent || 0
  const failed = snapshot?.ok === false
  return (
    <div className="up-panel">
      <Progress
        percent={percent}
        status={failed ? 'exception' : snapshot?.ok ? 'success' : 'active'}
        format={() => `${snapshot?.done || 0}/${snapshot?.total || files.length || 0}`}
      />
      {snapshot?.message && <div className="up-message">{snapshot.message}</div>}
      <div className="up-files">
        {files.map((file) => {
          const meta = STATUS_LABEL[file.status] || STATUS_LABEL.pending
          return (
            <div key={`${file.source}-${file.name}`} className={`up-file is-${file.status}`}>
              <span className="up-file-icon">
                {file.status === 'success' ? <CheckCircleOutlined /> : file.status === 'failed' ? <CloseCircleOutlined /> : <LoadingOutlined />}
              </span>
              <span className="up-file-name" title={file.source}>{file.name}</span>
              {file.message && <span className="up-file-message" title={file.message}>{file.message}</span>}
              <Tag color={meta.color} bordered={false}>{meta.label}</Tag>
            </div>
          )
        })}
        {files.length === 0 && <div className="up-empty">等待上传开始</div>}
      </div>
      {failed && onRetry && (
        <div className="up-retry">
          <Button icon={<ReloadOutlined />} onClick={onRetry}>重新上传</Button>
        </div>
      )}
    </div>
  )
}

async function readUploadStream(
  job: UploadJob,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal
): Promise<void> {
  const res = await fetch(`${BASE}/api/data-upload/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      profileId: job.profileId,
      paths: job.paths,
      execId: job.execId || '',
      name: job.title || '数据上传'
    }),
    signal
  })
  if (!res.ok || !res.body) {
    let detail = ''
    try {
      const body = await res.json() as { detail?: string }
      detail = body.detail || ''
    } catch { /* 非 JSON */ }
    throw new Error(detail || `请求失败: HTTP ${res.status}`)
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
      if (!line.startsWith('data: ')) continue
      try {
        onEvent(JSON.parse(line.slice(6)) as Record<string, unknown>)
      } catch { /* 跳过坏行 */ }
    }
  }
}

function snapshotFrom(event: Record<string, unknown>): UploadSnapshot {
  const files = Array.isArray(event.files) ? event.files as UploadFileState[] : []
  return {
    phase: String(event.phase || event.event || ''),
    files,
    done: Number(event.done || 0),
    total: Number(event.total || files.length || 0),
    percent: Number(event.percent || 0),
    message: String(event.message || ''),
    ok: event.ok == null ? null : Boolean(event.ok)
  }
}

export function UploadProgressHost(): JSX.Element {
  const [job, setJob] = useState<UploadJob | null>(null)
  const [runId, setRunId] = useState(0)
  const [snapshot, setSnapshot] = useState<UploadSnapshot | null>(null)
  const [running, setRunning] = useState(false)

  useEffect(() => subscribeDataUpload((next) => {
    setJob(next)
    setSnapshot(null)
    setRunId((value) => value + 1)
  }), [])

  useEffect(() => {
    if (!job || runId === 0) return
    const controller = new AbortController()
    setRunning(true)
    setSnapshot({
      phase: 'prepare',
      files: job.paths.map((path) => ({
        name: path.split(/[\\/]/).pop() || path,
        source: path,
        status: 'pending',
        message: '等待上传',
        attempt: 0
      })),
      done: 0,
      total: job.paths.length,
      percent: 0,
      message: '正在准备上传',
      ok: null
    })
    void readUploadStream(job, (event) => {
      if (event.event === 'upload_progress' || event.event === 'upload_settled') {
        setSnapshot((prev) => {
          const next = snapshotFrom(event)
          if (event.event === 'upload_settled' && next.files.length === 0 && prev) {
            return { ...prev, ok: next.ok, message: next.message || prev.message, percent: 100, phase: 'done' }
          }
          return next
        })
      }
    }, controller.signal).catch((error: unknown) => {
      if (controller.signal.aborted) return
      setSnapshot((prev) => ({
        phase: 'done',
        files: (prev?.files || []).map((file) => ({ ...file, status: 'failed', message: file.message || '上传失败' })),
        done: prev?.done || 0,
        total: prev?.total || job.paths.length,
        percent: 100,
        message: error instanceof Error ? error.message : String(error),
        ok: false
      }))
    }).finally(() => {
      if (!controller.signal.aborted) setRunning(false)
    })
    return () => controller.abort()
  }, [job, runId])

  return (
    <Modal
      open={Boolean(job)}
      title={<span><CloudUploadOutlined /> {job?.title || '上传进度'}</span>}
      centered
      width={720}
      zIndex={2100}
      maskClosable={false}
      onCancel={() => publish(null)}
      footer={
        <Button type="primary" disabled={running} onClick={() => publish(null)}>
          {running ? '上传中' : '关闭'}
        </Button>
      }
    >
      <UploadProgressView
        snapshot={snapshot}
        onRetry={!running && snapshot?.ok === false && job ? () => setRunId((value) => value + 1) : undefined}
      />
    </Modal>
  )
}
