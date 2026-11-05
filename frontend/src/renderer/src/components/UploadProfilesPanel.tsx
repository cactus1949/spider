import { forwardRef, useEffect, useImperativeHandle, useState, type ReactNode } from 'react'
import { Button, Card, Checkbox, Empty, Form, Input, Modal, Select, Space, Tooltip, App as AntApp } from 'antd'
import {
  ApiOutlined,
  DeleteOutlined,
  EditOutlined,
  LinkOutlined,
  PlusOutlined,
  WifiOutlined
} from '@ant-design/icons'
import { api, type UploadHeader, type UploadParam, type UploadProfile } from '../services/api'

export interface UploadProfilesHandle {
  save: () => Promise<void>
}

type Draft = UploadProfile

const HEADER_COLUMNS = 'minmax(0, 1fr) minmax(0, 1.4fr) 52px 32px'
const PARAM_COLUMNS = 'minmax(0, 1fr) minmax(0, 1.3fr) 112px 52px 32px'

function newId(prefix: string): string {
  return `${prefix}_${Math.random().toString(16).slice(2, 14)}`
}

function emptyHeader(): UploadHeader {
  return { id: newId('uh'), name: '', value: '', secret: false, saved: false }
}

function emptyParam(): UploadParam {
  return { id: newId('upm'), name: '', value: '', in: 'query', secret: false, saved: false }
}

function emptyDraft(): Draft {
  return {
    id: newId('up'),
    name: '',
    url: '',
    fileFieldName: 'file',
    headers: [],
    params: [],
    wifiSsid: '',
    wifiPassword: '',
    wifiPasswordSaved: false,
    wifiInterface: ''
  }
}

function normalizeHeader(raw: Partial<UploadHeader> | null | undefined): UploadHeader {
  const secret = Boolean(raw?.secret)
  return {
    id: raw?.id || newId('uh'),
    name: raw?.name || '',
    value: secret ? '' : (raw?.value ?? ''),
    secret,
    saved: Boolean(raw?.saved) || (secret && Boolean(raw?.value))
  }
}

function normalizeParam(raw: Partial<UploadParam> | null | undefined): UploadParam {
  const secret = Boolean(raw?.secret)
  return {
    id: raw?.id || newId('upm'),
    name: raw?.name || '',
    value: secret ? '' : (raw?.value ?? ''),
    in: raw?.in === 'form' ? 'form' : 'query',
    secret,
    saved: Boolean(raw?.saved) || (secret && Boolean(raw?.value))
  }
}

function normalize(raw: UploadProfile): Draft {
  return {
    ...emptyDraft(),
    ...raw,
    wifiPassword: '',
    fileFieldName: (raw.fileFieldName || 'file').trim() || 'file',
    headers: Array.isArray(raw.headers) ? raw.headers.map((item) => normalizeHeader(item)) : [],
    params: Array.isArray(raw.params) ? raw.params.map((item) => normalizeParam(item)) : []
  }
}

function compact(row: Draft): Draft {
  return {
    ...row,
    name: row.name.trim(),
    url: row.url.trim(),
    fileFieldName: row.fileFieldName.trim(),
    headers: row.headers
      .map((item) => ({ ...item, name: item.name.trim() }))
      .filter((item) => item.name || item.value.trim()),
    params: row.params
      .map((item) => ({
        ...item,
        name: item.name.trim(),
        in: item.in === 'form' ? 'form' as const : 'query' as const
      }))
      .filter((item) => item.name || item.value.trim())
  }
}

function validateDraft(row: Draft): string | null {
  const name = row.name.trim()
  if (!name) return '上传配置名称不能为空'
  const url = row.url.trim()
  if (!url) return `上传配置「${name}」的接口 URL 不能为空`
  if (!/^https?:\/\//i.test(url)) {
    return `上传配置「${name}」的接口 URL 必须以 http:// 或 https:// 开头`
  }
  const field = row.fileFieldName.trim()
  if (!field) return `上传配置「${name}」的文件字段名不能为空`
  const headerNames = new Set<string>()
  for (const header of row.headers) {
    if (!header.name) return '请求头名称不能为空'
    const key = header.name.toLowerCase()
    if (headerNames.has(key)) return `请求头名称重复: ${header.name}`
    headerNames.add(key)
  }
  const paramKeys = new Set<string>()
  for (const param of row.params) {
    if (!param.name) return '参数名称不能为空'
    const where = param.in === 'form' ? '表单参数' : '查询参数'
    const key = `${param.in}:${param.name}`
    if (paramKeys.has(key)) return `${where}名称重复: ${param.name}`
    paramKeys.add(key)
    if (param.in === 'form' && param.name === field) return `表单参数「${param.name}」与文件字段名冲突`
  }
  return null
}

function requestSummary(row: Draft): string {
  const field = row.fileFieldName.trim() || 'file'
  const headerCount = row.headers.filter((item) => item.name.trim()).length
  const paramCount = row.params.filter((item) => item.name.trim()).length
  return `字段 ${field} · 请求头 ${headerCount} · 参数 ${paramCount}`
}

function wifiSummary(row: Draft): string {
  const ssid = row.wifiSsid.trim()
  if (!ssid) return '不切换网络'
  return row.wifiPasswordSaved || row.wifiPassword.trim() ? `${ssid} · 已填密码` : ssid
}

function InfoLine({
  icon,
  label,
  value,
  title
}: {
  icon: ReactNode
  label: string
  value: ReactNode
  title?: string
}): JSX.Element {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
      <span style={{ color: '#8A8F95', fontSize: 12, flexShrink: 0, display: 'inline-flex' }}>{icon}</span>
      <span style={{ color: '#8A8F95', width: 48, flexShrink: 0 }}>{label}</span>
      <span
        title={title}
        style={{
          color: '#1A1D21',
          minWidth: 0,
          flex: 1,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap'
        }}
      >
        {value}
      </span>
    </div>
  )
}

function SectionLabel({ children }: { children: string }): JSX.Element {
  return (
    <div style={{ margin: '2px 0 12px', fontSize: 13, fontWeight: 600, color: '#5A6068' }}>
      {children}
    </div>
  )
}

function SecretValueInput({
  secret,
  saved,
  value,
  onChange
}: {
  secret: boolean
  saved: boolean
  value: string
  onChange: (value: string) => void
}): JSX.Element {
  const placeholder = secret ? (saved ? '已保存，留空保持' : '值') : '值'
  if (secret) {
    return (
      <Input.Password
        size="small"
        value={value}
        placeholder={placeholder}
        visibilityToggle={false}
        autoComplete="new-password"
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  return (
    <Input
      size="small"
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

function ColumnLabels({ columns, labels }: { columns: string; labels: string[] }): JSX.Element {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: columns,
        gap: 8,
        marginBottom: 6,
        color: '#8A8F95',
        fontSize: 12
      }}
    >
      {labels.map((label) => (
        <span key={label}>{label}</span>
      ))}
    </div>
  )
}

const UploadProfilesPanel = forwardRef<UploadProfilesHandle, { open: boolean }>(function UploadProfilesPanel(
  { open },
  ref
) {
  const { message, modal } = AntApp.useApp()
  const [rows, setRows] = useState<Draft[]>([])
  const [editor, setEditor] = useState<Draft | null>(null)
  const [testingUploadId, setTestingUploadId] = useState('')
  const [testingWifiId, setTestingWifiId] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState(false)

  useEffect(() => {
    if (!open) {
      setLoaded(false)
      setLoadError(false)
      setEditor(null)
      return
    }
    let cancelled = false
    setLoaded(false)
    setLoadError(false)
    void api.listUploadProfiles().then((res) => {
      if (cancelled) return
      const next = (res.profiles || []).map(normalize)
      setRows(next)
      setLoaded(true)
    }).catch(() => {
      if (cancelled) return
      setLoadError(true)
      message.warning('上传配置读取失败')
    })
    return () => { cancelled = true }
  }, [open, message])

  useImperativeHandle(ref, () => ({
    async save() {
      if (!loaded) return
      const nextRows = rows.map(compact)
      for (const row of nextRows) {
        const error = validateDraft(row)
        if (error) throw new Error(error)
      }
      const res = await api.saveUploadProfiles(nextRows)
      const next = (res.profiles || []).map(normalize)
      setRows(next)
      setEditor((current) => current && next.some((row) => row.id === current.id) ? current : null)
    }
  }), [loaded, rows])

  const isNew = editor ? !rows.some((row) => row.id === editor.id) : false

  const patchEditor = (partial: Partial<Draft>): void => {
    setEditor((prev) => prev ? { ...prev, ...partial } : prev)
  }

  const openCreate = (): void => {
    setEditor(emptyDraft())
  }

  const openEdit = (row: Draft): void => {
    setEditor({
      ...row,
      headers: row.headers.map((item) => ({ ...item })),
      params: row.params.map((item) => ({ ...item }))
    })
  }

  const closeEditor = (): void => {
    setEditor(null)
  }

  const commitEditor = (): void => {
    if (!editor) return
    const next = compact(editor)
    const error = validateDraft(next)
    if (error) {
      message.warning(error)
      return
    }
    setRows((prev) => prev.some((row) => row.id === next.id)
      ? prev.map((row) => row.id === next.id ? next : row)
      : [...prev, next])
    setEditor(null)
  }

  const confirmDelete = (row: Draft): void => {
    modal.confirm({
      title: `删除上传配置「${row.name.trim() || '未命名配置'}」？`,
      content: '删除后要点右下角「保存」才会生效。',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      zIndex: 2000,
      onOk: () => {
        setRows((prev) => prev.filter((item) => item.id !== row.id))
        setEditor((current) => current?.id === row.id ? null : current)
      }
    })
  }

  const testUpload = async (profile: Draft): Promise<void> => {
    const next = compact(profile)
    const error = validateDraft(next)
    if (error) {
      message.warning(error)
      return
    }
    setTestingUploadId(profile.id)
    try {
      const res = await api.testUploadProfile({ profile: next })
      if (res.ok) message.success(res.message || '测试上传成功')
      else message.error(res.message || '测试上传失败')
    } catch (error: unknown) {
      message.error('测试上传失败: ' + (error instanceof Error ? error.message : String(error)))
    } finally {
      setTestingUploadId('')
    }
  }

  const testWifi = async (profile: Draft): Promise<void> => {
    if (!profile.wifiSsid.trim()) {
      message.warning('请先填写 WiFi 名')
      return
    }
    setTestingWifiId(profile.id)
    try {
      const res = await api.testUploadWifi({ profile: compact(profile) })
      if (res.ok) message.success(res.message || 'WiFi 测试完成')
      else message.error(res.message || 'WiFi 测试失败')
    } catch (error: unknown) {
      message.error('WiFi 测试失败: ' + (error instanceof Error ? error.message : String(error)))
    } finally {
      setTestingWifiId('')
    }
  }

  const updateHeader = (id: string, partial: Partial<UploadHeader>): void => {
    setEditor((prev) => prev ? {
      ...prev,
      headers: prev.headers.map((item) => item.id === id ? { ...item, ...partial } : item)
    } : prev)
  }

  const updateParam = (id: string, partial: Partial<UploadParam>): void => {
    setEditor((prev) => prev ? {
      ...prev,
      params: prev.params.map((item) => item.id === id ? { ...item, ...partial } : item)
    } : prev)
  }

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 16,
          marginBottom: 16
        }}
      >
        <div style={{ minWidth: 0 }}>
          <h3 id="section-upload" style={{ fontSize: 18, fontWeight: 600, margin: '0 0 8px' }}>
            数据上传
          </h3>
          <div style={{ color: '#8b98a8', fontSize: 13, lineHeight: 1.7 }}>
            接口使用 multipart/form-data。文件字段名可配置，默认 file，文件夹会先打成 zip。
            自定义请求头和参数按原文发送。标成敏感的值和 WiFi 密码会加密保存，留空表示保持原值。
            点编辑图标修改，改完后点右下角「保存」。
          </div>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          disabled={!loaded}
          onClick={openCreate}
          style={{ flexShrink: 0 }}
        >
          新增配置
        </Button>
      </div>

      {!loaded && !loadError && (
        <div style={{ color: '#8A8F95', padding: '12px 0' }}>正在读取上传配置…</div>
      )}
      {loadError && (
        <div style={{ color: '#8A8F95', padding: '12px 0' }}>上传配置读取失败，关闭设置后重新打开再试。</div>
      )}
      {loaded && rows.length === 0 && (
        <Card style={{ borderRadius: 12 }} styles={{ body: { padding: '8px 16px 16px' } }}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有上传配置，点击右上角「新增配置」"
          />
        </Card>
      )}
      {loaded && rows.length > 0 && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 360px))',
            gap: 16
          }}
        >
          {rows.map((row) => {
            const named = row.name.trim() || '未命名配置'
            const url = row.url.trim()
            const summary = requestSummary(row)
            return (
              <Card
                key={row.id}
                styles={{ body: { padding: 18 } }}
                style={{ borderRadius: 12, position: 'relative' }}
              >
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
                      onClick={() => openEdit(row)}
                    />
                  </Tooltip>
                  <Tooltip title="删除">
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() => confirmDelete(row)}
                    />
                  </Tooltip>
                </div>

                <div
                  style={{
                    fontSize: 16,
                    fontWeight: 600,
                    marginBottom: 12,
                    paddingRight: 64,
                    color: '#1A1D21',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap'
                  }}
                  title={named}
                >
                  {named}
                </div>

                <div style={{ fontSize: 13, color: '#5A6068', lineHeight: 1.9 }}>
                  <InfoLine
                    icon={<LinkOutlined />}
                    label="接口"
                    title={url}
                    value={url || <span style={{ color: '#8A8F95' }}>未填写</span>}
                  />
                  <InfoLine icon={<ApiOutlined />} label="请求" title={summary} value={summary} />
                  <InfoLine icon={<WifiOutlined />} label="WiFi" title={wifiSummary(row)} value={wifiSummary(row)} />
                </div>

                <div
                  style={{
                    display: 'flex',
                    gap: 8,
                    marginTop: 14,
                    paddingTop: 12,
                    borderTop: '1px solid #F0F1F2'
                  }}
                >
                  <Button
                    size="small"
                    block
                    loading={testingUploadId === row.id}
                    onClick={() => { void testUpload(row) }}
                  >
                    测试上传
                  </Button>
                  <Button
                    size="small"
                    block
                    loading={testingWifiId === row.id}
                    onClick={() => { void testWifi(row) }}
                  >
                    测试 WiFi
                  </Button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      <Modal
        title={isNew ? '新增上传配置' : '编辑上传配置'}
        open={editor !== null}
        onCancel={closeEditor}
        width={860}
        destroyOnClose
        maskClosable={false}
        zIndex={1200}
        styles={{ body: { maxHeight: '62vh', overflowY: 'auto', paddingTop: 12 } }}
        footer={
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, width: '100%' }}>
            <Space>
              <Button
                loading={editor !== null && testingUploadId === editor.id}
                onClick={() => { if (editor) void testUpload(editor) }}
              >
                测试上传
              </Button>
              <Button
                loading={editor !== null && testingWifiId === editor.id}
                onClick={() => { if (editor) void testWifi(editor) }}
              >
                测试切换 WiFi
              </Button>
            </Space>
            <Space>
              <Button onClick={closeEditor}>取消</Button>
              <Button type="primary" onClick={commitEditor}>确定</Button>
            </Space>
          </div>
        }
      >
        {editor && (
          <div className="task-editor">
            <SectionLabel>接口</SectionLabel>
            <Form.Item label="名称" required>
              <Input
                autoFocus
                value={editor.name}
                placeholder="例如：生产环境"
                maxLength={64}
                onChange={(event) => patchEditor({ name: event.target.value })}
              />
            </Form.Item>
            <Form.Item label="上传接口 URL" required>
              <Input
                value={editor.url}
                placeholder="https://example.com/upload"
                onChange={(event) => patchEditor({ url: event.target.value })}
              />
            </Form.Item>
            <Form.Item label="文件字段名" extra="multipart 里每个文件都用这个字段名，默认 file">
              <Input
                value={editor.fileFieldName}
                placeholder="file"
                onChange={(event) => patchEditor({ fileFieldName: event.target.value })}
              />
            </Form.Item>

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 4 }}>
              <SectionLabel>自定义请求头</SectionLabel>
              <Button
                type="link"
                size="small"
                icon={<PlusOutlined />}
                onClick={() => patchEditor({ headers: [...editor.headers, emptyHeader()] })}
                style={{ marginBottom: 12 }}
              >
                添加
              </Button>
            </div>
            <div style={{ margin: '-4px 0 12px', color: '#8A8F95', fontSize: 12, lineHeight: 1.6 }}>
              名称不能重复。勾选敏感后加密保存，留空表示不修改；取消勾选并留空会清空该值。值按原文发送。
            </div>
            {editor.headers.length === 0 ? (
              <div className="task-window-empty" style={{ marginBottom: 16 }}>还没有请求头</div>
            ) : (
              <div style={{ marginBottom: 8 }}>
                <ColumnLabels columns={HEADER_COLUMNS} labels={['名称', '值', '敏感', '']} />
                {editor.headers.map((item) => (
                  <div
                    key={item.id}
                    style={{ display: 'grid', gridTemplateColumns: HEADER_COLUMNS, gap: 8, alignItems: 'center', marginBottom: 8 }}
                  >
                    <Input
                      size="small"
                      value={item.name}
                      placeholder="Authorization"
                      spellCheck={false}
                      onChange={(event) => updateHeader(item.id, { name: event.target.value })}
                    />
                    <SecretValueInput
                      secret={item.secret}
                      saved={item.saved}
                      value={item.value}
                      onChange={(value) => updateHeader(item.id, { value })}
                    />
                    <span style={{ display: 'flex', justifyContent: 'center' }}>
                      <Checkbox
                        checked={item.secret}
                        onChange={(event) => updateHeader(item.id, { secret: event.target.checked })}
                      />
                    </span>
                    <Tooltip title="删除">
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => patchEditor({ headers: editor.headers.filter((row) => row.id !== item.id) })}
                      />
                    </Tooltip>
                  </div>
                ))}
              </div>
            )}

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 4 }}>
              <SectionLabel>自定义参数</SectionLabel>
              <Button
                type="link"
                size="small"
                icon={<PlusOutlined />}
                onClick={() => patchEditor({ params: [...editor.params, emptyParam()] })}
                style={{ marginBottom: 12 }}
              >
                添加
              </Button>
            </div>
            <div style={{ margin: '-4px 0 12px', color: '#8A8F95', fontSize: 12, lineHeight: 1.6 }}>
              同一位置的名称不能重复。查询参数会合并进 URL，同名时以这里的值为准；表单参数作为普通字段，不能和文件字段名相同。
            </div>
            {editor.params.length === 0 ? (
              <div className="task-window-empty" style={{ marginBottom: 16 }}>还没有参数</div>
            ) : (
              <div style={{ marginBottom: 8 }}>
                <ColumnLabels columns={PARAM_COLUMNS} labels={['名称', '值', '位置', '敏感', '']} />
                {editor.params.map((item) => (
                  <div
                    key={item.id}
                    style={{ display: 'grid', gridTemplateColumns: PARAM_COLUMNS, gap: 8, alignItems: 'center', marginBottom: 8 }}
                  >
                    <Input
                      size="small"
                      value={item.name}
                      placeholder="token"
                      spellCheck={false}
                      onChange={(event) => updateParam(item.id, { name: event.target.value })}
                    />
                    <SecretValueInput
                      secret={item.secret}
                      saved={item.saved}
                      value={item.value}
                      onChange={(value) => updateParam(item.id, { value })}
                    />
                    <Select
                      size="small"
                      value={item.in}
                      options={[
                        { value: 'query', label: 'query' },
                        { value: 'form', label: 'form' }
                      ]}
                      onChange={(value: 'query' | 'form') => updateParam(item.id, { in: value })}
                    />
                    <span style={{ display: 'flex', justifyContent: 'center' }}>
                      <Checkbox
                        checked={item.secret}
                        onChange={(event) => updateParam(item.id, { secret: event.target.checked })}
                      />
                    </span>
                    <Tooltip title="删除">
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => patchEditor({ params: editor.params.filter((row) => row.id !== item.id) })}
                      />
                    </Tooltip>
                  </div>
                ))}
              </div>
            )}

            <SectionLabel>WiFi</SectionLabel>
            <div className="task-form-grid">
              <Form.Item label="WiFi 名（可选）">
                <Input
                  value={editor.wifiSsid}
                  placeholder="留空则不切换网络"
                  onChange={(event) => patchEditor({ wifiSsid: event.target.value })}
                />
              </Form.Item>
              <Form.Item
                label="WiFi 密码（可选）"
                extra={editor.wifiPasswordSaved ? '已保存，留空保持原值' : undefined}
              >
                <Input.Password
                  value={editor.wifiPassword}
                  placeholder={editor.wifiPasswordSaved ? '已保存，留空保持' : '可选'}
                  visibilityToggle={false}
                  onChange={(event) => patchEditor({ wifiPassword: event.target.value })}
                />
              </Form.Item>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
})

export default UploadProfilesPanel
