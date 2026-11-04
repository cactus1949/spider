/**
 * 采集后端进程管理：启动、监控、优雅关闭。
 *
 * 生产环境运行 extraResources/backend/ 下的服务程序。
 * 开发环境优先连接已启动的后端，否则使用仓库 .venv 中的 Python。
 */
import { app, BrowserWindow, net, dialog } from 'electron'
import { spawn, ChildProcess } from 'child_process'
import { join, resolve } from 'path'
import { existsSync } from 'fs'

const EXE_NAME = process.platform === 'win32' ? 'spider-gd-service.exe' : 'spider-gd-service'
const READY_URL = 'http://127.0.0.1:8800/openapi.json'
const READY_TITLE = 'spider-gd 控制接口'
const MAX_RESTART = 3
const RESTART_INTERVAL = 5000

let backendProcess: ChildProcess | null = null
let restartCount = 0
let restartTimer: ReturnType<typeof setTimeout> | null = null
let stopping = false

type BackendLaunch = { command: string; args: string[]; cwd: string }

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

async function isBackendReady(): Promise<boolean> {
  try {
    const res = await net.fetch(READY_URL, { signal: AbortSignal.timeout(1500) })
    if (!res.ok) return false
    const body = await res.json() as { info?: { title?: string } }
    return body.info?.title === READY_TITLE
  } catch {
    return false
  }
}

export async function startBackend(win: BrowserWindow | null): Promise<void> {
  if (stopping) return
  if (await isBackendReady()) {
    console.log('[backend] 已连接运行中的后端')
    notifyRenderer(win, 'ready')
    restartCount = 0
    return
  }
  if (stopping) return

  const launch = findBackendLaunch()
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

  const proc = spawn(launch.command, launch.args, {
    cwd: launch.cwd,
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
    if (await isBackendReady()) {
      console.log('[backend] 就绪')
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

export function stopBackend(): void {
  stopping = true
  if (restartTimer) {
    clearTimeout(restartTimer)
    restartTimer = null
  }

  restartCount = MAX_RESTART // 阻止自动重启

  // 优雅关闭我们 spawn 的子进程
  if (backendProcess && backendProcess.pid) {
    console.log('[backend] 正在关闭...')
    try {
      if (process.platform === 'win32') {
        // 杀整个进程树
        const { exec } = require('child_process')
        exec(`taskkill /PID ${backendProcess.pid} /T /F`, () => {})
      } else {
        backendProcess.kill('SIGTERM')
      }
    } catch { /* ignore */ }
    backendProcess = null
  }

}
