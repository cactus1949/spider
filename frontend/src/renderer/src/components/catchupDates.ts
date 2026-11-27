import dayjs, { type Dayjs } from 'dayjs'

export type PeriodKind = 'day' | 'week' | 'month'
export type CatchupPickMode = 'range' | 'multiple'
export type PeriodRange = [Dayjs | null, Dayjs | null] | null

export interface PeriodSelection {
  mode: CatchupPickMode
  range: PeriodRange
  dates: Dayjs[]
}

export type SelectionIssueKind = 'incomplete_start' | 'incomplete_end' | 'reversed' | 'over_limit'

export interface SelectionIssue {
  kind: SelectionIssueKind
  message: string
}

export interface RangePreset {
  label: string
  value: [Dayjs, Dayjs]
}

export interface MultiplePreset {
  label: string
  count: number
  endOffset: number
}

export interface PeriodSegment {
  start: string
  end: string
  count: number
}

export interface OutputPreviewEntry {
  label: string
  summary: string
  signature: string
}

export interface GroupedOutputPreview {
  labels: string[]
  summary: string
}

export interface PreviewItemInput {
  id: string
  name: string
  kind: PeriodKind
  isDefault: boolean
  overLimit: boolean
  values: string[]
  ownSignature: string
}

export interface GroupedPreviewRow<T extends PreviewItemInput = PreviewItemInput> {
  key: string
  items: T[]
}

export const CATCHUP_LIMITS: Record<PeriodKind, number> = {
  day: 366,
  week: 104,
  month: 60
}

export const CATCHUP_SOFT_WARN_DAYS = 62

export const DEFAULT_PREVIEW_SEGMENTS = 3
const MS_PER_DAY = 86_400_000

export function emptyPeriodSelection(mode: CatchupPickMode = 'range'): PeriodSelection {
  return { mode, range: null, dates: [] }
}

export function clonePeriodSelection(sel: PeriodSelection): PeriodSelection {
  return {
    mode: sel.mode,
    range: sel.range ? [sel.range[0], sel.range[1]] : null,
    dates: [...sel.dates]
  }
}

export function periodUnitLabel(kind: PeriodKind): string {
  if (kind === 'month') return '个月'
  if (kind === 'week') return '周'
  return '天'
}

export function periodKindLabel(kind: PeriodKind): string {
  if (kind === 'month') return '按月'
  if (kind === 'week') return '按周'
  return '按日'
}

export function periodIsoPattern(kind: PeriodKind): string {
  return kind === 'month' ? 'YYYY-MM' : 'YYYY-MM-DD'
}

export function weekMonday(value: Dayjs): Dayjs {
  return value.subtract((value.day() + 6) % 7, 'day').startOf('day')
}

export function normalizeAnchor(value: Dayjs, kind: PeriodKind): Dayjs {
  if (kind === 'month') return value.startOf('month')
  if (kind === 'week') return weekMonday(value)
  return value.startOf('day')
}

function utcDayIndex(year: number, monthIndex0: number, day: number): number {
  return Math.round(Date.UTC(year, monthIndex0, day) / MS_PER_DAY)
}

function parseIsoDayIndex(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number)
  return utcDayIndex(y, m - 1, d)
}

function parseIsoMonthIndex(iso: string): number {
  const [y, m] = iso.split('-').map(Number)
  return y * 12 + (m - 1)
}

export function periodNoun(kind: PeriodKind): string {
  if (kind === 'month') return '月份'
  if (kind === 'week') return '周'
  return '日期'
}

export function normalizeRange(range: PeriodRange, kind: PeriodKind): [Dayjs, Dayjs] | null {
  if (!range || !range[0] || !range[1]) return null
  const a = normalizeAnchor(range[0], kind)
  const b = normalizeAnchor(range[1], kind)
  return b.isBefore(a) ? [b, a] : [a, b]
}

export function isRangeReversed(range: PeriodRange, kind: PeriodKind): boolean {
  if (!range || !range[0] || !range[1]) return false
  return normalizeAnchor(range[1], kind).isBefore(normalizeAnchor(range[0], kind))
}

export function hasAnySelectionInput(sel: PeriodSelection | undefined): boolean {
  if (!sel) return false
  if (sel.mode === 'range') return Boolean(sel.range && (sel.range[0] || sel.range[1]))
  return sel.dates.length > 0
}

/** O(1) 计算起止区间包含的周期数；半填或起止倒挂时返回 0。 */
export function rangePeriodCount(range: PeriodRange, kind: PeriodKind): number {
  if (!range || !range[0] || !range[1]) return 0
  const start = normalizeAnchor(range[0], kind)
  const end = normalizeAnchor(range[1], kind)
  if (end.isBefore(start)) return 0
  if (kind === 'month') {
    return (end.year() - start.year()) * 12 + (end.month() - start.month()) + 1
  }
  const days =
    utcDayIndex(end.year(), end.month(), end.date()) -
    utcDayIndex(start.year(), start.month(), start.date())
  if (kind === 'week') return Math.round(days / 7) + 1
  return days + 1
}

/** 将合法起止区间展开为有序的 Dayjs 列表；半填、倒挂或超上限时返回空数组。 */
export function expandPeriodRange(
  range: PeriodRange,
  kind: PeriodKind,
  maxItems: number = CATCHUP_LIMITS[kind]
): Dayjs[] {
  if (!range || !range[0] || !range[1] || isRangeReversed(range, kind)) return []
  const count = rangePeriodCount(range, kind)
  if (count <= 0 || count > maxItems) return []
  const start = normalizeAnchor(range[0], kind)
  const unit = kind === 'month' ? 'month' : kind === 'week' ? 'week' : 'day'
  return Array.from({ length: count }, (_, index) => start.add(index, unit))
}

export function toIsoList(values: Dayjs[] | undefined, kind: PeriodKind): string[] {
  const pattern = periodIsoPattern(kind)
  return [...new Set((values || []).map((item) => normalizeAnchor(item, kind).format(pattern)))].sort()
}

export function dedupeSortedPeriods(values: Dayjs[] | undefined, kind: PeriodKind): Dayjs[] {
  const pattern = periodIsoPattern(kind)
  const map = new Map<string, Dayjs>()
  for (const item of values || []) {
    const anchored = normalizeAnchor(item, kind)
    map.set(anchored.format(pattern), anchored)
  }
  return [...map.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, val]) => val)
}

export function selectionCount(sel: PeriodSelection | undefined, kind: PeriodKind): number {
  if (!sel) return 0
  if (sel.mode === 'range') return rangePeriodCount(sel.range, kind)
  return toIsoList(sel.dates, kind).length
}

export function selectionIsEmpty(sel: PeriodSelection | undefined, kind: PeriodKind): boolean {
  return selectionCount(sel, kind) === 0
}

export function selectionExceedsLimit(sel: PeriodSelection | undefined, kind: PeriodKind): boolean {
  return selectionCount(sel, kind) > CATCHUP_LIMITS[kind]
}

export function selectionIssue(sel: PeriodSelection | undefined, kind: PeriodKind): SelectionIssue | null {
  if (!sel) return null
  const noun = periodNoun(kind)
  const unit = periodUnitLabel(kind)
  if (sel.mode === 'range' && sel.range) {
    const [start, end] = sel.range
    if (start && !end) return { kind: 'incomplete_end', message: `请选择结束${noun}` }
    if (!start && end) return { kind: 'incomplete_start', message: `请选择开始${noun}` }
    if (start && end && isRangeReversed(sel.range, kind)) {
      return { kind: 'reversed', message: `结束${noun}不能早于开始${noun}` }
    }
  }
  const count = selectionCount(sel, kind)
  if (count > CATCHUP_LIMITS[kind]) {
    return {
      kind: 'over_limit',
      message: `已选 ${count} ${unit}，一次最多补 ${CATCHUP_LIMITS[kind]} ${unit}`
    }
  }
  return null
}

export function selectionSoftWarn(sel: PeriodSelection | undefined, kind: PeriodKind): boolean {
  if (kind !== 'day') return false
  if (selectionIssue(sel, kind)) return false
  const count = selectionCount(sel, kind)
  return count > CATCHUP_SOFT_WARN_DAYS && count <= CATCHUP_LIMITS.day
}

/** 展开用户明确选中的周期为 ISO 字符串；未选择或超上限时返回空数组。 */
export function resolveCustomIso(sel: PeriodSelection | undefined, kind: PeriodKind): string[] {
  if (!sel) return []
  if (sel.mode === 'range') {
    const expanded = expandPeriodRange(sel.range, kind)
    return toIsoList(expanded, kind)
  }
  return toIsoList(sel.dates, kind)
}

function isContiguousPeriods(sorted: Dayjs[], kind: PeriodKind): boolean {
  if (sorted.length === 0) return false
  if (sorted.length === 1) return true
  const count = rangePeriodCount([sorted[0], sorted[sorted.length - 1]], kind)
  return count === sorted.length
}

/**
 * 切换「时间段 / 指定日期」模式：
 * - 时间段 -> 指定日期：若区间合法则自动展开，方便剔除个别日期；若区间为空则保留原指定日期。
 * - 指定日期 -> 时间段：若散点恰好连续则无损转为起止段；不连续时不强行拉成整段，避免误扩。
 */
export function switchSelectionMode(
  sel: PeriodSelection,
  nextMode: CatchupPickMode,
  kind: PeriodKind
): PeriodSelection {
  if (sel.mode === nextMode) return sel
  if (nextMode === 'multiple') {
    if (sel.range) {
      const count = rangePeriodCount(sel.range, kind)
      if (count > 0 && count <= CATCHUP_LIMITS[kind]) {
        return { mode: 'multiple', range: sel.range, dates: expandPeriodRange(sel.range, kind) }
      }
    }
    return { mode: 'multiple', range: sel.range, dates: sel.dates }
  }
  const sorted = dedupeSortedPeriods(sel.dates, kind)
  if (sorted.length > 0 && isContiguousPeriods(sorted, kind)) {
    return { mode: 'range', range: [sorted[0], sorted[sorted.length - 1]], dates: sorted }
  }
  return { mode: 'range', range: null, dates: sorted }
}

/** 将已排序的 ISO 日期/周/月合并成连续区间段。 */
export function groupConsecutivePeriods(values: string[], kind: PeriodKind): PeriodSegment[] {
  const unique = [...new Set(values.filter(Boolean))].sort()
  if (unique.length === 0) return []

  const isNext = (prev: string, cur: string): boolean => {
    if (kind === 'month') return parseIsoMonthIndex(cur) - parseIsoMonthIndex(prev) === 1
    const step = kind === 'week' ? 7 : 1
    return parseIsoDayIndex(cur) - parseIsoDayIndex(prev) === step
  }

  const segments: PeriodSegment[] = []
  let start = unique[0]
  let prev = unique[0]
  let count = 1

  for (let i = 1; i < unique.length; i += 1) {
    const cur = unique[i]
    if (isNext(prev, cur)) {
      prev = cur
      count += 1
    } else {
      segments.push({ start, end: prev, count })
      start = cur
      prev = cur
      count = 1
    }
  }
  segments.push({ start, end: prev, count })
  return segments
}

export function formatPeriodSegment(segment: PeriodSegment): string {
  return segment.start === segment.end ? segment.start : `${segment.start} ～ ${segment.end}`
}

/** 把已排序的 ISO 日期/月份列表合并成连续区间，防止长补数在预览区平铺撑爆界面。 */
export function formatPeriodSummary(
  values: string[],
  kind: PeriodKind,
  maxSegments: number = DEFAULT_PREVIEW_SEGMENTS
): string {
  const segments = groupConsecutivePeriods(values, kind)
  if (segments.length === 0) return ''
  const total = segments.reduce((sum, seg) => sum + seg.count, 0)
  if (total === 1) return segments[0].start

  const formatted = segments.map(formatPeriodSegment)
  const head =
    segments.length > maxSegments
      ? `${formatted.slice(0, maxSegments).join('、')} 等 ${segments.length} 段`
      : formatted.join('、')
  return `${head}（共 ${total} ${periodUnitLabel(kind)}）`
}

/** 同一指标下多个另选导出如果时间完全相同（或均为不补），合并为一条展示。 */
export function groupOutputPreviews(entries: OutputPreviewEntry[]): GroupedOutputPreview[] {
  const grouped: GroupedOutputPreview[] = []
  const indexBySig = new Map<string, number>()
  for (const entry of entries) {
    const existing = indexBySig.get(entry.signature)
    if (existing !== undefined) {
      grouped[existing].labels.push(entry.label)
    } else {
      indexBySig.set(entry.signature, grouped.length)
      grouped.push({ labels: [entry.label], summary: entry.summary })
    }
  }
  return grouped
}

/** 预览里多个指标的维度、默认态和日期完全一致时，合并成一行展示。 */
export function groupPreviewRows<T extends PreviewItemInput>(items: T[]): GroupedPreviewRow<T>[] {
  const rows: GroupedPreviewRow<T>[] = []
  const indexByKey = new Map<string, number>()
  for (const item of items) {
    const mergeKey = item.overLimit
      ? `over::${item.id}`
      : [
          item.kind,
          item.isDefault ? 'default' : 'custom',
          item.values.join(','),
          item.ownSignature
        ].join('|')
    const existing = indexByKey.get(mergeKey)
    if (existing !== undefined) {
      rows[existing].items.push(item)
    } else {
      indexByKey.set(mergeKey, rows.length)
      rows.push({ key: mergeKey, items: [item] })
    }
  }
  return rows
}

export function formatRangeLabel(range: PeriodRange, kind: PeriodKind): string {
  const normalized = normalizeRange(range, kind)
  if (!normalized) return ''
  const pattern = periodIsoPattern(kind)
  const start = normalized[0].format(pattern)
  const end = normalized[1].format(pattern)
  return start === end ? start : `${start} ～ ${end}`
}

export function recentPeriods(
  count: number,
  endOffset: number,
  kind: PeriodKind,
  reference: Dayjs = dayjs().startOf('day')
): Dayjs[] {
  const end = normalizeAnchor(reference, kind).subtract(endOffset, kind)
  return Array.from({ length: count }, (_, index) => end.subtract(count - index - 1, kind))
}

export function buildRangePresets(kind: PeriodKind, reference: Dayjs): RangePreset[] {
  const today = reference.startOf('day')
  if (kind === 'month') {
    const curMonth = today.startOf('month')
    const prevMonth = curMonth.subtract(1, 'month')
    const prevYear = today.subtract(1, 'year')
    return [
      { label: '上个月', value: [prevMonth, prevMonth] },
      { label: '近 3 个月', value: [curMonth.subtract(3, 'month'), prevMonth] },
      { label: '近 6 个月', value: [curMonth.subtract(6, 'month'), prevMonth] },
      { label: '近 1 年', value: [curMonth.subtract(12, 'month'), prevMonth] },
      { label: '今年至今', value: [today.startOf('year'), curMonth] },
      { label: '去年全年', value: [prevYear.startOf('year'), prevYear.endOf('year').startOf('month')] }
    ]
  }
  if (kind === 'week') {
    const thisWeek = weekMonday(today)
    const prevYear = today.subtract(1, 'year')
    return [
      { label: '本周', value: [thisWeek, thisWeek] },
      { label: '近 4 周', value: [thisWeek.subtract(3, 'week'), thisWeek] },
      { label: '近 12 周', value: [thisWeek.subtract(11, 'week'), thisWeek] },
      { label: '近 26 周', value: [thisWeek.subtract(25, 'week'), thisWeek] },
      { label: '今年至今', value: [weekMonday(today.startOf('year')), thisWeek] },
      { label: '去年全年', value: [weekMonday(prevYear.startOf('year')), weekMonday(prevYear.endOf('year'))] }
    ]
  }
  const prevMonth = today.subtract(1, 'month')
  const prevYear = today.subtract(1, 'year')
  return [
    { label: '近 7 天', value: [today.subtract(6, 'day'), today] },
    { label: '近 30 天', value: [today.subtract(29, 'day'), today] },
    {
      label: '上月整月',
      value: [prevMonth.startOf('month'), prevMonth.endOf('month').startOf('day')]
    },
    { label: '近 90 天', value: [today.subtract(89, 'day'), today] },
    { label: '近半年', value: [today.subtract(6, 'month').add(1, 'day'), today] },
    { label: '今年至今', value: [today.startOf('year'), today] },
    {
      label: '去年全年',
      value: [prevYear.startOf('year'), prevYear.endOf('year').startOf('day')]
    }
  ]
}

export const MULTIPLE_PRESETS: Record<PeriodKind, MultiplePreset[]> = {
  day: [
    { label: '近三天', count: 3, endOffset: 0 },
    { label: '近七天', count: 7, endOffset: 0 },
    { label: '近一个月', count: 30, endOffset: 0 }
  ],
  week: [
    { label: '本周', count: 1, endOffset: 0 },
    { label: '近四周', count: 4, endOffset: 0 },
    { label: '近十二周', count: 12, endOffset: 0 }
  ],
  month: [
    { label: '上个月', count: 1, endOffset: 1 },
    { label: '近三月', count: 3, endOffset: 1 },
    { label: '近一年', count: 12, endOffset: 1 }
  ]
}
