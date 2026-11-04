export interface DataType {
  key: string
  name: string
  schedule: string
}

// 与设计稿一致的固定数据类型清单（MVP 写死，后续可从后端配置拉取）
export const DATA_TYPES: DataType[] = [
  { key: 'dayahead', name: '日前市场出清价格', schedule: '每日 14:00, 16:00' },
  { key: 'realtime', name: '实时电价数据', schedule: '每日 14:00, 16:00' },
  { key: 'monthly', name: '月度交易结算', schedule: '每月 1 日 09:00' },
  { key: 'midlong', name: '中长期交易数据', schedule: '每周一 10:00' },
  { key: 'contract', name: '合同电量分解', schedule: '每日 09:00' }
]

export const STATUS_COLOR: Record<string, string> = {
  completed: 'success',
  running: 'processing',
  failed: 'error',
  pending: 'default'
}
