import type { ThemeConfig } from 'antd'

export const theme: ThemeConfig = {
  token: {
    colorPrimary: '#1F5FFF',
    colorSuccess: '#10B981',
    colorWarning: '#F59E0B',
    colorError: '#EF4444',
    colorBgLayout: '#F5F6F4',
    borderRadius: 10,
    fontFamily:
      "'Noto Sans SC', system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif"
  },
  components: {
    Layout: {
      siderBg: '#0E1116',
      headerBg: '#FFFFFF',
      headerHeight: 48,
      headerPadding: '0 20px'
    },
    Menu: {
      darkItemBg: '#0E1116',
      darkItemSelectedBg: '#1F5FFF'
    }
  }
}
