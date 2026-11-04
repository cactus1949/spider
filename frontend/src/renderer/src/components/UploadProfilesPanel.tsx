import { forwardRef, useEffect, useImperativeHandle, useState, type ReactNode } from 'react'
import { Button, Card, Empty, Form, Input, Modal, Select, Space, Tooltip, App as AntApp } from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  LinkOutlined,
  PlusOutlined,
  WifiOutlined
} from '@ant-design/icons'
import { api, type UploadProfile } from '../services/api'

export interface UploadProfilesHandle {
  save: () => Promise<void>
}

type Draft = UploadProfile

function emptyDraft(): Draft {
  return {
    id: `up_${Math.random().toString(16).slice(2, 14)}`,
    name: '',
    url: '',
    token: '',
    tokenSaved: false,
    tokenMode: 'header',
    tokenHeaderName: 'Authorization',
    tokenValuePrefix: 'Bearer ',
    tokenParamName: 'token',
    tokenParamIn: 'query',
    wifiSsid: '',
    wifiPassword: '',
    wifiPasswordSaved: false,
    wifiInterface: ''
  }
}

function normalize(raw: UploadProfile): Draft {
  return {
    ...emptyDraft(),
    ...raw,
    token: '',
    wifiPassword: '',
    tokenMode: raw.tokenMode === 'param' ? 'param' : 'header',
    tokenParamIn: raw.tokenParamIn === 'form' ? 'form' : 'query',
    tokenHeaderName: raw.tokenHeaderName || 'Authorization',
    tokenValuePrefix: raw.tokenValuePrefix ?? 'Bearer ',
    tokenParamName: raw.tokenParamName || 'token'
  }
}

function tokenSummary(row: Draft): string {
  const saved = row.tokenSaved || row.token.trim() ? ' · 已填写' : ''
  if (row.tokenMode === 'param') {
    const where = row.tokenParamIn === 'form' ? '表单' : '查询参数'
    return `${where} · ${row.tokenParamName || 'token'}${saved}`
  }
  const header = row.tokenHeaderName || 'Authorization'
  const prefix = (row.tokenValuePrefix || '').trim()
  return prefix ? `请求头 · ${header}（${prefix}）${saved}` : `请求头 · ${header}${saved}`
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
      for (const row of rows) {
        if (!row.name.trim()) throw new Error('上传配置名称不能为空')
        if (!row.url.trim()) throw new Error(`上传配置「${row.name || '未命名'}」的接口 URL 不能为空`)
      }
      const res = await api.saveUploadProfiles(rows)
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
    setEditor({ ...row })
  }

  const closeEditor = (): void => {
    setEditor(null)
  }

  const commitEditor = (): void => {
    if (!editor) return
    const name = editor.name.trim()
    const url = editor.url.trim()
    if (!name) {
      message.warning('请填写配置名称')
      return
    }
    if (!url) {
      message.warning('请填写上传接口 URL')
      return
    }
    const next: Draft = { ...editor, name, url }
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
    if (!profile.url.trim()) {
      message.warning('请先填写上传接口 URL')
      return
    }
    setTestingUploadId(profile.id)
    try {
      const res = await api.testUploadProfile({ profile })
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
      const res = await api.testUploadWifi({ profile })
      if (res.ok) message.success(res.message || 'WiFi 测试完成')
      else message.error(res.message || 'WiFi 测试失败')
    } catch (error: unknown) {
      message.error('WiFi 测试失败: ' + (error instanceof Error ? error.message : String(error)))
    } finally {
      setTestingWifiId('')
    }
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
            接口使用 multipart/form-data，文件字段名是 file，文件夹会先打成 zip。
            Token 和 WiFi 密码加密保存，留空表示保持原值。点编辑图标修改，改完后点右下角「保存」。
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
                  <InfoLine icon={<KeyOutlined />} label="Token" title={tokenSummary(row)} value={tokenSummary(row)} />
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
        width={720}
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

            <SectionLabel>Token</SectionLabel>
            <Form.Item
              label="Token（可选）"
              extra={editor.tokenSaved ? '已保存，留空保持原值' : '加密保存，留空表示不设置'}
            >
              <Input.Password
                value={editor.token}
                placeholder={editor.tokenSaved ? '已保存，留空保持' : '可选'}
                visibilityToggle={false}
                onChange={(event) => patchEditor({ token: event.target.value })}
              />
            </Form.Item>
            <div className="task-form-grid">
              <Form.Item label="传递方式">
                <Select
                  style={{ width: '100%' }}
                  value={editor.tokenMode}
                  options={[
                    { value: 'header', label: '请求头' },
                    { value: 'param', label: '请求参数' }
                  ]}
                  onChange={(value: 'header' | 'param') => patchEditor({ tokenMode: value })}
                />
              </Form.Item>
              {editor.tokenMode === 'header' ? (
                <Form.Item label="请求头名称">
                  <Input
                    value={editor.tokenHeaderName}
                    onChange={(event) => patchEditor({ tokenHeaderName: event.target.value })}
                  />
                </Form.Item>
              ) : (
                <Form.Item label="参数名">
                  <Input
                    value={editor.tokenParamName}
                    onChange={(event) => patchEditor({ tokenParamName: event.target.value })}
                  />
                </Form.Item>
              )}
            </div>
            {editor.tokenMode === 'header' ? (
              <Form.Item label="请求头值前缀" extra="默认 Bearer，和录制工坊现有上传一致">
                <Input
                  value={editor.tokenValuePrefix}
                  placeholder="Bearer "
                  onChange={(event) => patchEditor({ tokenValuePrefix: event.target.value })}
                />
              </Form.Item>
            ) : (
              <Form.Item label="参数位置">
                <Select
                  style={{ width: '100%' }}
                  value={editor.tokenParamIn}
                  options={[
                    { value: 'query', label: '查询参数' },
                    { value: 'form', label: '表单字段' }
                  ]}
                  onChange={(value: 'query' | 'form') => patchEditor({ tokenParamIn: value })}
                />
              </Form.Item>
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
