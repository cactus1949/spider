import { useCallback, useEffect, useState } from 'react'
import {
  Table, Pagination, Tag, Button, Drawer, Space, Select, Typography, DatePicker,
  App as AntApp, Tooltip, Popconfirm
} from 'antd'
import {
  EyeOutlined,
  DownloadOutlined,
  DeleteOutlined,
  ReloadOutlined
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type { Dayjs } from 'dayjs'
import { api, settingsStore, type ExecutionLogItem, type ExecutionLogDetail } from '../services/api'

const { Text } = Typography

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  workflow: { label: '指标播放', color: 'blue' },
  scheduled_auto: { label: '定时自动', color: 'purple' },
  scheduled_manual: { label: '定时手动', color: 'cyan' },
  catchup: { label: '补数据', color: 'gold' }
}

const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
  running: { label: '执行中', color: 'processing' },
  paused: { label: '已暂停', color: 'warning' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' }
}

const PAGE_SIZE_OPTIONS = ['10', '20', '50', '100']

export default function ExecutionLogs(): JSX.Element {
  const { message } = AntApp.useApp()
  const [data, setData] = useState<ExecutionLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [filterType, setFilterType] = useState<string>('')
  const [filterStatus, setFilterStatus] = useState<string>('')
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null] | null>(null)

  // 详情抽屉
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [detail, setDetail] = useState<ExecutionLogDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const [downloading, setDownloading] = useState<string | null>(null)

  // 多选批量删除（只针对当前页）
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [batchDeleting, setBatchDeleting] = useState(false)

  const loadList = useCallback(async (
    p = page, ft = filterType, fs = filterStatus,
    range = dateRange, size = pageSize
  ) => {
    setLoading(true)
    try {
      const startTime = range?.[0]?.startOf('day').format('YYYY-MM-DD HH:mm:ss')
      const endTime = range?.[1]?.endOf('day').format('YYYY-MM-DD HH:mm:ss')
      const res = await api.listExecutionLogs(p, size, ft || undefined, fs || undefined, startTime, endTime)
      setData(res.items)
      setTotal(res.total)
      setSelectedIds([])
    } catch {
      message.error('加载执行日志失败')
    } finally {
      setLoading(false)
    }
  }, [page, pageSize, filterType, filterStatus, dateRange, message])

  useEffect(() => {
    loadList()
  }, [])

  const handleRefresh = (): void => {
    setPage(1)
    loadList(1, filterType, filterStatus, dateRange)
  }

  const handlePageChange = (p: number): void => {
    setPage(p)
    loadList(p, filterType, filterStatus, dateRange)
  }

  const handleFilter = (): void => {
    setPage(1)
    loadList(1, filterType, filterStatus, dateRange)
  }

  // 查看详情
  const handleView = useCallback(async (execId: string) => {
    setDrawerOpen(true)
    setDetailLoading(true)
    try {
      const d = await api.getExecutionLog(execId)
      setDetail(d)
    } catch {
      message.error('加载详情失败')
    } finally {
      setDetailLoading(false)
    }
  }, [message])

  // 下载
  const handleDownload = useCallback(async (execId: string) => {
    const dlDir = settingsStore.get('downloadDir', '')
    setDownloading(execId)
    try {
      const res = await api.downloadExecutionLog(execId, dlDir)
      message.success(`诊断包已保存: ${res.filename}`)
      // 打开下载目录
      if (res.path) {
        try {
          // 提取目录部分（兼容 Windows \ 和 macOS/Linux /）
          const dirPath = res.path.replace(/[\\/][^\\/]*$/, '')
          if (dirPath) await window.api.openDir(dirPath)
        } catch { /* ignore */ }
      }
    } catch (e: unknown) {
      message.error('下载失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setDownloading(null)
    }
  }, [message])

  // 删除
  const handleBatchDelete = useCallback(async () => {
    if (selectedIds.length === 0) return
    setBatchDeleting(true)
    try {
      const res = await api.deleteExecutionLogs(selectedIds)
      message.success(`已删除 ${res.deleted} 条`)
      setSelectedIds([])
      loadList()
    } catch {
      message.error('批量删除失败')
    } finally {
      setBatchDeleting(false)
    }
  }, [selectedIds, message, loadList])

  const handleDelete = useCallback(async (execId: string) => {
    try {
      await api.deleteExecutionLog(execId)
      message.success('已删除')
      loadList()
    } catch {
      message.error('删除失败')
    }
  }, [message, loadList])

  const columns: ColumnsType<ExecutionLogItem> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 200,
      ellipsis: true
    },
    {
      title: '类型',
      dataIndex: 'type',
      key: 'type',
      width: 110,
      render: (t: string) => {
        const cfg = TYPE_LABELS[t] || { label: t, color: 'default' }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      }
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (s: string) => {
        const cfg = STATUS_CONFIG[s] || { label: s, color: 'default' }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      }
    },
    {
      title: '原因',
      dataIndex: 'reason',
      key: 'reason',
      width: 200,
      ellipsis: true,
      render: (r: string) => r ? (
        <Tooltip title={r}>
          <Text type="danger" ellipsis style={{ maxWidth: 180 }}>{r}</Text>
        </Tooltip>
      ) : <Text type="secondary">-</Text>
    },
    {
      title: '开始时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 165,
      sorter: (a, b) => a.created_at.localeCompare(b.created_at),
      defaultSortOrder: 'descend'
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Tooltip title="查看详情">
            <Button type="link" size="small" icon={<EyeOutlined />}
              onClick={() => handleView(record.exec_id)} />
          </Tooltip>
          <Tooltip title="下载诊断包">
            <Button type="link" size="small" icon={<DownloadOutlined />}
              loading={downloading === record.exec_id}
              onClick={() => handleDownload(record.exec_id)} />
          </Tooltip>
          <Popconfirm title="确定删除？" onConfirm={() => handleDelete(record.exec_id)}
            okText="删除" cancelText="取消">
            <Tooltip title="删除">
              <Button type="link" size="small" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      )
    }
  ]

  return (
    <div style={{ maxWidth: 1280, width: '100%', margin: '0 auto', flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* 顶部筛选栏 */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        marginBottom: 16, gap: 12, flexShrink: 0
      }}>
        <Space>
          <DatePicker.RangePicker
            value={dateRange}
            onChange={(value) => setDateRange(value as [Dayjs | null, Dayjs | null] | null)}
            placeholder={['开始时间', '结束时间']}
            allowEmpty={[false, false]}
          />
          <Select
            placeholder="类型筛选"
            allowClear
            style={{ width: 130 }}
            value={filterType || undefined}
            onChange={(v) => { setFilterType(v || ''); }}
            options={[
              { label: '指标播放', value: 'workflow' },
              { label: '定时自动', value: 'scheduled_auto' },
              { label: '定时手动', value: 'scheduled_manual' },
              { label: '补数据', value: 'catchup' }
            ]}
          />
          <Select
            placeholder="状态筛选"
            allowClear
            style={{ width: 110 }}
            value={filterStatus || undefined}
            onChange={(v) => { setFilterStatus(v || ''); }}
            options={[
              { label: '执行中', value: 'running' },
              { label: '已暂停', value: 'paused' },
              { label: '成功', value: 'success' },
              { label: '失败', value: 'failed' }
            ]}
          />
          <Button onClick={handleFilter}>筛选</Button>
        </Space>
        <Space>
          {selectedIds.length > 0 && (
            <Button type="link" size="small" onClick={() => setSelectedIds([])}>取消选择</Button>
          )}
          <Popconfirm
            title={`删除选中的 ${selectedIds.length} 条日志？`}
            description="删除后不能恢复。"
            disabled={selectedIds.length === 0}
            onConfirm={handleBatchDelete}
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
          >
            <Button danger icon={<DeleteOutlined />} disabled={selectedIds.length === 0} loading={batchDeleting}>
              批量删除{selectedIds.length > 0 ? `（${selectedIds.length}）` : ''}
            </Button>
          </Popconfirm>
          <Tooltip title="刷新">
            <Button icon={<ReloadOutlined />} onClick={handleRefresh} loading={loading} />
          </Tooltip>
        </Space>
      </div>

      {/* 列表 */}
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <Table
          className="execution-logs-table"
          style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}
          scroll={{ y: 'flex', x: 1100 }}
          columns={columns}
          dataSource={data}
          rowKey="exec_id"
          rowSelection={{
            selectedRowKeys: selectedIds,
            onChange: (keys) => setSelectedIds(keys as string[]),
            getCheckboxProps: (record) => ({ disabled: record.status === 'running' }),
            columnWidth: 44
          }}
          loading={loading}
          size="middle"
          pagination={false}
        />
        <Pagination
          current={page}
          pageSize={pageSize}
          total={total}
          onChange={(nextPage, nextPageSize) => {
            if (nextPageSize !== pageSize) {
              setPageSize(nextPageSize)
              setPage(1)
              loadList(1, filterType, filterStatus, dateRange, nextPageSize)
              return
            }
            handlePageChange(nextPage)
          }}
          showSizeChanger
          pageSizeOptions={PAGE_SIZE_OPTIONS}
          showTotal={(t) => `共 ${t} 条`}
          style={{ flexShrink: 0, alignSelf: 'flex-end', margin: '16px 0 0' }}
        />
      </div>

      {/* 详情抽屉 */}
      <Drawer
        title={detail ? `执行日志 - ${detail.name}` : '执行日志详情'}
        placement="right"
        width={640}
        open={drawerOpen}
        onClose={() => { setDrawerOpen(false); setDetail(null) }}
        loading={detailLoading}
        extra={
          detail ? (
            <Button icon={<DownloadOutlined />}
              loading={downloading === detail.exec_id}
              onClick={() => handleDownload(detail.exec_id)}>
              下载诊断包
            </Button>
          ) : null
        }
      >
        {detail && (
          <div>
            <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
              <div>
                <Text type="secondary">名称：</Text>
                <Text strong>{detail.name}</Text>
              </div>
              <div>
                <Text type="secondary">类型：</Text>
                <Tag color={TYPE_LABELS[detail.type]?.color || 'default'}>
                  {TYPE_LABELS[detail.type]?.label || detail.type}
                </Tag>
              </div>
              <div>
                <Text type="secondary">状态：</Text>
                <Tag color={STATUS_CONFIG[detail.status]?.color || 'default'}>
                  {STATUS_CONFIG[detail.status]?.label || detail.status}
                </Tag>
              </div>
              {detail.reason && (
                <div>
                  <Text type="secondary">原因：</Text>
                  <Text type="danger">{detail.reason}</Text>
                </div>
              )}
              <div>
                <Text type="secondary">开始时间：</Text>
                <Text>{detail.created_at}</Text>
              </div>
              <div>
                <Text type="secondary">更新时间：</Text>
                <Text>{detail.updated_at}</Text>
              </div>
              <div>
                <Text type="secondary">目标ID：</Text>
                <Text code>{detail.target_id}</Text>
              </div>
            </Space>
            <div style={{ borderTop: '1px solid #f0f0f0', paddingTop: 12 }}>
              <Text type="secondary" strong style={{ marginBottom: 8, display: 'block' }}>详细日志：</Text>
              <pre style={{
                background: '#1a1d21', color: '#e6edf3',
                padding: 16, borderRadius: 8, fontSize: 13,
                lineHeight: 1.7, maxHeight: 'calc(100vh - 360px)',
                overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                fontFamily: 'SF Mono, Monaco, Consolas, monospace'
              }}>
                {detail.detail_log || '(无日志内容)'}
              </pre>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  )
}
