import BrowserSessionPanel from '../components/BrowserSessionPanel'
import { useEffect, useRef, useState } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Button, Tooltip, Tag, Modal, Input, Dropdown } from 'antd'
import {
  FolderOutlined,
  ScheduleOutlined,
  CodeOutlined,
  SettingOutlined,
  ThunderboltFilled,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  FileTextOutlined,
  VideoCameraOutlined,
  WifiOutlined
} from '@ant-design/icons'
import HealthIndicators from '../components/HealthIndicators'
import SettingsModal from '../components/SettingsModal'
import { App } from 'antd'
import { api, settingsStore } from '../services/api'

const { Sider, Header, Content } = Layout

interface NavItem {
  key: string
  icon: JSX.Element
  label: string
  /** 为 true 时默认不出现在侧边栏，解锁后才显示。 */
  hidden?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { key: '/', icon: <ScheduleOutlined />, label: '任务配置' },
  { key: '/files', icon: <FolderOutlined />, label: '文件中心' },
  { key: '/execution-logs', icon: <FileTextOutlined />, label: '执行日志' },
  { key: '/indicators', icon: <CodeOutlined />, label: '指标配置', hidden: true },
  { key: '/record-workshop', icon: <VideoCameraOutlined />, label: '录制工坊' },
]

/** 连续点击版本号达到该次数后才弹出口令框。 */
const INDICATORS_UNLOCK_CLICKS = 5
/** 两次点击间隔超过该毫秒数则重新计数。 */
const INDICATORS_UNLOCK_GAP_MS = 3000
/** 维护口令。通过后仅本次窗口显示「指标配置」，退出程序后恢复隐藏。 */
const INDICATORS_UNLOCK_CODE = 'Welcome@2025!'
const INDICATORS_UNLOCK_STORAGE_KEY = 'spider-gd.indicators-unlocked'

function readIndicatorsUnlocked(): boolean {
  try {
    return sessionStorage.getItem(INDICATORS_UNLOCK_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export default function AppLayout(): JSX.Element {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [version, setVersion] = useState('')
  const [currentWifi, setCurrentWifi] = useState('')
  const [wifiError, setWifiError] = useState('')
  const [wifiChecked, setWifiChecked] = useState(false)
  const [indicatorsUnlocked, setIndicatorsUnlocked] = useState(readIndicatorsUnlocked)
  const [gateOpen, setGateOpen] = useState(false)
  const [gateCode, setGateCode] = useState('')
  const [gateError, setGateError] = useState('')
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false)
  const versionClicks = useRef({ count: 0, at: 0 })
  const { message } = App.useApp()
  const indicatorsLocked = location.pathname === '/indicators' && !indicatorsUnlocked

  useEffect(() => {
    if (indicatorsLocked) navigate('/', { replace: true })
  }, [indicatorsLocked, navigate])

  const closeGate = (): void => {
    setGateOpen(false)
    setGateCode('')
    setGateError('')
  }

  const submitGate = (): void => {
    if (gateCode !== INDICATORS_UNLOCK_CODE) {
      setGateError(gateCode ? '口令不正确' : '请输入口令')
      return
    }
    try {
      sessionStorage.setItem(INDICATORS_UNLOCK_STORAGE_KEY, '1')
    } catch { /* 隐私模式写不进时，本次内存状态仍然有效 */ }
    setIndicatorsUnlocked(true)
    closeGate()
    message.success('指标配置已显示')
  }

  useEffect(() => {
    window.api.getVersion().then(setVersion)
  }, [])

  useEffect(() => {
    let alive = true
    const refreshWifi = async (): Promise<void> => {
      try {
        const result = await api.currentWifi()
        if (!alive) return
        setCurrentWifi(result.ssid || '')
        setWifiError(result.error || '')
      } catch {
        if (!alive) return
        setCurrentWifi('')
        setWifiError('无法读取当前 Wi-Fi')
      } finally {
        if (alive) setWifiChecked(true)
      }
    }
    void refreshWifi()
    const timer = window.setInterval(() => { void refreshWifi() }, 15000)
    window.addEventListener('settings-updated', refreshWifi)
    return () => {
      alive = false
      window.clearInterval(timer)
      window.removeEventListener('settings-updated', refreshWifi)
    }
  }, [])

  return (
    <Layout style={{ height: '100vh' }}>
      <style>{`
        .app-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          border-bottom: 1px solid #E7E8E3;
          min-width: 0;
        }
        .app-header-left,
        .app-header-tools {
          display: flex;
          align-items: center;
          min-width: 0;
        }
        .app-header-left {
          gap: 12px;
        }
        .app-header-tools {
          gap: 8px;
        }
        .app-header-wifi {
          min-width: 0;
          display: flex;
          flex-shrink: 1;
        }
        .app-header-wifi-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .app-content {
          padding: 24px;
          overflow: auto;
          display: flex;
          flex-direction: column;
          min-height: 0;
        }
        .app-content.execution-logs-content {
          overflow: hidden;
        }
        .execution-logs-table,
        .execution-logs-table .ant-spin-nested-loading,
        .execution-logs-table .ant-spin-container {
          display: flex;
          flex-direction: column;
          min-height: 0;
          height: 100%;
        }
        .execution-logs-table .ant-spin-container {
          flex: 1;
        }
        .execution-logs-table .ant-table {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
        }
        .execution-logs-table .ant-table-container {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
        }
        .execution-logs-table .ant-table-body {
          flex: 1 1 0;
          min-height: 0;
          max-height: none !important;
          overflow-y: auto !important;
        }
        .execution-logs-table .ant-pagination {
          flex-shrink: 0;
        }
        .app-version {
          position: fixed;
          bottom: 4px;
          right: 8px;
          padding: 6px 10px;
          font-size: 11px;
          color: #BFBFBF;
          z-index: 1000;
          user-select: none;
          cursor: default;
        }
        @media (max-width: 520px) {
          .app-header {
            padding-inline: 8px !important;
          }
          .app-header-left {
            gap: 4px;
          }
          .app-header-left .ant-btn {
            width: 28px;
          }
          .app-header-tools {
            gap: 5px;
            overflow: hidden;
          }
          .app-header-wifi {
            max-width: 100px;
          }
          .health-indicators {
            gap: 4px !important;
            min-width: 0;
            overflow: hidden;
          }
          .health-indicators .health-tag {
            width: 28px;
            height: 24px;
            padding: 1px 0 !important;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            white-space: nowrap;
            font-size: 0 !important;
          }
          .health-indicators .health-tag .anticon {
            margin-inline-end: 0;
            font-size: 13px;
          }
          .app-header-tools > .ant-btn {
            width: 28px;
            padding-inline: 0;
          }
          .app-content {
            padding: 14px 12px;
          }
          .app-version {
            display: none;
          }
        }
        @media (max-width: 380px) {
          .app-header {
            padding-inline: 6px !important;
          }
          .app-header-tools {
            gap: 4px;
          }
          .health-indicators .health-tag {
            width: 25px;
          }
          .app-content {
            padding: 12px 10px;
          }
        }
        @media (max-width: 340px) {
          .app-sider.ant-layout-sider-collapsed {
            flex: 0 0 48px !important;
            max-width: 48px !important;
            min-width: 48px !important;
            width: 48px !important;
          }
          .app-sider .ant-menu-item {
            width: 40px;
            margin-inline: 4px;
            padding-inline: 0 !important;
            justify-content: center;
          }
          .app-sider .ant-menu-item .ant-menu-title-content {
            display: none;
          }
          .app-header {
            padding-inline: 4px !important;
          }
          .app-header-left .ant-btn,
          .app-header-tools > .ant-btn {
            width: 24px;
          }
          .app-header-tools {
            gap: 3px;
          }
          .health-indicators {
            gap: 3px !important;
          }
          .health-indicators .health-tag {
            width: 22px;
            height: 22px;
          }
          .health-indicators .health-tag .anticon {
            font-size: 12px;
          }
          .app-content {
            padding: 10px 7px;
          }
        }
        @media (max-width: 640px) and (max-height: 640px) {
          .app-content {
            padding-top: 7px;
            padding-bottom: 7px;
          }
        }
      `}</style>
      <Sider
        className="app-sider"
        width={200}
        collapsedWidth={56}
        collapsed={collapsed}
        theme="dark"
        style={{ overflow: 'hidden' }}
      >
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            height: '100%'
          }}
        >
          {/* 顶部 Logo 区 */}
          <div
            style={{
              height: 56,
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: collapsed ? 0 : '0 18px',
              justifyContent: collapsed ? 'center' : 'flex-start',
              color: '#fff',
              flexShrink: 0,
              cursor: 'default'
            }}
          >
            <ThunderboltFilled style={{ color: '#1F5FFF', fontSize: 22 }} />
            {!collapsed && (
              <span style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>电力采集</span>
            )}
          </div>

          {/* 中间 Menu，撑满剩余空间 */}
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={[location.pathname]}
            onClick={({ key }) => navigate(String(key))}
            items={NAV_ITEMS
              .filter((item) => !item.hidden || indicatorsUnlocked)
              .map(({ key, icon, label }) => ({ key, icon, label }))}
            style={{ flex: 1, minHeight: 0, borderInlineEnd: 'none' }}
          />

          {/* 底部：设置图标按钮 */}
          <div
            style={{
              padding: 8,
              borderTop: '1px solid rgba(255,255,255,0.08)',
              display: 'flex',
              justifyContent: 'center',
              flexShrink: 0
            }}
          >
            <Button
              type="text"
              style={{ color: 'rgba(255,255,255,0.65)' }}
              icon={<SettingOutlined />}
              onClick={() => setSettingsOpen(true)}
            />
          </div>
        </div>
      </Sider>

      <Layout>
        <Header
          className="app-header"
        >
          <div className="app-header-left">
            <Button
              type="text"
              icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => setCollapsed((c) => !c)}
            />
          </div>
          <div className="app-header-tools">
            <div className="app-header-wifi">
              <Tooltip title={currentWifi ? `当前连接的 Wi-Fi：${currentWifi}` : (wifiError || (wifiChecked ? '当前没有连接 Wi-Fi' : '正在读取 Wi-Fi'))}>
                <Tag
                  icon={<WifiOutlined />}
                  className="health-tag"
                  style={{
                    borderRadius: 6,
                    padding: '1px 10px',
                    margin: 0,
                    fontSize: 12,
                    lineHeight: '22px',
                    fontWeight: 500,
                    display: 'inline-flex',
                    alignItems: 'center',
                    maxWidth: 200,
                    color: currentWifi ? '#fff' : '#8b98a8',
                    background: currentWifi ? '#10b981' : '#2d333b',
                    borderColor: currentWifi ? '#10b981' : '#444c56'
                  }}
                >
                  <span className="app-header-wifi-name">
                    Wi-Fi {currentWifi || (wifiError ? '检测失败' : wifiChecked ? '未连接' : '检测中…')}
                  </span>
                </Tag>
              </Tooltip>
            </div>
            <HealthIndicators />
            <Dropdown.Button
              size="small"
              loading={diagnosticsLoading}
              menu={{
                items: [{ key: 'logs', label: '打开日志目录' }],
                onClick: async () => {
                  try {
                    const { path } = await api.logPath()
                    window.api.openDir(path)
                  } catch {
                    message.error('无法获取日志路径')
                  }
                }
              }}
              onClick={async () => {
                setDiagnosticsLoading(true)
                try {
                  const settings = await settingsStore.load().catch(() => ({} as Record<string, string>))
                  const res = await api.downloadDiagnostics(settings.downloadDir || '', version)
                  message.success(`诊断包已保存: ${res.filename}`)
                  if (res.path) await window.api.revealFile(res.path)
                } catch (error: unknown) {
                  message.error('诊断包下载失败: ' + (error instanceof Error ? error.message : String(error)))
                } finally {
                  setDiagnosticsLoading(false)
                }
              }}
            >
              下载诊断包
            </Dropdown.Button>
          </div>
        </Header>
        <BrowserSessionPanel />
        <Content className={`app-content${location.pathname === '/execution-logs' ? ' execution-logs-content' : ''}`}>
          {indicatorsLocked ? null : <Outlet />}
        </Content>
      </Layout>

      {/* 版本号：右下角固定。连点后输入维护口令，才显示指标配置。 */}
      <div
        className="app-version"
        onClick={() => {
          if (indicatorsUnlocked) return
          const now = Date.now()
          const prev = versionClicks.current
          const count = now - prev.at > INDICATORS_UNLOCK_GAP_MS ? 1 : prev.count + 1
          versionClicks.current = { count, at: now }
          if (count >= INDICATORS_UNLOCK_CLICKS) {
            versionClicks.current = { count: 0, at: 0 }
            setGateCode('')
            setGateError('')
            setGateOpen(true)
          }
        }}
      >
        v{version || '...'}
      </div>
      <Modal
        title="请输入口令"
        open={gateOpen}
        okText="确定"
        cancelText="取消"
        destroyOnClose
        onCancel={closeGate}
        onOk={submitGate}
      >
        <Input.Password
          autoFocus
          value={gateCode}
          placeholder="口令"
          status={gateError ? 'error' : undefined}
          onChange={(event) => {
            setGateCode(event.target.value)
            if (gateError) setGateError('')
          }}
          onPressEnter={submitGate}
        />
        {gateError && (
          <div style={{ color: '#EF4444', fontSize: 12, marginTop: 8 }}>{gateError}</div>
        )}
      </Modal>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </Layout>
  )
}
