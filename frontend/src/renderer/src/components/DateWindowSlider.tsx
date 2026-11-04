import { Slider, Typography } from 'antd'

const MIN = -14
const MAX = 7

export function formatDOffset(offset: number): string {
  if (offset === 0) return 'D'
  if (offset > 0) return `D+${offset}`
  return `D${offset}`
}

export function describeWindow(start: number, end: number): string {
  const days = end - start + 1
  return `${formatDOffset(start)} 到 ${formatDOffset(end)}，共 ${days} 天`
}

export function legacyWindowText(backfill: number, cover: number): string {
  const start = backfill > 0 ? -backfill : 0
  const end = cover > 0 ? cover - 1 : backfill > 0 ? -1 : 0
  return describeWindow(start, end)
}

export function offsetsFromBackfill(backfill: number, cover: number): [number, number] {
  const start = backfill > 0 ? -backfill : 0
  const end = cover > 0 ? cover - 1 : backfill > 0 ? -1 : 0
  return [start, end]
}

const MONTH_MIN = -12
const MONTH_MAX = 0

export function formatMOffset(offset: number): string {
  if (offset === 0) return 'M'
  if (offset > 0) return `M+${offset}`
  return `M${offset}`
}

export function describeMonthWindow(start: number, end: number): string {
  const months = end - start + 1
  return `${formatMOffset(start)} 到 ${formatMOffset(end)}，共 ${months} 个月`
}

export function clampMonthSpan(span: [number, number]): [number, number] {
  const start = Math.min(MONTH_MAX, Math.max(MONTH_MIN, span[0]))
  const end = Math.min(MONTH_MAX, Math.max(MONTH_MIN, span[1]))
  return start <= end ? [start, end] : [end, start]
}

export function formatWOffset(offset: number): string {
  return offset === 0 ? 'W' : `W${offset > 0 ? '+' : ''}${offset}`
}

export function describeWeekWindow(start: number, end: number): string {
  return `${formatWOffset(start)} 到 ${formatWOffset(end)}，共 ${end - start + 1} 周`
}

export function clampWeekSpan(span: [number, number]): [number, number] {
  const start = Math.min(4, Math.max(-12, span[0]))
  const end = Math.min(4, Math.max(-12, span[1]))
  return start <= end ? [start, end] : [end, start]
}

interface DateWindowSliderProps {
  value: [number, number]
  onChange: (value: [number, number]) => void
  onChangeComplete?: (value: [number, number]) => void
  compact?: boolean
}

export default function DateWindowSlider({ value, onChange, onChangeComplete, compact = false }: DateWindowSliderProps): JSX.Element {
  return (
    <div style={{ padding: '0 8px' }}>
      <Slider
        range
        min={MIN}
        max={MAX}
        value={value}
        marks={{
          [-14]: 'D-14',
          [-7]: 'D-7',
          0: 'D',
          7: 'D+7'
        }}
        tooltip={{ formatter: (current) => formatDOffset(current ?? 0) }}
        onChange={(next) => {
          const [start, end] = next as number[]
          onChange([start, end])
        }}
        onChangeComplete={(next) => {
          if (!onChangeComplete) return
          const [start, end] = next as number[]
          onChangeComplete([start, end])
        }}
      />
      {!compact && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {describeWindow(value[0], value[1])}。D 是运行当天。
        </Typography.Text>
      )}
    </div>
  )
}

export function MonthWindowSlider({ value, onChange, onChangeComplete, compact = false }: DateWindowSliderProps): JSX.Element {
  const span = clampMonthSpan(value)
  return (
    <div style={{ padding: '0 8px' }}>
      <Slider
        range
        min={MONTH_MIN}
        max={MONTH_MAX}
        value={span}
        marks={{
          [-12]: 'M-12',
          [-6]: 'M-6',
          0: 'M'
        }}
        tooltip={{ formatter: (current) => formatMOffset(current ?? 0) }}
        onChange={(next) => {
          const [start, end] = next as number[]
          onChange([start, end])
        }}
        onChangeComplete={(next) => {
          if (!onChangeComplete) return
          const [start, end] = next as number[]
          onChangeComplete([start, end])
        }}
      />
      {!compact && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {describeMonthWindow(span[0], span[1])}。M 是运行当天所在的月份，每个月查一次整月。
        </Typography.Text>
      )}
    </div>
  )
}

export function WeekWindowSlider({ value, onChange, onChangeComplete, compact = false }: DateWindowSliderProps): JSX.Element {
  const span = clampWeekSpan(value)
  return (
    <div style={{ padding: '0 8px' }}>
      <Slider
        range min={-12} max={4} value={span}
        marks={{ [-12]: 'W-12', [-4]: 'W-4', 0: 'W', 4: 'W+4' }}
        tooltip={{ formatter: (current) => formatWOffset(current ?? 0) }}
        onChange={(next) => onChange(next as [number, number])}
        onChangeComplete={(next) => onChangeComplete?.(next as [number, number])}
      />
      {!compact && <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {describeWeekWindow(span[0], span[1])}。W 是运行当天所在的周，每周采集一次。
      </Typography.Text>}
    </div>
  )
}
