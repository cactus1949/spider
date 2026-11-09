import { useCallback, useEffect, useRef, useState } from 'react'
import { Modal, Form, Input, Select, Switch, Button, Space, Menu, Alert, App as AntApp, Segmented, AutoComplete } from 'antd'
import { CopyOutlined, DownloadOutlined, FolderOpenOutlined, SyncOutlined, ExportOutlined, ImportOutlined, WarningOutlined } from '@ant-design/icons'
import { api, settingsStore, type DeliveryOutboxItem, type DeliveryOutboxSummary, type UsbDeviceOption, type WifiNetwork } from '../services/api'

interface Settings {
  downloadDir: string
  autoLaunch: boolean
  siteUrl: string
  loginMode: 'cert' | 'password'
  certPassword: string
  account: string
  accountPassword: string
  workshopUploadUrl: string
  workshopUploadToken: string
  deliveryEnabled: boolean
  deliveryEnvFile: string
  deliveryBackend: 'wecom_webhook' | 'serverchan' | 'pushplus' | 'desktop_wechat'
  deliveryWecomWebhook: string
  deliveryServerchanSendKey: string
  deliveryServerchanChannel: string
  deliveryServerchanOpenid: string
  deliveryPushplusToken: string
  deliveryPushplusChannel: string
  deliveryDesktopWechatTarget: string
  deliveryDesktopWechatBackend: 'powershell' | 'wxauto' | 'auto'
  deliveryDesktopWechatSelectMode: 'current' | 'bound' | 'exact' | 'legacy-search'
  deliveryDesktopWechatProfilePath: string
  deliveryDesktopWechatAppName: string
  deliveryDesktopWechatPauseSec: string
  deliveryDesktopWechatHashThreshold: string
  deliveryDesktopWechatAllowUnstableSearch: boolean
  deliveryFileDelivery: 'auto' | 'wecom_file' | 'remote_link'
  deliveryWecomFileMaxMb: string
  deliveryRemoteUploadUrl: string
  deliveryRemoteUploadToken: string
  deliveryRemoteUploadRelativePath: string
  deliveryRemoteUploadVerifyUrl: boolean
  deliveryRetryAttempts: string
  deliveryRetryDelaySec: string
  deliverySendMessage: boolean
  deliverySendFile: boolean
  deliveryTitle: string
  deliveryTestFilePath: string
  alertMode: string
  webhook: string
  serverchanOpenid: string
  alertWecomBackupWebhook: string
  wifiAutoConnectEnabled: boolean
  wifiInterfaceName: string
  wifiSsid: string
  wifiPassword: string
  wifiConnectTimeoutSec: string
  wifiFailTaskOnError: boolean
  multiStationEnabled: boolean
}

type DeliveryOutboxStatusFilter = 'pending' | 'sending' | 'failed' | 'sent' | ''

const SECTIONS = [
  { key: 'section-basic', label: '📦 基础' },
  { key: 'section-site', label: '🌐 网站信息' },
  // { key: 'section-workshop', label: '📹 录制工坊' },
  // { key: 'section-delivery', label: '📤 结果发送' },
  // { key: 'section-route', label: '📡 路由' },
  { key: 'section-about', label: 'ℹ️ 关于' }
] as const

const DEFAULTS: Settings = {
  downloadDir: '',
  autoLaunch: true,
  siteUrl: 'https://pm.gd.csg.cn',
  loginMode: 'cert',
  certPassword: '',
  account: '',
  accountPassword: '',
  workshopUploadUrl: '',
  workshopUploadToken: '',
  deliveryEnabled: false,
  deliveryEnvFile: '',
  deliveryBackend: 'wecom_webhook',
  deliveryWecomWebhook: '',
  deliveryServerchanSendKey: '',
  deliveryServerchanChannel: '',
  deliveryServerchanOpenid: '',
  deliveryPushplusToken: '',
  deliveryPushplusChannel: '',
  deliveryDesktopWechatTarget: '',
  deliveryDesktopWechatBackend: 'powershell',
  deliveryDesktopWechatSelectMode: 'current',
  deliveryDesktopWechatProfilePath: './data/wechat_targets.json',
  deliveryDesktopWechatAppName: '',
  deliveryDesktopWechatPauseSec: '0.7',
  deliveryDesktopWechatHashThreshold: '12',
  deliveryDesktopWechatAllowUnstableSearch: false,
  deliveryFileDelivery: 'auto',
  deliveryWecomFileMaxMb: '20',
  deliveryRemoteUploadUrl: '',
  deliveryRemoteUploadToken: '',
  deliveryRemoteUploadRelativePath: '/upload/publicData',
  deliveryRemoteUploadVerifyUrl: true,
  deliveryRetryAttempts: '2',
  deliveryRetryDelaySec: '0.5',
  deliverySendMessage: true,
  deliverySendFile: true,
  deliveryTitle: 'spider-gd 采集完成',
  deliveryTestFilePath: '',
  alertMode: 'off',
  webhook: '',
  serverchanOpenid: '',
  alertWecomBackupWebhook: '',
  wifiAutoConnectEnabled: false,
  wifiInterfaceName: '',
  wifiSsid: '',
  wifiPassword: '',
  wifiConnectTimeoutSec: '30',
  wifiFailTaskOnError: true,
  multiStationEnabled: false
}

interface StationDraft {
  id: string
  name: string
  deviceId: string
  orgName: string
  wifiSsid: string
  wifiPassword: string
  certPassword: string
  wifiPasswordSaved: boolean
  certPasswordSaved: boolean
}

function parseStationDrafts(raw: string): StationDraft[] {
  if (!raw.trim()) return []
  try {
    const data = JSON.parse(raw) as Array<Record<string, unknown>>
    if (!Array.isArray(data)) return []
    return data.flatMap((item, index) => {
      const name = String(item.name || '').trim()
      if (!name) return []
      return [{
        id: String(item.id || `st-${index + 1}`),
        name,
        deviceId: String(item.deviceId || ''),
        orgName: String(item.orgName || ''),
        wifiSsid: String(item.wifiSsid || ''),
        wifiPassword: '',
        certPassword: '',
        wifiPasswordSaved: Boolean(item.wifiPassword),
        certPasswordSaved: Boolean(item.certPassword)
      }]
    })
  } catch {
    return []
  }
}

function serializeStationDrafts(rows: StationDraft[]): string {
  const kept = rows.filter((row) => (
    row.name.trim() || row.deviceId.trim() || row.wifiSsid.trim() || row.orgName.trim()
  ))
  return JSON.stringify(
    kept.map((row, index) => ({
      id: row.id,
      name: row.name.trim() || `场站 ${index + 1}`,
      deviceId: row.deviceId,
      orgName: row.orgName.trim(),
      wifiSsid: row.wifiSsid.trim(),
      wifiPassword: row.wifiPassword || (row.wifiPasswordSaved ? '******' : ''),
      certPassword: row.certPassword || (row.certPasswordSaved ? '******' : '')
    }))
  )
}

export default function SettingsModal({
  open,
  onClose
}: {
  open: boolean
  onClose: () => void
}): JSX.Element {
  const { message } = AntApp.useApp()
  const [form] = Form.useForm<Settings>()
  const [dir, setDir] = useState('')
  const [activeSection, setActiveSection] = useState('section-basic')
  const [version, setVersion] = useState('')
  const [checking, setChecking] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [deliveryTesting, setDeliveryTesting] = useState(false)
  const [deliveryOutbox, setDeliveryOutbox] = useState<DeliveryOutboxItem[]>([])
  const [deliveryOutboxSummary, setDeliveryOutboxSummary] = useState<DeliveryOutboxSummary>({
    pending: 0,
    sending: 0,
    sent: 0,
    failed: 0,
    total: 0
  })
  const [deliveryOutboxStatus, setDeliveryOutboxStatus] = useState<DeliveryOutboxStatusFilter>('pending')
  const [deliveryOutboxLoading, setDeliveryOutboxLoading] = useState(false)
  const [deliveryVerifyingTask, setDeliveryVerifyingTask] = useState('')
  const [wifiTesting, setWifiTesting] = useState(false)
  const [stations, setStations] = useState<StationDraft[]>([])
  const [stationsReady, setStationsReady] = useState(false)
  const [stationPicking, setStationPicking] = useState(false)
  const stationsDirty = useRef(false)
  const [usbDevices, setUsbDevices] = useState<UsbDeviceOption[]>([])
  const [usbError, setUsbError] = useState('')
  const [usbLoading, setUsbLoading] = useState(false)
  const [ukeyChecking, setUkeyChecking] = useState(false)
  const [ukeyChecked, setUkeyChecked] = useState(false)
  const [ukeyDownloading, setUkeyDownloading] = useState(false)

  useEffect(() => {
    if (!open) {
      stationsDirty.current = false
      setStationsReady(false)
      return
    }
    stationsDirty.current = false
    if (open) {
      settingsStore.load().then((s) => {
        window.api.getVersion().then(setVersion)
        const merged = { ...DEFAULTS }
        for (const [k, v] of Object.entries(s)) {
          if (v === 'true') (merged as any)[k] = true
          else if (v === 'false') (merged as any)[k] = false
          else (merged as any)[k] = v
        }
        const mode = merged.loginMode || 'cert'
        setLoginMode(mode)
        form.setFieldsValue({
          downloadDir: merged.downloadDir,
          autoLaunch: merged.autoLaunch,
          siteUrl: merged.siteUrl,
          loginMode: mode,
          certPassword: merged.certPassword,
          account: merged.account,
          accountPassword: merged.accountPassword,
          workshopUploadUrl: merged.workshopUploadUrl,
          workshopUploadToken: merged.workshopUploadToken,
          deliveryEnabled: merged.deliveryEnabled,
          deliveryEnvFile: merged.deliveryEnvFile,
          deliveryBackend: merged.deliveryBackend,
          deliveryWecomWebhook: merged.deliveryWecomWebhook,
          deliveryServerchanSendKey: merged.deliveryServerchanSendKey,
          deliveryServerchanChannel: merged.deliveryServerchanChannel,
          deliveryServerchanOpenid: merged.deliveryServerchanOpenid,
          deliveryPushplusToken: merged.deliveryPushplusToken,
          deliveryPushplusChannel: merged.deliveryPushplusChannel,
          deliveryDesktopWechatTarget: merged.deliveryDesktopWechatTarget,
          deliveryDesktopWechatBackend: merged.deliveryDesktopWechatBackend,
          deliveryDesktopWechatSelectMode: merged.deliveryDesktopWechatSelectMode,
          deliveryDesktopWechatProfilePath: merged.deliveryDesktopWechatProfilePath,
          deliveryDesktopWechatAppName: merged.deliveryDesktopWechatAppName,
          deliveryDesktopWechatPauseSec: merged.deliveryDesktopWechatPauseSec,
          deliveryDesktopWechatHashThreshold: merged.deliveryDesktopWechatHashThreshold,
          deliveryDesktopWechatAllowUnstableSearch: merged.deliveryDesktopWechatAllowUnstableSearch,
          deliveryFileDelivery: merged.deliveryFileDelivery,
          deliveryWecomFileMaxMb: merged.deliveryWecomFileMaxMb,
          deliveryRemoteUploadUrl: merged.deliveryRemoteUploadUrl,
          deliveryRemoteUploadToken: merged.deliveryRemoteUploadToken,
          deliveryRemoteUploadRelativePath: merged.deliveryRemoteUploadRelativePath,
          deliveryRemoteUploadVerifyUrl: merged.deliveryRemoteUploadVerifyUrl,
          deliveryRetryAttempts: merged.deliveryRetryAttempts,
          deliveryRetryDelaySec: merged.deliveryRetryDelaySec,
          deliverySendMessage: merged.deliverySendMessage,
          deliverySendFile: merged.deliverySendFile,
          deliveryTitle: merged.deliveryTitle,
          deliveryTestFilePath: merged.deliveryTestFilePath,
          alertMode: 'off',
          webhook: '',
          serverchanOpenid: '',
          alertWecomBackupWebhook: '',
          wifiAutoConnectEnabled: merged.wifiAutoConnectEnabled,
          wifiInterfaceName: merged.wifiInterfaceName,
          wifiSsid: merged.wifiSsid,
          wifiPassword: '',
          wifiConnectTimeoutSec: merged.wifiConnectTimeoutSec,
          wifiFailTaskOnError: merged.wifiFailTaskOnError,
          multiStationEnabled: merged.multiStationEnabled
        })
        setDir(merged.downloadDir)
        const savedStations = (merged as { stationProfiles?: string }).stationProfiles || ''
        if (!stationsDirty.current) setStations(parseStationDrafts(savedStations))
        setStationsReady(true)
      })
      setActiveSection('section-basic')
    }
  }, [open, form])

  const pickDir = async (): Promise<void> => {
    const picked = await window.api.pickDir()
    if (picked) {
      setDir(picked)
      form.setFieldValue('downloadDir', picked)
    }
  }

  const persistSettings = async (): Promise<Settings> => {
    await form.validateFields()
    const v = form.getFieldsValue(true) as Settings
    const merged: Settings = {
      ...v,
      downloadDir: dir,
      alertMode: 'off',
      webhook: '',
      serverchanOpenid: '',
      alertWecomBackupWebhook: ''
    }
    const kv: Record<string, string> = {}
    for (const [k, val] of Object.entries(merged)) {
      if (k === 'wifiPassword' && !String(val ?? '').trim()) continue
      kv[k] = String(val ?? '')
    }
    if (stationsReady) kv.stationProfiles = serializeStationDrafts(stations)
    kv.alertChannelsJson = '[]'
    await settingsStore.save(kv)
    window.dispatchEvent(new Event('settings-updated'))
    return merged
  }

  const save = async (): Promise<void> => {
    await persistSettings()
    // 保存路由规则（表单保存成功后）
    if (routeRules.length > 0) {
      try {
        await api.saveRouteRules(routeRules)
      } catch { /* ignore */ }
    }
    message.success('设置已保存')
  }

  const editStations = (updater: (rows: StationDraft[]) => StationDraft[]): void => {
    stationsDirty.current = true
    setStations(updater)
  }

  const downloadUkeyBundle = async (): Promise<void> => {
    setUkeyDownloading(true)
    try {
      const res = await api.downloadUkeyReport(dir)
      if (res.status === 'error') {
        message.error(res.message || 'UKey 诊断包没有生成')
        return
      }
      if (res.warning) message.warning(`诊断包已保存，但采集不完整：${res.warning}`)
      else message.success(`UKey 诊断包已保存: ${res.filename}`)
      if (res.path) {
        const folder = res.path.replace(/[\\/][^\\/]*$/, '')
        if (folder) await window.api.openDir(folder)
      }
    } catch (e: unknown) {
      message.error('下载失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setUkeyDownloading(false)
    }
  }

  const checkInsertedUkeys = async (): Promise<void> => {
    setUkeyChecking(true)
    try {
      const report = await api.ukeyReport()
      setUkeyChecked(true)
      const deviceLines = (report.devices || []).map((device) => (
        `${device.ukeyCandidate ? 'UKey' : 'USB'}  ${device.name}  序列号 ${device.serial || '无'}`
      ))
      const certLines = (report.certificates || []).map((item) => (
        `单位 ${item.organization || '无'}  截止 ${item.notAfter}  颁发者 ${item.issuer}`
      ))
      Modal.info({
        title: report.distinguishable ? '可以区分多把 UKey' : 'UKey 检测结果',
        width: 680,
        okText: '关闭',
        footer: (
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button
              icon={<DownloadOutlined />}
              onClick={() => { void downloadUkeyBundle() }}
            >
              下载诊断包
            </Button>
            <Button type="primary" onClick={() => Modal.destroyAll()}>关闭</Button>
          </div>
        ),
        content: (
          <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.7 }}>
            {report.summary}
            {deviceLines.length > 0 ? `\n\n外部 USB：\n${deviceLines.join('\n')}` : ''}
            {certLines.length > 0 ? `\n\n证书：\n${certLines.join('\n')}` : ''}
          </div>
        )
      })
    } catch (e: unknown) {
      setUkeyChecked(true)
      message.error('检测失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setUkeyChecking(false)
    }
  }

  const previewStationWindow = async (): Promise<void> => {
    setStationPicking(true)
    try {
      const res = await api.pickStation({
        forceWindow: true,
        useSupplied: true,
        stations: stations
          .filter((row) => row.name.trim())
          .map((row) => ({ id: row.id, name: row.name.trim(), wifiSsid: row.wifiSsid.trim() }))
      })
      if (!res.ok || !res.station) {
        message.info(res.message || '已取消选择场站')
        return
      }
      const prefix = res.demo ? '当前不足两个场站，已用示例数据。' : ''
      message.success(`${prefix}已选择 ${res.station.name}，将连接 ${res.station.wifiSsid}`)
    } catch (e: unknown) {
      message.error('选择窗口失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setStationPicking(false)
    }
  }

  const [loginMode, setLoginMode] = useState<string>('cert')
  const deliveryEnabled = Form.useWatch('deliveryEnabled', form)
  const deliveryEnvFile = Form.useWatch('deliveryEnvFile', form) || ''
  const deliveryBackend = Form.useWatch('deliveryBackend', form) || 'wecom_webhook'
  const deliveryFileDelivery = Form.useWatch('deliveryFileDelivery', form) || 'auto'
  const requireDeliverySecret = deliveryEnabled && deliveryBackend !== 'desktop_wechat' && !String(deliveryEnvFile).trim()
  const showRemoteUploadFields = deliveryBackend !== 'desktop_wechat' || deliveryFileDelivery === 'remote_link'
  const wifiAutoConnectEnabled = Form.useWatch('wifiAutoConnectEnabled', form)
  const wifiInterfaceName = Form.useWatch('wifiInterfaceName', form) || ''

  const refreshDeliveryOutbox = useCallback(async (
    status: DeliveryOutboxStatusFilter = deliveryOutboxStatus
  ): Promise<void> => {
    setDeliveryOutboxLoading(true)
    try {
      const res = await api.listDeliveryOutbox(status, 8)
      setDeliveryOutbox(res.items || [])
      setDeliveryOutboxSummary(res.summary || { pending: 0, sending: 0, sent: 0, failed: 0, total: 0 })
    } catch (e: unknown) {
      message.warning('补发队列读取失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setDeliveryOutboxLoading(false)
    }
  }, [deliveryOutboxStatus, message])

  useEffect(() => {
    if (open && deliveryEnabled) {
      refreshDeliveryOutbox().catch(() => {})
    }
  }, [open, deliveryEnabled, deliveryOutboxStatus, refreshDeliveryOutbox])

  // 路由规则
  const [routeRules, setRouteRules] = useState<{ domain_pattern: string; interface_name: string; expected_ssid?: string; enabled: boolean }[]>([])
  const [interfaces, setInterfaces] = useState<{ name: string; desc: string }[]>([])
  const [interfaceError, setInterfaceError] = useState('')
  const [routeApplying, setRouteApplying] = useState(false)
  const [wifiNetworks, setWifiNetworks] = useState<WifiNetwork[]>([])
  const [wifiNetworkError, setWifiNetworkError] = useState('')
  const [wifiScanning, setWifiScanning] = useState(false)

  const refreshWifiNetworks = useCallback(async (): Promise<void> => {
    setWifiScanning(true)
    try {
      const res = await api.listWifiNetworks(String(form.getFieldValue('wifiInterfaceName') || ''))
      setWifiNetworks(res.networks || [])
      setWifiNetworkError(res.error || '')
    } catch (e: unknown) {
      setWifiNetworks([])
      setWifiNetworkError(e instanceof Error ? e.message : String(e))
    } finally {
      setWifiScanning(false)
    }
  }, [form])

  // 加载路由规则和接口
  useEffect(() => {
    if (open && activeSection === 'section-route') {
      api.listRouteRules().then(r => setRouteRules(r.rules || [])).catch(() => {})
      api.listInterfaces().then(r => {
        setInterfaces(r.interfaces || [])
        setInterfaceError(r.error || '')
      }).catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e)
        setInterfaceError(`无法连接后端: ${msg}`)
      })
    }
  }, [open, activeSection])

  const refreshUsbDevices = useCallback(async (): Promise<void> => {
    setUsbLoading(true)
    try {
      const res = await api.listUsbDevices()
      setUsbDevices(res.devices || [])
      setUsbError(res.error || '')
    } catch (e: unknown) {
      setUsbDevices([])
      setUsbError(e instanceof Error ? e.message : String(e))
    } finally {
      setUsbLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open && activeSection === 'section-route' && wifiAutoConnectEnabled) {
      refreshWifiNetworks().catch(() => {})
    }
  }, [open, activeSection, wifiAutoConnectEnabled, wifiInterfaceName, refreshWifiNetworks])

  useEffect(() => {
    if (open && activeSection === 'section-site') {
      refreshWifiNetworks().catch(() => {})
      refreshUsbDevices().catch(() => {})
    }
  }, [open, activeSection, refreshWifiNetworks, refreshUsbDevices])

  // 同步 form watch 到本地 state
  const watchedLoginMode = Form.useWatch('loginMode', form)
  useEffect(() => {
    if (watchedLoginMode) setLoginMode(watchedLoginMode)
  }, [watchedLoginMode])

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width="80vw"
      footer={null}
      closable={false}
      styles={{
        body: { padding: 0, height: 'calc(75vh)', maxHeight: 700 }
      }}
    >
      <div style={{ display: 'flex', height: '100%' }}>
        {/* ── Left sidebar ── */}
        <div
          style={{
            width: 180,
            minWidth: 180,
            display: 'flex',
            flexDirection: 'column',
            borderRadius: '8px 0 0 8px'
          }}
        >
          <div
            style={{
              padding: '24px 20px 16px',
              fontSize: 16,
              fontWeight: 600,
              letterSpacing: 0.5
            }}
          >
            设置
          </div>

          <Menu
            mode="inline"
            selectedKeys={[activeSection]}
            onClick={({ key }) => setActiveSection(key)}
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              padding: '8px 0'
            }}
            items={SECTIONS.map((sec) => ({
              key: sec.key,
              label: sec.label
            }))}
          />
        </div>

        {/* ── Right content ── */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
          <div
            key={activeSection}
            style={{
              flex: 1,
              overflowY: 'auto',
              padding: '28px 32px'
            }}
          >
            <Form form={form} layout="vertical" style={{ maxWidth: 520 }}>
              {/* ── 基础 ── */}
              {activeSection === 'section-basic' && <>
              <h3
                id="section-basic"
                style={{ fontSize: 18, fontWeight: 600, marginBottom: 20, marginTop: 0 }}
              >
                基础
              </h3>

              <Form.Item label="下载目录">
                <Space.Compact style={{ width: '100%' }}>
                  <Input value={dir} readOnly placeholder="未设置" />
                  <Button icon={<FolderOpenOutlined />} onClick={pickDir}>
                    浏览
                  </Button>
                </Space.Compact>
              </Form.Item>

              <Form.Item name="autoLaunch" label="开机自启动" valuePropName="checked">
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
              </>}

              {/* ── 网站信息配置 ── */}
              {activeSection === 'section-site' && <>
              <h3
                id="section-site"
                style={{ fontSize: 18, fontWeight: 600, marginBottom: 20, marginTop: 0 }}
              >
                网站信息配置
              </h3>

              <Form.Item name="siteUrl" label="目标站点">
                <Input placeholder="https://pm.gd.csg.cn" />
              </Form.Item>

              <Form.Item name="loginMode" label="登录方式">
                <Select
                  options={[
                    { value: 'cert', label: '证书登录' },
                    { value: 'password', label: '密码登录' }
                  ]}
                />
              </Form.Item>

              {/* 证书登录：证书密码 */}
              {loginMode === 'cert' && (
                <>
                <Form.Item
                  name="certPassword"
                  label="证书密码"
                  extra="只登录一个场站时可以继续用这一项。多个场站请在下面按场站分别填写。"
                >
                  <Input.Password visibilityToggle={false} />
                </Form.Item>
                <Form.Item
                  name="multiStationEnabled"
                  label="启用多场站自动切换"
                  valuePropName="checked"
                  extra="开启后，执行任务前弹出场站多选窗口，并按所选场站切换 WiFi 和证书登录。关闭后仍可保存场站，但运行时不切换。"
                >
                  <Switch checkedChildren="开" unCheckedChildren="关" />
                </Form.Item>
                <div style={{ marginBottom: 8, fontWeight: 600 }}>场站、WiFi 与证书密码</div>
                <div style={{ color: '#8b98a8', fontSize: 13, marginBottom: 12, lineHeight: 1.6 }}>
                  配了两个及以上场站时，点立即执行会先弹出多选窗口。证书窗口的列是颁发给、单位、截止日期、颁发者，用来对上的是「单位」。单位名称写一段能对上的字即可，例如单位是「三峡新能源阳江发电有限公司」时写「阳江」，显示成「三峡新能源发电（韶关浈江）有...」时写「韶关浈江」或「浈江」。改完要点右下角「保存」。
                </div>
                {usbError && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message="USB 设备读取失败"
                    description={usbError}
                  />
                )}
                {stations.map((station, index) => (
                  <div
                    key={station.id}
                    style={{
                      border: '1px solid #f0f0f0',
                      borderRadius: 8,
                      padding: 12,
                      marginBottom: 12
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                      <span style={{ color: '#667085' }}>场站 {index + 1}</span>
                      <Button
                        danger
                        size="small"
                        htmlType="button"
                        onClick={() => editStations((rows) => rows.filter((row) => row.id !== station.id))}
                      >
                        删除
                      </Button>
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                      <Input
                        placeholder="场站名称，例如阳江明阳"
                        value={station.name}
                        onChange={(event) => {
                          const name = event.target.value
                          editStations((rows) => rows.map((row) => row.id === station.id ? { ...row, name } : row))
                        }}
                      />
                      <AutoComplete
                        allowClear
                        value={station.deviceId}
                        placeholder={usbLoading ? '正在读取外部 USB' : '特征码：外部 USB / UKey，或粘贴'}
                        options={usbDevices.map((device) => ({
                          value: device.id,
                          label: device.ukeyCandidate ? `${device.label} · UKey` : device.label
                        }))}
                        filterOption={(input, option) =>
                          String(option?.label || option?.value || '').toLowerCase().includes(input.toLowerCase())
                        }
                        onChange={(value) => {
                          const deviceId = String(value ?? '')
                          editStations((rows) => rows.map((row) => row.id === station.id ? { ...row, deviceId } : row))
                        }}
                      />
                      <Input
                        placeholder="单位名称，例如阳江、韶关浈江"
                        value={station.orgName}
                        onChange={(event) => {
                          const orgName = event.target.value
                          editStations((rows) => rows.map((row) => row.id === station.id ? { ...row, orgName } : row))
                        }}
                      />
                      <AutoComplete
                        allowClear
                        value={station.wifiSsid}
                        placeholder={wifiScanning ? '正在扫描 WiFi' : '选择或输入 WiFi 名称'}
                        options={wifiNetworks.map((item) => ({
                          value: item.ssid,
                          label: [
                            item.ssid,
                            item.current ? '当前' : '',
                            item.signal ? `信号 ${item.signal}` : ''
                          ].filter(Boolean).join(' · ')
                        }))}
                        filterOption={(input, option) =>
                          String(option?.value || '').toLowerCase().includes(input.toLowerCase())
                        }
                        onChange={(value) => {
                          const wifiSsid = String(value ?? '')
                          editStations((rows) => rows.map((row) => row.id === station.id
                            ? { ...row, wifiSsid }
                            : row))
                        }}
                      />
                      <Input.Password
                        visibilityToggle={false}
                        placeholder={station.wifiPasswordSaved ? 'WiFi 密码已保存，留空保持' : 'WiFi 密码'}
                        value={station.wifiPassword}
                        onChange={(event) => {
                          const wifiPassword = event.target.value
                          editStations((rows) => rows.map((row) => row.id === station.id ? { ...row, wifiPassword } : row))
                        }}
                      />
                      <Input.Password
                        visibilityToggle={false}
                        placeholder={station.certPasswordSaved ? '证书密码已保存，留空保持' : '证书密码'}
                        value={station.certPassword}
                        onChange={(event) => {
                          const certPassword = event.target.value
                          editStations((rows) => rows.map((row) => row.id === station.id ? { ...row, certPassword } : row))
                        }}
                      />
                    </div>
                  </div>
                ))}
                <Space style={{ marginBottom: 8 }}>
                  <Button
                    htmlType="button"
                    loading={ukeyChecking}
                    onClick={() => { void checkInsertedUkeys() }}
                  >
                    检测 UKey
                  </Button>
                  {ukeyChecked && (
                    <Button
                      htmlType="button"
                      icon={<DownloadOutlined />}
                      loading={ukeyDownloading}
                      onClick={() => { void downloadUkeyBundle() }}
                    >
                      下载诊断包
                    </Button>
                  )}
                  <Button
                    htmlType="button"
                    icon={<SyncOutlined />}
                    loading={usbLoading || wifiScanning}
                    onClick={() => {
                      refreshUsbDevices().catch(() => {})
                      refreshWifiNetworks().catch(() => {})
                    }}
                  >
                    刷新设备和 WiFi
                  </Button>
                  <Button
                    htmlType="button"
                    onClick={() => editStations((rows) => [...rows, {
                      id: `st-${Date.now().toString(36)}`,
                      name: '',
                      deviceId: '',
                      orgName: '',
                      wifiSsid: '',
                      wifiPassword: '',
                      certPassword: '',
                      wifiPasswordSaved: false,
                      certPasswordSaved: false
                    }])}
                  >
                    添加场站
                  </Button>
                  <Button htmlType="button" loading={stationPicking} onClick={() => { void previewStationWindow() }}>
                    预览选择窗口
                  </Button>
                </Space>
                </>
              )}

              {/* 密码登录：账号 + 密码 */}
              {loginMode === 'password' && (
                <>
                  <Form.Item name="account" label="账号">
                    <Input />
                  </Form.Item>
                  <Form.Item name="accountPassword" label="密码">
                    <Input.Password visibilityToggle={false} />
                  </Form.Item>
                </>
              )}
              </>}

              {/* ── 录制工坊 ── */}
              {activeSection === 'section-workshop' && <>
              <h3
                id="section-workshop"
                style={{ fontSize: 18, fontWeight: 600, marginBottom: 20, marginTop: 0 }}
              >
                录制工坊
              </h3>

              <Form.Item
                name="workshopUploadUrl"
                label="远程上传地址"
                extra="留空则提交时仅做本地标记；远程接口对接后再填写"
              >
                <Input placeholder="https://api.example.com/workshop/upload" />
              </Form.Item>

              <Form.Item name="workshopUploadToken" label="上传 Token（可选）">
                <Input.Password placeholder="Bearer Token 或 API Key" />
              </Form.Item>
              </>}

              {/* ── 结果发送 ── */}
              {activeSection === 'section-delivery' && <>
              <h3
                id="section-delivery"
                style={{ fontSize: 18, fontWeight: 600, marginBottom: 20, marginTop: 0 }}
              >
                结果发送
              </h3>
              <div style={{ color: '#8b98a8', fontSize: 13, marginBottom: 16 }}>
                管任务执行成功后的 Excel、压缩包和下载链接；适合给业务人员拿结果文件。
              </div>

              <Form.Item name="deliveryEnabled" label="启用任务产物发送" valuePropName="checked">
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>

              {deliveryEnabled && (
                <>
                  <Form.Item name="deliveryBackend" label="发送通道">
                    <Select
                      options={[
                        { value: 'wecom_webhook', label: '企业微信群机器人' },
                        { value: 'serverchan', label: 'Server酱（个人微信链接）' },
                        { value: 'pushplus', label: 'PushPlus（个人微信链接）' },
                        { value: 'desktop_wechat', label: '本机微信 PoC（Windows 实验）' }
                      ]}
                    />
                  </Form.Item>

                  <Form.Item name="deliveryEnvFile" label="凭据文件路径（可选）">
                    <Input placeholder="/c/secure/spider-gd-delivery.env" />
                  </Form.Item>

                  {deliveryBackend === 'serverchan' && (
                    <>
                      <Form.Item
                        name="deliveryServerchanSendKey"
                        label="Server酱 SendKey"
                        rules={requireDeliverySecret ? [{ required: true, message: '请填写 Server酱 SendKey 或凭据文件路径' }] : []}
                      >
                        <Input.Password placeholder="SCT..." />
                      </Form.Item>

                      <Form.Item
                        name="deliveryServerchanChannel"
                        label="Server酱通道（可选）"
                      >
                        <Input placeholder="留空默认；多个用 | 分隔" />
                      </Form.Item>

                      <Form.Item
                        name="deliveryServerchanOpenid"
                        label="抄送 OpenID（可选）"
                      >
                        <Input placeholder="多个用 , 分隔" />
                      </Form.Item>
                    </>
                  )}

                  {deliveryBackend === 'pushplus' && (
                    <>
                      <Form.Item
                        name="deliveryPushplusToken"
                        label="PushPlus Token"
                        rules={requireDeliverySecret ? [{ required: true, message: '请填写 PushPlus Token 或凭据文件路径' }] : []}
                      >
                        <Input.Password placeholder="your_token" />
                      </Form.Item>

                      <Form.Item
                        name="deliveryPushplusChannel"
                        label="PushPlus 通道（可选）"
                      >
                        <Input placeholder="留空默认微信；如 webhook / mail / sms" />
                      </Form.Item>
                    </>
                  )}

                  {deliveryBackend === 'wecom_webhook' && (
                    <Form.Item
                      name="deliveryWecomWebhook"
                      label="企业微信群机器人 Webhook"
                      rules={requireDeliverySecret ? [{ required: true, message: '请填写企业微信群机器人 Webhook 或凭据文件路径' }] : []}
                    >
                      <Input placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." />
                    </Form.Item>
                  )}

                  {deliveryBackend === 'desktop_wechat' && (
                    <>
                      <Alert
                        type="warning"
                        showIcon
                        style={{ marginBottom: 16 }}
                        message="本机微信 PoC 只适合 Windows 值守机：需要微信已登录、桌面未锁屏，并且当前会话或绑定会话可被校验。"
                      />

                      <Form.Item
                        name="deliveryDesktopWechatTarget"
                        label="微信目标会话"
                        rules={[
                          { required: true, message: '请填写微信目标会话，例如 文件传输助手' }
                        ]}
                      >
                        <Input placeholder="文件传输助手" />
                      </Form.Item>

                      <Form.Item name="deliveryDesktopWechatBackend" label="桌面控制后端">
                        <Select
                          options={[
                            { value: 'powershell', label: 'PowerShell UIA（推荐）' },
                            { value: 'wxauto', label: 'wxauto' },
                            { value: 'auto', label: '自动选择' }
                          ]}
                        />
                      </Form.Item>

                      <Form.Item name="deliveryDesktopWechatSelectMode" label="会话选择方式">
                        <Select
                          options={[
                            { value: 'current', label: '校验当前会话' },
                            { value: 'bound', label: '使用已绑定会话' },
                            { value: 'exact', label: '精确搜索（实验）' },
                            { value: 'legacy-search', label: '旧搜索兼容（实验）' }
                          ]}
                        />
                      </Form.Item>

                      <Form.Item name="deliveryDesktopWechatProfilePath" label="绑定配置路径">
                        <Input placeholder="./data/wechat_targets.json" />
                      </Form.Item>

                      <Form.Item name="deliveryDesktopWechatAppName" label="微信进程名（可选）">
                        <Input placeholder="留空自动匹配 WeChat/微信" />
                      </Form.Item>

                      <Form.Item name="deliveryDesktopWechatPauseSec" label="操作间隔（秒）">
                        <Input placeholder="0.7" />
                      </Form.Item>

                      <Form.Item name="deliveryDesktopWechatHashThreshold" label="绑定校验阈值">
                        <Input placeholder="12" />
                      </Form.Item>

                      <Form.Item
                        name="deliveryDesktopWechatAllowUnstableSearch"
                        label="允许搜索式真实发送"
                        valuePropName="checked"
                        extra="仅临时 PoC 排查时打开；日常值守建议使用当前会话或绑定会话。"
                      >
                        <Switch checkedChildren="开" unCheckedChildren="关" />
                      </Form.Item>
                    </>
                  )}

                  <Form.Item name="deliveryTitle" label="消息标题">
                    <Input placeholder="spider-gd 采集完成" />
                  </Form.Item>

                  <Form.Item
                    name="deliveryFileDelivery"
                    label="文件发送方式"
                    extra={deliveryBackend === 'desktop_wechat' ? '本机微信 PoC 下，自动/直发会直接把本地文件发到微信；远程链接会先上传再发链接。' : undefined}
                  >
                    <Select
                      options={deliveryBackend === 'desktop_wechat'
                        ? [
                            { value: 'auto', label: '直接发送本地文件' },
                            { value: 'wecom_file', label: '直接发送本地文件' },
                            { value: 'remote_link', label: '远程上传后发送链接' }
                          ]
                        : [
                            { value: 'auto', label: '自动：小文件直发，大文件发链接' },
                            { value: 'wecom_file', label: '优先企业微信文件直发，超 20MB 发链接' },
                            { value: 'remote_link', label: '始终远程上传后发链接' }
                          ]}
                    />
                  </Form.Item>

                  {deliveryBackend !== 'desktop_wechat' && (
                    <Form.Item name="deliveryWecomFileMaxMb" label="企业微信文件直发上限（MB）">
                      <Input placeholder="20" />
                    </Form.Item>
                  )}

                  <Form.Item name="deliveryRetryAttempts" label="失败重试次数">
                    <Input placeholder="2" />
                  </Form.Item>

                  <Form.Item name="deliveryRetryDelaySec" label="重试间隔（秒）">
                    <Input placeholder="0.5" />
                  </Form.Item>

                  <Form.Item name="deliverySendMessage" label="发送完成消息" valuePropName="checked">
                    <Switch checkedChildren="开" unCheckedChildren="关" />
                  </Form.Item>

                  <Form.Item name="deliverySendFile" label="发送文件或链接" valuePropName="checked">
                    <Switch checkedChildren="开" unCheckedChildren="关" />
                  </Form.Item>

                  {showRemoteUploadFields && (
                    <>
                      <Form.Item
                        name="deliveryRemoteUploadUrl"
                        label="远程上传地址（可选）"
                        extra="远程链接模式、个人微信通道、企业微信超 20MB 回退时使用；留空则复用录制工坊地址"
                      >
                        <Input placeholder="https://api.example.com" />
                      </Form.Item>

                      <Form.Item
                        name="deliveryRemoteUploadToken"
                        label="远程上传 Token（可选）"
                        extra="留空则复用录制工坊的上传 Token"
                      >
                        <Input.Password placeholder="Bearer Token 或 API Key" />
                      </Form.Item>

                      <Form.Item name="deliveryRemoteUploadRelativePath" label="远程目录">
                        <Input placeholder="/upload/publicData" />
                      </Form.Item>

                      <Form.Item
                        name="deliveryRemoteUploadVerifyUrl"
                        label="验证下载链接"
                        valuePropName="checked"
                      >
                        <Switch checkedChildren="开" unCheckedChildren="关" />
                      </Form.Item>
                    </>
                  )}

                  <Form.Item name="deliveryTestFilePath" label="测试文件路径（可选）">
                    <Input placeholder="C:\\path\\to\\result.zip" />
                  </Form.Item>

                  <Space style={{ marginBottom: 16 }}>
                    <Button
                      size="small"
                      loading={deliveryTesting}
                      onClick={async () => {
                        setDeliveryTesting(true)
                        try {
                          const latest = await persistSettings()
                          const res = await api.testDelivery({
                            filePath: latest.deliveryTestFilePath,
                            send: false
                          })
                          if (res.ok) {
                            const text = res.actions?.slice(-1)[0] || '发送配置检查通过'
                            if (res.warnings?.length) {
                              message.warning(`${text}；${res.warnings.join('; ')}`)
                            } else {
                              message.success(text)
                            }
                          } else {
                            message.error(res.errors?.join('; ') || '发送配置检查失败')
                          }
                        } catch (e: unknown) {
                          message.error('检查失败: ' + (e instanceof Error ? e.message : String(e)))
                        } finally {
                          setDeliveryTesting(false)
                        }
                      }}
                    >
                      检查发送计划
                    </Button>
                    <Button
                      size="small"
                      type="primary"
                      loading={deliveryTesting}
                      onClick={() => {
                        Modal.confirm({
	                          title: '发送测试',
	                          content: '将按当前发送配置真实发送；未填写测试文件路径时会自动生成样例 zip 验证文件或链接链路。',
                          okText: '发送',
                          cancelText: '取消',
                          onOk: async () => {
                            setDeliveryTesting(true)
                            try {
                              const latest = await persistSettings()
                              const res = await api.testDelivery({
                                filePath: latest.deliveryTestFilePath,
                                send: true,
                                sampleZip: !latest.deliveryTestFilePath,
                                verify: true
                              })
                              if (res.sent) {
	                                const receipt = res.outbox?.delivery_note
	                                  ? `送达方式：${res.outbox.delivery_note}`
	                                  : '发送测试已提交'
                                const link = res.outbox?.remote_url ? '；已生成下载链接' : ''
                                const verifyText = res.verification
                                  ? res.verification.ok
                                    ? '；核验通过'
                                    : `；核验未通过：${res.verification.errors?.[0] || '请检查回执'}`
                                  : ''
                                if (!res.ok || res.warnings?.length) {
                                  const warningText = res.warnings?.length ? `；${res.warnings.join('; ')}` : ''
                                  message.warning(`${receipt}${link}${verifyText}${warningText}`)
                                } else {
                                  message.success(`${receipt}${link}${verifyText}`)
                                }
                              } else {
	                                message.error(res.errors?.join('; ') || '发送测试失败')
                              }
                            } catch (e: unknown) {
                              message.error('发送失败: ' + (e instanceof Error ? e.message : String(e)))
                            } finally {
                              setDeliveryTesting(false)
                            }
                          }
                        })
                      }}
                    >
                      真实发送测试
                    </Button>
                    <Button
                      size="small"
                      loading={deliveryTesting}
                      onClick={() => {
                        Modal.confirm({
                          title: '远程链接验收',
                          content: '将临时强制使用远程链接模式，发送样例 zip 或指定测试文件，并下载远端文件比对 SHA256。',
                          okText: '验收',
                          cancelText: '取消',
                          onOk: async () => {
                            setDeliveryTesting(true)
                            try {
                              const latest = await persistSettings()
                              const res = await api.testDelivery({
                                filePath: latest.deliveryTestFilePath,
                                send: true,
                                sampleZip: !latest.deliveryTestFilePath,
                                verify: true,
                                forceRemoteLink: true,
                                requireRemoteUrl: true,
                                downloadRemote: true
                              })
                              if (res.sent && res.ok && res.outbox?.remote_url) {
                                message.success(`远程链接验收通过：${res.outbox.remote_url}`)
                              } else {
                                const err = res.errors?.[0] || res.verification?.errors?.[0] || '未生成可转发下载链接'
                                message.warning(`远程链接验收未通过：${err}`)
                              }
                              await refreshDeliveryOutbox('sent')
                            } catch (e: unknown) {
                              message.error('远程链接验收失败: ' + (e instanceof Error ? e.message : String(e)))
                            } finally {
                              setDeliveryTesting(false)
                            }
                          }
                        })
                      }}
                    >
                      远程链接验收
                    </Button>
                  </Space>

                  <div style={{ borderTop: '1px solid #edf1f7', paddingTop: 12, marginBottom: 16 }}>
                    <div
                      style={{
                        display: 'grid',
                        gap: 8,
                        marginBottom: 8
                      }}
                    >
                      <div
                        style={{
                          color: '#344054',
                          fontSize: 13,
                          fontWeight: 600,
                          lineHeight: 1.5,
                          minWidth: 0
                        }}
                      >
                        补发队列：待处理 {deliveryOutboxSummary.pending}，发送中 {deliveryOutboxSummary.sending}，失败 {deliveryOutboxSummary.failed}，已发送 {deliveryOutboxSummary.sent}
                      </div>
                      <div
                        style={{
                          display: 'flex',
                          flexWrap: 'wrap',
                          alignItems: 'center',
                          gap: 8
                        }}
                      >
                        <Segmented
                          size="small"
                          value={deliveryOutboxStatus}
                          options={[
                            { label: '待补发', value: 'pending' },
                            { label: '发送中', value: 'sending' },
                            { label: '失败', value: 'failed' },
                            { label: '已发送', value: 'sent' },
                            { label: '全部', value: '' }
                          ]}
                          onChange={(value) => {
                            setDeliveryOutboxStatus(value as DeliveryOutboxStatusFilter)
                          }}
                        />
                        <Button
                          size="small"
                          loading={deliveryOutboxLoading}
                          onClick={() => {
                            refreshDeliveryOutbox().catch(() => {})
                          }}
                        >
                          刷新
                        </Button>
                        <Button
                          size="small"
                          loading={deliveryOutboxLoading}
                          disabled={deliveryOutboxSummary.sent === 0}
                          onClick={async () => {
                            setDeliveryOutboxLoading(true)
                            try {
                              const res = await api.cleanupDeliveryOutbox('sent', 200)
                              message.success(`已清理 ${res.deleted} 条已发送记录`)
                              await refreshDeliveryOutbox()
                            } catch (e: unknown) {
                              message.error('清理失败: ' + (e instanceof Error ? e.message : String(e)))
                            } finally {
                              setDeliveryOutboxLoading(false)
                            }
                          }}
                        >
                          清理已发送
                        </Button>
                        <Button
                          size="small"
                          loading={deliveryOutboxLoading}
                          disabled={deliveryOutboxSummary.failed === 0}
                          onClick={async () => {
                            setDeliveryOutboxLoading(true)
                            try {
                              const res = await api.requeueDeliveryOutbox('failed', 200)
                              message.success(`已恢复 ${res.requeued} 条失败记录`)
                              setDeliveryOutboxStatus('pending')
                              await refreshDeliveryOutbox('pending')
                            } catch (e: unknown) {
                              message.error('恢复失败: ' + (e instanceof Error ? e.message : String(e)))
                            } finally {
                              setDeliveryOutboxLoading(false)
                            }
                          }}
                        >
                          恢复失败
                        </Button>
                        <Button
                          size="small"
                          loading={deliveryOutboxLoading}
                          disabled={deliveryOutboxSummary.failed === 0}
                          onClick={async () => {
                            setDeliveryOutboxLoading(true)
                            try {
                              const res = await api.cleanupDeliveryOutbox('failed', 200)
                              message.success(`已清理 ${res.deleted} 条失败记录`)
                              await refreshDeliveryOutbox()
                            } catch (e: unknown) {
                              message.error('清理失败: ' + (e instanceof Error ? e.message : String(e)))
                            } finally {
                              setDeliveryOutboxLoading(false)
                            }
                          }}
                        >
                          清理失败
                        </Button>
                        <Button
                          size="small"
                          loading={deliveryOutboxLoading}
                          disabled={
                            deliveryOutboxSummary.pending === 0 &&
                            deliveryOutboxSummary.failed === 0 &&
                            deliveryOutboxSummary.sending === 0
                          }
                          onClick={async () => {
                            setDeliveryOutboxLoading(true)
                            try {
                              await persistSettings()
                              const includeFailed = deliveryOutboxSummary.failed > 0
                              const res = await api.retryDelivery(20, includeFailed)
                              const recovered = res.requeued_failed ? `，恢复 ${res.requeued_failed}` : ''
                              const firstError = !res.ok && res.errors?.length ? `；${res.errors[0]}` : ''
                              const text = `补发完成${recovered}：成功 ${res.sent}，失败 ${res.failed}，阻塞 ${res.blocked || 0}，跳过 ${res.skipped}${firstError}`
                              if (res.ok) {
                                message.success(text)
                              } else {
                                message.warning(text)
                              }
                              await refreshDeliveryOutbox()
                            } catch (e: unknown) {
                              message.error('补发失败: ' + (e instanceof Error ? e.message : String(e)))
                            } finally {
                              setDeliveryOutboxLoading(false)
                            }
                          }}
                        >
                          {deliveryOutboxSummary.failed > 0
                            ? '恢复并补发'
                            : deliveryOutboxSummary.sending > 0 && deliveryOutboxSummary.pending === 0
                              ? '检查发送中'
                              : '立即补发'}
                        </Button>
                      </div>
                    </div>
                    {deliveryOutbox.length > 0 ? (
                      <div style={{ display: 'grid', gap: 8 }}>
                        {deliveryOutbox.map((item) => {
                          const forwarding = item.forwarding
                          const forwardUrl = forwarding?.remote_url || item.remote_url
                          return (
                            <div
                            key={item.id || item.task_id}
                            style={{
                              border: '1px solid #edf1f7',
                              borderRadius: 6,
                              padding: '8px 10px',
                              background: '#fbfcff'
                            }}
                            >
                            <div
                              style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                                gap: 8
                              }}
                            >
                              <div style={{ color: '#1f2937', fontSize: 13, fontWeight: 600 }}>
                                {item.task_id}
                              </div>
                              <Button
                                size="small"
                                loading={deliveryVerifyingTask === item.task_id}
                                onClick={async () => {
                                  setDeliveryVerifyingTask(item.task_id)
                                  try {
                                    const res = await api.verifyDelivery({ taskId: item.task_id })
                                    const remote = res.checks?.find(check => check.name === 'remote_url_reachable')
                                    const remoteText = remote
                                      ? remote.skipped
                                        ? '；远程链接未检查'
                                        : remote.ok
                                          ? '；远程链接可访问'
                                          : '；远程链接不可访问'
                                      : ''
                                    if (res.ok) {
                                      message.success(`核验通过：${res.receipt?.zip_sha256?.slice(0, 16) || item.task_id}${remoteText}`)
                                    } else {
                                      message.warning(`核验未通过：${res.errors?.[0] || '请检查回执和文件'}${remoteText}`)
                                    }
                                  } catch (e: unknown) {
                                    message.error('核验失败: ' + (e instanceof Error ? e.message : String(e)))
                                  } finally {
                                    setDeliveryVerifyingTask('')
                                  }
                                }}
                              >
                                核验
                              </Button>
                            </div>
                            <div style={{ color: '#667085', fontSize: 12, marginTop: 4 }}>
                              {item.zip_path.split(/[\\/]/).pop() || item.zip_path} · 已尝试 {item.attempts} 次
                            </div>
                            {(item.zip_size || item.zip_sha256) && (
                              <div style={{ color: '#667085', fontSize: 12, marginTop: 4 }}>
                                {item.zip_size ? `大小 ${item.zip_size} bytes` : ''}
                                {item.zip_size && item.zip_sha256 ? ' · ' : ''}
                                {item.zip_sha256 ? `SHA256 ${item.zip_sha256.slice(0, 16)}...` : ''}
                              </div>
                            )}
                            {item.remote_url && (
                              <div style={{ fontSize: 12, marginTop: 4 }}>
                                <a href={item.remote_url} target="_blank" rel="noreferrer">
                                  下载链接
                                </a>
                              </div>
                            )}
                            {forwarding && (
                              <div
                                style={{
                                  color: forwarding.ready ? '#027a48' : '#667085',
                                  fontSize: 12,
                                  marginTop: 4,
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: 8,
                                  flexWrap: 'wrap'
                                }}
                              >
                                <span>可转发：{forwarding.instruction}</span>
                                {forwarding.can_forward_link && forwardUrl && (
                                  <Button
                                    size="small"
                                    icon={<CopyOutlined />}
                                    onClick={async () => {
                                      try {
                                        await navigator.clipboard.writeText(forwardUrl)
                                        message.success('下载链接已复制')
                                      } catch (e: unknown) {
                                        message.error('复制失败: ' + (e instanceof Error ? e.message : String(e)))
                                      }
                                    }}
                                  >
                                    复制链接
                                  </Button>
                                )}
                              </div>
                            )}
                            {Boolean(item.file_sent) && (
                              <div style={{ color: '#027a48', fontSize: 12, marginTop: 4 }}>
                                附件已送达，等待说明消息补发
                              </div>
                            )}
                            {item.delivery_note && (
                              <div style={{ color: '#027a48', fontSize: 12, marginTop: 4 }}>
                                送达方式：{item.delivery_note}
                              </div>
                            )}
                            {item.last_error && (
                              <div style={{ color: '#b42318', fontSize: 12, marginTop: 4 }}>
                                {item.last_error}
                              </div>
                            )}
                            </div>
                          )
                        })}
                      </div>
                    ) : (
                      <div style={{ color: '#98a2b3', fontSize: 13 }}>当前筛选暂无产物</div>
                    )}
                  </div>
                </>
              )}

              </>}

              {/* ── 路由 ── */}
              {activeSection === 'section-route' && <>
              <h3
                id="section-route"
                style={{ fontSize: 18, fontWeight: 600, marginBottom: 20, marginTop: 0 }}
              >
                路由
              </h3>
              <div style={{ color: '#8b98a8', fontSize: 13, marginBottom: 16 }}>
                指定目标站点通过特定网卡访问（需要管理员权限才能应用）
              </div>

              <Form.Item
                name="wifiAutoConnectEnabled"
                label="任务前连接指定 WiFi"
                valuePropName="checked"
              >
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>

              {wifiAutoConnectEnabled && (
                <>
                  <Form.Item name="wifiInterfaceName" label="WiFi 网卡">
                    <Select
                      allowClear
                      showSearch
                      placeholder="留空自动选择"
                      options={interfaces.map(i => ({ label: i.name, value: i.name }))}
                    />
                  </Form.Item>

                  <Form.Item label="目标 WiFi" required>
                    <Space.Compact style={{ width: '100%' }}>
                      <Form.Item
                        name="wifiSsid"
                        noStyle
                        rules={[{ required: true, message: '请选择或填写 WiFi 名称' }]}
                      >
                        <AutoComplete
                          allowClear
                          style={{ width: '100%' }}
                          placeholder={wifiScanning ? '正在扫描 WiFi' : '选择或输入 WiFi 名称'}
                          options={wifiNetworks.map(item => ({
                            value: item.ssid,
                            label: [
                              item.ssid,
                              item.current ? '当前' : '',
                              item.signal ? `信号 ${item.signal}` : '',
                              item.security || ''
                            ].filter(Boolean).join(' · ')
                          }))}
                          filterOption={(input, option) =>
                            String(option?.value || '').toLowerCase().includes(input.toLowerCase())
                          }
                        />
                      </Form.Item>
                      <Button
                        icon={<SyncOutlined />}
                        loading={wifiScanning}
                        onClick={() => refreshWifiNetworks().catch(() => {})}
                      >
                        刷新
                      </Button>
                    </Space.Compact>
                  </Form.Item>

                  {wifiNetworkError && (
                    <Alert
                      type="warning"
                      message="WiFi 列表获取失败"
                      description={wifiNetworkError}
                      showIcon
                      closable
                      style={{ marginBottom: 16 }}
                    />
                  )}

                  <Form.Item
                    name="wifiPassword"
                    label="WiFi 密码"
                    extra="留空则保持已保存密码；如果系统已有该 WiFi 配置，也可以留空尝试连接。"
                  >
                    <Input.Password visibilityToggle={false} placeholder="留空保持已保存密码" />
                  </Form.Item>

                  <Form.Item name="wifiConnectTimeoutSec" label="连接等待时间（秒）">
                    <Input placeholder="30" />
                  </Form.Item>

                  <Form.Item
                    name="wifiFailTaskOnError"
                    label="连接失败中止任务"
                    valuePropName="checked"
                  >
                    <Switch checkedChildren="开" unCheckedChildren="关" />
                  </Form.Item>

                  <Button
                    size="small"
                    loading={wifiTesting}
                    style={{ marginBottom: 16 }}
                    onClick={async () => {
                      setWifiTesting(true)
                      try {
                        await persistSettings()
                        const res = await api.testWifiConnection()
                        if (res.ok) {
                          message.success(res.message || 'WiFi 准备完成')
                        } else {
                          message.warning(res.message || 'WiFi 准备失败')
                        }
                      } catch (e: unknown) {
                        message.error('测试失败: ' + (e instanceof Error ? e.message : String(e)))
                      } finally {
                        setWifiTesting(false)
                      }
                    }}
                  >
                    测试连接
                  </Button>
                </>
              )}

              {interfaceError && (
                <Alert
                  type="warning"
                  message="网卡列表获取失败"
                  description={interfaceError}
                  showIcon
                  closable
                  style={{ marginBottom: 16 }}
                />
              )}

              {routeRules.map((rule, idx) => (
                <div key={idx} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                  <Input
                    placeholder="pm.gd.csg.cn"
                    value={rule.domain_pattern}
                    onChange={e => {
                      const next = [...routeRules]
                      next[idx] = { ...next[idx], domain_pattern: e.target.value }
                      setRouteRules(next)
                    }}
                    style={{ flex: 1 }}
                  />
                  <Select
                    placeholder="网卡"
                    value={rule.interface_name || undefined}
                    onChange={v => {
                      const next = [...routeRules]
                      next[idx] = { ...next[idx], interface_name: v }
                      setRouteRules(next)
                    }}
                    style={{ width: 160 }}
                    options={interfaces.map(i => ({ label: i.name, value: i.name }))}
                  />
                  <Input
                    placeholder="SSID(可选)"
                    value={rule.expected_ssid || ''}
                    onChange={e => {
                      const next = [...routeRules]
                      next[idx] = { ...next[idx], expected_ssid: e.target.value }
                      setRouteRules(next)
                    }}
                    style={{ width: 140 }}
                  />
                  <Button danger size="small" onClick={() => setRouteRules(routeRules.filter((_, i) => i !== idx))}>
                    删
                  </Button>
                </div>
              ))}
              <Space style={{ marginTop: 8 }}>
                <Button size="small" onClick={() => setRouteRules([...routeRules, { domain_pattern: '', interface_name: '', expected_ssid: '', enabled: true }])}>
                  + 添加
                </Button>
                <Button
                  size="small"
                  type="primary"
                  loading={routeApplying}
                  onClick={async () => {
                    // 过滤掉空的无效规则
                    const validRules = routeRules.filter(r => r.domain_pattern.trim() && r.interface_name.trim())
                    if (validRules.length === 0) {
                      message.warning('请先添加路由规则，填写域名和网卡')
                      return
                    }
                    if (validRules.length < routeRules.length) {
                      message.info('已自动跳过空的规则')
                      setRouteRules(validRules)
                    }
                    try {
                      await api.saveRouteRules(validRules)
                      setRouteApplying(true)
                      const res = await api.applyRouteRules()
                      if (res.details?.length) {
                        message.info(res.details.slice(0, 5).join('\n'))
                      }
                      if (res.status === 'ok') {
                        message.success(`双网卡验证通过，已应用 ${res.applied} 条路由`)
                      } else if (res.status === 'degraded') {
                        message.warning(res.message || '专网可用，公网不可达；采集可继续落本地')
                      } else {
                        message.warning(res.message || '专网闭环未通过，请查看诊断详情')
                      }
                    } catch (e: unknown) {
                      message.error('应用失败: ' + (e instanceof Error ? e.message : String(e)))
                    } finally {
                      setRouteApplying(false)
                    }
                  }}
                >
                  保存并应用
                </Button>
              </Space>

              </>}

              {/* ── 关于 ── */}
              {activeSection === 'section-about' && <>
              <h3
                id="section-about"
                style={{ fontSize: 18, fontWeight: 600, marginBottom: 20, marginTop: 0 }}
              >
                关于
              </h3>

              <div style={{ fontSize: 13, color: '#888', marginBottom: 12 }}>
                <div>广东电力数据采集系统</div>
                <div>版本 v{version || '...'}</div>
              </div>

              <Space wrap>
                <Button
                  icon={<SyncOutlined spin={checking} />}
                  onClick={async () => {
                    setChecking(true)
                    try {
                      const result = await window.api.checkUpdate()
                      if (result.error) {
                        message.error('检查失败: ' + result.error)
                      } else if (result.hasUpdate) {
                        message.info('发现新版本 v' + (result.version || '?'))
                      } else {
                        message.success('已是最新版本')
                      }
                    } catch {
                      message.error('检查更新失败')
                    } finally {
                      setChecking(false)
                    }
                  }}
                >
                  检查更新
                </Button>

                {/* 导出数据 */}
                <Button
                  icon={<ExportOutlined />}
                  onClick={async () => {
                    try {
                      const data = await api.exportAllData()
                      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
                      const url = URL.createObjectURL(blob)
                      const a = document.createElement('a')
                      a.href = url
                      a.download = `spider-gd-backup-${new Date().toISOString().slice(0, 10)}.json`
                      a.click()
                      URL.revokeObjectURL(url)
                      message.success('数据已导出')
                    } catch (e: unknown) {
                      message.error('导出失败: ' + (e instanceof Error ? e.message : String(e)))
                    }
                  }}
                >
                  导出数据
                </Button>

                {/* 导入数据 */}
                <Button
                  icon={<ImportOutlined />}
                  onClick={() => {
                    const input = document.createElement('input')
                    input.type = 'file'
                    input.accept = '.json'
                    input.onchange = async () => {
                      const file = input.files?.[0]
                      if (!file) return
                      try {
                        const text = await file.text()
                        const data = JSON.parse(text)
                        const res = await api.importAllData(data)
                        if (res.errors?.length) {
                          message.warning('导入完成，但有错误: ' + res.errors.join('; '))
                        } else {
                          message.success(`导入完成: ${res.imported?.join(', ') || ''}`)
                        }
                      } catch (e: unknown) {
                        message.error('导入失败: ' + (e instanceof Error ? e.message : String(e)))
                      }
                    }
                    input.click()
                  }}
                >
                  导入数据
                </Button>

                {/* 恢复出厂设置 */}
                <Button
                  danger
                  icon={<WarningOutlined />}
                  loading={resetting}
                  onClick={() => {
                    Modal.confirm({
                      title: '恢复出厂设置',
                      content: '将清空所有设置、任务、工作流和路由规则。此操作不可撤销，建议先导出数据备份。',
                      okText: '确认重置',
                      cancelText: '取消',
                      okType: 'danger',
                      onOk: async () => {
                        setResetting(true)
                        try {
                          const res = await api.factoryReset()
                          message.success(res.message || '已恢复出厂设置，请重启生效')
                          onClose()
                        } catch (e: unknown) {
                          message.error('重置失败: ' + (e instanceof Error ? e.message : String(e)))
                        } finally {
                          setResetting(false)
                        }
                      }
                    })
                  }}
                >
                  恢复出厂设置
                </Button>
              </Space>
              </>}
            </Form>
          </div>

          {/* ── Footer ── */}
          <div
            style={{
              borderTop: '1px solid #f0f0f0',
              padding: '12px 32px',
              display: 'flex',
              justifyContent: 'flex-end',
              gap: 8
            }}
          >
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" onClick={save}>
              保存
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  )
}
