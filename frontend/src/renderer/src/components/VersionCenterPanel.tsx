import { useEffect, useRef, useState } from 'react'
import { Alert, App as AntApp, Button, Card, Empty, Modal, Space, Spin, Tag } from 'antd'
import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ReleaseInfo } from '../../../preload/index.d'
import type { VersionCatalog, VersionRecord } from '../../../main/versionCatalog.mjs'

function readableError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  // Electron 会把主进程错误包成 "Error invoking remote method 'x': Error: 真实原因"，界面只展示真实原因。
  const cleaned = raw.replace(/^Error invoking remote method '[^']+':\s*(?:Error:\s*)?/, '').trim()
  return cleaned || '版本记录读取失败，请刷新重试'
}

export default function VersionCenterPanel(): JSX.Element {
  const { message } = AntApp.useApp()
  const [release, setRelease] = useState<ReleaseInfo | null>(null)
  const [catalog, setCatalog] = useState<VersionCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [selected, setSelected] = useState<VersionRecord | null>(null)
  const [opening, setOpening] = useState(false)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    setCatalog(null)
    Promise.all([
      window.api.getReleaseInfo().then(info => { if (active) setRelease(info) }),
      window.api.getVersionCatalog()
    ]).then(([, data]) => {
      if (active) setCatalog(data)
    }).catch((err: unknown) => {
      if (active) setError(readableError(err))
    }).finally(() => {
      if (active) setLoading(false)
    })
    return () => { active = false }
  }, [refresh])

  async function download(): Promise<void> {
    if (!selected || opening) return
    setOpening(true)
    try {
      const result = await window.api.downloadHistoricalVersion(selected.version)
      if (!result.ok) throw new Error('浏览器未能打开下载，请重试')
      if (mounted.current) {
        setSelected(null)
        message.success('已在浏览器中打开下载，请下载完成后手动安装')
      }
    } catch (err: unknown) {
      if (mounted.current) message.error(readableError(err))
    } finally {
      if (mounted.current) setOpening(false)
    }
  }

  return (
    <section aria-label="版本中心" style={{ minWidth: 0 }}>
      <h3 style={{ fontSize: 18, fontWeight: 600, margin: '0 0 20px' }}>版本中心</h3>
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <span>当前安装</span>
          {release && <Tag color={release.channel === 'beta' ? 'orange' : 'blue'}>{release.channelLabel}</Tag>}
          <strong style={{ fontFamily: 'monospace', fontSize: 18 }}>{release ? `v${release.version}` : '读取中'}</strong>
        </Space>
      </Card>
      <Alert
        type="warning"
        showIcon
        message="重新安装旧版前，请先备份数据"
        description="旧版不一定兼容新版数据。这里只下载当前渠道的安装包，请下载完成后手动安装，不会自动安装或回退。"
        style={{ marginBottom: 24 }}
      />
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <h4 style={{ margin: 0, fontSize: 15 }}>版本记录</h4>
        <Button icon={<ReloadOutlined aria-hidden="true" />} loading={loading} disabled={opening} onClick={() => setRefresh(value => value + 1)}>
          刷新版本记录
        </Button>
      </div>
      <p style={{ margin: '0 0 16px', color: 'rgba(0, 0, 0, 0.55)' }}>
        版本记录需要联网获取；离线时不影响采集、任务与浏览器接管，联网后点「刷新版本记录」即可。
      </p>
      {loading && <Space role="status"><Spin size="small" /><span>正在读取版本记录</span></Space>}
      {!loading && error && <Alert type="error" showIcon message="版本记录没有加载成功" description={error} />}
      {!loading && !error && catalog?.releases.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前渠道暂无版本记录" />}
      {!loading && !error && catalog?.releases.map(item => (
        <Card key={item.version} size="small" style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
            <Space wrap>
              <strong style={{ fontFamily: 'monospace', fontSize: 16 }}>v{item.version}</strong>
              {item.version === release?.version && <Tag color="blue">当前版本</Tag>}
              <span style={{ color: 'var(--text-secondary)' }}>{item.publishedAt}</span>
            </Space>
            <Button icon={<DownloadOutlined aria-hidden="true" />} disabled={opening || !release} onClick={() => setSelected(item)}>下载 v{item.version}</Button>
          </div>
          {item.notes.length > 0 ? (
            <ul style={{ margin: '16px 0 0', paddingLeft: 20, lineHeight: 1.8, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
              {item.notes.map((note, index) => <li key={index}>{note}</li>)}
            </ul>
          ) : <p style={{ marginBottom: 0, color: 'var(--text-secondary)' }}>此版本未提供更新说明</p>}
        </Card>
      ))}
      <Modal
        open={selected !== null}
        title={`下载 v${selected?.version || ''} 安装包？`}
        okText="继续下载"
        cancelText="取消下载"
        confirmLoading={opening}
        cancelButtonProps={{ disabled: opening }}
        closable={!opening}
        maskClosable={!opening}
        keyboard={!opening}
        onCancel={() => { if (!opening) setSelected(null) }}
        onOk={() => { void download() }}
      >
        <p>将用默认浏览器打开{release?.channelLabel}安装包下载，不会自动运行安装包。</p>
        <p>旧版可能不兼容现有数据，请先在“关于”中导出备份，再手动重新安装。</p>
      </Modal>
    </section>
  )
}
