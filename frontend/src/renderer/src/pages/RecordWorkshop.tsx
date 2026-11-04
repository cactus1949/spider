import { useCallback, useEffect, useState } from 'react'
import {
  Card, Button, Input, Space, Table, Modal, Tag,
  App as AntApp, Tooltip, Typography, Form, Popconfirm
} from 'antd'
import {
  ToolOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  QuestionCircleOutlined,
  CloudUploadOutlined,
  DeleteOutlined
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { api, settingsStore, type WorkshopExport } from '../services/api'

const { TextArea } = Input
const { Text, Title } = Typography

const STATUS_MAP: Record<WorkshopExport['status'], { text: string; color: string }> = {
  pending: { text: '未提交', color: 'default' },
  submitted: { text: '已提交', color: 'success' },
  changed: { text: '已变更', color: 'warning' },
  failed: { text: '提交失败', color: 'error' }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function resolveBuildStartUrl(): string | undefined {
  const raw = settingsStore.get('siteUrl', '').trim()
  if (!raw) return undefined
  if (raw.startsWith('http://') || raw.startsWith('https://')) return raw
  return `https://${raw}`
}

export default function RecordWorkshop(): JSX.Element {
  const { message } = AntApp.useApp()
  const [form] = Form.useForm<{ name: string; description: string }>()

  const [building, setBuilding] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [records, setRecords] = useState<WorkshopExport[]>([])
  const [loading, setLoading] = useState(false)
  const [submittingId, setSubmittingId] = useState('')
  const [deletingId, setDeletingId] = useState('')
  const [exportDir, setExportDir] = useState('')

  const loadRecords = useCallback(async () => {
    setLoading(true)
    try {
      const dlDir = settingsStore.get('downloadDir', '')
      setExportDir(dlDir ? `${dlDir}/录制工坊` : '')
      setRecords(await api.listWorkshopExports(dlDir || undefined))
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    settingsStore.load().then(loadRecords)
  }, [loadRecords])

  const handleBuild = async () => {
    const values = await form.validateFields()
    const downloadDir = settingsStore.get('downloadDir', '')
    if (!downloadDir) {
      message.warning('请先在设置中配置下载路径')
      return
    }

    setBuilding(true)
    try {
      const res = await api.buildWorkshop({
        name: values.name.trim(),
        url: resolveBuildStartUrl(),
        description: values.description?.trim() || undefined,
        download_dir: downloadDir
      })
      if (res.exported?.length) {
        message.success(`已保存 ${res.exported.length} 个 ZIP`)
        form.resetFields()
      } else {
        message.info('构建会话已结束（未保存 ZIP）')
      }
      loadRecords()
    } catch (e: unknown) {
      message.error('构建失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setBuilding(false)
    }
  }

  const handleSubmit = async (record: WorkshopExport) => {
    const downloadDir = settingsStore.get('downloadDir', '')
    if (!downloadDir) {
      message.warning('请先在设置中配置下载路径')
      return
    }
    setSubmittingId(record.export_id)
    try {
      const res = await api.submitWorkshopExport({
        path: record.path,
        download_dir: downloadDir
      })
      if (res.already) {
        message.info(res.message || '已提交')
      } else {
        message.success(res.message || (record.status === 'submitted' ? '重新提交成功' : '提交成功'))
      }
      loadRecords()
    } catch (e: unknown) {
      message.error('提交失败: ' + (e instanceof Error ? e.message : String(e)))
      loadRecords()
    } finally {
      setSubmittingId('')
    }
  }

  const handleDelete = async (record: WorkshopExport) => {
    const downloadDir = settingsStore.get('downloadDir', '')
    if (!downloadDir) {
      message.warning('请先在设置中配置下载路径')
      return
    }
    setDeletingId(record.export_id)
    try {
      await api.deleteWorkshopExport({ path: record.path, download_dir: downloadDir })
      message.success('已删除，可重新构建并提交')
      loadRecords()
    } catch (e: unknown) {
      message.error('删除失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setDeletingId('')
    }
  }

  const columns: ColumnsType<WorkshopExport> = [
    { title: '名称', dataIndex: 'filename', key: 'filename', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (s: WorkshopExport['status']) => {
        const info = STATUS_MAP[s] || STATUS_MAP.pending
        return <Tag color={info.color}>{info.text}</Tag>
      }
    },
    {
      title: '大小', dataIndex: 'size', key: 'size', width: 90,
      render: (v: number) => formatSize(v)
    },
    {
      title: '导出时间', dataIndex: 'modified_at', key: 'modified_at', width: 170,
      sorter: (a, b) => a.modified_at.localeCompare(b.modified_at),
      defaultSortOrder: 'descend'
    },
    {
      title: '操作', key: 'actions', width: 150,
      render: (_, r) => (
        <Space size={4}>
          <Tooltip title={r.status === 'submitted' ? '重新提交' : '提交'}>
            <Button
              type="link"
              size="small"
              icon={<CloudUploadOutlined />}
              loading={submittingId === r.export_id}
              onClick={() => handleSubmit(r)}
            />
          </Tooltip>
          <Tooltip title="在系统中打开">
            <Button type="link" size="small" icon={<FolderOpenOutlined />}
              onClick={() => window.api.revealFile(r.path)} />
          </Tooltip>
          <Popconfirm
            title="确定删除此导出？"
            description="删除后可重新构建并提交"
            onConfirm={() => handleDelete(r)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Tooltip title="删除">
              <Button
                type="link"
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={deletingId === r.export_id}
              />
            </Tooltip>
          </Popconfirm>
        </Space>
      )
    }
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 16, flexShrink: 0 }}>
        <Title level={5} style={{ margin: 0 }}>📹 录制工坊</Title>
        <Tooltip title="使用说明">
          <Button
            type="text"
            size="small"
            icon={<QuestionCircleOutlined style={{ color: '#8c8c8c' }} />}
            onClick={() => setHelpOpen(true)}
          />
        </Tooltip>
      </div>

      <Modal
        title="交互式工作流构建"
        open={helpOpen}
        onCancel={() => setHelpOpen(false)}
        footer={
          <Button type="primary" onClick={() => setHelpOpen(false)}>
            知道了
          </Button>
        }
        width={480}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text>点击「开始构建」后浏览器会打开设置中的网站地址，右侧出现步骤面板。</Text>
          <Text>鼠标悬停元素会高亮，用快捷键录步骤：</Text>
          <Text type="secondary" style={{ display: 'block', paddingLeft: 8 }}>
            C 点击 · H 悬停 · D 日期 · S 下拉 · F 填入 · E 导出 · X 提取 · W 等待
          </Text>
          <Text>录完后在浏览器面板点「完成」，ZIP 会保存到下载路径下的「录制工坊」文件夹，并自动关闭浏览器。</Text>
          <Text>历史记录中可点击「提交」标记为已提交；远程上传地址可在设置中配置（暂未对接）。</Text>
        </Space>
      </Modal>

      <div style={{ display: 'flex', flex: 1, minHeight: 0, gap: 16 }}>
        <Card
          title="新建构建"
          style={{ width: 400, flexShrink: 0, display: 'flex', flexDirection: 'column' }}
          styles={{ body: { flex: 1, overflow: 'auto' } }}
        >
          <Form form={form} layout="vertical" disabled={building}>
            <Form.Item
              name="name"
              label="工作流名称"
              rules={[{ required: true, message: '请填写名称（将作为 ZIP 文件名）' }]}
            >
              <Input placeholder="例如：日清算概览导出" suffix=".zip" />
            </Form.Item>
            <Form.Item name="description" label="描述（可选）">
              <TextArea rows={3} placeholder="例如：需要先点查询再导出" />
            </Form.Item>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Button
                type="primary"
                size="large"
                block
                icon={<ToolOutlined />}
                loading={building}
                onClick={handleBuild}
              >
                {building ? '构建中，请在浏览器中操作...' : '开始构建'}
              </Button>
              {exportDir && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  保存目录：{exportDir}
                </Text>
              )}
            </Space>
          </Form>
        </Card>

        <Card
          title="历史导出"
          style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}
          styles={{ body: { flex: 1, minHeight: 0, overflow: 'auto', padding: 0 } }}
          extra={
            <Button size="small" icon={<ReloadOutlined />} onClick={loadRecords} loading={loading}>
              刷新
            </Button>
          }
        >
          <Table
            columns={columns}
            dataSource={records}
            rowKey="export_id"
            loading={loading}
            size="small"
            pagination={false}
            locale={{ emptyText: '暂无导出记录' }}
          />
        </Card>
      </div>
    </div>
  )
}
