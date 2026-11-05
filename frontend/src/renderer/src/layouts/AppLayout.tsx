import BrowserSessionPanel from '../components/BrowserSessionPanel'
import { useEffect, useState } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Button, Tooltip, Tag, Dropdown } from 'antd'
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
}

const NAV_ITEMS: NavItem[] = [
  { key: '/', icon: <ScheduleOutlined />, label: '任务配置' },
  { key: '/files', icon: <FolderOutlined />, label: '文件中心' },
  { key: '/execution-logs', icon: <FileTextOutlined />, label: '执行日志' },
  { key: '/indicators', icon: <CodeOutlined />, label: '指标配置' },
  { key: '/record-workshop', icon: <VideoCameraOutlined />, label: '录制工坊' },
]

export default function AppLayout(): JSX.Element {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [version, setVersion] = useState('')
  const [currentWifi, setCurrentWifi] = useState('')
  const [wifiError, setWifiError] = useState('')
  const [wifiChecked, setWifiChecked] = useState(false)
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false)
  const { message } = App.useApp()

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
          flex: 0 0 auto;
        }
        .app-header-tools {
          flex: 1 1 auto;
          justify-content: flex-end;
          gap: 8px;
          overflow: hidden;
        }
        .app-header-wifi {
          min-width: 0;
          max-width: 220px;
          display: flex;
          flex: 0 1 auto;
          overflow: hidden;
        }
        .app-header-wifi > * {
          min-width: 0;
          max-width: 100%;
          overflow: hidden;
        }
        .app-header-wifi-tag {
          display: inline-flex !important;
          align-items: center;
          max-width: 100%;
          min-width: 0;
          overflow: hidden;
        }
        .app-header-wifi-tag .anticon {
          flex-shrink: 0;
        }
        .app-header-wifi-name {
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .health-indicators {
          flex: 0 0 auto;
        }
        /* Dropdown.Button 自带 width:100%，会把整行撑满并把前面的标签挤扁。 */
        .app-header-tools > .app-header-diagnostics {
          flex: 0 0 auto;
          width: auto;
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
          .app-header-tools > .app-header-diagnostics {
            width: auto;
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
          .app-header-tools > .app-header-diagnostics {
            width: auto;
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
            items={NAV_ITEMS.map(({ key, icon, label }) => ({ key, icon, label }))}
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
                  className="app-header-wifi-tag"
                  style={{
                    borderRadius: 6,
                    padding: '1px 10px',
                    margin: 0,
                    fontSize: 12,
                    lineHeight: '22px',
                    fontWeight: 500,
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
              className="app-header-diagnostics"
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
          <Outlet />
        </Content>
      </Layout>

      <div className="app-version">
        v{version || '...'}
      </div>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </Layout>
  )
}
