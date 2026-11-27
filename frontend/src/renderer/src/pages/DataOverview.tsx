import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, Segmented, Space, Switch, Tag, Tooltip } from 'antd'
import {
  AimOutlined,
  CloseOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  FileExcelOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SyncOutlined,
  UpOutlined
} from '@ant-design/icons'
import dayjs from 'dayjs'
import {
  api,
  backendUrl,
  type OverviewCell,
  type OverviewMatrixResult,
  type OverviewRow,
  type OverviewScope
} from '../services/api'
import ProgressModal, { type CatchupRun } from '../components/ProgressModal'
import './DataOverview.css'

type Density = 'compact' | 'normal' | 'large'

interface MonthGroup {
  monthKey: string
  year: number
  month: number
  days: string[]
  startCol: number
  endCol: number
}

interface TipState {
  x: number
  y: number
  owner: string
  indicator: string
  period: string
  isMonth: boolean
  cell: OverviewCell
}

interface ContextMenuState {
  x: number
  y: number
  scopeId: string
  ownerName: string
  row: OverviewRow
  period: string
  cell: OverviewCell
}

interface FlatVisibleRow {
  rowIndex: number
  scope: OverviewScope
  row: OverviewRow
}

const CELL_STEP: Record<Density, number> = {
  compact: 21.5,
  normal: 27,
  large: 31.5
}

function formatOffsetLabel(lag: number, dateKind?: string): string {
  if (dateKind === 'month') return lag === 0 ? 'M' : lag > 0 ? `M+${lag}` : `M${lag}`
  if (lag === 0) return 'D'
  return lag > 0 ? `D+${lag}` : `D${lag}`
}

function formatBytes(size?: number): string {
  if (!size || size <= 0) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(2)} MB`
}

function buildDateList(startIso: string, endIso: string): string[] {
  const out: string[] = []
  let cur = dayjs(startIso)
  const end = dayjs(endIso)
  let guard = 0
  while (!cur.isAfter(end, 'day') && guard++ < 420) {
    out.push(cur.format('YYYY-MM-DD'))
    cur = cur.add(1, 'day')
  }
  return out
}

function extractUniqueMonths(dates: string[]): string[] {
  const out: string[] = []
  for (const d of dates) {
    const mk = d.slice(0, 7)
    if (out[out.length - 1] !== mk) out.push(mk)
  }
  return out
}

/** 本地无配置任务时提供贴合广东电力业务的演示数据（日度方格 + 整月长条 + D+2）。 */
function buildDemoMatrix(startIso: string, endIso: string, todayIso: string): OverviewMatrixResult {
  const dates = buildDateList(startIso, endIso)
  const months = extractUniqueMonths(dates)
  const today = dayjs(todayIso)

  const pubDefs = [
    { id: '日前节点电价查询导出', name: '日前节点电价查询', lag: 1, date_kind: 'day', size: 1111490 },
    { id: '日前预测信息披露', name: '日前预测信息（D+2）', lag: 2, date_kind: 'day', size: 197632 },
    { id: '实时节点电价查询导出', name: '实时节点电价查询', lag: -2, date_kind: 'day', size: 1195376 },
    { id: '现货交易-现货分日出清量价导出', name: '现货分日出清量价', lag: -1, date_kind: 'day', size: 21299 },
    { id: '现货分时分类型-日前', name: '现货分时分类型 · 日前', lag: -1, date_kind: 'day', size: 11776 },
    { id: '现货分时分类型-实时', name: '现货分时分类型 · 实时', lag: -2, date_kind: 'day', size: 10547 },
    { id: '市场用电信息', name: '市场用电信息', lag: -1, date_kind: 'day', size: 5529 },
    { id: '信息披露查询实际信息', name: '信息披露查询实际信息', lag: -5, date_kind: 'day', size: 151552 },
    { id: '结算管理-日清算概览导出', name: '日清算概况 / 统一结算价', lag: -2, date_kind: 'day', size: 9113 },
    { id: '运行日报', name: '运行日报 (PDF)', lag: -1, date_kind: 'day', size: 3040870 },
    { id: '结算管理-月结算概况', name: '月结算概况 & 日历', lag: -1, date_kind: 'month', size: 184320 }
  ]

  const priDefs = [
    { id: '场站-日前交易结果发电侧导出', name: '场站-日前交易结果发电侧', lag: 2, date_kind: 'day', size: 4710 },
    { id: '场站-实际运行结果发电侧导出', name: '场站-实际运行结果发电侧', lag: -1, date_kind: 'day', size: 5939 },
    { id: '场站-信息披露实际数据导出', name: '场站-信息披露实际数据', lag: -5, date_kind: 'day', size: 153600 },
    { id: '结算管理-日清结果查询及结算单导出', name: '结算管理-日清结果结算单', lag: -2, date_kind: 'day', size: 28672 },
    { id: '结算管理-月结算单导出', name: '结算管理-月结算单', lag: -1, date_kind: 'month', size: 96256 }
  ]

  const stations: OverviewScope[] = [
    { id: 'st1', name: '阳江明阳', ssid: 'HUAWEI-YJ01', slot: 1, kind: 'private', retention_days: 60 },
    { id: 'st2', name: '阳西', ssid: 'HUAWEI-YX02', slot: 2, kind: 'private', retention_days: 60 },
    { id: 'st3', name: '韶关浈江', ssid: 'HUAWEI-SG03', slot: 3, kind: 'private', retention_days: 60 },
    { id: 'st4', name: '蕴阳海风场', ssid: 'HUAWEI-YY04', slot: 4, kind: 'private', retention_days: 60 },
    { id: 'st5', name: '惠州平海', ssid: 'HUAWEI-HZ05', slot: 5, kind: 'private', retention_days: 60 },
    { id: 'st6', name: '湛江徐闻', ssid: 'HUAWEI-ZJ06', slot: 6, kind: 'private', retention_days: 60 }
  ]

  const scopes: OverviewScope[] = [
    { id: 'public', name: '公有数据', kind: 'public', retention_days: 30 },
    ...stations
  ]

  const data: Record<string, { rows: OverviewRow[] }> = {}

  const dMinus1 = today.subtract(1, 'day').format('YYYY-MM-DD')
  const dMinus2 = today.subtract(2, 'day').format('YYYY-MM-DD')
  const dMinus5 = today.subtract(5, 'day').format('YYYY-MM-DD')
  const dPlus2 = today.add(2, 'day').format('YYYY-MM-DD')
  const mMinus1 = today.subtract(1, 'month').format('YYYY-MM')

  const buildMonthCells = (def: { id: string; name: string; lag: number; size: number }, ownerPrefix: string, stId?: string): Record<string, OverviewCell> => {
    const cutoffYm = today.add(def.lag, 'month').format('YYYY-MM')
    const cells: Record<string, OverviewCell> = {}
    for (const mk of months) {
      if (mk > cutoffYm) {
        cells[mk] = { status: 'future' }
        continue
      }
      if (stId === 'st6' && mk === mMinus1) {
        cells[mk] = {
          status: 'partial',
          size: 82944,
          mtime: `${todayIso} 08:40`,
          hash: 'zj_m_part',
          file_name: `${ownerPrefix}${def.name}(${mk}).xlsx`,
          summary: '月度结算汇总中「调频辅助服务费」列为空'
        }
        continue
      }
      cells[mk] = {
        status: 'ok',
        size: def.size,
        mtime: `${todayIso} 08:30`,
        hash: `m${mk.replace('-', '')}9a`,
        updated_today: mk === mMinus1 && !stId,
        prev_hash: mk === mMinus1 && !stId ? 'm202610old' : undefined,
        file_name: `${ownerPrefix}${def.name}(${mk}).xlsx`
      }
    }
    return cells
  }

  data.public = {
    rows: pubDefs.map((def) => {
      if (def.date_kind === 'month') {
        return {
          id: def.id,
          name: def.name,
          lag: def.lag,
          date_kind: 'month',
          cells: buildMonthCells(def, '')
        }
      }
      const cutoff = today.add(def.lag, 'day')
      const cells: Record<string, OverviewCell> = {}
      for (const d of dates) {
        if (dayjs(d).isAfter(cutoff, 'day')) {
          cells[d] = { status: 'future' }
          continue
        }
        let cell: OverviewCell = {
          status: 'ok',
          size: def.size,
          mtime: `${todayIso} 08:18`,
          hash: `a91f${d.slice(5).replace('-', '')}`,
          file_name: def.id === '运行日报' ? `广东电力现货市场运行日报(${d}).pdf` : `${def.name}(${d}).xlsx`
        }
        if (def.id === '现货交易-现货分日出清量价导出' && d === dMinus1) {
          cell = {
            status: 'partial',
            size: 19558,
            mtime: `${todayIso} 08:21`,
            hash: 'b201c88e',
            file_name: `现货分日出清量价-按日(${d}).xlsx`,
            summary: '单 Sheet 4 数值列中 2 列为空（实时出清均价、实时电量）'
          }
        } else if (def.id === '日前预测信息披露' && d === dPlus2) {
          cell = {
            status: 'partial',
            size: 180224,
            mtime: `${todayIso} 08:22`,
            hash: '32afe8ca',
            file_name: `信息披露查询预测信息(${d}).xlsx`,
            summary: '17 个 Sheet 中 4 个仅含表头（阻塞断面 / 机组详情等）'
          }
        } else if (def.id === '现货分时分类型-实时' && d === dMinus2) {
          cell = {
            status: 'empty',
            size: 10547,
            mtime: `${todayIso} 11:30`,
            hash: '5c285f3e',
            file_name: `现货分时分类型出清电量_实时(${d}).xlsx`,
            summary: '8 个 Sheet 仅有 24 时刻骨架，数值全空'
          }
        } else if (def.id === '信息披露查询实际信息' && d === dMinus5) {
          cell = {
            status: 'empty',
            size: 9420,
            mtime: `${todayIso} 11:34`,
            hash: 'd9c78d52',
            file_name: `信息披露查询实际信息(${d}).xlsx`,
            summary: '5 个 Sheet 仅含表头（0 行数据）'
          }
        } else if ((def.id === '结算管理-日清算概览导出' || def.id === '市场用电信息') && d === dMinus2) {
          cell.updated_today = true
          cell.prev_hash = '3ab9b842'
          cell.mtime = `${todayIso} 11:32`
        } else if (def.id === '运行日报' && d === dMinus1) {
          cell = {
            status: 'miss',
            summary: '已过水位截止，本地无该日文件'
          }
        }
        cells[d] = cell
      }
      return { id: def.id, name: def.name, lag: def.lag, date_kind: 'day', cells }
    })
  }

  for (const st of stations) {
    data[st.id] = {
      rows: priDefs.map((def) => {
        if (def.date_kind === 'month') {
          return {
            id: def.id,
            name: def.name,
            lag: def.lag,
            date_kind: 'month',
            cells: buildMonthCells(def, `${st.name}_`, st.id)
          }
        }
        const cutoff = today.add(def.lag, 'day')
        const cells: Record<string, OverviewCell> = {}
        for (const d of dates) {
          if (dayjs(d).isAfter(cutoff, 'day')) {
            cells[d] = { status: 'future' }
            continue
          }
          let cell: OverviewCell = {
            status: 'ok',
            size: def.size,
            mtime: `${todayIso} 08:38`,
            hash: `${st.id}${d.slice(8)}f1`,
            file_name: `${st.name}_${def.name}(${d}).xlsx`
          }
          if (st.id === 'st1' && def.id === '场站-信息披露实际数据导出' && d === dMinus5) {
            cell = {
              status: 'empty',
              size: 9318,
              mtime: `${todayIso} 11:38`,
              hash: 'yj_empty',
              file_name: `${st.name}_信息披露实际(${d}).xlsx`,
              summary: '仅含表头 0 数据行，等待回刷覆盖'
            }
          } else if (st.id === 'st4' && def.id === '场站-信息披露实际数据导出' && d === dMinus5) {
            cell.updated_today = true
            cell.prev_hash = 'yy_old'
            cell.mtime = `${todayIso} 11:41`
          } else if (st.id === 'st6' && d === cutoff.format('YYYY-MM-DD')) {
            cell = {
              status: 'miss',
              summary: '硬件阻断：未检测到湛江徐闻 UKey'
            }
          }
          cells[d] = cell
        }
        return { id: def.id, name: def.name, lag: def.lag, date_kind: 'day', cells }
      })
    }
  }

  return {
    today: todayIso,
    start: dates[0],
    end: dates[dates.length - 1],
    dates,
    months,
    has_real_config: false,
    download_dir: '',
    scopes,
    data
  }
}

function mergeMatrices(base: OverviewMatrixResult | null, incoming: OverviewMatrixResult): OverviewMatrixResult {
  if (!base) return incoming
  const dates = Array.from(new Set([...base.dates, ...incoming.dates])).sort()
  const months = Array.from(new Set([...(base.months || []), ...(incoming.months || [])])).sort()
  const mergedData: Record<string, { rows: OverviewRow[] }> = {}
  const scopeIds = new Set([...Object.keys(base.data), ...Object.keys(incoming.data)])

  for (const sid of scopeIds) {
    const baseRows = base.data[sid]?.rows || []
    const incRows = incoming.data[sid]?.rows || []
    const byId = new Map<string, OverviewRow>()
    for (const r of baseRows) byId.set(r.id, { ...r, cells: { ...r.cells } })
    for (const r of incRows) {
      const prev = byId.get(r.id)
      if (!prev) byId.set(r.id, { ...r, cells: { ...r.cells } })
      else byId.set(r.id, { ...r, cells: { ...prev.cells, ...r.cells } })
    }
    mergedData[sid] = { rows: Array.from(byId.values()) }
  }

  return {
    ...incoming,
    start: dates[0],
    end: dates[dates.length - 1],
    dates,
    months,
    scopes: incoming.scopes.length ? incoming.scopes : base.scopes,
    data: mergedData
  }
}

export default function DataOverview(): JSX.Element {
  const { message, modal } = AntApp.useApp()
  const todayIso = useMemo(() => dayjs().format('YYYY-MM-DD'), [])
  const [range, setRange] = useState(() => ({
    start: dayjs().subtract(45, 'day').format('YYYY-MM-DD'),
    end: dayjs().add(21, 'day').format('YYYY-MM-DD')
  }))
  const [matrix, setMatrix] = useState<OverviewMatrixResult | null>(null)
  const [demoMode, setDemoMode] = useState(false)
  const [hasRealConfig, setHasRealConfig] = useState(true)
  const [loading, setLoading] = useState(false)
  const [currentScope, setCurrentScope] = useState<string>('public')
  const [onlyTodayIssues, setOnlyTodayIssues] = useState(false)
  const [density, setDensity] = useState<Density>('normal')
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set())
  const [applyAllStations, setApplyAllStations] = useState(false)
  const [cartExpanded, setCartExpanded] = useState(false)
  const [tip, setTip] = useState<TipState | null>(null)
  const [ctxMenu, setCtxMenu] = useState<ContextMenuState | null>(null)
  const [packing, setPacking] = useState(false)
  const [packProgress, setPackProgress] = useState<{
    done: number
    total: number
    percent: number
    currentFile: string
  } | null>(null)
  const [catchupProgressOpen, setCatchupProgressOpen] = useState(false)
  const [refetchQueue, setRefetchQueue] = useState<Array<{ title: string; run: CatchupRun }>>([])
  const [refetchIdx, setRefetchIdx] = useState(0)

  // 无感横向滚动 refs
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const initialScrolledRef = useRef(false)
  const prevScrollWidthRef = useRef<number | null>(null)
  const extendingRef = useRef(false)
  const hasLoadedOnceRef = useRef(false)
  const rangeRef = useRef(range)
  rangeRef.current = range

  // 默认即开启 2D 跨行跨列矩形拖选（支持同时框选日度方格与月度长条）
  const dragRectRef = useRef<{
    active: boolean
    add: boolean
    anchorRow: number
    anchorColStart: number
    anchorColEnd: number
    baseKeys: Set<string>
  } | null>(null)

  const loadRange = useCallback(
    async (start: string, end: string, mode: 'replace' | 'merge', forceDemo?: boolean): Promise<void> => {
      const useDemo = forceDemo !== undefined ? forceDemo : demoMode
      if (useDemo) {
        hasLoadedOnceRef.current = true
        const built = buildDemoMatrix(start, end, todayIso)
        setMatrix((prev) => (mode === 'merge' ? mergeMatrices(prev, built) : built))
        return
      }
      setLoading(true)
      try {
        const res = await api.getOverviewMatrix(start, end)
        hasLoadedOnceRef.current = true
        setHasRealConfig(res.has_real_config)
        if (!res.has_real_config && forceDemo === undefined) {
          setDemoMode(true)
          const demo = buildDemoMatrix(start, end, todayIso)
          setMatrix(demo)
          return
        }
        setMatrix((prev) => (mode === 'merge' ? mergeMatrices(prev, res) : res))
      } catch {
        if (forceDemo === undefined && !hasLoadedOnceRef.current) {
          hasLoadedOnceRef.current = true
          setDemoMode(true)
          setHasRealConfig(false)
          setMatrix(buildDemoMatrix(start, end, todayIso))
        }
      } finally {
        setLoading(false)
      }
    },
    [demoMode, todayIso]
  )

  useEffect(() => {
    void loadRange(range.start, range.end, 'replace')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!matrix || !matrix.scopes.length) return
    if (currentScope === 'all_all') return
    if (!matrix.scopes.some((s) => s.id === currentScope)) {
      setCurrentScope(matrix.scopes[0].id)
    }
  }, [matrix, currentScope])

  const monthGroups = useMemo<MonthGroup[]>(() => {
    const dates = matrix?.dates || []
    const groups: MonthGroup[] = []
    dates.forEach((d, colIdx) => {
      const mk = d.slice(0, 7)
      const last = groups[groups.length - 1]
      if (!last || last.monthKey !== mk) {
        groups.push({
          monthKey: mk,
          year: Number(d.slice(0, 4)),
          month: Number(d.slice(5, 7)),
          days: [d],
          startCol: colIdx,
          endCol: colIdx
        })
      } else {
        last.days.push(d)
        last.endCol = colIdx
      }
    })
    return groups
  }, [matrix?.dates])

  const privateScopes = useMemo(
    () => (matrix?.scopes || []).filter((s) => s.kind === 'private'),
    [matrix?.scopes]
  )

  // 汇总今天（公有 + 全部场站）应到位水位的更新情况
  const todayOverview = useMemo(() => {
    const baseToday = dayjs(matrix?.today || todayIso)
    let total = 0
    let ok = 0
    let miss = 0
    let part = 0
    const scopeStats: Record<string, { ok: number; miss: number; part: number; total: number }> = {}
    const issueRowKeys = new Set<string>()
    const catchupTargets: Array<{
      scopeId: string
      owner: string
      indicatorId: string
      indicator: string
      period: string
      isMonth: boolean
      status: OverviewCell['status']
    }> = []

    for (const sc of matrix?.scopes || []) {
      const rows = matrix?.data[sc.id]?.rows || []
      let scOk = 0
      let scMiss = 0
      let scPart = 0

      for (const r of rows) {
        const isMonth = r.date_kind === 'month'
        const target = isMonth
          ? baseToday.add(r.lag, 'month').format('YYYY-MM')
          : baseToday.add(r.lag, 'day').format('YYYY-MM-DD')
        const cell = r.cells[target]
        if (!cell || cell.status === 'future') continue
        total++
        if (cell.status === 'ok') {
          ok++
          scOk++
        } else if (cell.status === 'miss') {
          miss++
          scMiss++
          issueRowKeys.add(`${sc.id}|${r.id}`)
          catchupTargets.push({
            scopeId: sc.id,
            owner: sc.name,
            indicatorId: r.id,
            indicator: r.name,
            period: target,
            isMonth,
            status: cell.status
          })
        } else {
          part++
          scPart++
          issueRowKeys.add(`${sc.id}|${r.id}`)
          catchupTargets.push({
            scopeId: sc.id,
            owner: sc.name,
            indicatorId: r.id,
            indicator: r.name,
            period: target,
            isMonth,
            status: cell.status
          })
        }
      }

      scopeStats[sc.id] = { ok: scOk, miss: scMiss, part: scPart, total: scOk + scMiss + scPart }
    }

    return { total, ok, miss, part, issueCount: miss + part, scopeStats, issueRowKeys, catchupTargets }
  }, [matrix, todayIso])

  const visibleGroups = useMemo(() => {
    if (!matrix) return []
    if (onlyTodayIssues) {
      return matrix.scopes
        .map((sc) => ({
          scope: sc,
          rows: (matrix.data[sc.id]?.rows || []).filter((r) =>
            todayOverview.issueRowKeys.has(`${sc.id}|${r.id}`)
          )
        }))
        .filter((g) => g.rows.length > 0)
    }
    if (currentScope === 'all_all') {
      return matrix.scopes.map((sc) => ({ scope: sc, rows: matrix.data[sc.id]?.rows || [] }))
    }
    const sc = matrix.scopes.find((s) => s.id === currentScope)
    if (!sc) return []
    return [{ scope: sc, rows: matrix.data[sc.id]?.rows || [] }]
  }, [matrix, currentScope, onlyTodayIssues, todayOverview.issueRowKeys])

  const flatRows = useMemo<FlatVisibleRow[]>(() => {
    const out: FlatVisibleRow[] = []
    for (const g of visibleGroups) {
      for (const r of g.rows) {
        out.push({ rowIndex: out.length, scope: g.scope, row: r })
      }
    }
    return out
  }, [visibleGroups])

  const rowIndexMap = useMemo(() => {
    const map = new Map<string, number>()
    for (const fr of flatRows) {
      map.set(`${fr.scope.id}|${fr.row.id}`, fr.rowIndex)
    }
    return map
  }, [flatRows])

  const scrollToToday = useCallback((smooth = false) => {
    const el = scrollRef.current
    if (!el || !matrix) return
    const todayIndex = matrix.dates.indexOf(matrix.today)
    if (todayIndex < 0) return
    const step = CELL_STEP[density]
    const visibleDateWidth = Math.max(200, el.clientWidth - 240)
    const futurePaddingPx = Math.min(visibleDateWidth * 0.3, 11 * step)
    const targetLeft = Math.max(0, todayIndex * step - (visibleDateWidth - futurePaddingPx))
    el.scrollTo({ left: targetLeft, behavior: smooth ? 'smooth' : 'auto' })
  }, [matrix, density])

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el || !matrix) return
    if (prevScrollWidthRef.current !== null) {
      const delta = el.scrollWidth - prevScrollWidthRef.current
      prevScrollWidthRef.current = null
      if (delta > 0) el.scrollLeft += delta
      extendingRef.current = false
      return
    }
    if (!initialScrolledRef.current && matrix.dates.length > 0) {
      initialScrolledRef.current = true
      scrollToToday(false)
    }
  }, [matrix?.dates.length, scrollToToday, matrix])

  const handleHorizontalScroll = useCallback(() => {
    setCtxMenu(null)
    const el = scrollRef.current
    if (!el || !matrix || extendingRef.current) return

    const threshold = 240
    if (el.scrollLeft < threshold && matrix.dates.length < 365) {
      extendingRef.current = true
      const curStart = rangeRef.current.start
      const newStart = dayjs(curStart).subtract(30, 'day').format('YYYY-MM-DD')
      const prevEnd = dayjs(curStart).subtract(1, 'day').format('YYYY-MM-DD')
      prevScrollWidthRef.current = el.scrollWidth
      setRange({ start: newStart, end: rangeRef.current.end })
      void loadRange(newStart, prevEnd, 'merge').finally(() => {
        window.setTimeout(() => { extendingRef.current = false }, 120)
      })
      return
    }

    const remainRight = el.scrollWidth - el.scrollLeft - el.clientWidth
    if (remainRight < threshold && matrix.dates.length < 365) {
      extendingRef.current = true
      const curEnd = rangeRef.current.end
      const nextStart = dayjs(curEnd).add(1, 'day').format('YYYY-MM-DD')
      const newEnd = dayjs(curEnd).add(30, 'day').format('YYYY-MM-DD')
      setRange({ start: rangeRef.current.start, end: newEnd })
      void loadRange(nextStart, newEnd, 'merge').finally(() => {
        window.setTimeout(() => { extendingRef.current = false }, 120)
      })
    }
  }, [matrix, loadRange])

  const handleWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    if (!e.shiftKey || !scrollRef.current) return
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault()
      scrollRef.current.scrollLeft += e.deltaY
    }
  }, [])

  const openNativeFile = useCallback(async (cell: OverviewCell, fallbackName: string): Promise<void> => {
    if (cell.status === 'miss') {
      message.warning('本地暂无该文件，可右键点击「重采」')
      return
    }
    if (cell.file_path && window.api?.openFile) {
      await window.api.openFile(cell.file_path)
    } else if (demoMode) {
      message.info(`演示文件：${cell.file_name || fallbackName}（真实本地文件将直接调用本机 Excel / WPS 打开）`)
    } else {
      message.warning('本地未找到该文件路径')
    }
  }, [demoMode, message])

  useEffect(() => {
    const onMouseUp = (): void => {
      dragRectRef.current = null
    }
    const onWindowClick = (): void => {
      setCtxMenu(null)
    }
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        if (ctxMenu) setCtxMenu(null)
        else if (cartExpanded) setCartExpanded(false)
      }
    }
    window.addEventListener('mouseup', onMouseUp)
    window.addEventListener('click', onWindowClick)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('mouseup', onMouseUp)
      window.removeEventListener('click', onWindowClick)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [ctxMenu, cartExpanded])

  const applyRectSelection = useCallback((
    baseKeys: Set<string>,
    add: boolean,
    r1: number,
    r2: number,
    cStart1: number,
    cEnd1: number,
    cStart2: number,
    cEnd2: number
  ) => {
    if (!matrix) return
    const rLo = Math.min(r1, r2)
    const rHi = Math.max(r1, r2)
    const cLo = Math.min(cStart1, cStart2)
    const cHi = Math.max(cEnd1, cEnd2)
    const next = new Set(baseKeys)

    for (let rIdx = rLo; rIdx <= rHi; rIdx++) {
      const fr = flatRows[rIdx]
      if (!fr) continue
      if (fr.row.date_kind === 'month') {
        for (const g of monthGroups) {
          if (g.endCol < cLo || g.startCol > cHi) continue
          const cell = fr.row.cells[g.monthKey]
          if (!cell || cell.status === 'future') continue
          const k = `${fr.scope.id}|${fr.row.id}|${g.monthKey}`
          if (add) next.add(k)
          else next.delete(k)
        }
      } else {
        for (let c = cLo; c <= cHi; c++) {
          const d = matrix.dates[c]
          if (!d) continue
          const cell = fr.row.cells[d]
          if (!cell || cell.status === 'future') continue
          const k = `${fr.scope.id}|${fr.row.id}|${d}`
          if (add) next.add(k)
          else next.delete(k)
        }
      }
    }
    setSelectedKeys(next)
  }, [matrix, flatRows, monthGroups])

  const onCellMouseDown = useCallback((
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    colStart: number,
    colEnd: number,
    cell: OverviewCell
  ) => {
    if (e.button !== 0 || cell.status === 'future') return
    setCtxMenu(null)
    const rowIndex = rowIndexMap.get(`${scope.id}|${row.id}`)
    if (rowIndex === undefined) return
    const key = `${scope.id}|${row.id}|${period}`
    const add = !selectedKeys.has(key)
    const baseKeys = new Set(selectedKeys)
    dragRectRef.current = {
      active: true,
      add,
      anchorRow: rowIndex,
      anchorColStart: colStart,
      anchorColEnd: colEnd,
      baseKeys
    }
    applyRectSelection(baseKeys, add, rowIndex, rowIndex, colStart, colEnd, colStart, colEnd)
  }, [rowIndexMap, selectedKeys, applyRectSelection])

  const onCellMouseEnter = useCallback((
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    colStart: number,
    colEnd: number,
    cell: OverviewCell
  ) => {
    setTip({
      x: Math.min(window.innerWidth - 318, e.clientX + 14),
      y: Math.min(window.innerHeight - 140, e.clientY + 14),
      owner: scope.name,
      indicator: row.name,
      period,
      isMonth: row.date_kind === 'month',
      cell
    })

    const drag = dragRectRef.current
    if (!drag || !drag.active) return
    const rowIndex = rowIndexMap.get(`${scope.id}|${row.id}`)
    if (rowIndex === undefined) return
    applyRectSelection(
      drag.baseKeys,
      drag.add,
      drag.anchorRow,
      rowIndex,
      drag.anchorColStart,
      drag.anchorColEnd,
      colStart,
      colEnd
    )
  }, [rowIndexMap, applyRectSelection])

  const onCellContextMenu = useCallback((
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    cell: OverviewCell
  ) => {
    e.preventDefault()
    if (cell.status === 'future') return
    setTip(null)
    const menuW = 224
    const menuH = 196
    setCtxMenu({
      x: Math.min(window.innerWidth - menuW - 8, e.clientX),
      y: Math.min(window.innerHeight - menuH - 8, e.clientY),
      scopeId: scope.id,
      ownerName: scope.name,
      row,
      period,
      cell
    })
  }, [])

  // 已选数据清单
  const selectedItems = useMemo(() => {
    if (!matrix || selectedKeys.size === 0) return []
    const out: Array<{
      key: string
      rawKey: string
      scopeId: string
      owner: string
      indicatorId: string
      indicator: string
      isMonth: boolean
      period: string
      cell: OverviewCell
    }> = []
    const dedup = new Set<string>()

    selectedKeys.forEach((rawKey) => {
      const [sid, indId, period] = rawKey.split('|')
      const sc = matrix.scopes.find((s) => s.id === sid)
      const targetScopes =
        applyAllStations && sc?.kind === 'private' ? privateScopes : sc ? [sc] : []
      for (const tsc of targetScopes) {
        const row = matrix.data[tsc.id]?.rows.find((r) => r.id === indId)
        const cell = row?.cells[period]
        if (!row || !cell || cell.status === 'future') continue
        const dk = `${tsc.id}|${row.id}|${period}`
        if (dedup.has(dk)) continue
        dedup.add(dk)
        out.push({
          key: dk,
          rawKey,
          scopeId: tsc.id,
          owner: tsc.name,
          indicatorId: row.id,
          indicator: row.name,
          isMonth: row.date_kind === 'month',
          period,
          cell
        })
      }
    })
    out.sort((a, b) => (a.scopeId === b.scopeId ? (a.indicatorId === b.indicatorId ? a.period.localeCompare(b.period) : a.indicator.localeCompare(b.indicator)) : a.scopeId.localeCompare(b.scopeId)))
    return out
  }, [matrix, selectedKeys, applyAllStations, privateScopes])

  const cartGroups = useMemo(() => {
    const map = new Map<string, {
      groupKey: string
      scopeId: string
      owner: string
      indicatorId: string
      indicator: string
      isMonth: boolean
      items: typeof selectedItems
    }>()
    for (const it of selectedItems) {
      const gk = `${it.scopeId}|${it.indicatorId}`
      const existing = map.get(gk)
      if (existing) existing.items.push(it)
      else {
        map.set(gk, {
          groupKey: gk,
          scopeId: it.scopeId,
          owner: it.owner,
          indicatorId: it.indicatorId,
          indicator: it.indicator,
          isMonth: it.isMonth,
          items: [it]
        })
      }
    }
    return Array.from(map.values())
  }, [selectedItems])

  const cartSummary = useMemo(() => {
    let ok = 0
    let part = 0
    let miss = 0
    let hasPrivate = false
    for (const item of selectedItems) {
      if (item.scopeId !== 'public') hasPrivate = true
      if (item.cell.status === 'ok') ok++
      else if (item.cell.status === 'miss') miss++
      else part++
    }
    return { total: selectedItems.length, ok, part, miss, hasPrivate }
  }, [selectedItems])

  const handlePackDownload = async (mode: 'zip' | 'folder'): Promise<void> => {
    if (selectedItems.length === 0 || packing) return

    // 始终调起本机文件夹选择器（与设置里的采集下载目录完全独立）
    const pickedDir = window.api?.pickDir ? await window.api.pickDir() : null
    if (!pickedDir) return

    const packLabel = `电力数据取数包_${dayjs().format('YYYYMMDD_HHmm')}`
    const packFilename = mode === 'zip' ? `${packLabel}.zip` : packLabel

    if (demoMode) {
      setPacking(true)
      const total = selectedItems.length
      setPackProgress({ done: 0, total, percent: 0, currentFile: '准备打包…' })
      for (let i = 0; i < total; i++) {
        const it = selectedItems[i]
        setPackProgress({
          done: i + 1,
          total,
          percent: Math.round(((i + 1) * 100) / total),
          currentFile: it.cell.file_name || `${it.owner}_${it.indicator}(${it.period}).xlsx`
        })
        await new Promise((r) => window.setTimeout(r, Math.max(18, Math.min(90, Math.round(900 / total)))))
      }
      setPacking(false)
      setPackProgress(null)
      message.success(
        mode === 'zip'
          ? `演示模式：已模拟打包 ${total} 份文件到 ${pickedDir}/${packFilename}`
          : `演示模式：已模拟导出 ${total} 份文件到 ${pickedDir}/${packFilename}`
      )
      setSelectedKeys(new Set())
      setCartExpanded(false)
      return
    }

    const available = selectedItems.filter((it) => it.cell.file_path)
    if (available.length === 0) {
      message.warning('所选格子里暂无本地文件，可先点击「重采缺口」下载')
      return
    }

    setPacking(true)
    setPackProgress({ done: 0, total: available.length, percent: 0, currentFile: '正在开始…' })
    try {
      const url = await backendUrl('/api/overview/pack/stream')
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: available.map((it) => ({
            owner: it.owner,
            indicator: it.indicator,
            date: it.period,
            file_path: it.cell.file_path || ''
          })),
          target_path: pickedDir,
          mode,
          layout: 'station_indicator',
          filename: packFilename
        })
      })
      if (!res.ok || !res.body) {
        throw new Error(`导出请求失败 (HTTP ${res.status})`)
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let finalResult: { ok?: boolean; count?: number; output?: string; error?: string } | null = null

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const ev = JSON.parse(line.slice(6)) as {
              event?: string
              done?: number
              total?: number
              percent?: number
              current_file?: string
              ok?: boolean
              count?: number
              output?: string
              error?: string
            }
            if (ev.event === 'progress') {
              setPackProgress({
                done: ev.done ?? 0,
                total: ev.total ?? available.length,
                percent: ev.percent ?? 0,
                currentFile: ev.current_file || ''
              })
            } else if (ev.event === 'done') {
              finalResult = ev
            }
          } catch { /* ignore malformed chunk */ }
        }
      }

      if (finalResult?.ok) {
        message.success(`已导出 ${finalResult.count ?? available.length} 份最新文件`)
        if (mode === 'zip' && finalResult.output && window.api?.revealFile) {
          await window.api.revealFile(finalResult.output)
        } else if (mode === 'folder' && finalResult.output && window.api?.openDir) {
          await window.api.openDir(finalResult.output)
        }
        setSelectedKeys(new Set())
        setCartExpanded(false)
      } else {
        message.error(finalResult?.error || '打包导出失败')
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '打包失败')
    } finally {
      setPacking(false)
      setPackProgress(null)
    }
  }

  const executeCatchupBatches = (
    items: Array<{
      scopeId: string
      owner: string
      indicatorId: string
      indicator: string
      period: string
      isMonth?: boolean
      status?: OverviewCell['status']
    }>,
    clearSelectionAfter = false
  ): void => {
    if (demoMode) {
      setMatrix((prev) => {
        if (!prev) return prev
        const nextData = { ...prev.data }
        for (const it of items) {
          const rows = nextData[it.scopeId]?.rows
          if (!rows) continue
          nextData[it.scopeId] = {
            rows: rows.map((r) =>
              r.id === it.indicatorId
                ? {
                    ...r,
                    cells: {
                      ...r.cells,
                      [it.period]: {
                        status: 'ok',
                        size: 152400,
                        mtime: `${todayIso} 刚刚`,
                        hash: 'f91a20cc',
                        updated_today: true,
                        file_name: `${r.name}(${it.period}).xlsx`
                      }
                    }
                  }
                : r
            )
          }
        }
        return { ...prev, data: nextData }
      })
      if (clearSelectionAfter) {
        setSelectedKeys(new Set())
        setCartExpanded(false)
      }
      message.success(`演示模式：已重采覆盖 ${items.length} 份数据`)
      return
    }

    // 按公有 / 各私有场站拆批串行执行，避免公有与不同场站 UKey 上下文混跑
    const byScope = new Map<string, { scopeId: string; owner: string; list: typeof items }>()
    for (const it of items) {
      const existing = byScope.get(it.scopeId)
      if (existing) existing.list.push(it)
      else byScope.set(it.scopeId, { scopeId: it.scopeId, owner: it.owner, list: [it] })
    }

    const scopeBatches = Array.from(byScope.values())
    const built: Array<{ title: string; run: CatchupRun }> = scopeBatches.map((grp, idx) => {
      const indicatorDates: Record<string, string[]> = {}
      const indicatorMonths: Record<string, string[]> = {}
      const indSet = new Set<string>()
      for (const it of grp.list) {
        indSet.add(it.indicatorId)
        if (it.isMonth || it.period.length === 7) {
          const list = indicatorMonths[it.indicatorId] || []
          if (!list.includes(it.period)) list.push(it.period)
          indicatorMonths[it.indicatorId] = list.sort()
        } else {
          const list = indicatorDates[it.indicatorId] || []
          if (!list.includes(it.period)) list.push(it.period)
          indicatorDates[it.indicatorId] = list.sort()
        }
      }
      const indicators = Array.from(indSet)
      const isPublic = grp.scopeId === 'public'
      const batchPrefix = scopeBatches.length > 1 ? `(${idx + 1}/${scopeBatches.length}) ` : ''
      return {
        title: `重采 ${batchPrefix}${grp.owner} · ${indicators.length} 项指标`,
        run: {
          login: '',
          indicators,
          indicatorDates,
          indicatorMonths,
          outputDates: {},
          uploadProfileId: '',
          autoUpload: false,
          downloadDir: matrix?.download_dir || '',
          pathTemplate: '',
          pathFolders: [],
          pathStructure: '',
          customFilenames: {},
          stationMode: isPublic ? 'any_one' : 'selected',
          ukeyStationIds: isPublic || grp.scopeId === 'private_default' ? undefined : [grp.scopeId],
          waitForHardware: true
        }
      }
    })

    if (clearSelectionAfter) {
      setSelectedKeys(new Set())
      setCartExpanded(false)
    }
    setRefetchQueue(built)
    setRefetchIdx(0)
    setCatchupProgressOpen(true)
  }

  const triggerCatchupForItems = (
    items: Array<{
      scopeId: string
      owner: string
      indicatorId: string
      indicator: string
      period: string
      isMonth?: boolean
      status?: OverviewCell['status']
    }>,
    clearSelectionAfter = false
  ): void => {
    if (items.length === 0) return
    const okCount = items.filter((it) => it.status === 'ok').length
    const scopeCounts = new Map<string, number>()
    for (const it of items) {
      const label = it.scopeId === 'public' ? '公有' : it.owner
      scopeCounts.set(label, (scopeCounts.get(label) || 0) + 1)
    }
    const scopeSummary = Array.from(scopeCounts.entries())
      .map(([name, cnt]) => `${name} ${cnt} 份`)
      .join(' · ')

    modal.confirm({
      title: `确认重采 ${items.length} 份数据？`,
      okText: '开始重采',
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.65, color: '#374151' }}>
          <div>范围：{scopeSummary}</div>
          {okCount > 0 && (
            <div style={{ color: '#256444' }}>
              其中 {okCount} 份当前已到位，重采后将对比哈希覆盖更新。
            </div>
          )}
          <div style={{ fontSize: 12, color: '#8c929a', marginTop: 4 }}>
            执行过程将显式打开浏览器；若当前有自动采集任务正在运行，会自动单轨排队等待完成后执行，不产生冲突。
          </div>
        </div>
      ),
      onOk: () => {
        executeCatchupBatches(items, clearSelectionAfter)
      }
    })
  }

  const totalColumns = (matrix?.dates.length || 0) + Math.max(0, monthGroups.length - 1) + 1

  const statusLabel = (s: OverviewCell['status']): string => {
    if (s === 'ok') return '完整到位'
    if (s === 'partial') return '部分有数（缺 Sheet 或缺列）'
    if (s === 'empty') return '空表头 / 全 0'
    if (s === 'miss') return '缺失未到'
    return '未到水位期'
  }

  return (
    <div className="overview-page">
      {/* 顶部控制条 */}
      <div className="overview-toolbar">
        <div className="overview-scopes">
          {(matrix?.scopes || []).map((sc, idx) => {
            const prev = idx > 0 ? matrix?.scopes[idx - 1] : null
            const needSep = prev && prev.kind !== sc.kind
            const st = todayOverview.scopeStats[sc.id]
            const lvl: 'err' | 'warn' | 'ok' = st?.miss ? 'err' : st?.part ? 'warn' : 'ok'
            const tipText = st
              ? `今日到位 ${st.ok}/${st.total}${st.miss ? ` · 缺 ${st.miss}` : ''}${st.part ? ` · 空/残 ${st.part}` : ''}`
              : sc.name
            return (
              <span key={sc.id} style={{ display: 'inline-flex', alignItems: 'center' }}>
                {needSep && <span className="overview-scope-sep" />}
                <button
                  type="button"
                  title={tipText}
                  className={`overview-scope-btn${!onlyTodayIssues && currentScope === sc.id ? ' active' : ''}`}
                  onClick={() => {
                    setOnlyTodayIssues(false)
                    setCurrentScope(sc.id)
                  }}
                >
                  <i className={`overview-dot ${lvl}`} />
                  {sc.name}
                </button>
              </span>
            )
          })}
          {(matrix?.scopes.length || 0) > 1 && (
            <>
              <span className="overview-scope-sep" />
              <button
                type="button"
                className={`overview-scope-btn${!onlyTodayIssues && currentScope === 'all_all' ? ' active' : ''}`}
                onClick={() => {
                  setOnlyTodayIssues(false)
                  setCurrentScope('all_all')
                }}
              >
                全部总览
              </button>
            </>
          )}
        </div>

        <Space size={8} wrap>
          <Segmented
            size="small"
            value={density}
            onChange={(v) => setDensity(v as Density)}
            options={[
              { label: '密', value: 'compact' },
              { label: '标准', value: 'normal' },
              { label: '大', value: 'large' }
            ]}
          />
          <Button size="small" icon={<AimOutlined />} onClick={() => scrollToToday(true)}>
            回到今天
          </Button>
          <Tooltip title={hasRealConfig ? '切换真实本地文件与演示数据' : '本地尚无配置任务，当前展示演示数据'}>
            <Space size={4} style={{ fontSize: 12, color: '#59606b' }}>
              <span>演示</span>
              <Switch
                size="small"
                checked={demoMode}
                onChange={(checked) => {
                  setDemoMode(checked)
                  initialScrolledRef.current = false
                  setSelectedKeys(new Set())
                  setCartExpanded(false)
                  void loadRange(range.start, range.end, 'replace', checked)
                }}
              />
            </Space>
          </Tooltip>
          <Button
            size="small"
            icon={<ReloadOutlined spin={loading} />}
            onClick={() => void loadRange(range.start, range.end, 'replace')}
          />
        </Space>
      </div>

      {/* 主热力矩阵卡片 */}
      <div className="overview-card">
        <div className="overview-legend-bar">
          <div className="overview-legend-items">
            <span className="overview-lg"><i className="overview-lg-box ok" /> 完整到位</span>
            <span className="overview-lg"><i className="overview-lg-box partial" /> 部分有数（缺 Sheet/列）</span>
            <span className="overview-lg"><i className="overview-lg-box empty" /> 空表头 / 全 0</span>
            <span className="overview-lg"><i className="overview-lg-box miss" /> 缺失未到</span>
            <span className="overview-lg"><i className="overview-lg-box future" /> 未到水位期</span>
            <span className="overview-lg"><i className="overview-lg-box upd" /> 今日覆盖更新</span>
          </div>
          {todayOverview.total > 0 && (
            <div className="overview-legend-right">
              <span className="overview-today-text">
                今日到位 <strong>{todayOverview.ok}/{todayOverview.total}</strong>
              </span>
              {todayOverview.issueCount > 0 && (
                <>
                  <button
                    type="button"
                    className={`overview-anomaly-btn${onlyTodayIssues ? ' active' : ''}`}
                    onClick={() => setOnlyTodayIssues((v) => !v)}
                  >
                    <i className={`overview-dot ${todayOverview.miss > 0 ? 'err' : 'warn'}`} />
                    {onlyTodayIssues ? '返回全部' : `只看今日异常 (${todayOverview.issueCount})`}
                  </button>
                  {onlyTodayIssues && (
                    <button
                      type="button"
                      className="overview-anomaly-btn"
                      onClick={() => triggerCatchupForItems(todayOverview.catchupTargets)}
                    >
                      <SyncOutlined /> 重采全部 ({todayOverview.catchupTargets.length})
                    </button>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        <div
          ref={scrollRef}
          className="overview-scroll"
          onScroll={handleHorizontalScroll}
          onWheel={handleWheel}
        >
          <table className={`overview-table density-${density}`}>
            <thead>
              <tr>
                <th className="col-ind" rowSpan={2}>
                  {onlyTodayIssues
                    ? `今日异常汇总（共 ${todayOverview.issueCount} 项）`
                    : currentScope === 'all_all'
                      ? `全部总览（公有 + ${privateScopes.length} 场站）`
                      : currentScope === 'public'
                        ? '公有指标（外网上传 · 留 30 天）'
                        : `${matrix?.scopes.find((s) => s.id === currentScope)?.name || '私有'} · 指标（留 60 天）`}
                </th>
                {monthGroups.map((g, gi) => (
                  <FragmentMonthHeader key={g.monthKey} group={g} withGap={gi > 0} />
                ))}
              </tr>
              <tr>
                {monthGroups.map((g, gi) => (
                  <FragmentDayHeader
                    key={`${g.monthKey}-days`}
                    group={g}
                    withGap={gi > 0}
                    todayIso={matrix?.today || todayIso}
                    density={density}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleGroups.length === 0 ? (
                <tr>
                  <td colSpan={Math.max(2, totalColumns)} style={{ padding: 36, textAlign: 'center', color: '#8c929a' }}>
                    当前还没有已配置的采集任务，可先在右上角开启「演示」预览效果，或到「任务配置」新建任务。
                  </td>
                </tr>
              ) : (
                visibleGroups.map(({ scope, rows }) => (
                  <FragmentScopeRows
                    key={scope.id}
                    scope={scope}
                    rows={rows}
                    showGroupHeader={onlyTodayIssues || currentScope === 'all_all'}
                    monthGroups={monthGroups}
                    selectedKeys={selectedKeys}
                    totalColumns={totalColumns}
                    onMouseDown={onCellMouseDown}
                    onMouseEnter={onCellMouseEnter}
                    onMouseLeave={() => setTip(null)}
                    onContextMenu={onCellContextMenu}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 跟随鼠标的轻量 Tooltip */}
      {tip && !ctxMenu && (
        <div className="overview-tooltip" style={{ left: tip.x, top: tip.y }}>
          <div style={{ fontWeight: 600 }}>{tip.indicator}</div>
          <div className="overview-tooltip-sub">
            {(onlyTodayIssues || currentScope === 'all_all') ? `${tip.owner} · ` : ''}
            {tip.isMonth ? `${tip.period}（月度）` : tip.period} · {statusLabel(tip.cell.status)}
            {tip.cell.updated_today ? ' · 今日已覆盖' : ''}
          </div>
          {tip.cell.summary && (
            <div style={{ marginTop: 4, color: '#f8d488' }}>{tip.cell.summary}</div>
          )}
          {tip.cell.status !== 'miss' && tip.cell.status !== 'future' && (
            <div className="overview-tooltip-sub" style={{ marginTop: 2 }}>
              {formatBytes(tip.cell.size)} · {tip.cell.mtime || ''}
            </div>
          )}
        </div>
      )}

      {/* 极简方格右键菜单 */}
      {ctxMenu && (
        <div
          className="overview-ctx-menu"
          style={{ left: ctxMenu.x, top: ctxMenu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="overview-ctx-header">
            <div className="overview-ctx-title" title={ctxMenu.row.name}>{ctxMenu.row.name}</div>
            <div className="overview-ctx-sub">
              {(onlyTodayIssues || currentScope === 'all_all') ? `${ctxMenu.ownerName} · ` : ''}
              {ctxMenu.row.date_kind === 'month' ? `${ctxMenu.period}（月度）` : ctxMenu.period}
            </div>
          </div>
          <button
            type="button"
            className="overview-ctx-item"
            disabled={ctxMenu.cell.status === 'miss'}
            onClick={() => {
              const target = ctxMenu
              setCtxMenu(null)
              void openNativeFile(target.cell, target.row.name)
            }}
          >
            <FileExcelOutlined /> 打开文件
          </button>
          <button
            type="button"
            className="overview-ctx-item"
            disabled={!ctxMenu.cell.file_path}
            onClick={() => {
              const fp = ctxMenu.cell.file_path
              setCtxMenu(null)
              if (fp && window.api?.revealFile) void window.api.revealFile(fp)
            }}
          >
            <FolderOpenOutlined /> 打开所在目录
          </button>
          <div className="overview-ctx-sep" />
          <button
            type="button"
            className="overview-ctx-item"
            onClick={() => {
              const target = ctxMenu
              setCtxMenu(null)
              triggerCatchupForItems([{
                scopeId: target.scopeId,
                owner: target.ownerName,
                indicatorId: target.row.id,
                indicator: target.row.name,
                period: target.period,
                isMonth: target.row.date_kind === 'month',
                status: target.cell.status
              }])
            }}
          >
            <SyncOutlined /> 重采
          </button>
          <button
            type="button"
            className="overview-ctx-item"
            disabled={!ctxMenu.cell.file_path && !ctxMenu.cell.file_name}
            onClick={() => {
              const text = ctxMenu.cell.file_path || ctxMenu.cell.file_name || ''
              setCtxMenu(null)
              if (text && navigator.clipboard?.writeText) {
                void navigator.clipboard.writeText(text)
                message.success('已复制文件路径')
              }
            }}
          >
            <CopyOutlined /> 复制文件路径
          </button>
        </div>
      )}

      {/* 右下角极简悬浮胶囊 + 点击向上展开清单详情 */}
      {(cartSummary.total > 0 || packProgress) && (
        <div className="overview-cart-dock">
          {cartExpanded && (
            <div className="overview-cart-panel">
              <div className="overview-cart-panel-head">
                <span style={{ fontWeight: 600, fontSize: 13 }}>
                  待下载数据清单（{cartSummary.total} 份）
                </span>
                <Space size={4}>
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => {
                      setSelectedKeys(new Set())
                      setApplyAllStations(false)
                      setCartExpanded(false)
                    }}
                  >
                    清空
                  </Button>
                  <Button
                    type="text"
                    size="small"
                    icon={<CloseOutlined />}
                    onClick={() => setCartExpanded(false)}
                  />
                </Space>
              </div>

              <div className="overview-cart-panel-body">
                {cartGroups.map((g) => (
                  <div key={g.groupKey} className="overview-cart-group">
                    <div className="overview-cart-group-head">
                      <div className="overview-cart-group-title" title={`${g.owner} · ${g.indicator}`}>
                        <Tag bordered={false} style={{ margin: 0, fontSize: 11 }}>{g.owner}</Tag>
                        <span>{g.indicator}</span>
                        <span style={{ fontSize: 11, color: '#8c929a', fontWeight: 400 }}>({g.items.length})</span>
                      </div>
                      <Button
                        type="text"
                        size="small"
                        icon={<CloseOutlined style={{ fontSize: 11 }} />}
                        onClick={() => {
                          setSelectedKeys((prev) => {
                            const next = new Set(prev)
                            for (const it of g.items) next.delete(it.rawKey)
                            return next
                          })
                        }}
                      />
                    </div>
                    <div className="overview-cart-chips">
                      {g.items.map((it) => {
                        const shortTag =
                          it.cell.status === 'partial'
                            ? '残缺'
                            : it.cell.status === 'empty'
                              ? '空表'
                              : it.cell.status === 'miss'
                                ? '缺失'
                                : ''
                        return (
                          <span
                            key={it.key}
                            className={`overview-cart-chip ${it.cell.status}`}
                            title={`${it.period} · ${statusLabel(it.cell.status)}`}
                          >
                            <span>{it.isMonth ? `${it.period} 月` : it.period.slice(5)}</span>
                            {shortTag && <span className="overview-cart-chip-tag">{shortTag}</span>}
                            <button
                              type="button"
                              className="overview-cart-chip-del"
                              aria-label="移除"
                              onClick={() => {
                                setSelectedKeys((prev) => {
                                  const next = new Set(prev)
                                  next.delete(it.rawKey)
                                  return next
                                })
                              }}
                            >
                              ×
                            </button>
                          </span>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>

              <div className="overview-cart-panel-foot">
                {cartSummary.hasPrivate && privateScopes.length > 1 && (
                  <label className="overview-cart-check">
                    <input
                      type="checkbox"
                      checked={applyAllStations}
                      onChange={(e) => setApplyAllStations(e.target.checked)}
                    />
                    <span>同步打包全部 {privateScopes.length} 个场站相同区间</span>
                  </label>
                )}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <Space size={6}>
                    <Button
                      size="small"
                      icon={<SyncOutlined />}
                      onClick={() =>
                        triggerCatchupForItems(
                          selectedItems.map((it) => ({
                            scopeId: it.scopeId,
                            owner: it.owner,
                            indicatorId: it.indicatorId,
                            indicator: it.indicator,
                            period: it.period,
                            isMonth: it.isMonth,
                            status: it.cell.status
                          })),
                          true
                        )
                      }
                    >
                      重采已选 ({cartSummary.total})
                    </Button>
                    <Button
                      size="small"
                      loading={packing}
                      icon={<FolderOpenOutlined />}
                      onClick={() => void handlePackDownload('folder')}
                    >
                      导出文件夹
                    </Button>
                  </Space>
                  <Button
                    size="small"
                    type="primary"
                    loading={packing}
                    icon={<DownloadOutlined />}
                    onClick={() => void handlePackDownload('zip')}
                  >
                    一键打包 ZIP
                  </Button>
                </div>
              </div>
            </div>
          )}

          <div className="overview-cart-pill">
            {packProgress ? (
              <div className="overview-pack-progress">
                <div className="overview-pack-progress-info">
                  <div className="overview-pack-progress-text">
                    <span style={{ fontWeight: 600 }}>
                      正在导出 {packProgress.done}/{packProgress.total}
                    </span>
                    <span style={{ color: '#93c5fd', fontWeight: 600 }}>{packProgress.percent}%</span>
                  </div>
                  <div className="overview-pack-bar">
                    <div className="overview-pack-bar-fill" style={{ width: `${packProgress.percent}%` }} />
                  </div>
                  {packProgress.currentFile && (
                    <div className="overview-pack-progress-file" title={packProgress.currentFile}>
                      {packProgress.currentFile}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <>
                <button
                  type="button"
                  className="overview-cart-pill-trigger"
                  onClick={() => setCartExpanded((v) => !v)}
                >
                  <span className="overview-cart-pill-badge">{cartSummary.total}</span>
                  <span style={{ fontWeight: 600 }}>已选 {cartSummary.total} 份数据</span>
                  {cartExpanded ? <DownOutlined style={{ fontSize: 11 }} /> : <UpOutlined style={{ fontSize: 11 }} />}
                </button>

                {cartSummary.ok === 0 ? (
                  <Button
                    size="small"
                    type="primary"
                    shape="round"
                    icon={<SyncOutlined />}
                    onClick={() =>
                      triggerCatchupForItems(
                        selectedItems.map((it) => ({
                          scopeId: it.scopeId,
                          owner: it.owner,
                          indicatorId: it.indicatorId,
                          indicator: it.indicator,
                          period: it.period,
                          isMonth: it.isMonth,
                          status: it.cell.status
                        })),
                        true
                      )
                    }
                  >
                    重采
                  </Button>
                ) : (
                  <Button
                    size="small"
                    type="primary"
                    shape="round"
                    loading={packing}
                    icon={<DownloadOutlined />}
                    onClick={() => void handlePackDownload('zip')}
                  >
                    打包下载
                  </Button>
                )}
              </>
            )}
          </div>
        </div>
      )}

      <ProgressModal
        open={catchupProgressOpen && Boolean(refetchQueue[refetchIdx])}
        workflowName="补数据"
        title={refetchQueue[refetchIdx]?.title || '重采数据'}
        headless={false}
        minimized={false}
        isTask={true}
        catchup={refetchQueue[refetchIdx]?.run || null}
        suppressResultModal={true}
        autoCloseOnFinish={true}
        onAutoFinish={() => {
          if (refetchIdx + 1 < refetchQueue.length) {
            setRefetchIdx((i) => i + 1)
          } else {
            setCatchupProgressOpen(false)
            setRefetchQueue([])
            setRefetchIdx(0)
            void loadRange(range.start, range.end, 'replace')
          }
        }}
        onClose={() => {
          setCatchupProgressOpen(false)
          setRefetchQueue([])
          setRefetchIdx(0)
          void loadRange(range.start, range.end, 'replace')
        }}
      />
    </div>
  )
}

function FragmentMonthHeader({ group, withGap }: { group: MonthGroup; withGap: boolean }): JSX.Element {
  const label = group.days.length >= 3 ? `${group.year}年${group.month}月` : `${group.month}月`
  return (
    <>
      {withGap && <th className="month-gap" />}
      <th className="col-month" colSpan={group.days.length}>{label}</th>
    </>
  )
}

function FragmentDayHeader({
  group,
  withGap,
  todayIso,
  density
}: {
  group: MonthGroup
  withGap: boolean
  todayIso: string
  density: Density
}): JSX.Element {
  return (
    <>
      {withGap && <th className="month-gap" />}
      {group.days.map((d) => {
        const dayNum = Number(d.slice(8, 10))
        const isToday = d === todayIso
        const text =
          density === 'compact'
            ? (isToday || dayNum === 1 || dayNum % 2 === 1 ? d.slice(8, 10) : '')
            : d.slice(8, 10)
        return (
          <th key={d} className={`col-day${isToday ? ' today-col' : ''}`} title={d}>
            {text}
          </th>
        )
      })}
    </>
  )
}

function FragmentScopeRows({
  scope,
  rows,
  showGroupHeader,
  monthGroups,
  selectedKeys,
  totalColumns,
  onMouseDown,
  onMouseEnter,
  onMouseLeave,
  onContextMenu
}: {
  scope: OverviewScope
  rows: OverviewRow[]
  showGroupHeader: boolean
  monthGroups: MonthGroup[]
  selectedKeys: Set<string>
  totalColumns: number
  onMouseDown: (
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    colStart: number,
    colEnd: number,
    cell: OverviewCell
  ) => void
  onMouseEnter: (
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    colStart: number,
    colEnd: number,
    cell: OverviewCell
  ) => void
  onMouseLeave: () => void
  onContextMenu: (
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    cell: OverviewCell
  ) => void
}): JSX.Element {
  return (
    <>
      {showGroupHeader && (
        <tr className="overview-group-row">
          <td className="col-ind">
            {scope.slot ? `0${scope.slot} ` : ''}
            {scope.name}
          </td>
          <td className="group-fill" colSpan={Math.max(1, totalColumns - 1)} />
        </tr>
      )}
      {rows.map((row) => {
        const isMonth = row.date_kind === 'month'
        return (
          <tr key={`${scope.id}-${row.id}`}>
            <td className="col-ind">
              <div className="overview-ind-wrap">
                <span className="overview-ind-name" title={row.name}>{row.name}</span>
                <span className={`overview-ind-lag${isMonth ? ' month' : row.lag > 0 ? ' pos' : ''}`}>
                  {formatOffsetLabel(row.lag, row.date_kind)}
                </span>
              </div>
            </td>
            {monthGroups.map((g, gi) => (
              <FragmentGroupCells
                key={`${scope.id}-${row.id}-${g.monthKey}`}
                scope={scope}
                row={row}
                group={g}
                withGap={gi > 0}
                selectedKeys={selectedKeys}
                onMouseDown={onMouseDown}
                onMouseEnter={onMouseEnter}
                onMouseLeave={onMouseLeave}
                onContextMenu={onContextMenu}
              />
            ))}
          </tr>
        )
      })}
    </>
  )
}

function monthBarLabel(month: number, status: OverviewCell['status'], daysLen: number): string {
  if (daysLen < 4) return ''
  const suffix =
    status === 'ok'
      ? '已到位'
      : status === 'partial'
        ? '部分有数'
        : status === 'empty'
          ? '空表头'
          : status === 'miss'
            ? '缺失'
            : '未出账'
  return daysLen >= 8 ? `${month}月 · ${suffix}` : `${month}月`
}

function FragmentGroupCells({
  scope,
  row,
  group,
  withGap,
  selectedKeys,
  onMouseDown,
  onMouseEnter,
  onMouseLeave,
  onContextMenu
}: {
  scope: OverviewScope
  row: OverviewRow
  group: MonthGroup
  withGap: boolean
  selectedKeys: Set<string>
  onMouseDown: (
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    colStart: number,
    colEnd: number,
    cell: OverviewCell
  ) => void
  onMouseEnter: (
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    colStart: number,
    colEnd: number,
    cell: OverviewCell
  ) => void
  onMouseLeave: () => void
  onContextMenu: (
    e: React.MouseEvent,
    scope: OverviewScope,
    row: OverviewRow,
    period: string,
    cell: OverviewCell
  ) => void
}): JSX.Element {
  if (row.date_kind === 'month') {
    const cell: OverviewCell = row.cells[group.monthKey] || { status: 'future' }
    const key = `${scope.id}|${row.id}|${group.monthKey}`
    const isSelected = selectedKeys.has(key)
    const cls = `hm-month-bar ${cell.status}${cell.updated_today ? ' updated' : ''}${isSelected ? ' selected' : ''}`
    return (
      <>
        {withGap && <td className="month-gap" />}
        <td className="hm-month-td" colSpan={group.days.length}>
          <div
            className={cls}
            onMouseDown={(e) => onMouseDown(e, scope, row, group.monthKey, group.startCol, group.endCol, cell)}
            onMouseEnter={(e) => onMouseEnter(e, scope, row, group.monthKey, group.startCol, group.endCol, cell)}
            onMouseLeave={onMouseLeave}
            onContextMenu={(e) => onContextMenu(e, scope, row, group.monthKey, cell)}
          >
            <span className="hm-month-label">{monthBarLabel(group.month, cell.status, group.days.length)}</span>
          </div>
        </td>
      </>
    )
  }

  return (
    <>
      {withGap && <td className="month-gap" />}
      {group.days.map((d, dayOffset) => {
        const colIdx = group.startCol + dayOffset
        const cell: OverviewCell = row.cells[d] || { status: 'future' }
        const key = `${scope.id}|${row.id}|${d}`
        const isSelected = selectedKeys.has(key)
        const cls = `hm-sq ${cell.status}${cell.updated_today ? ' updated' : ''}${isSelected ? ' selected' : ''}`
        return (
          <td key={d}>
            <div
              className={cls}
              onMouseDown={(e) => onMouseDown(e, scope, row, d, colIdx, colIdx, cell)}
              onMouseEnter={(e) => onMouseEnter(e, scope, row, d, colIdx, colIdx, cell)}
              onMouseLeave={onMouseLeave}
              onContextMenu={(e) => onContextMenu(e, scope, row, d, cell)}
            />
          </td>
        )
      })}
    </>
  )
}
