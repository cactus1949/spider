import { useEffect, useState } from 'react'
import { Button, Modal, Tag } from 'antd'
import { FileTextOutlined, FolderOpenOutlined, FolderOutlined } from '@ant-design/icons'
import {
  buildResultTree,
  countTreeFiles,
  dismissExecutionResult,
  subscribeExecutionResults,
  type DownloadStatus,
  type ExecutionResult,
  type ResultTreeNode
} from '../services/executionResult'

const TITLE_COLOR: Record<ExecutionResult['status'], string> = {
  success: '#10B981',
  warning: '#F59E0B',
  error: '#EF4444'
}

const FILE_STATUS: Record<DownloadStatus, { color: string; label: string }> = {
  expected: { color: 'default', label: '未下载' },
  saved: { color: 'success', label: '已下载' },
  empty: { color: 'warning', label: '没数据' },
  failed: { color: 'error', label: '下载失败' },
  missed: { color: 'default', label: '未下载' }
}

function ResultTree({ nodes }: { nodes: ResultTreeNode[] }): JSX.Element {
  return (
    <div className="er-children">
      {nodes.map((node) => node.kind === 'folder' ? (
        <div key={node.key} className="er-node">
          <div className="er-row">
            <FolderOutlined className="er-folder-icon" />
            <span className="er-folder-name">{node.name}</span>
            <span className="er-count">{countTreeFiles(node.children)}</span>
          </div>
          <ResultTree nodes={node.children} />
        </div>
      ) : (
        <div key={node.key} className={`er-file is-${node.file?.status || 'missed'}`}>
          <FileTextOutlined className="er-file-icon" />
          <span className="er-file-name" title={node.name}>{node.name}</span>
          {node.file?.source && <span className="er-file-source">{node.file.source}</span>}
          {node.file?.reason && <span className="er-file-reason" title={node.file.reason}>{node.file.reason}</span>}
          {node.file && (
            <Tag color={FILE_STATUS[node.file.status].color} bordered={false}>
              {FILE_STATUS[node.file.status].label}
            </Tag>
          )}
        </div>
      ))}
    </div>
  )
}

export default function ExecutionResultModal(): JSX.Element {
  const [queue, setQueue] = useState<ExecutionResult[]>([])

  useEffect(() => subscribeExecutionResults(setQueue), [])

  const current = queue[0]
  const pending = Math.max(0, queue.length - 1)
  const files = current?.files || []
  const tree = buildResultTree(files)
  const saved = files.filter((item) => item.status === 'saved').length
  const empty = files.filter((item) => item.status === 'empty').length
  const failed = files.filter((item) => item.status === 'failed').length
  const missed = files.filter((item) => item.status === 'missed' || item.status === 'expected').length

  return (
    <Modal
      open={Boolean(current)}
      title={<span style={{ color: current ? TITLE_COLOR[current.status] : undefined }}>{current?.title || '执行结果'}</span>}
      centered
      width={760}
      zIndex={2000}
      closable={false}
      maskClosable={false}
      keyboard={false}
      destroyOnClose
      onCancel={dismissExecutionResult}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button type="primary" onClick={dismissExecutionResult}>确认</Button>
        </div>
      }
    >
      {current && (
        <div>
          <div style={{ fontSize: 16, fontWeight: 600, color: '#1A1D21', lineHeight: 1.5 }}>{current.name}</div>
          {current.message && (
            <div style={{ marginTop: 8, color: '#5A6068', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{current.message}</div>
          )}
          {files.length > 0 ? (
            <>
              <div className="er-summary">
                <span>预期 {files.length}</span>
                <span>已下载 {saved}</span>
                <span>没数据 {empty}</span>
                <span>下载失败 {failed}</span>
                <span>未下载 {missed}</span>
              </div>
              <div className="er-tree">
                <div className="er-root">
                  <FolderOpenOutlined />
                  <span>下载结果</span>
                </div>
                <ResultTree nodes={tree} />
              </div>
            </>
          ) : (
            <div className="er-none">这次没有计划下载的文件</div>
          )}
          {pending > 0 && (
            <div style={{ marginTop: 10, color: '#8A8F95', fontSize: 12 }}>还有 {pending} 条结果，确认后继续查看</div>
          )}
        </div>
      )}
    </Modal>
  )
}
