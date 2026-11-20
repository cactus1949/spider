import { realpathSync } from 'node:fs'
import { win32, posix } from 'node:path'

/** Resolve symlinks/junctions when present; POSIX identity remains case-sensitive. */
export function normalizePath(value) {
  if (!value) return ''
  let path = String(value)
  try { path = realpathSync.native(path) } catch { /* legacy/missing path: compare lexically */ }
  const windows = /^[a-z]:[\\/]|^\\\\/i.test(path)
  return windows
    ? win32.normalize(path).replace(/[\\/]+$/, '').toLowerCase()
    : posix.normalize(path).replace(/\/+$/, '') || '/'
}

export function ownServiceName(executable) {
  return /^spider-gd-service(?:-beta)?(?:\.exe)?$/i.test(String(executable || '').split(/[\\/]/).pop() || '')
}

export function killPlan(input) {
  const listener = input.listener
  if (!listener?.pid || !listener.executable || !input.expectedExe) {
    return { kill: false, reason: '没有完整的操作系统进程归属证据，没有关闭它' }
  }
  if (normalizePath(listener.executable) !== normalizePath(input.expectedExe)) {
    return { kill: false, reason: '监听程序不属于本次安装路径，没有关闭它' }
  }
  if (input.reportedPid && Number(listener.pid) !== Number(input.reportedPid)) {
    return { kill: false, reason: '接口里的进程号和实际监听进程不一致，没有关闭它' }
  }
  if (input.reportedExe && normalizePath(input.reportedExe) !== normalizePath(listener.executable)) {
    return { kill: false, reason: '接口里的程序路径和实际监听进程不一致，没有关闭它' }
  }
  return { kill: true, pid: Number(listener.pid), executable: listener.executable }
}

export function portAfterStop(input) {
  const listener = input.listener
  if (!listener) return { state: 'clear' }
  if (Number(listener.pid) === Number(input.oldPid) && input.expectedExe
    && normalizePath(listener.executable) === normalizePath(input.expectedExe)) {
    return { state: 'still-ours', pid: Number(listener.pid), executable: listener.executable }
  }
  return { state: 'taken', reason: '端口上的进程已经变化，没有继续关闭；新 PID 不代表子进程' }
}

export function startupPort(input) {
  const decision = backendDecision(input)
  const port = Number(input.preferredPort) || 8800
  if (decision.action === 'attach' || decision.action === 'replace') return { ...decision, port }
  if (input.listener || input.health || input.listenerError) {
    return { action: 'start', port: 0, reason: `${port} 的归属未获确认，改用空闲端口，没有关闭它` }
  }
  return { action: 'start', port, reason: '' }
}

export function backendDecision(input) {
  const health = input.health
  if (!health) return { action: 'start' }
  if (input.listenerError || health.runtime === 'foreign') {
    return { action: 'error', reason: '监听归属无法确认，没有关闭它' }
  }
  const plan = killPlan({
    expectedExe: input.expectedExe,
    reportedPid: health.runtime === 'ok' ? health.pid : undefined,
    reportedExe: health.runtime === 'ok' ? health.executable : undefined,
    listener: input.listener,
  })
  if (!plan.kill) return { action: 'error', reason: plan.reason }
  if (health.runtime === 'missing') {
    return input.packaged && ownServiceName(input.expectedExe)
      ? { action: 'replace', pid: plan.pid, executable: plan.executable, reason: '本安装旧版没有身份接口，将按实际监听进程替换' }
      : { action: 'error', reason: '开发后端缺少运行身份，没有关闭它' }
  }
  if (health.runtime !== 'ok' || !health.pid || !health.executable
    || !input.expectedChannel || health.channel !== input.expectedChannel
    || !input.expectedCwd || normalizePath(health.cwd) !== normalizePath(input.expectedCwd)
    || !input.expectedHome || normalizePath(health.data_home) !== normalizePath(input.expectedHome)) {
    return { action: 'error', reason: '后端渠道、工作目录或数据目录不是本次运行，没有关闭它' }
  }
  if (input.expectedBuild && health.build_id === input.expectedBuild) {
    return { action: 'attach', reason: '本安装、本渠道的运行构建与 OS 监听一致' }
  }
  return { action: 'replace', pid: plan.pid, executable: plan.executable, reason: '同一安装上是旧构建，将先替换它' }
}
