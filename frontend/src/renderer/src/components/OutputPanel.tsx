import type { ReactNode } from 'react'
import { Tag, Tooltip } from 'antd'
import { RightOutlined } from '@ant-design/icons'
import type { WorkflowOutput } from '../services/api'

interface OutputPanelProps {
  title: string
  /** 已单独配的个数 / 可配的总数 */
  count: number
  total: number
  open: boolean
  onToggle: () => void
  /** 标题行右侧，收起时也显示（失效、全部不补之类的提醒） */
  extra?: ReactNode
  /** 展开后列表下方的说明 */
  note?: ReactNode
  children?: ReactNode
}

/** 指标卡片底部「按导出另配」面板，任务、补数据、指标日期弹窗共用 */
export function OutputPanel({ title, count, total, open, onToggle, extra, note, children }: OutputPanelProps): JSX.Element {
  return (
    <div className={`task-output-panel${open ? ' is-open' : ''}`}>
      <div className="task-output-head">
        <button type="button" className="task-output-toggle" onClick={onToggle}>
          <RightOutlined className="task-output-arrow" />
          <span>{title}</span>
          <span className={`task-output-count${count > 0 ? ' is-active' : ''}`}>
            {count > 0 ? `已另配 ${count}/${total}` : `${total} 个导出 · 全部跟随`}
          </span>
        </button>
        {extra && <div className="task-output-extra">{extra}</div>}
      </div>
      {open && (
        <>
          <div className="task-output-items">{children}</div>
          {note && <div className="task-output-note">{note}</div>}
        </>
      )}
    </div>
  )
}

interface OutputNameProps {
  output: WorkflowOutput
  /** 重名时提示去哪补 name */
  duplicateHint: string
}

export function OutputName({ output, duplicateHint }: OutputNameProps): JSX.Element {
  const position = output.position || `第 ${output.step_index + 1} 步`
  return (
    <div className="task-output-name">
      <span className="task-output-label" title={output.label}>{output.label}</span>
      <span className="task-output-position">{position}</span>
      {output.duplicate && (
        <Tooltip title={duplicateHint}>
          <Tag color="gold" bordered={false}>重名 · 建议补 name</Tag>
        </Tooltip>
      )}
    </div>
  )
}
