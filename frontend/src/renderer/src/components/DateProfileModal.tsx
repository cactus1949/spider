import { useEffect, useState } from 'react'
import { Alert, Modal, Segmented, Spin } from 'antd'
import { type WorkflowOutput, api } from '../services/api'
import { OutputName, OutputPanel } from './OutputPanel'
import DateWindowSlider, {
  MonthWindowSlider,
  describeMonthWindow,
  describeWeekWindow,
  describeWindow
} from './DateWindowSlider'

type Kind = 'day' | 'week' | 'month'

export interface DateProfileChoice {
  kind: Kind
  start: number
  end: number
  /** 只在按日时给：导出 key → [start, end]，null = 跟随指标 */
  outputWindows?: Record<string, [number, number] | null>
}

interface DateProfileModalProps {
  open: boolean
  yamlText: string
  onCancel: () => void
  /** 写进 YAML，成功返回 true 才关弹窗 */
  onOk: (choice: DateProfileChoice) => Promise<boolean>
}

export function describeProfile(kind: Kind, start: number, end: number): string {
  if (kind === 'month') return `按月 · ${describeMonthWindow(start, end)}`
  if (kind === 'week') return `按周 · ${describeWeekWindow(start, end)}`
  return `按日 · ${describeWindow(start, end)}`
}

export default function DateProfileModal({ open, yamlText, onCancel, onOk }: DateProfileModalProps): JSX.Element {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [kind, setKind] = useState<Kind>('day')
  const [span, setSpan] = useState<[number, number]>([-1, -1])
  const [outputs, setOutputs] = useState<WorkflowOutput[]>([])
  const [own, setOwn] = useState<Record<string, [number, number]>>({})
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!open) return
    let alive = true
    setLoading(true)
    setError('')
    api.readDateProfile(yamlText)
      .then((profile) => {
        if (!alive) return
        setKind(profile.kind)
        setSpan([profile.start, profile.end])
        setOutputs(profile.outputs || [])
        const initial: Record<string, [number, number]> = {}
        for (const output of profile.outputs || []) {
          if (output.window) initial[output.key] = [output.window[0], output.window[1]]
        }
        setOwn(initial)
        setExpanded(Object.keys(initial).length > 0)
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : '读不出日期配置，先检查 YAML 语法')
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, yamlText])

  const looped = outputs.filter((output) => output.in_loop)
  const ownCount = looped.filter((output) => own[output.key]).length
  const showOutputs = kind === 'day' && outputs.length > 1

  const submit = async (): Promise<void> => {
    const choice: DateProfileChoice = { kind, start: span[0], end: span[1] }
    if (kind === 'day') {
      choice.outputWindows = Object.fromEntries(looped.map((output) => [output.key, own[output.key] || null]))
    }
    setSaving(true)
    try {
      if (await onOk(choice)) onCancel()
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title="日期范围"
      width={820}
      onCancel={onCancel}
      onOk={() => { void submit() }}
      okText="写入配置"
      confirmLoading={saving}
      okButtonProps={{ disabled: loading || Boolean(error) }}
      destroyOnClose
    >
      {error ? (
        <Alert type="error" showIcon message={error} />
      ) : (
        <Spin spinning={loading}>
          <div className="profile-kind">
            <span>日期维度</span>
            <Segmented
              value={kind}
              onChange={(value) => {
                const next = value as Kind
                setKind(next)
                setSpan([-1, -1])
              }}
              options={[
                { label: '按日', value: 'day' },
                { label: '按月', value: 'month' }
              ]}
            />
          </div>

          <div className="task-window-block">
            <div className="profile-default">
              <div className="profile-default-head">
                <span>指标默认</span>
                <span>{describeProfile(kind, span[0], span[1])}</span>
              </div>
              {kind === 'month' ? (
                <MonthWindowSlider compact value={span} onChange={setSpan} />
              ) : (
                <DateWindowSlider compact value={span} onChange={setSpan} />
              )}
            </div>

            {showOutputs && (
              <OutputPanel
                title="按导出另配"
                count={ownCount}
                total={looped.length}
                open={expanded}
                onToggle={() => setExpanded((prev) => !prev)}
                note="指定的范围写在 YAML 对应导出步骤的 start / end 上。几个导出范围不同时按天合并执行：同一天只选一次日期，当天不要的导出跳过。"
              >
                {outputs.map((output) => {
                  const value = own[output.key]
                  return (
                    <div key={output.key} className={`task-output-item${output.in_loop ? '' : ' is-muted'}`}>
                      <OutputName output={output} duplicateHint="同一指标里有重名导出，在 YAML 的这一步补上 name 才能稳定区分" />
                      {output.in_loop ? (
                        <>
                          <Segmented
                            size="small"
                            value={value ? 'own' : 'follow'}
                            options={[{ label: '跟随', value: 'follow' }, { label: '指定', value: 'own' }]}
                            onChange={(mode) => setOwn((prev) => {
                              const next = { ...prev }
                              if (mode === 'own') next[output.key] = [...span] as [number, number]
                              else delete next[output.key]
                              return next
                            })}
                          />
                          {value ? (
                            <div className="task-output-detail">
                              <div className="task-output-slider">
                                <DateWindowSlider
                                  compact
                                  value={value}
                                  onChange={(next) => setOwn((prev) => ({ ...prev, [output.key]: next }))}
                                />
                              </div>
                              <span className="task-window-range">{describeWindow(value[0], value[1])}</span>
                            </div>
                          ) : (
                            <span className="task-output-follow">
                              同指标：<strong>{describeWindow(span[0], span[1])}</strong>
                            </span>
                          )}
                        </>
                      ) : (
                        <>
                          <span />
                          <span className="task-output-follow">
                            {output.in_loop === undefined ? '后端没返回循环信息，重启后端后再配' : '不在日期循环里，跟指标跑一次'}
                          </span>
                        </>
                      )}
                    </div>
                  )
                })}
              </OutputPanel>
            )}
          </div>

          <div className="profile-hint">
            {kind === 'month' ? 'M 是运行当月，每个月查一次整月。' : 'D 是运行当天。'}
            直接执行时用这里的范围；任务或补数据传入的日期会盖过它。
            {kind !== 'day' && outputs.length > 1 && ' 按月不能按导出另配，切过去会清掉导出上的范围。'}
          </div>
        </Spin>
      )}
    </Modal>
  )
}
