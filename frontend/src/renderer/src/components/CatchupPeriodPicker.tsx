import { useMemo } from 'react'
import { Button, DatePicker, Segmented } from 'antd'
import type { Dayjs } from 'dayjs'
import {
  CATCHUP_LIMITS,
  MULTIPLE_PRESETS,
  buildRangePresets,
  normalizeRange,
  periodUnitLabel,
  recentPeriods,
  selectionCount,
  selectionExceedsLimit,
  selectionSoftWarn,
  switchSelectionMode,
  type CatchupPickMode,
  type PeriodKind,
  type PeriodSelection
} from './catchupDates'

const { RangePicker } = DatePicker

const MODE_OPTIONS: { label: string; value: CatchupPickMode }[] = [
  { label: '时间段', value: 'range' },
  { label: '指定日期', value: 'multiple' }
]

interface Props {
  kind: PeriodKind
  value: PeriodSelection
  referenceDate: Dayjs
  onChange: (next: PeriodSelection) => void
  size?: 'small' | 'middle'
  status?: 'error' | 'warning'
  rangePlaceholder?: [string, string]
  multiplePlaceholder?: string
  showCountBadge?: boolean
}

function defaultRangePlaceholder(kind: PeriodKind): [string, string] {
  if (kind === 'month') return ['开始月份', '结束月份']
  if (kind === 'week') return ['开始周', '结束周']
  return ['开始日期', '结束日期']
}

function defaultMultiplePlaceholder(kind: PeriodKind): string {
  if (kind === 'month') return '留空使用默认月份'
  if (kind === 'week') return '留空使用默认周'
  return '留空使用默认日期'
}

export default function CatchupPeriodPicker({
  kind,
  value,
  referenceDate,
  onChange,
  size = 'middle',
  status,
  rangePlaceholder,
  multiplePlaceholder,
  showCountBadge = true
}: Props): JSX.Element {
  const picker = kind === 'month' ? 'month' : kind === 'week' ? 'week' : 'date'
  const count = selectionCount(value, kind)
  const overLimit = selectionExceedsLimit(value, kind)
  const softWarn = selectionSoftWarn(value, kind)
  const resolvedStatus = status ?? (overLimit ? 'error' : softWarn ? 'warning' : undefined)
  const rangePresets = useMemo(() => buildRangePresets(kind, referenceDate), [kind, referenceDate])
  const multiplePresets = MULTIPLE_PRESETS[kind]
  const unit = periodUnitLabel(kind)
  const maxTagCount = value.dates.length > 8 ? 2 : 'responsive'

  return (
    <div className={`catchup-picker-bar${size === 'small' ? ' is-small' : ''}`}>
      <Segmented
        size={size}
        className="catchup-mode-switch"
        value={value.mode}
        options={MODE_OPTIONS}
        onChange={(next) => {
          onChange(switchSelectionMode(value, next as CatchupPickMode, kind))
        }}
      />
      {value.mode === 'range' ? (
        <RangePicker
          size={size}
          picker={picker}
          allowClear
          className="catchup-picker-input"
          value={value.range}
          status={resolvedStatus}
          placeholder={rangePlaceholder || defaultRangePlaceholder(kind)}
          presets={rangePresets}
          onChange={(next) => {
            const pair = next && next[0] && next[1] ? normalizeRange([next[0], next[1]], kind) : null
            onChange({ mode: 'range', range: pair, dates: [] })
          }}
        />
      ) : (
        <DatePicker
          multiple
          size={size}
          picker={picker}
          className="catchup-picker-input"
          value={value.dates}
          status={resolvedStatus}
          placeholder={multiplePlaceholder || defaultMultiplePlaceholder(kind)}
          maxTagCount={maxTagCount}
          renderExtraFooter={() => (
            <div className="catchup-date-presets">
              <span>快捷选择</span>
              {multiplePresets.map((preset) => (
                <Button
                  key={preset.label}
                  type="link"
                  size="small"
                  onClick={() => {
                    const nextDates = recentPeriods(preset.count, preset.endOffset, kind, referenceDate)
                    onChange({ mode: 'multiple', range: null, dates: nextDates })
                  }}
                >
                  {preset.label}
                </Button>
              ))}
            </div>
          )}
          onChange={(next) => {
            const list = (next as Dayjs[] | null) || []
            onChange({ mode: 'multiple', range: null, dates: list })
          }}
        />
      )}
      {showCountBadge && count > 0 && (
        <span
          className={`catchup-count-pill${overLimit ? ' is-error' : softWarn ? ' is-warn' : ''}`}
          title={overLimit ? `一次最多补 ${CATCHUP_LIMITS[kind]} ${unit}` : undefined}
        >
          {`共 ${count} ${unit}`}
        </span>
      )}
    </div>
  )
}
