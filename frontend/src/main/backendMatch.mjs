/** 决定新界面该接上、换掉，还是放过端口上已有的进程。 */

export function normalizePath(value) {
  return String(value || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
}

export function ownServiceName(executable) {
  const base = normalizePath(executable).split('\\').pop() || ''
  return base === 'spider-gd-service.exe' || base === 'spider-gd-service'
}

/**
 * @param {{ packaged: boolean, expectedExe: string, health: { executable?: string, pid?: number, spider_gd?: string } | null }} input
 */
export function backendDecision(input) {
  const health = input.health
  if (!health) return { action: 'start' }
  const expected = normalizePath(input.expectedExe)
  const actual = normalizePath(health.executable)
  if (input.packaged && expected && actual === expected) {
    return { action: 'attach', reason: '本次安装的后端已在运行' }
  }
  if (!input.packaged) {
    return { action: 'attach', reason: '开发模式沿用已启动的后端' }
  }
  if (ownServiceName(health.executable) && health.pid) {
    return {
      action: 'replace',
      pid: health.pid,
      reason: `端口上是另一份采集后端 ${health.executable || ''}（${health.spider_gd || '未知版本'}）`,
    }
  }
  return {
    action: 'error',
    reason: `8800 端口上的程序不是本次安装的采集后端：${health.executable || '未知路径'}，没有关闭它`,
  }
}
