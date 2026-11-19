/**
 * 采集后端进程管理：启动、监控、优雅关闭。
 *
 * 生产环境运行 extraResources/backend/ 下的服务程序。
 * 开发环境优先连接已启动的后端，否则使用仓库 .venv 中的 Python。
 */
import { app, BrowserWindow, net, dialog } from 'electron'
import { spawn, ChildProcess, execFile } from 'child_process'
import { join, resolve } from 'path'
import { existsSync, readFileSync } from 'fs'
import { backendDecision, killPlan, portAfterStop } from './backendMatch.mjs'

const EXE_NAME = process.platform === 'win32' ? 'spider-gd-service.exe' : 'spider-gd-service'
const READY_URL = 'http://127.0.0.1:8800/openapi.json'
const RUNTIME_URL = 'http://127.0.0.1:8800/api/runtime'
const READY_TITLE = 'spider-gd 控制接口'
const MAX_RESTART = 3
const RESTART_INTERVAL = 5000

let backendProcess: ChildProcess | null = null
let restartCount = 0
let restartTimer: ReturnType<typeof setTimeout> | null = null
let stopping = false

type BackendLaunch = { command: string; args: string[]; cwd: string }

function userDataHome(): string {
  try {
    return app.getPath('userData')
  } catch {
    const base = process.env.APPDATA || app.getPath('home')
    return join(base, 'SpiderGD')
  }
}

function backendEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env }
  // 打包后把用户数据放到 Electron userData，避免升级时被安装目录清掉。
  // 开发环境不设置，后端继续使用仓库工作目录。
  if (app.isPackaged && !env.SPIDER_GD_HOME) {
    env.SPIDER_GD_HOME = userDataHome()
  }
  return env
}

function findBackendLaunch(): BackendLaunch | null {
  const envPath = process.env['SPIDER_GD_BACKEND_EXE']
  if (envPath && existsSync(envPath)) {
    return { command: envPath, args: [], cwd: resolve(envPath, '..') }
  }

  // 打包环境：electron-builder 的 extraResources
  const resourcesPath = process.resourcesPath
  const prodPath = resourcesPath ? join(resourcesPath, 'backend', EXE_NAME) : ''
  if (app.isPackaged) {
    return prodPath && existsSync(prodPath)
      ? { command: prodPath, args: [], cwd: resolve(prodPath, '..') }
      : null
  }

  // 开发环境可使用已有的打包服务，通常直接运行仓库中的 Python 后端。
  const devPath = join(app.getAppPath(), 'resources', 'backend', EXE_NAME)
  if (existsSync(devPath)) {
    return { command: devPath, args: [], cwd: resolve(devPath, '..') }
  }

  const projectRoot = resolve(app.getAppPath(), '..')
  const python = process.platform === 'win32'
    ? join(projectRoot, '.venv', 'Scripts', 'python.exe')
    : join(projectRoot, '.venv', 'bin', 'python')
  if (existsSync(python) && existsSync(join(projectRoot, 'main.py'))) {
    return {
      command: python,
      args: ['main.py', '--config', 'configs/mode_dev.yaml', 'run'],
      cwd: projectRoot,
    }
  }

  return null
}

function notifyRenderer(win: BrowserWindow | null, status: string) {
  win?.webContents.send('backend:status', { status })
}

type RuntimeHealth = {
  runtime: 'ok' | 'missing' | 'foreign'
  executable?: string
  pid?: number
  spider_gd?: string
  build_id?: string
  frozen?: boolean
}

type Listener = { pid: number; executable: string }

function expectedBuildId(exePath: string): string {
  if (!exePath) return ''
  try {
    const raw = readFileSync(join(resolve(exePath, '..'), 'build_stamp.json'), 'utf8')
    const parsed = JSON.parse(raw) as { build_id?: string }
    return String(parsed.build_id || '')
  } catch {
    return ''
  }
}

async function readOwnRuntime(): Promise<RuntimeHealth | null> {
  try {
    const spec = await net.fetch(READY_URL, { signal: AbortSignal.timeout(1500) })
    if (!spec.ok) return null
    const body = await spec.json() as { info?: { title?: string } }
    if (body.info?.title !== READY_TITLE) return { runtime: 'foreign' }
    const runtime = await net.fetch(RUNTIME_URL, { signal: AbortSignal.timeout(1500) })
    if (runtime.status === 404 || !runtime.ok) return { runtime: 'missing' }
    const data = await runtime.json() as Omit<RuntimeHealth, 'runtime'>
    return { runtime: 'ok', ...data }
  } catch {
    return null
  }
}

function queryListener(): Promise<Listener | null> {
  return new Promise((resolvePromise) => {
    if (process.platform === 'win32') {
      const script = [
        "$c = Get-NetTCPConnection -LocalPort 8800 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1;",
        "if (-not $c) { exit 0 }",
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\";",
        "Write-Output \"$($c.OwningProcess)|$($p.ExecutablePath)\"",
      ].join(' ')
      execFile('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], { timeout: 4000 }, (err, stdout) => {
        if (err) { resolvePromise(null); return }
        resolvePromise(parseListener(String(stdout || '')))
      })
      return
    }
    execFile('ss', ['-ltnp', 'sport = :8800'], { timeout: 2000 }, (err, stdout) => {
      if (err) { resolvePromise(null); return }
      const match = String(stdout || '').match(/pid=(\d+)/)
      if (!match) { resolvePromise(null); return }
      const pid = Number(match[1])
      execFile('readlink', ['-f', `/proc/${pid}/exe`], (linkErr, linkOut) => {
        if (linkErr) { resolvePromise(null); return }
        const executable = String(linkOut || '').trim()
        resolvePromise(executable ? { pid, executable } : null)
      })
    })
  })
}

function parseListener(text: string): Listener | null {
  const line = text.split(/\r?\n/).map((item) => item.trim()).find(Boolean) || ''
  const splitAt = line.indexOf('|')
  if (splitAt <= 0) return null
  const pid = Number(line.slice(0, splitAt))
  const executable = line.slice(splitAt + 1).trim()
  if (!pid || !executable) return null
  return { pid, executable }
}

function stopOwnedPid(pid: number): Promise<void> {
  return new Promise((resolvePromise) => {
    if (process.platform === 'win32') {
      execFile('taskkill', ['/PID', String(pid), '/T', '/F'], { timeout: 8000 }, () => resolvePromise())
      return
    }
    try {
      process.kill(pid, 'SIGTERM')
    } catch { /* 进程已经不在 */ }
    resolvePromise()
  })
}

async function replaceVerifiedListener(pid: number, executable: string): Promise<string> {
  let targetPid = pid
  const targetExe = executable
  let childKills = 0
  const before = await queryListener()
  const plan = killPlan({ reportedPid: targetPid, reportedExe: targetExe, listener: before })
  if (!plan.kill || plan.pid !== targetPid) return plan.reason || '监听进程已经变化，没有关闭它'
  await stopOwnedPid(targetPid)
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    const now = await queryListener()
    const after = portAfterStop({ oldPid: targetPid, listener: now })
    if (after.state === 'clear') return ''
    if (after.state === 'child' && after.pid && after.executable && childKills < 3) {
      const again = await queryListener()
      const child = killPlan({
        reportedPid: after.pid,
        reportedExe: after.executable,
        listener: again,
      })
      if (!child.kill || child.pid == null) return child.reason || '子进程已经变化，没有继续关闭'
      childKills += 1
      targetPid = child.pid
      await stopOwnedPid(targetPid)
      continue
    }
    if (after.state !== 'still-ours') return after.reason || '端口上的进程已经变化，没有继续关闭'
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 400))
  }
  return '采集后端没有在时限内退出'
}

export async function startBackend(win: BrowserWindow | null): Promise<void> {
  if (stopping) return
  const launch = findBackendLaunch()
  const running = await readOwnRuntime()
  if (running) {
    const expectedExe = launch ? resolve(launch.command) : ''
    const decision = backendDecision({
      packaged: app.isPackaged,
      expectedExe,
      expectedBuild: expectedBuildId(expectedExe),
      health: running,
      listener: await queryListener(),
    })
    console.log('[backend]', decision.reason || decision.action, running.executable || '', 'pid=', running.pid || '')
    if (decision.action === 'attach') {
      notifyRenderer(win, 'ready')
      restartCount = 0
      return
    }
    if (decision.action === 'error') {
      notifyRenderer(win, 'error')
      if (win) {
        const show = () => dialog.showErrorBox('后端不是本次安装的程序', decision.reason || '')
        if (win.isVisible()) show()
        else win.once('ready-to-show', show)
      }
      return
    }
    if (decision.action === 'replace' && decision.pid && decision.executable) {
      const blocked = await replaceVerifiedListener(decision.pid, decision.executable)
      if (blocked) {
        notifyRenderer(win, 'error')
        if (win) dialog.showErrorBox('旧后端没有退出', blocked)
        return
      }
    }
  }
  if (stopping) return

  if (!launch) {
    const msg = app.isPackaged
      ? `未找到打包后端 (${EXE_NAME})\n请检查安装包的 backend 资源。`
      : '未找到开发后端。请在项目根目录创建 .venv 并安装 Python 依赖。'
    console.error('[backend]', msg)
    // 延迟弹窗，等窗口就绪
    if (win) {
      win.once('ready-to-show', () => {
        dialog.showErrorBox('后端服务未找到', msg)
      })
    }
    return
  }

  console.log('[backend] 启动:', launch.command, ...launch.args)

  const env = backendEnv()
  console.log('[backend] SPIDER_GD_HOME=', env.SPIDER_GD_HOME || '(未设置，使用工作目录)')
  const proc = spawn(launch.command, launch.args, {
    cwd: launch.cwd,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })

  backendProcess = proc

  proc.stdout?.on('data', (data: Buffer) => {
    console.log(`[backend] ${data.toString().trim()}`)
  })

  proc.stderr?.on('data', (data: Buffer) => {
    console.error(`[backend:err] ${data.toString().trim()}`)
  })

  proc.on('error', (err) => {
    console.error('[backend] 启动失败:', err.message)
    notifyRenderer(win, 'error')
    attemptRestart(win)
  })

  proc.on('exit', (code, signal) => {
    console.log(`[backend] 进程退出: code=${code} signal=${signal}`)
    backendProcess = null
    notifyRenderer(win, 'stopped')

    // 非主动关闭 → 尝试重启
    if (code !== 0 && code !== null) {
      attemptRestart(win)
    }
  })

  // 等待后端就绪后通知渲染进程
  waitForReady(win)
}

function attemptRestart(win: BrowserWindow | null): void {
  if (stopping) return
  if (restartCount >= MAX_RESTART) {
    console.error(`[backend] 已重启 ${MAX_RESTART} 次，放弃`)
    notifyRenderer(win, 'failed')
    return
  }

  restartCount++
  console.log(`[backend] ${RESTART_INTERVAL / 1000}s 后第 ${restartCount} 次重试...`)
  restartTimer = setTimeout(() => {
    startBackend(win)
  }, RESTART_INTERVAL)
}

async function waitForReady(win: BrowserWindow | null): Promise<void> {
  const maxWait = 15_000
  const started = Date.now()

  while (Date.now() - started < maxWait) {
    const running = await readOwnRuntime()
    const startedByUs = Boolean(
      running?.runtime === 'ok' && backendProcess?.pid && running.pid === backendProcess.pid,
    )
    if (startedByUs) {
      console.log('[backend] 就绪', running?.executable || '', running?.spider_gd || '', 'pid=', running?.pid)
      notifyRenderer(win, 'ready')
      restartCount = 0
      return
    }
    console.log('[backend] 等待服务就绪...')
    await new Promise((r) => setTimeout(r, 800))
  }

  console.warn('[backend] 启动超时（15s），将继续等待')
  notifyRenderer(win, 'starting')
}

export async function stopBackend(): Promise<void> {
  stopping = true
  if (restartTimer) {
    clearTimeout(restartTimer)
    restartTimer = null
  }
  restartCount = MAX_RESTART
  const proc = backendProcess
  backendProcess = null
  if (!proc?.pid) return Promise.resolve()
  const pid = proc.pid
  console.log('[backend] 正在关闭', pid)
  await new Promise<void>((resolvePromise) => {
    const timer = setTimeout(() => {
      if (process.platform === 'win32') {
        void stopOwnedPid(pid).then(() => resolvePromise())
        return
      }
      try { proc.kill('SIGKILL') } catch { /* 已经退出 */ }
      resolvePromise()
    }, 4000)
    proc.once('exit', () => {
      clearTimeout(timer)
      resolvePromise()
    })
    if (process.platform === 'win32') {
      execFile('taskkill', ['/PID', String(pid), '/T'], () => {})
    } else {
      try { proc.kill('SIGTERM') } catch { /* 已经退出 */ }
    }
  })
  const leftover = portAfterStop({ oldPid: pid, listener: await queryListener() })
  if ((leftover.state === 'still-ours' || leftover.state === 'child') && leftover.pid && leftover.executable) {
    const blocked = await replaceVerifiedListener(leftover.pid, leftover.executable)
    if (blocked) console.error('[backend]', blocked)
  } else if (leftover.state === 'taken') {
    console.error('[backend]', leftover.reason)
  }
}
