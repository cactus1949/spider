import { useCallback, useEffect, useState } from 'react'
import { Alert, App, Button, Form, Input, Modal, Popconfirm, Select, Space, Tag, Typography } from 'antd'
import { ChromeOutlined, ReloadOutlined } from '@ant-design/icons'

const BASE = 'http://localhost:8800/api/browser'
const DISMISSED_ALERT_KEY = 'spider-gd:dismissed-browser-run-alert'
const PAGE_LOAD_FAILED = '业务页面加载失败（chrome-error），请在专用浏览器中刷新并确认 U-Key 证书客户端已运行后重新登录'
type Page = { id: string; title: string; url: string; load_error?: boolean }

function pageLabel(page: Page): string {
  const text = `${page.title || '无标题'} · ${page.url}`
  return page.load_error ? `加载失败 · ${text}` : text
}
type Run = { id: string; name: string; state: string; reason: string; pause_version: number }
type State = {
  running: boolean; busy: boolean; owns_process: boolean; selected_page_id?: string
  download_dir?: string
  run?: Run; config: Record<string, string>
}

export default function BrowserSessionPanel(): JSX.Element {
  const { message } = App.useApp()
  const [state, setState] = useState<State | null>(null)
  const [pages, setPages] = useState<Page[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [dismissedAlertKey, setDismissedAlertKey] = useState(() => {
    try { return window.localStorage.getItem(DISMISSED_ALERT_KEY) || '' } catch { return '' }
  })
  const [form] = Form.useForm()
  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}/status`)
      if (!res.ok) return
      let next: State = await res.json()
      if (next.running) {
        const result = await fetch(`${BASE}/pages`)
        if (result.ok) setPages(await result.json())
        const again = await fetch(`${BASE}/status`)
        if (again.ok) next = await again.json()
      } else setPages([])
      setState(next)
    } catch { /* backend may be starting */ }
  }, [])
  useEffect(() => {
    void refresh()
    const timer = setInterval(() => void refresh(), 2000)
    return () => clearInterval(timer)
  }, [refresh])

  const action = async (path: string, body?: unknown): Promise<boolean> => {
    setLoading(true)
    try {
      const res = await fetch(`${BASE}/${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body)
      })
      const result = await res.json()
      if (!res.ok || result.status === 'error') throw new Error(result.message || result.detail || '操作失败')
      if (path === 'auth/check') {
        if (result.state === 'authenticated') message.success('业务登录状态已验证')
        else message.warning(result.reason || '尚未确认登录')
      }
      if (path === 'reset') message.success(`已清除 ${result.origin} 的 cookie 并刷新当前页`)
      await refresh()
      return true
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error))
      return false
    } finally { setLoading(false) }
  }
  const configure = (): void => {
    form.setFieldsValue({ ...state?.config, mode: 'managed',
      auth_policy: state?.config.auth_policy || 'reuse_only',
      target_origin: state?.config.target_origin || 'https://pm.gd.csg.cn',
      success_selector: state?.config.success_selector || '.ant-menu-horizontal, .el-menu',
      profile_dir: state?.config.mode === 'managed' ? state?.config.profile_dir || '' : '' })
    setOpen(true)
  }
  const run = state?.run
  const runAlertKey = run ? JSON.stringify([run.id, run.state, run.reason, run.pause_version]) : ''
  return <div style={{ padding: '8px 20px', background: '#fff', borderBottom: '1px solid #eee', flexShrink: 0 }}>
    <Space wrap size={8}>
      <ChromeOutlined />
      <Tag color={state?.running ? 'green' : 'default'}>{state?.running ? '浏览器已连接' : '浏览器未连接'}</Tag>
      {!state?.running && <Button size="small" onClick={() => configure()} disabled={loading || state?.busy}>打开专用浏览器</Button>}
      {state?.running && <>
        <Typography.Text type="secondary">当前绑定标签：</Typography.Text>
        <Select size="small" aria-label="当前绑定标签" value={state.selected_page_id || undefined}
          placeholder="等待当前标签页" showSearch optionFilterProp="label"
          style={{ width: 380, maxWidth: '45vw' }} disabled={state.busy || loading}
          options={pages.map(page => ({ value: page.id, label: pageLabel(page) }))}
          onChange={page_id => void action('select', { page_id })} />
        <Button size="small" disabled={state.busy || loading || !state.selected_page_id} onClick={() => void action('auth/check')}>检查登录</Button>
        <Button size="small" disabled={state.busy || loading} onClick={() => void action('detach')}>断开连接</Button>
        {state.owns_process && <Button size="small" disabled={state.busy || loading} onClick={() => void action('close')}>关闭专用浏览器</Button>}
        <Popconfirm title="清除当前站点 cookie 并刷新该页？" description="会退出登录态，需要重新登录。"
          okText="清除并刷新" cancelText="取消" onConfirm={() => void action('reset')}>
          <Button size="small" disabled={state.busy || loading || !state.selected_page_id}>重置刷新</Button>
        </Popconfirm>
      </>}
      <Button size="small" type="text" icon={<ReloadOutlined />} onClick={() => void refresh()} />
      {state?.busy && run && <>
        <Tag color={run.state === 'paused' ? 'orange' : 'blue'}>{run.name} · {run.state === 'paused' ? '已暂停' : '执行中'}</Tag>
        {run.state === 'paused' && <Button size="small" type="primary" loading={loading}
          onClick={() => void action('resume', { run_id: run.id, pause_version: run.pause_version })}>重新检查后继续</Button>}
        <Button size="small" danger loading={loading} onClick={() => void action('cancel', { run_id: run.id })}>取消任务</Button>
      </>}
    </Space>
    {state?.running && <div style={{ marginTop: 4 }}>
      <Typography.Text type="secondary">
        空闲时跟随专用浏览器当前标签{state.busy ? '；任务占用期间保持原来的绑定' : ''}
        {state.download_dir ? `。下载目录：${state.download_dir}` : ''}
      </Typography.Text>
      {pages.some((page) => page.load_error && page.id === state.selected_page_id) && (
        <div><Typography.Text type="warning">{PAGE_LOAD_FAILED}</Typography.Text></div>
      )}
    </div>}
    {run && ['paused', 'interrupted', 'failed'].includes(run.state) && runAlertKey !== dismissedAlertKey &&
      <Alert style={{ marginTop: 8 }} showIcon closable
        onClose={() => {
          setDismissedAlertKey(runAlertKey)
          try { window.localStorage.setItem(DISMISSED_ALERT_KEY, runAlertKey) } catch { /* storage unavailable */ }
        }}
        type={run.state === 'failed' ? 'error' : 'warning'} message={run.reason || '等待处理'} />}
    <Modal title="打开专用浏览器" open={open} confirmLoading={loading} okText="打开" cancelText="取消"
      onCancel={() => setOpen(false)} onOk={async () => {
        try {
          const values = await form.validateFields()
          if (await action('connect', { ...values, mode: 'managed' })) setOpen(false)
        } catch { /* form shows validation */ }
      }}>
      <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
        <Form.Item name="profile_dir" label="专用浏览器数据目录（留空使用默认目录）"><Input /></Form.Item>
        <Form.Item name="auth_policy" label="登录方式" rules={[{ required: true }]}><Select options={[
          { value: 'reuse_only', label: '仅复用已有登录；未登录时暂停' },
          { value: 'reuse_or_auto', label: '优先复用，未登录时执行已配置登录流程' },
          { value: 'manual', label: '由用户手动登录后继续' }
        ]} /></Form.Item>
        <Form.Item name="target_origin" label="业务站点" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="success_selector" label="登录成功元素选择器" rules={[{ required: true }]}><Input /></Form.Item>
        <Space align="start">
          <Form.Item name="identity_selector" label="企业 / 账号元素选择器（可选）"><Input /></Form.Item>
          <Form.Item name="expected_identity" label="预期企业 / 账号文字（精确匹配）"><Input /></Form.Item>
        </Space>
        <Typography.Text type="secondary">指标单步、工作流和任务共用此会话。任务运行或暂停期间，会话保持独占。</Typography.Text>
      </Form>
    </Modal>
  </div>
}
