import { Button, DatePicker, Segmented } from 'antd'
import type { Dayjs } from 'dayjs'
import {
  CATCHUP_LIMITS,
  MULTIPLE_PRESETS,
  normalizeAnchor,
  periodNoun,
  periodUnitLabel,
  recentPeriods,
  selectionCount,
  selectionIssue,
  selectionSoftWarn,
  switchSelectionMode,
  type CatchupPickMode,
  type PeriodKind,
  type PeriodRange,
  type PeriodSelection
} from './catchupDates'

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
  const noun = periodNoun(kind)
  return [`开始${noun}`, `结束${noun}`]
}

function defaultMultiplePlaceholder(kind: PeriodKind): string {
  if (kind === 'month') return '留空使用默认月份'
  if (kind === 'week') return '留空使用默认周'
  return '留空使用默认日期'
}

function packRange(start: Dayjs | null, end: Dayjs | null): PeriodRange {
  return start || end ? [start, end] : null
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
  const issue = selectionIssue(value, kind)
  const overLimit = issue?.kind === 'over_limit'
  const softWarn = selectionSoftWarn(value, kind)
  const multiplePresets = MULTIPLE_PRESETS[kind]
  const unit = periodUnitLabel(kind)
  const placeholders = rangePlaceholder || defaultRangePlaceholder(kind)
  const maxTagCount = value.dates.length > 8 ? 2 : 'responsive'

  const startVal = value.range?.[0] ?? null
  const endVal = value.range?.[1] ?? null

  const startStatus =
    status ??
    (issue && (issue.kind === 'incomplete_start' || issue.kind === 'reversed' || issue.kind === 'over_limit')
      ? 'error'
      : softWarn
        ? 'warning'
        : undefined)
  const endStatus =
    status ??
    (issue && (issue.kind === 'incomplete_end' || issue.kind === 'reversed' || issue.kind === 'over_limit')
      ? 'error'
      : softWarn
        ? 'warning'
        : undefined)
  const multipleStatus = status ?? (issue ? 'error' : softWarn ? 'warning' : undefined)

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
        <div className="catchup-range-pair">
          <DatePicker
            size={size}
            picker={picker}
            allowClear
            className="catchup-range-input"
            value={startVal}
            defaultPickerValue={startVal ?? endVal ?? undefined}
            status={startStatus}
            placeholder={placeholders[0]}
            onChange={(next) => {
              const anchored = next ? normalizeAnchor(next as Dayjs, kind) : null
              onChange({ mode: 'range', range: packRange(anchored, endVal), dates: [] })
            }}
          />
          <span className="catchup-range-sep">至</span>
          <DatePicker
            size={size}
            picker={picker}
            allowClear
            className="catchup-range-input"
            value={endVal}
            defaultPickerValue={endVal ?? startVal ?? undefined}
            status={endStatus}
            placeholder={placeholders[1]}
            onChange={(next) => {
              const anchored = next ? normalizeAnchor(next as Dayjs, kind) : null
              onChange({ mode: 'range', range: packRange(startVal, anchored), dates: [] })
            }}
          />
        </div>
      ) : (
        <DatePicker
          multiple
          size={size}
          picker={picker}
          className="catchup-picker-input"
          value={value.dates}
          status={multipleStatus}
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
