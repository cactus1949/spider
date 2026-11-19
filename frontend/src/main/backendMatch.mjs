/** 决定新界面该接上、换掉，还是放过端口上已有的进程。 */

export function normalizePath(value) {
  return String(value || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
}

export function ownServiceName(executable) {
  const base = normalizePath(executable).split('\\').pop() || ''
  return base === 'spider-gd-service.exe' || base === 'spider-gd-service'
}

/**
 * 终止前必须用操作系统看到的监听进程，不能只信接口里自报的 pid。
 * @param {{ reportedPid?: number, reportedExe?: string, listener?: { pid?: number, executable?: string } | null }} input
 */
export function killPlan(input) {
  const listener = input.listener
  if (!listener || !listener.pid || !listener.executable) {
    return { kill: false, reason: '读不到正在监听端口的程序，没有关闭它' }
  }
  if (!ownServiceName(listener.executable)) {
    return { kill: false, reason: `监听端口的是 ${listener.executable}，不是采集后端，没有关闭它` }
  }
  if (input.reportedPid && Number(listener.pid) !== Number(input.reportedPid)) {
    return { kill: false, reason: '接口里的进程号和实际监听进程不一致，没有关闭它' }
  }
  if (input.reportedExe && normalizePath(input.reportedExe) !== normalizePath(listener.executable)) {
    return { kill: false, reason: '接口里的程序路径和实际监听进程不一致，没有关闭它' }
  }
  return { kill: true, pid: Number(listener.pid), executable: listener.executable }
}

/**
 * 旧进程退出后，端口上如果换成了别的 pid，不能再拿原来的 pid 去杀。
 * @param {{ oldPid: number, listener?: { pid?: number, executable?: string } | null }} input
 */
export function portAfterStop(input) {
  const listener = input.listener
  if (!listener || !listener.pid) return { state: 'clear' }
  if (!ownServiceName(listener.executable)) {
    return {
      state: 'taken',
      reason: `原进程退出后，端口改由 ${listener.executable || '未知程序'} 监听，没有继续关闭`,
    }
  }
  if (Number(listener.pid) === Number(input.oldPid)) {
    return { state: 'still-ours', pid: Number(listener.pid), executable: listener.executable }
  }
  return { state: 'child', pid: Number(listener.pid), executable: listener.executable }
}

export function startupPort(input) {
  const decision = backendDecision(input)
  const preferred = Number(input.preferredPort) || 8800
  if (decision.action === 'attach' || decision.action === 'replace') {
    return { ...decision, port: preferred }
  }
  if (input.health && input.health.runtime === 'ok') {
    return { ...decision, port: preferred }
  }
  const listener = input.listener
  const occupiedByOther = Boolean(
    listener && listener.pid && listener.executable && !ownServiceName(listener.executable)
  )
  if (occupiedByOther || (input.health && input.health.runtime === 'foreign')) {
    const owner = listener && listener.executable ? listener.executable : '其他程序'
    return {
      action: 'start',
      port: 0,
      reason: `8800 已被 ${owner} 占用，改用空闲端口，没有关闭它`,
    }
  }
  if (decision.action === 'error') return { ...decision, port: preferred }
  return { action: 'start', port: preferred, reason: decision.reason || '' }
}

/**
 * @param {{
 *   packaged: boolean,
 *   expectedExe: string,
 *   expectedBuild: string,
 *   health: { runtime?: string, executable?: string, pid?: number, spider_gd?: string, build_id?: string } | null,
 *   listener?: { pid?: number, executable?: string } | null,
 *   preferredPort?: number,
 * }} input
 */
export function backendDecision(input) {
  const health = input.health
  if (!health) return { action: 'start' }
  if (!input.packaged) {
    if (health.runtime === 'foreign') {
      return { action: 'error', reason: '8800 端口上的接口不是采集后端，没有关闭它' }
    }
    return { action: 'attach', reason: '开发模式沿用已启动的后端' }
  }
  if (health.runtime === 'foreign') {
    return { action: 'error', reason: '8800 端口上的接口不是采集后端，没有关闭它' }
  }

  const plan = killPlan({
    reportedPid: health.runtime === 'ok' ? health.pid : undefined,
    reportedExe: health.runtime === 'ok' ? health.executable : undefined,
    listener: input.listener,
  })

  if (health.runtime === 'missing') {
    if (!plan.kill) {
      return { action: 'error', reason: `旧版采集后端没有身份接口。${plan.reason}` }
    }
    return {
      action: 'replace',
      pid: plan.pid,
      executable: plan.executable,
      reason: '旧版采集后端没有身份接口，将按监听进程结束它',
    }
  }

  const samePath = Boolean(input.expectedExe)
    && normalizePath(health.executable) === normalizePath(input.expectedExe)
  const sameBuild = Boolean(input.expectedBuild) && health.build_id === input.expectedBuild
  if (samePath && sameBuild) {
    if (!plan.kill && input.listener) {
      return { action: 'error', reason: plan.reason }
    }
    return { action: 'attach', reason: '本次安装的这份构建已在运行' }
  }
  if (!plan.kill) {
    return {
      action: 'error',
      reason: `正在跑的构建（${health.build_id || health.spider_gd || '未知'}）不是本次安装（${input.expectedBuild || '未知'}）。${plan.reason}`,
    }
  }
  return {
    action: 'replace',
    pid: plan.pid,
    executable: plan.executable,
    reason: `同一安装路径上仍是旧构建 ${health.build_id || health.spider_gd || ''}，将先结束它`,
  }
}
