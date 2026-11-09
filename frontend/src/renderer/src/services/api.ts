const BASE = 'http://localhost:8800'

export interface HealthCheck {
  module: string
  message: string
  status?: string
}
export interface Health {
  overall: string
  checks: HealthCheck[]
}

export interface TaskItem {
  indicator: string
  status: string
  date: string
  started_at?: string
  finished_at?: string
  error?: string
}
export interface Task {
  id: string
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'partial'
  created_at?: string
  finished_at?: string
  items?: TaskItem[]
  output_dir?: string
}

export interface WorkflowInfo {
  name: string
  label: string
  group_id?: string | null
  type: string
  needs_login: boolean
  start_from: string
  description: string
  steps_count: number
  date_kind?: 'day' | 'week' | 'month'
  window_start?: number
  window_end?: number
  has_repeat?: boolean
  output_mode?: 'export' | 'extract' | 'mixed' | ''
  output_name?: string
  outputs?: WorkflowOutput[]
}

export interface WorkflowOutput {
  key: string
  label: string
  save_as: string
  action: string
  step_index: number
  /** 步骤位置，如 s3 / s1.2 */
  position?: string
  /** 同一工作流里 name / save_as 文件名重复，需要在步骤上补 name 区分 */
  duplicate?: boolean
  /** 按日跑时落在日期循环里，可以单独配范围 / 补数日期 */
  in_loop?: boolean
  /** 指标 YAML 里给这个导出写的默认范围（导出步骤上的 start/end），没写为 null */
  window?: [number, number] | null
}

export interface DateProfileRead {
  kind: 'day' | 'week' | 'month'
  start: number
  end: number
  has_repeat: boolean
  outputs: WorkflowOutput[]
}

export interface DateProfileResult extends DateProfileRead {
  yaml_text: string
  steps: Record<string, unknown>[]
}

export interface WorkflowDetail {
  name: string
  label: string
  group_id?: string | null
  type: string
  needs_login: boolean
  start_from: string
  description: string
  steps: Record<string, unknown>[]
  yaml_text: string
}

export interface WorkflowSaveRequest {
  name: string
  yaml_text: string
  group_id?: string | null
}

export interface WorkflowGroup {
  id: string
  name: string
  parent_id: string | null
}

export interface UploadHeader {
  id: string
  name: string
  value: string
  secret: boolean
  saved: boolean
}

export interface UploadParam {
  id: string
  name: string
  value: string
  in: 'query' | 'form'
  secret: boolean
  saved: boolean
}

export interface UploadProfile {
  id: string
  name: string
  url: string
  fileFieldName: string
  headers: UploadHeader[]
  params: UploadParam[]
  wifiSsid: string
  wifiPassword: string
  wifiPasswordSaved: boolean
  wifiInterface: string
}

export interface UploadProfileTestResult {
  ok: boolean
  message: string
  attempts?: number
  alreadyConnected?: boolean
  restored?: boolean
  details?: string[]
}

export interface ExecutionLogItem {
  exec_id: string
  name: string
  type: 'workflow' | 'scheduled_auto' | 'scheduled_manual' | 'catchup' | 'data_upload'
  target_id: string
  status: 'running' | 'success' | 'failed'
  reason: string
  created_at: string
  updated_at: string
}

export interface ExecutionLogDetail extends ExecutionLogItem {
  detail_log: string
}

export interface ExecutionLogListResponse {
  items: ExecutionLogItem[]
  total: number
  page: number
  page_size: number
}

export interface WorkshopExport {
  export_id: string
  name: string
  filename: string
  path: string
  size: number
  modified_at: string
  status: 'pending' | 'submitted' | 'changed' | 'failed'
  submitted_at?: string
  file_hash?: string
}

export interface RouteRule {
  id?: number
  domain_pattern: string
  interface_name: string
  expected_ssid?: string
  enabled: boolean
}

export interface RouteApplyResult {
  status: string
  applied: number
  details: string[]
  message?: string
  results?: {
    domain: string
    interface_name: string
    status: string
    expected_ssid?: string
    current_ssid?: string
    resolved_ips: string[]
    stale_ips: string[]
    gateway?: string
    private_ok: boolean
    public_ok: boolean
    routes_ok: boolean
    error?: string
  }[]
}

export interface WifiTestResult {
  ok: boolean
  message: string
  details?: string[]
}

export interface WifiNetwork {
  ssid: string
  signal?: string
  security?: string
  current?: boolean
}

export interface WifiNetworksResult {
  networks: WifiNetwork[]
  current_ssid?: string
  error?: string
}

export interface CurrentWifiResult {
  ssid: string
  error?: string
}

export interface UsbDeviceOption {
  id: string
  name: string
  label: string
  vid?: string
  pid?: string
  ukeyCandidate?: boolean
  serial?: string
}

export interface UkeyCertificate {
  subject: string
  issuer: string
  notAfter: string
  thumbprint: string
  organization: string
}

export interface UkeyReport {
  summary: string
  distinguishable: boolean
  error?: string
  devices: UsbDeviceOption[]
  ukeys: UsbDeviceOption[]
  certificates: UkeyCertificate[]
}

export interface StationPickResult {
  ok: boolean
  cancelled?: boolean
  demo?: boolean
  message?: string
  station?: { id: string; name: string; wifiSsid: string }
}

export interface DeliveryTestResult {
  ok: boolean
  dry_run?: boolean
  sent?: boolean
  task_id?: string
  errors?: string[]
  warnings?: string[]
  actions?: string[]
  file_delivery?: string
  file?: string
  requires_remote_upload?: boolean
  remote_fallback_available?: boolean
  remote_upload_verify_url?: boolean
  force_remote_link?: boolean
  require_remote_url?: boolean
  download_remote?: boolean
  temporary_overrides?: Record<string, string>
  verification?: DeliveryVerifyResult
  outbox?: {
    task_id?: string
    status?: string
    attempts?: number
    zip_path?: string
    zip_size?: number
    zip_sha256?: string
    remote_url?: string
    file_sent?: boolean
    delivery_note?: string
    last_error?: string
    delivered_at?: string
    updated_at?: string
    forwarding?: DeliveryForwardingSummary
  }
}

export interface DeliveryForwardingSummary {
  ready?: boolean
  method?: string
  can_forward_link?: boolean
  remote_url?: string
  file_sent?: boolean
  zip_size?: number
  zip_sha256?: string
  instruction?: string
}

export interface DeliveryOutboxItem {
  id: number
  task_id: string
  task_dir: string
  zip_path: string
  status: string
  attempts: number
  zip_size?: number
  zip_sha256?: string
  remote_url?: string
  file_sent?: number
  delivery_note?: string
  last_error: string
  created_at: string
  updated_at: string
  delivered_at: string
  summary?: Record<string, unknown>
  forwarding?: DeliveryForwardingSummary
}

export interface DeliveryOutboxSummary {
  pending: number
  sending: number
  sent: number
  failed: number
  total: number
}

export interface DeliveryRetryResult {
  ok: boolean
  retried: number
  sent: number
  failed: number
  skipped: number
  blocked: number
  requeued_failed?: number
  errors?: string[]
}

export interface DeliveryVerifyResult {
  ok: boolean
  source?: string
  receipt?: {
    task_id?: string
    status?: string
    zip_path?: string
    zip_size?: number
    zip_sha256?: string
    remote_url?: string
    file_sent?: boolean
    delivery_note?: string
    last_error?: string
    forwarding?: DeliveryForwardingSummary
  }
  checks?: {
    name: string
    ok: boolean
    value?: string
    expected?: string | number
    actual?: string | number
    message?: string
    skipped?: boolean
  }[]
  errors?: string[]
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({})) as { detail?: string }
    throw new Error(error.detail || `HTTP ${res.status}`)
  }
  return (await res.json()) as T
}

export const api = {
  health: () => req<Health>('/api/health'),
  listTaskRuns: (limit = 20) => req<{ tasks: Task[] }>(`/api/tasks?limit=${limit}`),
  getTask: (id: string) => req<Task>(`/api/tasks/${id}`),
  startCrawl: (body: { target_date?: string; task_name?: string; indicators?: string[]; data_dir?: string }) =>
    req<{ status?: string; task_id?: string; id?: string }>('/api/crawl/start', {
      method: 'POST',
      body: JSON.stringify(body)
    }),

  // ---- 工作流（指标配置） ----
  listWorkflows: () => req<WorkflowInfo[]>('/api/workflows'),
  listWorkflowGroups: () => req<WorkflowGroup[]>('/api/workflow-groups'),
  createWorkflowGroup: (body: { name: string; parent_id: string | null }) => req<WorkflowGroup>('/api/workflow-groups', { method: 'POST', body: JSON.stringify(body) }),
  updateWorkflowGroup: (id: string, body: { name: string; parent_id: string | null }) => req<{ status: string }>(`/api/workflow-groups/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteWorkflowGroup: (id: string) => req<{ status: string }>(`/api/workflow-groups/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  updateWorkflowMeta: (id: string, body: { label: string; group_id: string | null }) => req<{ status: string }>(`/api/workflows/${encodeURIComponent(id)}/meta`, { method: 'PATCH', body: JSON.stringify(body) }),
  getWorkflow: (name: string) => req<WorkflowDetail>(`/api/workflows/${encodeURIComponent(name)}`),
  applyDateProfile: (body: {
    yaml_text: string
    date_kind: 'day' | 'week' | 'month'
    start: number
    end: number
    /** 导出 key → [start, end]；null = 跟随指标。不传不动导出上的范围 */
    output_windows?: Record<string, [number, number] | null>
  }) =>
    req<DateProfileResult>('/api/workflows/date-profile', {
      method: 'POST',
      body: JSON.stringify(body)
    }),
  readDateProfile: (yamlText: string) =>
    req<DateProfileRead>('/api/workflows/date-profile/read', {
      method: 'POST',
      body: JSON.stringify({ yaml_text: yamlText })
    }),
  saveWorkflow: (name: string, body: WorkflowSaveRequest) =>
    req<{ status: string }>(`/api/workflows/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body)
    }),
  deleteWorkflow: (name: string) =>
    req<{ status: string }>(`/api/workflows/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  runWorkflow: (name: string) =>
    req<{ status: string; error?: string }>(`/api/workflows/${encodeURIComponent(name)}/run`, { method: 'POST' }),
  recordWorkflow: (name: string, siteUrl?: string) => {
    const params = new URLSearchParams()
    if (siteUrl) params.set('site_url', siteUrl)
    const qs = params.toString()
    return req<{ status: string; steps_count?: number; yaml_text?: string; message?: string; hint?: string; stderr?: string }>(
      `/api/workflows/${encodeURIComponent(name)}/record${qs ? '?' + qs : ''}`, { method: 'POST' }
    )
  },
  exportAllWorkflows: () => `${BASE}/api/workflows/export-all`,
  importWorkflows: (files: { name: string; yaml_text: string }[]) =>
    req<{ status: string; imported: string[]; errors: string[] }>('/api/workflows/import', {
      method: 'POST',
      body: JSON.stringify({ files })
    }),
  importWorkflowArchive: (data: string) => req<{ status: string; imported: string[]; errors: string[] }>('/api/workflows/import-archive', { method: 'POST', body: JSON.stringify({ data }) }),

  // ---- 任务 ----
  listTaskFiles: () => req<{ name: string; label: string; indicators: string[] }[]>('/api/tasks/list'),
  saveTask: (name: string, body: Record<string, unknown>) =>
    req<{ status: string }>(`/api/tasks/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body)
    }),
  deleteTask: (name: string) =>
    req<{ status: string }>(`/api/tasks/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  runTaskStream: (name: string, params: Record<string, string>) => {
    return fetch(`${BASE}/api/tasks/${encodeURIComponent(name)}/run/stream`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params)
    })
  },

  // ---- 任务配置（SQLite 存储，替代 localStorage） ----
  listTaskConfigs: () => req<Record<string, unknown>[]>('/api/task-configs'),
  saveTaskConfig: (id: string, body: Record<string, unknown>) =>
    req<{ status: string }>(`/api/task-configs/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(body)
    }),
  deleteTaskConfig: (id: string) =>
    req<{ status: string }>(`/api/task-configs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  previewTaskPath: (body: { template: string; root?: string; exec_date?: string; ssid?: string }) =>
    req<{ path: string; relative: string; segments: string[]; error: string }>(
      '/api/task-configs/preview-path',
      {
        method: 'POST',
        body: JSON.stringify(body)
      }
    ),
  previewTaskStructure: (body: {
    structure: string
    indicators: string[]
    root?: string
    exec_date?: string
    ssid?: string
  }) =>
    req<{
      error: string
      base: string
      assignments: {
        indicator: string
        output_key: string
        output_label: string
        folder: string
        path: string
      }[]
      unassigned: { indicator: string; output_key: string; label: string }[]
      invalid?: string[]
      /** 同名 save_as 落到同一目录的 指标::输出key；放在不同文件夹不算 */
      conflicts?: string[]
    }>('/api/task-configs/preview-structure', {
      method: 'POST',
      body: JSON.stringify(body)
    }),
  exportAllTaskConfigs: () => `${BASE}/api/task-configs/export-all`,
  importTaskConfigs: (files: Record<string, unknown>[]) =>
    req<{ status: string; imported: string[]; errors: string[] }>('/api/task-configs/import', {
      method: 'POST',
      body: JSON.stringify({ files })
    }),

  // ---- 告警 ----
  testAlert: () => req<{ sent: boolean; error?: string }>('/api/alert/test', { method: 'POST' }),

  // ---- 日志 ----
  recentLogs: (lines = 100) => req<{ lines: string[]; total: number; error?: string }>(`/api/logs/recent?lines=${lines}`),
  logPath: () => req<{ path: string }>('/api/logs/path'),

  // ---- 执行日志 ----
  listExecutionLogs: (page = 1, pageSize = 20, type?: string, status?: string, startTime?: string, endTime?: string) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (type) params.set('type', type)
    if (status) params.set('status', status)
    if (startTime) params.set('start_time', startTime)
    if (endTime) params.set('end_time', endTime)
    return req<ExecutionLogListResponse>(`/api/execution-logs?${params.toString()}`)
  },
  getExecutionLog: (execId: string) => req<ExecutionLogDetail>(`/api/execution-logs/${encodeURIComponent(execId)}`),
  deleteExecutionLog: (execId: string) =>
    req<{ status: string }>(`/api/execution-logs/${encodeURIComponent(execId)}`, { method: 'DELETE' }),
  deleteExecutionLogs: (execIds: string[]) =>
    req<{ status: string; deleted: number }>('/api/execution-logs/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ exec_ids: execIds })
    }),
  downloadExecutionLog: (execId: string, downloadDir: string) =>
    req<{ status: string; path: string; filename: string; message?: string }>(`/api/execution-logs/${encodeURIComponent(execId)}/download`, {
      method: 'POST',
      body: JSON.stringify({ download_dir: downloadDir })
    }),

  // ---- 应用设置 ----
  getSettings: () => req<{ settings: Record<string, string> }>('/api/settings'),
  saveSettings: (settings: Record<string, string>) =>
    req<{ status: string; settings?: Record<string, string> }>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ settings })
    }),
  testDelivery: (body: {
    filePath?: string
    send?: boolean
    sampleZip?: boolean
    verify?: boolean
    noRemote?: boolean
    forceRemoteLink?: boolean
    requireRemoteUrl?: boolean
    downloadRemote?: boolean
    maxRemoteMb?: number
  }) =>
    req<DeliveryTestResult>('/api/delivery/test', {
      method: 'POST',
      body: JSON.stringify(body)
    }),
  listDeliveryOutbox: (status = 'pending', limit = 5) => {
    const params = new URLSearchParams({ status, limit: String(limit) })
    return req<{ items: DeliveryOutboxItem[]; summary: DeliveryOutboxSummary }>(`/api/delivery/outbox?${params.toString()}`)
  },
  retryDelivery: (limit = 20, includeFailed = false) =>
    req<DeliveryRetryResult>('/api/delivery/retry', {
      method: 'POST',
      body: JSON.stringify({ limit, includeFailed })
    }),
  verifyDelivery: (body: {
    taskId?: string
    receiptPath?: string
    noRemote?: boolean
    downloadRemote?: boolean
    maxRemoteMb?: number
  }) =>
    req<DeliveryVerifyResult>('/api/delivery/verify', {
      method: 'POST',
      body: JSON.stringify(body)
    }),
  cleanupDeliveryOutbox: (status = 'sent', limit = 200) =>
    req<{ deleted: number }>('/api/delivery/outbox/cleanup', {
      method: 'POST',
      body: JSON.stringify({ status, limit })
    }),
  requeueDeliveryOutbox: (status = 'failed', limit = 200) =>
    req<{ requeued: number }>('/api/delivery/outbox/requeue', {
      method: 'POST',
      body: JSON.stringify({ status, limit })
    }),

  // ---- 录制工坊 ----
  listWorkshopExports: (downloadDir?: string) => {
    const params = new URLSearchParams()
    if (downloadDir) params.set('download_dir', downloadDir)
    const qs = params.toString()
    return req<WorkshopExport[]>(`/api/record-workshop/exports${qs ? '?' + qs : ''}`)
  },
  buildWorkshop: (body: { name: string; url?: string; description?: string; download_dir: string }) =>
    req<{ status: string; export_dir: string; exported: string[]; message: string }>(
      '/api/record-workshop/build',
      { method: 'POST', body: JSON.stringify(body) }
    ),
  submitWorkshopExport: (body: { path: string; download_dir: string }) =>
    req<{ status: string; export_id: string; uploaded?: boolean; local_only?: boolean; message: string; already?: boolean }>(
      '/api/record-workshop/submit',
      { method: 'POST', body: JSON.stringify(body) }
    ),
  deleteWorkshopExport: (body: { path: string; download_dir: string }) =>
    req<{ status: string; export_id: string; message: string }>(
      '/api/record-workshop/delete',
      { method: 'POST', body: JSON.stringify(body) }
    ),

  // ---- 网络路由 ----
  listInterfaces: () => req<{ interfaces: { name: string; desc: string; status: string; index: number }[]; error?: string }>('/api/network/interfaces'),
  listRouteRules: () => req<{ rules: RouteRule[] }>('/api/network/route-rules'),
  saveRouteRules: (rules: RouteRule[]) =>
    req<{ status: string }>('/api/network/route-rules', {
      method: 'PUT',
      body: JSON.stringify({ rules })
    }),
  testWifiConnection: () =>
    req<WifiTestResult>('/api/network/wifi/test', { method: 'POST' }),
  listUsbDevices: () =>
    req<{ devices: UsbDeviceOption[]; error?: string }>('/api/stations/usb-devices'),
  ukeyReport: () => req<UkeyReport>('/api/stations/ukey-report'),
  downloadUkeyReport: (downloadDir: string) =>
    req<{ status: string; path: string; filename: string; warning?: string; message?: string }>('/api/stations/ukey-report/download', {
      method: 'POST',
      body: JSON.stringify({ download_dir: downloadDir })
    }),
  downloadDiagnostics: (downloadDir = '', appVersion = '') =>
    req<{ status: string; path: string; filename: string }>('/api/diagnostics/bundle', {
      method: 'POST',
      body: JSON.stringify({ download_dir: downloadDir, app_version: appVersion })
    }),
  pickStation: (body: {
    choice?: string
    forceWindow?: boolean
    useSupplied?: boolean
    stations?: { id: string; name: string; wifiSsid: string }[]
  } = {}) =>
    req<StationPickResult>('/api/stations/pick', {
      method: 'POST',
      body: JSON.stringify(body)
    }),
  currentWifi: () => req<CurrentWifiResult>('/api/network/wifi/current'),
  listWifiNetworks: (interfaceName = '') =>
    req<WifiNetworksResult>(
      `/api/network/wifi/networks${interfaceName ? `?interface_name=${encodeURIComponent(interfaceName)}` : ''}`
    ),
  applyRouteRules: () =>
    req<RouteApplyResult>('/api/network/route-rules/apply', { method: 'POST' }),

  // ---- 数据管理 ----
  exportAllData: () => req<Record<string, unknown>>('/api/data/export'),
  importAllData: (data: Record<string, unknown>) =>
    req<{ status: string; imported: string[]; errors: string[] }>('/api/data/import', {
      method: 'POST',
      body: JSON.stringify(data)
    }),
  factoryReset: () =>
    req<{ status: string; cleared: string[]; message: string }>('/api/data/reset', { method: 'POST' }),

  // ---- 文件上传 ----
  uploadFile: (body: { localPath: string; relativePath?: string }) =>
    req<{ status: string; message: string; remote?: unknown }>('/api/files/upload', {
      method: 'POST',
      body: JSON.stringify(body)
    }),

  listUploadProfiles: () => req<{ profiles: UploadProfile[] }>('/api/upload-profiles'),
  saveUploadProfiles: (profiles: UploadProfile[]) =>
    req<{ status: string; profiles: UploadProfile[] }>('/api/upload-profiles', {
      method: 'PUT',
      body: JSON.stringify({ profiles })
    }),
  testUploadProfile: (body: { profileId?: string; profile?: Partial<UploadProfile> }) =>
    req<UploadProfileTestResult>('/api/upload-profiles/test-upload', {
      method: 'POST',
      body: JSON.stringify(body)
    }),
  testUploadWifi: (body: { profileId?: string; profile?: Partial<UploadProfile> }) =>
    req<UploadProfileTestResult>('/api/upload-profiles/test-wifi', {
      method: 'POST',
      body: JSON.stringify(body)
    })
}

// ── 设置缓存（内存，启动时从 SQLite 加载一次） ──

let _settingsCache: Record<string, string> | null = null
let _settingsPromise: Promise<Record<string, string>> | null = null

function maskStationProfiles(raw: string): string {
  try {
    const rows = JSON.parse(raw) as Array<Record<string, unknown>>
    if (!Array.isArray(rows)) return raw
    for (const row of rows) {
      if (row.wifiPassword) row.wifiPassword = '******'
      if (row.certPassword) row.certPassword = '******'
    }
    return JSON.stringify(rows)
  } catch {
    return raw
  }
}

export const settingsStore = {
  /** 是否已经从服务端成功加载过。失败时为 false，不能拿默认值冒充。 */
  ready(): boolean {
    return _settingsCache !== null
  },

  /** 加载设置（幂等，首次调 API，后续走缓存）。失败会抛出，不把空对象当成成功。 */
  async load(): Promise<Record<string, string>> {
    if (_settingsCache) return _settingsCache
    if (!_settingsPromise) {
      _settingsPromise = api.getSettings().then(r => {
        _settingsCache = r.settings || {}
        return _settingsCache
      }).catch((error: unknown) => {
        _settingsPromise = null
        _settingsCache = null
        throw error
      })
    }
    return _settingsPromise
  },

  /** 丢掉缓存后重新读取，用来核对刚刚保存的值。 */
  async reload(): Promise<Record<string, string>> {
    _settingsCache = null
    _settingsPromise = null
    return this.load()
  },

  /** 同步获取单个值（需先 load） */
  get(key: string, fallback = ''): string {
    return _settingsCache?.[key] ?? fallback
  },

  /** 获取所有设置（同步，需先 load） */
  getAll(): Record<string, string> {
    return _settingsCache ?? {}
  },

  /** 保存并更新缓存 */
  async save(settings: Record<string, string>): Promise<Record<string, string>> {
    const saved = await api.saveSettings(settings)
    if (!_settingsCache) _settingsCache = {}
    const cached = { ...settings }
    if (cached.wifiPassword) cached.wifiPassword = '******'
    if (cached.stationProfiles) cached.stationProfiles = maskStationProfiles(cached.stationProfiles)
    Object.assign(_settingsCache, cached)
    return saved.settings || {}
  },

  /** 重置缓存（下次 load 重新拉） */
  reset(): void {
    _settingsCache = null
    _settingsPromise = null
  }
}
