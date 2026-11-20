/** 后端生命周期：只有运行身份和 OS 监听归属均核实后才向界面公布地址。 */
import { app, BrowserWindow, net, dialog } from 'electron'
import { spawn, ChildProcess, execFile } from 'child_process'
import { createServer } from 'net'
import { join, resolve, dirname } from 'path'
import { existsSync, readFileSync, readlinkSync, writeFileSync } from 'fs'
import { backendDecision, killPlan, normalizePath, portAfterStop, startupPort, type BackendInput, type Listener, type RuntimeHealth } from './backendMatch.mjs'
import { backendEnvironment, resolveBackendDataDir } from '../../release-channel.mjs'
import { releaseIdentity } from './release'

const EXE_NAME = `${releaseIdentity.serviceName}${process.platform === 'win32' ? '.exe' : ''}`
const PREFERRED_PORT = releaseIdentity.preferredPort
const READY_TITLE = 'spider-gd 控制接口'
type BackendLaunch = { command: string; args: string[]; cwd: string }
type Run = { generation: number; proc: ChildProcess; expected: Omit<BackendInput, 'health' | 'listener'>; port: number; verified?: Listener; cleanup?: Promise<boolean>; exited?: boolean }
type Connection = { generation: number; expected: Run['expected']; port: number; listener: Listener }
let active: Run | null = null
let connection: Connection | null = null
let connectionTimer: ReturnType<typeof setTimeout> | null = null
let statusWindow: BrowserWindow | null = null
let generation = 0
let stopping = false
let starting = false
let replacingPid: number | null = null
let stopPromise: Promise<void> | null = null
let readyPort: number | null = null
let connectionFailures = 0
let portResolve: ((port: number) => void) | undefined
let portReject: ((error: Error) => void) | undefined
function pendingPort(): Promise<number> {
  const promise = new Promise<number>((resolvePort, rejectPort) => { portResolve = resolvePort; portReject = rejectPort })
  // IPC/renderer may subscribe later. Keep the original promise rejected for callers,
  // but don't produce an unhandled rejection before the first subscription.
  void promise.catch(() => {})
  return promise
}
let portReady = pendingPort()

function userDataHome(): string {
  try { return app.getPath('userData') }
  catch { return join(process.env.APPDATA || app.getPath('home'), 'SpiderGD') }
}
function backendEnv(): NodeJS.ProcessEnv {
  return backendEnvironment(releaseIdentity, app.isPackaged, userDataHome())
}
function findBackendLaunch(): BackendLaunch | null {
  const override = process.env.SPIDER_GD_BACKEND_EXE
  if (override && existsSync(override)) return { command: override, args: [], cwd: dirname(resolve(override)) }
  const prod = process.resourcesPath ? join(process.resourcesPath, 'backend', EXE_NAME) : ''
  if (app.isPackaged) return prod && existsSync(prod) ? { command: prod, args: [], cwd: dirname(prod) } : null
  const dev = join(app.getAppPath(), 'resources', 'backend', EXE_NAME)
  if (existsSync(dev)) return { command: dev, args: [], cwd: dirname(dev) }
  const root = resolve(app.getAppPath(), '..')
  const python = process.platform === 'win32' ? join(root, '.venv', 'Scripts', 'python.exe') : join(root, '.venv', 'bin', 'python')
  return existsSync(python) && existsSync(join(root, 'main.py'))
    ? { command: python, args: ['main.py', '--config', 'configs/mode_dev.yaml', 'run'], cwd: root } : null
}
export function currentBackendLaunchCwd(): string {
  return findBackendLaunch()?.cwd ?? (app.isPackaged ? resolve(process.resourcesPath, 'backend') : resolve(app.getAppPath(), '..'))
}
export function currentBackendDataDir(): string {
  const cwd = currentBackendLaunchCwd()
  return resolveBackendDataDir(backendEnv().SPIDER_GD_HOME, cwd, app.isPackaged || releaseIdentity.channel === 'beta' ? userDataHome() : cwd)
}
export function currentBackendBase(): string {
  if (readyPort === null) throw new Error('后端尚未核实就绪或已停止')
  return `http://127.0.0.1:${readyPort}`
}
export function whenBackendPort(): Promise<number> { return portReady }
function notifyRenderer(win: BrowserWindow | null, status: string): void {
  win?.webContents.send('backend:status', status === 'ready' ? { status, base: currentBackendBase() } : { status })
}
function invalidate(message: string): void {
  if (connectionTimer) clearTimeout(connectionTimer)
  connectionTimer = null
  connection = null
  readyPort = null
  portReject?.(new Error(message))
  portResolve = undefined; portReject = undefined
  portReady = Promise.reject(new Error(message))
  void portReady.catch(() => {})
}
function fail(win: BrowserWindow | null, message: string): void {
  invalidate(message)
  notifyRenderer(win, 'error')
  if (win) dialog.showErrorBox('后端服务不可用', message)
}
function publishPort(port: number, win: BrowserWindow | null): void {
  readyPort = port
  portResolve?.(port)
  portResolve = undefined; portReject = undefined
  try { writeFileSync(join(userDataHome(), 'backend-port.json'), JSON.stringify({ port }), 'utf8') } catch { /* 不影响本次已核实连接 */ }
  notifyRenderer(win, 'ready')
}
function readSavedPort(): number {
  try {
    const port = Number((JSON.parse(readFileSync(join(userDataHome(), 'backend-port.json'), 'utf8')) as { port?: number }).port)
    return validPort(port) ? port : 0
  } catch { return 0 }
}
function validPort(port: number): boolean { return Number.isInteger(port) && port > 0 && port < 65536 }
function allocateFreePort(): Promise<number> {
  return new Promise((done, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close(() => validPort(port) ? done(port) : reject(new Error('无法分配后端端口')))
    })
  })
}
function expectedBuildId(launch: BackendLaunch): string {
  if (launch.args.includes('main.py')) return 'source'
  try { return String((JSON.parse(readFileSync(join(dirname(launch.command), 'build_stamp.json'), 'utf8')) as { build_id?: string }).build_id || '') }
  catch { return '' }
}
async function readOwnRuntime(port: number): Promise<RuntimeHealth | null> {
  try {
    const spec = await net.fetch(`http://127.0.0.1:${port}/openapi.json`, { signal: AbortSignal.timeout(1500) })
    if (!spec.ok) return null
    if ((await spec.json() as { info?: { title?: string } }).info?.title !== READY_TITLE) return { runtime: 'foreign' }
    const response = await net.fetch(`http://127.0.0.1:${port}/api/runtime`, { signal: AbortSignal.timeout(1500) })
    if (response.status === 404) return { runtime: 'missing' }
    if (!response.ok) return null
    return { ...await response.json() as Omit<RuntimeHealth, 'runtime'>, runtime: 'ok' }
  } catch { return null }
}

const PS_UTF8 = "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding;"
function output(command: string, args: string[], noMatchIsEmpty = false): Promise<string> {
  return new Promise((done, reject) => {
    execFile(command, args, { timeout: 12000, encoding: 'utf8', windowsHide: true }, (error, stdout, stderr) => {
      if (error) {
        if (noMatchIsEmpty && error.code === 1 && !stdout.trim() && !stderr.trim()) { done(''); return }
        reject(error); return
      }
      done(stdout.trim())
    })
  })
}
function parseProcess(text: string): Listener {
  const data = JSON.parse(text) as Listener
  if (!Number.isInteger(data.pid) || data.pid <= 0 || !data.executable || !Number.isInteger(data.parentPid)) throw new Error('OS 未返回完整进程身份')
  return data
}
async function queryProcess(pid: number): Promise<Listener> {
  if (process.platform === 'win32') {
    return parseProcess(await output('powershell', ['-NoProfile', '-NonInteractive', '-Command', `${PS_UTF8} $p = Get-CimInstance Win32_Process -Filter "ProcessId=${pid}"; if (-not $p -or -not $p.ExecutablePath) { throw 'Process identity unavailable' }; @{pid=[int]$p.ProcessId; executable=$p.ExecutablePath; parentPid=[int]$p.ParentProcessId} | ConvertTo-Json -Compress`]))
  }
  if (process.platform === 'linux') {
    const executable = readlinkSync(`/proc/${pid}/exe`)
    const stat = readFileSync(`/proc/${pid}/stat`, 'utf8')
    const parentPid = Number(stat.slice(stat.lastIndexOf(')') + 2).split(/\s+/)[1])
    if (!executable || !Number.isInteger(parentPid)) throw new Error('无法读取 /proc 进程身份')
    return { pid, executable, parentPid }
  }
  const text = await output('ps', ['-ww', '-p', String(pid), '-o', 'ppid=', '-o', 'comm='])
  const match = text.match(/^(\d+)\s+(.+)$/)
  if (!match) throw new Error('无法读取 macOS 进程身份')
  return { pid, parentPid: Number(match[1]), executable: match[2] }
}
/** null means a successful, explicitly empty OS query; errors remain errors. */
export async function queryListener(port: number): Promise<Listener | null> {
  if (!validPort(port)) throw new Error('监听端口无效')
  if (process.platform === 'win32') {
    // Enumerate successfully before filtering: "no matching connection" is not a CIM failure.
    const text = await output('powershell', ['-NoProfile', '-NonInteractive', '-Command', `${PS_UTF8} $c = @(Get-NetTCPConnection -ErrorAction Stop | Where-Object { $_.LocalPort -eq ${port} -and $_.State -eq 'Listen' }); $owners = @($c.OwningProcess | Select-Object -Unique); if ($owners.Count -eq 0) { exit 0 }; if ($owners.Count -ne 1) { throw 'Ambiguous listener' }; $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($owners[0])"; if (-not $p.ExecutablePath) { throw 'Listener identity unavailable' }; @{pid=[int]$p.ProcessId; executable=$p.ExecutablePath; parentPid=[int]$p.ParentProcessId} | ConvertTo-Json -Compress`])
    return text ? parseProcess(text) : null
  }
  const text = process.platform === 'darwin'
    ? await output('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-Fp'], true)
    : await output('ss', ['-ltnp', `sport = :${port}`])
  const pids = [...new Set([...text.matchAll(process.platform === 'darwin' ? /^p(\d+)$/gm : /pid=(\d+)/g)].map((m) => Number(m[1])))]
  if (pids.length > 1) throw new Error('端口有多个监听进程')
  if (!pids.length) {
    if (process.platform !== 'darwin' && /LISTEN/.test(text)) throw new Error('OS 隐藏了监听进程身份')
    return null
  }
  return queryProcess(pids[0])
}
async function inspect(port: number, expected: Run['expected']): Promise<BackendInput> {
  try {
    const listener = await queryListener(port)
    const health = listener && normalizePath(listener.executable) === normalizePath(expected.expectedExe) ? await readOwnRuntime(port) : null
    return { ...expected, preferredPort: port, listener, health }
  } catch (error) {
    return { ...expected, preferredPort: port, listener: null, health: null, listenerError: String(error) }
  }
}
function watchConnection(current: Connection, win: BrowserWindow | null): void {
  connection = current
  connectionFailures = 0
  connectionTimer = setTimeout(() => { void verifyConnection(current, win) }, 2000)
}
async function verifyConnection(current: Connection, win: BrowserWindow | null): Promise<void> {
  const candidate = await inspect(current.port, current.expected)
  if (stopping || generation !== current.generation || connection !== current) return
  const unchanged = killPlan({ expectedExe: current.expected.expectedExe, reportedPid: current.listener.pid, reportedExe: current.listener.executable, listener: candidate.listener })
  if (!unchanged.kill || backendDecision(candidate).action !== 'attach') {
    // 单次复核失败可能只是 OS 查询超时或一时无法读取进程身份；
    // 连续多次才判定失效，避免把瞬时抖动升级成永久错误。
    connectionFailures += 1
    if (connectionFailures < 3) {
      connectionTimer = setTimeout(() => { void verifyConnection(current, win) }, 2000)
      return
    }
    fail(win, '后端连接已失效，无法再次核实监听归属')
    return
  }
  watchConnection(current, win)
}
function processHasExited(pid: number): boolean {
  try { process.kill(pid, 0); return false } catch (error) {
    if (typeof error === 'object' && error !== null && 'code' in error && error.code === 'ESRCH') return true
    throw error
  }
}
async function stopPid(pid: number): Promise<void> {
  if (process.platform === 'win32') { await output('taskkill', ['/PID', String(pid), '/F']); return }
  process.kill(pid, 'SIGTERM')
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    if (processHasExited(pid)) return
    await new Promise((done) => setTimeout(done, 400))
  }
  throw new Error('后端进程未及时退出')
}
async function replaceVerifiedListener(port: number, pid: number, executable: string, token: number): Promise<string> {
  try {
    const plan = killPlan({ expectedExe: executable, reportedPid: pid, reportedExe: executable, listener: await queryListener(port) })
    if (stopping || token !== generation) return '启动已取消'
    if (!plan.kill) return plan.reason || '监听进程已变化'
    replacingPid = pid
    await stopPid(pid)
    replacingPid = null
    const deadline = Date.now() + 8000
    while (!stopping && token === generation && Date.now() < deadline) {
      const after = portAfterStop({ oldPid: pid, expectedExe: executable, listener: await queryListener(port) })
      if (after.state === 'clear') return ''
      if (after.state === 'taken') return after.reason || '端口归属已变化'
      await new Promise((done) => setTimeout(done, 400))
    }
    return '旧后端未及时退出'
  } catch (error) { return `无法再次核实或关闭旧后端：${String(error)}` }
}
function cleanupRun(run: Run): Promise<boolean> {
  run.cleanup ??= Promise.resolve().then(async () => {
    if (!run.proc.pid) return true
    let complete = true
    let stoppedPid: number | undefined
    try {
      const listener = await queryListener(run.port)
      const target = run.verified || listener
      const owned = target && killPlan({ expectedExe: run.expected.expectedExe, reportedPid: target.pid, reportedExe: target.executable, listener }).kill
        && (run.verified || await spawnedOwner(run, target))
      if (owned && target) {
        const confirmed = await queryListener(run.port)
        const plan = killPlan({ expectedExe: run.expected.expectedExe, reportedPid: target.pid, reportedExe: target.executable, listener: confirmed })
        if (plan.kill && confirmed && (run.verified || await spawnedOwner(run, confirmed))) {
          await stopPid(target.pid)
          stoppedPid = target.pid
        } else { complete = false; console.warn('[backend]', plan.reason || '启动进程归属已变化') }
      } else if (run.verified) { complete = false; console.warn('[backend] 已核实的监听进程已变化') }
    } catch (error) { complete = false; console.warn('[backend] 无法核实监听进程', String(error)) }
    try {
      if (!run.exited && run.verified?.pid !== run.proc.pid && stoppedPid !== run.proc.pid) {
        const parent = await queryProcess(run.proc.pid)
        if (!run.exited) {
          const plan = killPlan({ expectedExe: run.expected.expectedExe, reportedPid: run.proc.pid, listener: parent })
          if (plan.kill) await stopPid(run.proc.pid)
          else { complete = false; console.warn('[backend]', plan.reason) }
        }
      }
    } catch (error) {
      if (!run.exited) { complete = false; console.warn('[backend] 无法核实启动进程', String(error)) }
    }
    return complete
  })
  return run.cleanup
}
async function failRun(run: Run, win: BrowserWindow | null, message: string): Promise<void> {
  if (!isCurrent(run)) return
  ++generation
  fail(win, message)
  const complete = await cleanupRun(run)
  if (complete && active === run) active = null
}
function isCurrent(run: Run): boolean { return !stopping && active === run && generation === run.generation }
async function spawnedOwner(run: Run, listener: Listener): Promise<boolean> {
  if (listener.pid === run.proc.pid) return true
  if (!run.proc.pid || listener.parentPid !== run.proc.pid) return false
  const parent = await queryProcess(run.proc.pid)
  return parent.pid === run.proc.pid && normalizePath(parent.executable) === normalizePath(run.expected.expectedExe)
}
async function waitForReady(run: Run, win: BrowserWindow | null): Promise<void> {
  // 现场机器装完包后杀软扫描/系统繁忙时，PowerShell 查监听可能要数秒；
  // 15s 会把"查询慢"当成"启动失败"直接杀掉后端。给查询和整体就绪都留足余量。
  const deadline = Date.now() + 30000
  while (isCurrent(run) && Date.now() < deadline) {
    const port = run.port
    const candidate = await inspect(port, run.expected)
    if (!isCurrent(run)) return
    if (port !== run.port) continue
    if (backendDecision(candidate).action === 'attach' && candidate.listener && await spawnedOwner(run, candidate.listener).catch(() => false)) {
      // Recheck after HTTP and parent inspection, so stale runtime/PID evidence cannot publish.
      const listener = await queryListener(port).catch(() => null)
      if (!isCurrent(run)) return
      if (port === run.port && killPlan({ expectedExe: run.expected.expectedExe, reportedPid: candidate.listener.pid, reportedExe: candidate.listener.executable, listener }).kill) {
        run.verified = listener || undefined
        watchConnection({ generation: run.generation, expected: run.expected, port, listener: listener! }, win)
        publishPort(port, win)
        return
      }
    }
    await new Promise((done) => setTimeout(done, 800))
  }
  if (isCurrent(run)) await failRun(run, win, '后端启动超时，未能核实监听归属')
}
export async function startBackend(win: BrowserWindow | null): Promise<void> {
  if (stopping || starting || active || connection) return
  starting = true
  const token = ++generation
  statusWindow = win
  readyPort = null
  // Keep the initial pending promise for IPC calls that arrived before startup.
  if (!portResolve) portReady = pendingPort()
  notifyRenderer(win, 'starting')
  try {
    if (replacingPid !== null) {
      if (!processHasExited(replacingPid)) { fail(win, '旧后端仍在退出，不能重新启动'); return }
      replacingPid = null
    }
    const launch = findBackendLaunch()
    if (!launch) { fail(win, `未找到后端服务 (${EXE_NAME})`); return }
    const expected: Run['expected'] = { packaged: app.isPackaged, expectedExe: normalizePath(launch.command), expectedBuild: expectedBuildId(launch), expectedChannel: releaseIdentity.channel, expectedCwd: normalizePath(launch.cwd), expectedHome: normalizePath(dirname(currentBackendDataDir())) }
    const saved = readSavedPort()
    const ports = [...new Set([PREFERRED_PORT, ...(saved ? [saved] : [])])]
    const candidates: BackendInput[] = []
    for (const port of ports) {
      candidates.push(await inspect(port, expected))
      if (stopping || token !== generation) return
    }
    const attached = candidates.find((c) => backendDecision(c).action === 'attach')
    if (attached) {
      const listener = await queryListener(attached.preferredPort!).catch(() => null)
      if (stopping || token !== generation) return
      if (listener && killPlan({ expectedExe: expected.expectedExe, reportedPid: attached.health?.pid, reportedExe: attached.health?.executable, listener }).kill) {
        watchConnection({ generation: token, expected, port: attached.preferredPort!, listener }, win)
        publishPort(attached.preferredPort!, win); return
      }
    }
    const replacement = candidates.find((c) => backendDecision(c).action === 'replace')
    let port = startupPort(candidates[0]).port
    if (replacement) {
      const choice = backendDecision(replacement)
      const blocked = await replaceVerifiedListener(replacement.preferredPort!, choice.pid!, choice.executable!, token)
      if (stopping || token !== generation) return
      if (blocked) { fail(win, blocked); return }
      port = replacement.preferredPort!
    } else if (attached) port = 0
    if (!port) port = await allocateFreePort()
    if (stopping || token !== generation) return
    const env = { ...backendEnv(), WEB__PORT: String(port), PYTHONUNBUFFERED: '1' }
    const proc = spawn(launch.command, launch.args, { cwd: launch.cwd, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true })
    const run: Run = { generation: token, proc, expected, port }
    active = run
    let stdout = ''
    proc.stdout?.on('data', (data: Buffer) => {
      if (!isCurrent(run) || run.verified) return
      stdout = (stdout + data.toString()).slice(-4096)
      for (const match of stdout.matchAll(/SPIDER_GD_LISTEN=\S+:(\d+)(?:\r?\n|$)/g)) {
        const actual = Number(match[1])
        if (validPort(actual)) run.port = actual
      }
    })
    proc.stderr?.on('data', (data: Buffer) => console.error(`[backend:err] ${data.toString().trim()}`))
    proc.on('error', (error) => {
      if (!isCurrent(run)) return
      active = null; ++generation
      fail(win, `后端启动失败：${error.message}`)
    })
    proc.on('exit', () => {
      run.exited = true
      if (active !== run || token !== generation) return
      active = null; ++generation
      invalidate('后端已退出'); notifyRenderer(win, 'stopped')
    })
    void waitForReady(run, win).catch((error: unknown) => failRun(run, win, String(error)))
  } catch (error) {
    if (!stopping && token === generation) fail(win, `无法启动后端：${String(error)}`)
  } finally { starting = false }
}
export function stopBackend(): Promise<void> {
  if (stopPromise) return stopPromise
  stopping = true; ++generation
  invalidate('后端已停止')
  notifyRenderer(statusWindow, 'stopped')
  const run = active; active = null
  stopPromise = Promise.resolve().then(async () => { if (run) await cleanupRun(run) })
  return stopPromise
}
