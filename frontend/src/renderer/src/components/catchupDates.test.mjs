import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)
const ts = require('typescript')
const dayjs = require('dayjs')

function loadCatchupDates() {
  const sourcePath = path.join(here, 'catchupDates.ts')
  const source = fs.readFileSync(sourcePath, 'utf8')
  const { outputText } = ts.transpileModule(source, {
    fileName: sourcePath,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      esModuleInterop: true
    }
  })
  const mod = { exports: {} }
  const fn = vm.runInThisContext(`(function(exports, require, module) {${outputText}\n})`, {
    filename: sourcePath
  })
  fn(mod.exports, require, mod)
  return mod.exports
}

const {
  CATCHUP_LIMITS,
  CATCHUP_SOFT_WARN_DAYS,
  MULTIPLE_PRESETS,
  emptyPeriodSelection,
  expandPeriodRange,
  formatPeriodSummary,
  groupConsecutivePeriods,
  groupOutputPreviews,
  groupPreviewRows,
  hasAnySelectionInput,
  rangePeriodCount,
  recentPeriods,
  resolveCustomIso,
  selectionCount,
  selectionExceedsLimit,
  selectionIssue,
  selectionSoftWarn,
  switchSelectionMode,
  weekMonday
} = loadCatchupDates()

test('day range count and expansion support a full leap year and guard overflow in O(1)', () => {
  const start = dayjs('2024-01-01')
  const end = dayjs('2024-12-31')
  assert.equal(rangePeriodCount([start, end], 'day'), 366)
  // 倒挂区间不展开并报告 reversed 问题
  assert.equal(rangePeriodCount([end, start], 'day'), 0)
  assert.deepEqual(expandPeriodRange([end, start], 'day'), [])
  assert.equal(selectionIssue({ mode: 'range', range: [end, start], dates: [] }, 'day')?.kind, 'reversed')

  // 半填区间识别为 incomplete，不会误当成空默认
  const onlyStart = { mode: 'range', range: [start, null], dates: [] }
  assert.equal(hasAnySelectionInput(onlyStart), true)
  assert.equal(selectionIssue(onlyStart, 'day')?.kind, 'incomplete_end')
  assert.equal(rangePeriodCount([start, null], 'day'), 0)

  const onlyEnd = { mode: 'range', range: [null, end], dates: [] }
  assert.equal(hasAnySelectionInput(onlyEnd), true)
  assert.equal(selectionIssue(onlyEnd, 'month')?.kind, 'incomplete_start')

  const expanded = expandPeriodRange([start, end], 'day')
  assert.equal(expanded.length, 366)
  assert.equal(expanded[0].format('YYYY-MM-DD'), '2024-01-01')
  assert.equal(expanded[365].format('YYYY-MM-DD'), '2024-12-31')

  // 超出 366 天不展开成超大数组
  const huge = [dayjs('2020-01-01'), dayjs('2026-12-31')]
  assert.ok(rangePeriodCount(huge, 'day') > 2500)
  assert.deepEqual(expandPeriodRange(huge, 'day'), [])
  const overSel = { mode: 'range', range: huge, dates: [] }
  assert.equal(selectionExceedsLimit(overSel, 'day'), true)
  assert.equal(selectionIssue(overSel, 'day')?.kind, 'over_limit')
  assert.deepEqual(resolveCustomIso(overSel, 'day'), [])
})

test('week range normalizes Sunday and mid-week dates to ISO Monday and enforces 104-week limit', () => {
  // 2026-11-29 是周日，所属周一是 2026-11-23
  assert.equal(weekMonday(dayjs('2026-11-29')).format('YYYY-MM-DD'), '2026-11-23')
  const iso = resolveCustomIso(
    { mode: 'range', range: [dayjs('2026-11-11'), dayjs('2026-11-29')], dates: [] },
    'week'
  )
  assert.deepEqual(iso, ['2026-11-09', '2026-11-16', '2026-11-23'])

  const mon = dayjs('2025-01-06')
  assert.equal(rangePeriodCount([mon, mon.add(103, 'week')], 'week'), 104)
  assert.equal(selectionExceedsLimit({ mode: 'range', range: [mon, mon.add(103, 'week')], dates: [] }, 'week'), false)
  assert.equal(selectionExceedsLimit({ mode: 'range', range: [mon, mon.add(104, 'week')], dates: [] }, 'week'), true)
})

test('month range crosses years cleanly and enforces 60-month limit', () => {
  const iso = resolveCustomIso(
    { mode: 'range', range: [dayjs('2025-11-15'), dayjs('2026-02-02')], dates: [] },
    'month'
  )
  assert.deepEqual(iso, ['2025-11', '2025-12', '2026-01', '2026-02'])
  assert.equal(rangePeriodCount([dayjs('2022-01-01'), dayjs('2026-12-01')], 'month'), 60)
  assert.equal(
    selectionExceedsLimit({ mode: 'range', range: [dayjs('2022-01-01'), dayjs('2026-12-01')], dates: [] }, 'month'),
    false
  )
  assert.equal(
    selectionExceedsLimit({ mode: 'range', range: [dayjs('2022-01-01'), dayjs('2027-01-01')], dates: [] }, 'month'),
    true
  )
})

test('switching modes preserves contiguous spans and protects scattered picks from accidental expansion', () => {
  // 时间段 -> 指定日期：自动展开
  const rangeSel = { mode: 'range', range: [dayjs('2026-03-01'), dayjs('2026-03-04')], dates: [] }
  const asMultiple = switchSelectionMode(rangeSel, 'multiple', 'day')
  assert.equal(asMultiple.mode, 'multiple')
  assert.deepEqual(resolveCustomIso(asMultiple, 'day'), [
    '2026-03-01', '2026-03-02', '2026-03-03', '2026-03-04'
  ])

  // 指定日期（连续）-> 时间段：无损合并为起止段
  const backToRange = switchSelectionMode(asMultiple, 'range', 'day')
  assert.equal(backToRange.mode, 'range')
  assert.equal(backToRange.range[0].format('YYYY-MM-DD'), '2026-03-01')
  assert.equal(backToRange.range[1].format('YYYY-MM-DD'), '2026-03-04')

  // 指定日期（散点）-> 时间段：不强行扩成整段，且切回指定日期时保留原散点
  const scattered = {
    mode: 'multiple',
    range: null,
    dates: [dayjs('2026-01-01'), dayjs('2026-06-01')]
  }
  const peekRange = switchSelectionMode(scattered, 'range', 'day')
  assert.equal(peekRange.mode, 'range')
  assert.equal(peekRange.range, null)
  assert.equal(selectionCount(peekRange, 'day'), 0)

  const backToScattered = switchSelectionMode(peekRange, 'multiple', 'day')
  assert.deepEqual(resolveCustomIso(backToScattered, 'day'), ['2026-01-01', '2026-06-01'])

  // 超限区间切到指定日期时不展开
  const overRange = { mode: 'range', range: [dayjs('2024-01-01'), dayjs('2026-01-01')], dates: [] }
  const overMultiple = switchSelectionMode(overRange, 'multiple', 'day')
  assert.deepEqual(overMultiple.dates, [])
})

test('formatPeriodSummary folds consecutive days, weeks, and months and truncates many scattered segments', () => {
  assert.equal(formatPeriodSummary(['2026-11-26'], 'day'), '2026-11-26')

  const ninety = resolveCustomIso(
    { mode: 'range', range: [dayjs('2026-01-01'), dayjs('2026-03-31')], dates: [] },
    'day'
  )
  assert.equal(formatPeriodSummary(ninety, 'day'), '2026-01-01 ～ 2026-03-31（共 90 天）')

  assert.equal(
    formatPeriodSummary(['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-10', '2026-01-15', '2026-01-16'], 'day'),
    '2026-01-01 ～ 2026-01-03、2026-01-10、2026-01-15 ～ 2026-01-16（共 6 天）'
  )

  const manyScattered = ['01', '03', '05', '07', '09', '11', '13', '15'].map((d) => `2026-05-${d}`)
  assert.equal(
    formatPeriodSummary(manyScattered, 'day'),
    '2026-05-01、2026-05-03、2026-05-05 等 8 段（共 8 天）'
  )

  assert.equal(
    formatPeriodSummary(['2026-11-09', '2026-11-16', '2026-11-23'], 'week'),
    '2026-11-09 ～ 2026-11-23（共 3 周）'
  )

  assert.equal(
    formatPeriodSummary(['2025-11', '2025-12', '2026-01', '2026-02'], 'month'),
    '2025-11 ～ 2026-02（共 4 个月）'
  )

  assert.deepEqual(
    groupConsecutivePeriods(['2026-01-01', '2026-01-02', '2026-01-05'], 'day'),
    [
      { start: '2026-01-01', end: '2026-01-02', count: 2 },
      { start: '2026-01-05', end: '2026-01-05', count: 1 }
    ]
  )
})

test('groupOutputPreviews and groupPreviewRows merge identical entries while preserving order', () => {
  const outputs = groupOutputPreviews([
    { label: '负荷', summary: '不补', signature: 'skip' },
    { label: '日前', summary: '2026-01-01 ～ 2026-01-31（共 31 天）', signature: 'own:a' },
    { label: '现货出清', summary: '不补', signature: 'skip' },
    { label: '实时', summary: '2026-01-01 ～ 2026-01-31（共 31 天）', signature: 'own:a' }
  ])
  assert.deepEqual(outputs, [
    { labels: ['负荷', '现货出清'], summary: '不补' },
    { labels: ['日前', '实时'], summary: '2026-01-01 ～ 2026-01-31（共 31 天）' }
  ])

  const rows = groupPreviewRows([
    { id: 'a', name: 'A', kind: 'day', isDefault: false, overLimit: false, values: ['2026-01-01', '2026-01-02'], ownSignature: '' },
    { id: 'b', name: 'B', kind: 'day', isDefault: false, overLimit: false, values: ['2026-01-01', '2026-01-02'], ownSignature: '' },
    { id: 'c', name: 'C', kind: 'day', isDefault: true, overLimit: false, values: ['2026-01-01', '2026-01-02'], ownSignature: '' },
    { id: 'd', name: 'D', kind: 'day', isDefault: false, overLimit: true, values: [], ownSignature: '' },
    { id: 'e', name: 'E', kind: 'day', isDefault: false, overLimit: true, values: [], ownSignature: '' }
  ])
  assert.equal(rows.length, 4)
  assert.deepEqual(rows[0].items.map((x) => x.id), ['a', 'b'])
  assert.deepEqual(rows[1].items.map((x) => x.id), ['c'])
  assert.deepEqual(rows[2].items.map((x) => x.id), ['d'])
  assert.deepEqual(rows[3].items.map((x) => x.id), ['e'])
})

test('soft warning triggers only for day selections between 63 and 366 days', () => {
  const base = dayjs('2026-01-01')
  assert.equal(CATCHUP_SOFT_WARN_DAYS, 62)
  assert.equal(selectionSoftWarn({ mode: 'range', range: [base, base.add(61, 'day')], dates: [] }, 'day'), false)
  assert.equal(selectionSoftWarn({ mode: 'range', range: [base, base.add(62, 'day')], dates: [] }, 'day'), true)
  assert.equal(selectionSoftWarn({ mode: 'range', range: [dayjs('2024-01-01'), dayjs('2024-12-31')], dates: [] }, 'day'), true)
  assert.equal(selectionSoftWarn({ mode: 'range', range: [dayjs('2024-01-01'), dayjs('2025-01-01')], dates: [] }, 'day'), false)
  assert.equal(selectionSoftWarn({ mode: 'range', range: [base, base.add(80, 'week')], dates: [] }, 'week'), false)
  assert.equal(selectionSoftWarn(emptyPeriodSelection('range'), 'day'), false)
})

test('all multiple-mode presets stay within limits', () => {
  const ref = dayjs('2026-11-27')
  for (const kind of ['day', 'week', 'month']) {
    for (const preset of MULTIPLE_PRESETS[kind]) {
      const list = recentPeriods(preset.count, preset.endOffset, kind, ref)
      assert.equal(list.length, preset.count)
      assert.ok(list.length > 0 && list.length <= CATCHUP_LIMITS[kind])
    }
  }
})
