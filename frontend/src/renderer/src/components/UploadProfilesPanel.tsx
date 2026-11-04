import { forwardRef, useEffect, useImperativeHandle, useState } from 'react'
import { Button, Input, Select, Space, App as AntApp } from 'antd'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
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

const UploadProfilesPanel = forwardRef<UploadProfilesHandle, { open: boolean }>(function UploadProfilesPanel(
  { open },
  ref
) {
  const { message } = AntApp.useApp()
  const [rows, setRows] = useState<Draft[]>([])
  const [activeId, setActiveId] = useState('')
  const [testingUpload, setTestingUpload] = useState(false)
  const [testingWifi, setTestingWifi] = useState(false)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    if (!open) {
      setLoaded(false)
      return
    }
    let cancelled = false
    setLoaded(false)
    void api.listUploadProfiles().then((res) => {
      if (cancelled) return
      const next = (res.profiles || []).map(normalize)
      setRows(next)
      setActiveId(next[0]?.id || '')
      setLoaded(true)
    }).catch(() => {
      if (!cancelled) message.warning('上传配置读取失败')
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
      setActiveId((current) => next.some((row) => row.id === current) ? current : (next[0]?.id || ''))
    }
  }), [loaded, rows])

  const active = rows.find((row) => row.id === activeId) || null

  const patch = (partial: Partial<Draft>): void => {
    if (!active) return
    setRows((prev) => prev.map((row) => row.id === active.id ? { ...row, ...partial } : row))
  }

  const testBody = () => active ? { profile: active } : { profileId: '' }

  const testUpload = async (): Promise<void> => {
    if (!active) return
    setTestingUpload(true)
    try {
      const res = await api.testUploadProfile(testBody())
      if (res.ok) message.success(res.message || '测试上传成功')
      else message.error(res.message || '测试上传失败')
    } catch (error: unknown) {
      message.error('测试上传失败: ' + (error instanceof Error ? error.message : String(error)))
    } finally {
      setTestingUpload(false)
    }
  }

  const testWifi = async (): Promise<void> => {
    if (!active) return
    setTestingWifi(true)
    try {
      const res = await api.testUploadWifi(testBody())
      if (res.ok) message.success(res.message || 'WiFi 测试完成')
      else message.error(res.message || 'WiFi 测试失败')
    } catch (error: unknown) {
      message.error('WiFi 测试失败: ' + (error instanceof Error ? error.message : String(error)))
    } finally {
      setTestingWifi(false)
    }
  }

  return (
    <div>
      <h3 id="section-upload" style={{ fontSize: 18, fontWeight: 600, marginBottom: 8, marginTop: 0 }}>
        数据上传
      </h3>
      <div style={{ color: '#8b98a8', fontSize: 13, marginBottom: 16, lineHeight: 1.7 }}>
        接口使用 multipart/form-data，文件字段名是 file，一次可以带多个文件。文件夹会先打成 zip。
        Token 和 WiFi 密码加密保存，留空表示保持原值。默认把 token 放在 Authorization 头，前缀 Bearer，和录制工坊现有上传一致。
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 16 }}>
        <div>
          <Space direction="vertical" style={{ width: '100%' }}>
            {rows.map((row) => (
              <Button
                key={row.id}
                block
                type={row.id === activeId ? 'primary' : 'default'}
                onClick={() => setActiveId(row.id)}
              >
                {row.name.trim() || '未命名配置'}
              </Button>
            ))}
            <Button
              icon={<PlusOutlined />}
              block
              onClick={() => {
                const row = emptyDraft()
                setRows((prev) => [...prev, row])
                setActiveId(row.id)
              }}
            >
              新增配置
            </Button>
          </Space>
        </div>
        <div>
          {!active && <div style={{ color: '#8A8F95' }}>还没有上传配置。点左侧「新增配置」开始。</div>}
          {active && (
            <div style={{ display: 'grid', gap: 12 }}>
              <label>
                <div style={{ marginBottom: 4 }}>名称</div>
                <Input value={active.name} placeholder="例如：生产环境" onChange={(event) => patch({ name: event.target.value })} />
              </label>
              <label>
                <div style={{ marginBottom: 4 }}>上传接口 URL</div>
                <Input value={active.url} placeholder="https://example.com/upload" onChange={(event) => patch({ url: event.target.value })} />
              </label>
              <label>
                <div style={{ marginBottom: 4 }}>Token（可选）</div>
                <Input.Password
                  value={active.token}
                  placeholder={active.tokenSaved ? '已保存，留空保持' : '可选'}
                  visibilityToggle={false}
                  onChange={(event) => patch({ token: event.target.value })}
                />
              </label>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <label>
                  <div style={{ marginBottom: 4 }}>Token 传递方式</div>
                  <Select
                    style={{ width: '100%' }}
                    value={active.tokenMode}
                    options={[
                      { value: 'header', label: '请求头' },
                      { value: 'param', label: '请求参数' }
                    ]}
                    onChange={(value: 'header' | 'param') => patch({ tokenMode: value })}
                  />
                </label>
                {active.tokenMode === 'header' ? (
                  <label>
                    <div style={{ marginBottom: 4 }}>请求头名称</div>
                    <Input value={active.tokenHeaderName} onChange={(event) => patch({ tokenHeaderName: event.target.value })} />
                  </label>
                ) : (
                  <label>
                    <div style={{ marginBottom: 4 }}>参数名</div>
                    <Input value={active.tokenParamName} onChange={(event) => patch({ tokenParamName: event.target.value })} />
                  </label>
                )}
              </div>
              {active.tokenMode === 'header' ? (
                <label>
                  <div style={{ marginBottom: 4 }}>请求头值前缀</div>
                  <Input
                    value={active.tokenValuePrefix}
                    placeholder="Bearer "
                    onChange={(event) => patch({ tokenValuePrefix: event.target.value })}
                  />
                </label>
              ) : (
                <label>
                  <div style={{ marginBottom: 4 }}>参数位置</div>
                  <Select
                    style={{ width: '100%' }}
                    value={active.tokenParamIn}
                    options={[
                      { value: 'query', label: '查询参数' },
                      { value: 'form', label: '表单字段' }
                    ]}
                    onChange={(value: 'query' | 'form') => patch({ tokenParamIn: value })}
                  />
                </label>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <label>
                  <div style={{ marginBottom: 4 }}>WiFi 名（可选）</div>
                  <Input value={active.wifiSsid} placeholder="留空则不切换网络" onChange={(event) => patch({ wifiSsid: event.target.value })} />
                </label>
                <label>
                  <div style={{ marginBottom: 4 }}>WiFi 密码（可选）</div>
                  <Input.Password
                    value={active.wifiPassword}
                    placeholder={active.wifiPasswordSaved ? '已保存，留空保持' : '可选'}
                    visibilityToggle={false}
                    onChange={(event) => patch({ wifiPassword: event.target.value })}
                  />
                </label>
              </div>
              <Space>
                <Button loading={testingUpload} onClick={() => { void testUpload() }}>测试上传</Button>
                <Button loading={testingWifi} onClick={() => { void testWifi() }}>测试切换 WiFi</Button>
                <Button
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => {
                    const next = rows.filter((row) => row.id !== active.id)
                    setRows(next)
                    setActiveId(next[0]?.id || '')
                  }}
                >
                  删除
                </Button>
              </Space>
            </div>
          )}
        </div>
      </div>
    </div>
  )
})

export default UploadProfilesPanel
