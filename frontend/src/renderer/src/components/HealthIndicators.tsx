import { useCallback, useEffect, useState } from 'react'
import { Tag, Space, Tooltip, App } from 'antd'
import {
  UsbOutlined,
  WifiOutlined,
  GlobalOutlined,
  ApiOutlined,
  ReloadOutlined
} from '@ant-design/icons'
import { api, type Health } from '../services/api'

type Status = 'ok' | 'warn' | 'err' | 'unknown'

const TAG_STYLE: Record<Status, React.CSSProperties> = {
  ok: { color: '#fff', background: '#10b981', borderColor: '#10b981' },
  warn: { color: '#fff', background: '#f59e0b', borderColor: '#f59e0b' },
  err: { color: '#fff', background: '#ef4444', borderColor: '#ef4444' },
  unknown: { color: '#8b98a8', background: '#2d333b', borderColor: '#444c56' },
}

function pickStatus(health: Health | null, keywords: string[]): Status {
  if (!health) return 'unknown'
  const c = health.checks?.find((x) =>
    keywords.some((k) => x.module?.toLowerCase().includes(k))
  )
  if (!c) return health.overall === 'healthy' ? 'ok' : 'err'
  const s = (c.status || '').toLowerCase()
  if (s.includes('ok') || s.includes('healthy') || s.includes('up')) return 'ok'
  if (s === 'warn' || s.includes('warning') || s.includes('degraded')) return 'warn'
  return 'err'
}

function tipText(health: Health | null, keywords: string[], label: string): string {
  if (!health) return `${label} - 等待检测…`
  const c = health.checks?.find((x) =>
    keywords.some((k) => x.module?.toLowerCase().includes(k))
  )
  return c?.message || (health.overall === 'healthy' ? `${label} 正常` : `${label} 异常`)
}

const TAG_BASE: React.CSSProperties = {
  borderRadius: 6, padding: '1px 10px', margin: 0,
  fontSize: 12, lineHeight: '22px', fontWeight: 500,
  cursor: 'pointer', userSelect: 'none',
}

export default function HealthIndicators(): JSX.Element {
  const { message } = App.useApp()
  const [health, setHealth] = useState<Health | null>(null)
  const [online, setOnline] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [applyingRoute, setApplyingRoute] = useState(false)

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const h = await api.health()
      setHealth(h)
      setOnline(true)
    } catch {
      setHealth(null)
      setOnline(false)
    } finally {
      setRefreshing(false)
    }
  }, [])

  // 专网 Tag: 解析 DNS + 应用路由
  const refreshRoute = useCallback(async () => {
    setApplyingRoute(true)
    try {
      const r = await api.applyRouteRules()
      if (r.status === 'error') {
        message.warning(r.message || '专网闭环未通过')
      } else if (r.status === 'degraded') {
        message.warning(r.message || '专网可用，公网不可达；采集可继续落本地')
      } else if (r.applied && r.applied > 0) {
        message.success(`双网卡验证通过 (${r.applied} 条路由)`)
      } else {
        message.info('路由规则已应用，无新增')
      }
      // 同时刷新健康状态
      await refresh()
    } catch {
      message.error('路由应用失败，请确认以管理员身份运行')
    } finally {
      setApplyingRoute(false)
    }
  }, [refresh, message])

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const h = await api.health()
        if (!alive) return
        setHealth(h)
        setOnline(true)
      } catch {
        if (!alive) return
        setHealth(null)
        setOnline(false)
      }
    }
    void tick()
    const t = setInterval(tick, 15000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  const ukey = online ? pickStatus(health, ['ukey', 'key', 'usb']) : 'unknown'
  const net  = online ? pickStatus(health, ['network', 'net']) : 'unknown'
  const wan  = online ? pickStatus(health, ['wan', 'internet']) : 'unknown'
  const svc  = online ? 'ok' as const : 'err' as const

  return (
    <Space size={6} className="health-indicators">
      <Tooltip title={tipText(health, ['ukey', 'key', 'usb'], 'U-Key') + ' - 点击刷新'}>
        <Tag icon={<UsbOutlined />} onClick={refresh} className="health-tag"
          style={{ ...TAG_BASE, ...TAG_STYLE[ukey] }}
        >U-Key</Tag>
      </Tooltip>
      <Tooltip title={tipText(health, ['network', 'net'], '专网') + ' - 点击解析DNS并应用路由'}>
        <Tag
          icon={applyingRoute ? <ReloadOutlined spin /> : <WifiOutlined />}
          onClick={refreshRoute}
          className="health-tag"
          style={{ ...TAG_BASE, ...TAG_STYLE[net] }}
        >专网</Tag>
      </Tooltip>
      <Tooltip title={tipText(health, ['wan', 'internet'], '外网') + ' - 点击刷新'}>
        <Tag icon={<GlobalOutlined />} onClick={refresh} className="health-tag"
          style={{ ...TAG_BASE, ...TAG_STYLE[wan] }}
        >外网</Tag>
      </Tooltip>
      <Tooltip title={(online ? '采集服务运行正常' : '采集服务离线') + ' - 点击刷新'}>
        <Tag icon={<ApiOutlined />} onClick={refresh} className="health-tag"
          style={{ ...TAG_BASE, ...TAG_STYLE[svc] }}
        >采集服务</Tag>
      </Tooltip>
      <Tooltip title="刷新全部状态">
        <ReloadOutlined
          spin={refreshing}
          onClick={refresh}
          style={{ cursor: 'pointer', color: '#8b98a8', fontSize: 14 }}
        />
      </Tooltip>
    </Space>
  )
}
