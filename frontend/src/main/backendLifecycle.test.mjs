import assert from 'node:assert/strict'
import test from 'node:test'
import { EventEmitter } from 'node:events'
import { win32, posix } from 'node:path'
import * as channels from '../../release-channel.mjs'
import contract from '../../../src/spider_gd/release-channels.json' with { type: 'json' }
import { loadProduction, flush } from './boundaryFixtures.mjs'

function fixture({ platform = 'win32', channel = 'beta', packaged = true, saved = 0, missing = false, home, aliases = {}, image, probe } = {}) {
  const path = platform === 'win32' ? win32 : posix
  const root = platform === 'win32' ? 'C:\\安装' : '/repo'
  const resourcesPath = path.join(root, 'resources')
  const cwd = packaged ? path.join(resourcesPath, 'backend') : root
  const release = channels.buildRelease('0.2.8', channel, channel === 'beta' ? '7' : undefined)
  const exe = packaged ? path.join(cwd, `${release.serviceName}${platform === 'win32' ? '.exe' : ''}`)
    : platform === 'win32' ? path.join(root, '.venv', 'Scripts', 'python.exe') : path.join(root, '.venv', 'bin', 'python')
  const userData = path.join(root, '用户', channel)
  const env = home ? { SPIDER_GD_HOME: home } : {}
  const listeners = new Map(), runtimes = new Map(), processes = new Map()
  const queries = [], http = [], kills = [], writes = [], statuses = [], spawned = [], timers = [], dialogs = [], logs = []
  const canonical = (file) => aliases[file] || file
  const hooks = { onKill: null, onQuery: null, onSignal: null }
  const removeProcess = (pid) => {
    processes.delete(pid)
    for (const [port, listener] of listeners) {
      if (listener?.pid === pid) { listeners.delete(port); runtimes.delete(port) }
    }
  }
  let clock = 0
  class Clock extends Date { static now() { return clock } }
  const fs = {
    existsSync: (file) => !missing && (file === exe || file === path.join(root, 'main.py')),
    realpathSync: Object.assign(canonical, { native: canonical }),
    readlinkSync: (file) => {
      const pid = Number(file.match(/\/proc\/(\d+)/)?.[1])
      const p = processes.get(pid) || [...listeners.values()].find((p) => p?.pid === pid)
      if (!p) throw new Error('missing process')
      return p.executable
    },
    readFileSync: (file) => {
      if (file.endsWith('build_stamp.json')) return '{"build_id":"new"}'
      if (file.endsWith('backend-port.json')) return JSON.stringify({ port: saved })
      const pid = Number(file.match(/\/proc\/(\d+)/)?.[1])
      const p = processes.get(pid) || [...listeners.values()].find((p) => p?.pid === pid)
      if (p) return `${pid} (service name) S ${p.parentPid || 1} 0 0`
      throw new Error(`unexpected read: ${file}`)
    },
    writeFileSync: (file, text) => writes.push([file, JSON.parse(text)])
  }
  const app = { isPackaged: packaged, getAppPath: () => path.join(root, 'frontend'), getPath: () => userData }
  const execFile = (cmd, args, options, callback) => {
    if (typeof options === 'function') callback = options
    if (cmd === 'taskkill') {
      assert.ok(!args.includes('/T'), 'must not kill an unverified process tree')
      const pid = Number(args[args.indexOf('/PID') + 1]); kills.push(pid)
      const finished = (error, ...values) => { if (!error) removeProcess(pid); callback(error, ...values) }
      if (hooks.onKill?.(pid, finished) !== true) finished(null, '')
      return
    }
    if (cmd === 'powershell') {
      const script = args.at(-1)
      const port = Number(script.match(/(?:-LocalPort |LocalPort -eq )(\d+)/)?.[1])
      const pid = Number(script.match(/ProcessId=(\d+)/)?.[1])
      queries.push({ cmd, port, pid, script, options })
      if (hooks.onQuery?.(port, pid, callback)) return
      const value = port ? listeners.get(port) : processes.get(pid)
      if (value instanceof Error) { callback(value, ''); return }
      callback(null, value ? JSON.stringify({ pid: value.pid, executable: value.executable, parentPid: value.parentPid || 1 }) : '')
    } else if (cmd === 'lsof' || cmd === 'ss') {
      const port = Number(args.join(' ').match(/:(\d+)/)?.[1])
      queries.push({ cmd, port, args })
      const value = listeners.get(port)
      if (value instanceof Error) { callback(value, '', 'denied'); return }
      if (cmd === 'ss') callback(null, value ? `LISTEN 0 128 127.0.0.1:${port} users:(("service",pid=${value.pid},fd=4))` : 'State Recv-Q Send-Q Local Address:Port Peer Address:Port Process')
      else callback(value ? null : Object.assign(new Error('no match'), { code: 1 }), value ? `p${value.pid}\ncservice\n` : '', '')
    } else if (cmd === 'ps') {
      const pid = Number(args[args.indexOf('-p') + 1])
      const p = processes.get(pid) || [...listeners.values()].find((p) => p?.pid === pid)
      queries.push({ cmd, pid, args })
      callback(p ? null : new Error('missing process'), p ? `${p.parentPid || 1} ${p.executable}\n` : '')
    } else if (cmd === exe && args.includes('-c')) {
      // The read-only interpreter probe is a real spawned process in bookkeeping.
      spawned.push({ probe: true, command: cmd, args, options, pid: 90 + spawned.filter((s) => s.probe).length })
      if (typeof probe === 'function') { probe(cmd, args, options, callback); return }
      if (probe instanceof Error) { callback(probe, Buffer.alloc(0)); return }
      const stdout = probe && probe.stdout !== undefined ? probe.stdout : canonical(image || exe)
      callback(null, Buffer.from(stdout))
    } else assert.fail(`unexpected system command ${cmd}`)
  }
  const spawn = (command, args, options) => {
    const proc = new EventEmitter()
    proc.pid = 100 + spawned.filter((s) => !s.probe).length
    proc.stdout = new EventEmitter(); proc.stderr = new EventEmitter()
    proc.kill = () => assert.fail('must verify OS PID/path before kill')
    processes.set(proc.pid, { pid: proc.pid, executable: canonical(exe), parentPid: 1 })
    spawned.push({ proc, command, args, options })
    return proc
  }
  const net = { fetch: async (url) => {
    const port = Number(url.match(/:(\d+)\//)[1]); http.push(port)
    const runtime = runtimes.get(port)
    if (typeof runtime === 'function') return runtime(url)
    if (!runtime) throw new Error('no service')
    const status = runtime.runtime === 'missing' && url.endsWith('/api/runtime') ? 404 : 200
    return { ok: status === 200, status,
      json: async () => url.endsWith('openapi.json') ? { info: { title: runtime.runtime === 'foreign' ? 'other' : 'spider-gd 控制接口' } } : runtime }
  } }
  const processContext = { platform, resourcesPath, env, kill: (pid, signal) => {
    if (signal === 0) {
      if (!processes.has(pid) && ![...listeners.values()].some((p) => p?.pid === pid)) {
        throw Object.assign(new Error('missing process'), { code: 'ESRCH' })
      }
      return true
    }
    kills.push(pid)
    if (hooks.onSignal) hooks.onSignal(pid, signal)
    else removeProcess(pid)
    return true
  } }
  const pathModule = { ...path, default: path }
  const match = loadProduction('./backendMatch.mjs', { 'node:fs': fs, 'node:path': { win32, posix } })
  const releaseModule = loadProduction('../../release-channel.mjs', { 'node:path': pathModule, 'node:fs': fs, 'node:os': { homedir: () => userData }, '../src/spider_gd/release-channels.json': contract }, { process: processContext })
  const backend = loadProduction('./backend.ts', {
    electron: { app, net, dialog: { showErrorBox: (title, message) => dialogs.push({ title, message }) } }, child_process: { execFile, spawn }, fs, 'node:fs': fs,
    path: pathModule, 'node:path': pathModule, './backendMatch.mjs': match,
    '../../release-channel.mjs': releaseModule,
    './release': { releaseIdentity: release },
    net: { createServer: () => ({ once() {}, listen(_port, _host, cb) { cb() }, address: () => ({ port: 9900 }), close(cb) { cb() } }) }
  }, { process: processContext, Date: Clock, TextDecoder, console: { log: (...args) => logs.push(args), warn: (...args) => logs.push(args), error: (...args) => logs.push(args) }, setTimeout: (cb, ms) => { const timer = { cb, ms }; timers.push(timer); return timer }, clearTimeout: (timer) => { timer.cancelled = true } })
  const win = { webContents: { send: (_event, info) => statuses.push(info) }, isVisible: () => true, once() {} }
  const owned = (pid = 40, extra = {}) => ({ runtime: 'ok', executable: canonical(exe), pid, build_id: packaged ? 'new' : 'source', channel, cwd: canonical(cwd), data_home: canonical(home || (packaged || channel === 'beta' ? userData : cwd)), ...extra })
  const listen = (port, runtime = owned(), parentPid = 1) => { listeners.set(port, { pid: runtime.pid, executable: runtime.executable, parentPid }); runtimes.set(port, runtime) }
  const tick = async () => { await flush(); const timer = timers.find((t) => !t.used && !t.cancelled); if (timer) { timer.used = true; clock += timer.ms; timer.cb() }; await flush() }
  return { backend, win, owned, listen, exe, cwd, userData, release, listeners, runtimes, processes, queries, http, kills, writes, statuses, spawned, timers, dialogs, logs, tick, hooks }
}

async function loseInspection(f, port = 8801) {
  f.listeners.set(port, new Error('temporary OS inspection denied'))
  for (let i = 0; i < 3; i++) await f.tick()
  assert.throws(() => f.backend.currentBackendBase())
  await assert.rejects(f.backend.whenBackendPort())
  assert.equal(f.statuses.at(-1).status, 'error')
}

// Substitute only the external HTTP boundary; real Response.json() exercises parsing failures.
function probeFault(f, { endpoint = '/api/runtime', status = 200, body = '{}', error }, port = 8801) {
  f.runtimes.set(port, async (url) => {
    if (url.endsWith(endpoint)) {
      if (error) throw error
      return new Response(body, { status })
    }
    return new Response(JSON.stringify(url.endsWith('/openapi.json')
      ? { info: { title: 'spider-gd 控制接口' } } : f.owned()), { status: 200 })
  })
}

async function assertTerminalIdentityFault(f, fault, evidence) {
  probeFault(f, fault)
  await f.tick()
  // Let discarded async verifier promises reach Node's unhandled-rejection check.
  // The test runner fails the test on any such rejection; do not swallow it.
  await new Promise(setImmediate)
  assert.throws(() => f.backend.currentBackendBase(), 'malformed supplied identity must immediately withdraw the public base')
  await assert.rejects(f.backend.whenBackendPort())
  assert.equal(f.statuses.at(-1).status, 'error')
  assert.ok(!f.statuses.at(-1).base)
  assert.ok(f.logs.flat().join(' ').includes(fault.endpoint || '/api/runtime'))
  if (evidence) assert.match(f.logs.flat().join(' '), evidence)
  assert.ok(!f.timers.some((t) => !t.used && !t.cancelled), 'identity contradiction must cancel automatic retries')
  const queries = f.queries.length, statuses = f.statuses.length
  f.runtimes.set(8801, f.owned())
  for (let i = 0; i < 4; i++) await f.tick()
  assert.equal(f.queries.length, queries, 'later valid HTTP identity must not revive a revoked candidate')
  assert.equal(f.statuses.length, statuses)
  assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
  assert.equal(f.spawned.length, 0); assert.deepEqual(f.kills, [])
  await f.backend.stopBackend()
}

function holdRecovery(f, stage, runtime) {
  let release
  if (stage === 'runtime') {
    f.runtimes.set(8801, (url) => url.endsWith('/api/runtime')
      ? new Promise((resolve) => { release = () => resolve({ ok: true, status: 200, json: async () => runtime }) })
      : Promise.resolve({ ok: true, json: async () => ({ info: { title: 'spider-gd 控制接口' } }) }))
  } else {
    let reads = 0
    f.hooks.onQuery = (port, _pid, callback) => {
      if (port !== 8801 || ++reads !== (stage === 'final ownership' ? 2 : 1)) return false
      const listener = JSON.stringify(f.listeners.get(port))
      release = () => callback(null, listener)
      return true
    }
  }
  return () => {
    assert.equal(typeof release, 'function', `recovery must reach ${stage}`)
    return release
  }
}

test('preferred foreign installation cannot hide a saved owned listener; query the actual saved port', async () => {
  const f = fixture({ saved: 9911 })
  f.listen(8801, f.owned(9, { executable: 'D:\\其他安装\\spider-gd-service-beta.exe' }))
  f.listen(9911)
  await f.backend.startBackend(f.win)
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 9911)
  assert.deepEqual(f.kills, [])
  assert.equal(f.spawned.length, 0)
  assert.ok(f.queries.some((q) => q.port === 9911))
  assert.ok(!f.http.includes(8801), 'do not read a foreign target')
})

test('cross-install/cross-channel same and old builds start dynamically without attach, kill or persistence', async () => {
  for (const extra of [{ executable: 'D:\\other\\spider-gd-service-beta.exe' }, { executable: 'D:\\other\\spider-gd-service-beta.exe', build_id: 'old' }, { channel: 'stable' }]) {
    const f = fixture(); f.listen(8801, f.owned(9, extra))
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned[0]?.options.env.WEB__PORT, '9900')
    assert.deepEqual(f.kills, []); assert.deepEqual(f.writes, [])
    assert.ok(!f.statuses.some((s) => s.status === 'ready' || s.base))
    await f.backend.stopBackend()
  }
})

test('OS missing/query failure cannot establish readiness, and query failure is not free-port evidence', async () => {
  for (const listener of [undefined, new Error('denied')]) {
    const f = fixture(); f.runtimes.set(8801, f.owned())
    if (listener) f.listeners.set(8801, listener)
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned.length, 1)
    assert.equal(f.spawned[0].options.env.WEB__PORT, listener ? '9900' : '8801')
    assert.deepEqual(f.writes, [])
    await f.backend.stopBackend()
  }
})

test('stdout is only a candidate; publish its actual port after verified PyInstaller child identity', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  const proc = f.spawned[0].proc
  proc.stdout.emit('data', Buffer.from('SPIDER_GD_LISTEN=127.0.0.1:9922\n'))
  await flush(); assert.deepEqual(f.writes, [])
  f.listen(9922, f.owned(101), proc.pid)
  await f.tick()
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 9922)
  assert.equal(f.statuses.at(-1).status, 'ready')
  assert.ok(f.queries.some((q) => q.port === 9922))
})

test('stdout after ready cannot redirect the verified shutdown target to another port', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  const proc = f.spawned[0].proc
  proc.stdout.emit('data', Buffer.from('SPIDER_GD_LISTEN=127.0.0.1:9922\n'))
  f.listen(9922, f.owned(101), proc.pid); await f.tick()
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 9922)
  f.listen(9933, f.owned(999, { executable: 'D:/foreign/service.exe' }))
  proc.stdout.emit('data', Buffer.from('SPIDER_GD_LISTEN=127.0.0.1:9933\n'))
  await f.backend.stopBackend()
  assert.deepEqual(f.kills, [101, 100])
})

test('ready wait rejects PID/path inconsistency and same-executable non-child', async () => {
  for (const mode of ['pid', 'path', 'non-child']) {
    const f = fixture(); await f.backend.startBackend(f.win)
    f.listen(8801, f.owned(mode === 'non-child' ? 999 : 100))
    if (mode === 'pid') f.listeners.get(8801).pid = 999
    if (mode === 'path') f.listeners.get(8801).executable = 'D:\\other.exe'
    await f.tick()
    assert.deepEqual(f.writes, [])
    assert.ok(!f.statuses.some((s) => s.status === 'ready'))
    await f.backend.stopBackend(); assert.ok(!f.kills.includes(999))
  }
})

test('missing launch and spawn error settle port promise as failure, never default or foreign port', async () => {
  for (const missing of [true, false]) {
    const f = fixture({ missing }); await f.backend.startBackend(f.win)
    if (!missing) f.spawned[0].proc.emit('error', new Error('launch denied'))
    const result = await Promise.race([f.backend.whenBackendPort().then(() => 'ready', () => 'rejected'), flush().then(() => 'pending')])
    assert.equal(result, 'rejected')
    assert.deepEqual(f.writes, [])
    assert.throws(() => f.backend.currentBackendBase())
  }
})

test('stop invalidates an in-flight ready read; old exit cannot clear a new generation', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  const old = f.spawned[0].proc
  old.emit('exit', 0, null)
  await f.backend.startBackend(f.win)
  const newer = f.spawned[1].proc
  old.emit('exit', 0, null)
  f.listen(8801, f.owned(newer.pid)); await f.tick(); await f.tick()
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  await f.backend.stopBackend()
  assert.ok(f.kills.includes(newer.pid))
  assert.throws(() => f.backend.currentBackendBase())
})

test('attached existing process is not stopped; a PID changed before replace is not killed', async () => {
  const attached = fixture(); attached.listen(8801)
  await attached.backend.startBackend(attached.win); await attached.backend.stopBackend()
  assert.deepEqual(attached.kills, [])
  const f = fixture(); f.listen(8801, f.owned(40, { build_id: 'old' }))
  f.runtimes.set(8801, async (url) => {
    if (url.endsWith('/api/runtime')) f.listeners.set(8801, { pid: 99, executable: f.exe })
    return { ok: true, status: 200, json: async () => url.endsWith('openapi.json') ? { info: { title: 'spider-gd 控制接口' } } : f.owned(40, { build_id: 'old' }) }
  })
  await f.backend.startBackend(f.win)
  assert.deepEqual(f.kills, []); assert.deepEqual(f.writes, [])
})

test('Windows listener query explicitly emits UTF8 and preserves non-ASCII path', async () => {
  const f = fixture(); f.listen(8801)
  await f.backend.startBackend(f.win)
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  assert.match(f.queries[0].script, /UTF8Encoding|utf-?8/i)
  assert.match(f.queries[0].script, /OutputEncoding/)
  assert.equal(f.queries[0].options.encoding, 'utf8')
})

test('stop informs renderer and cannot publish an in-flight runtime response afterwards', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  f.listen(8801, f.owned(100))
  let releaseRuntime
  f.runtimes.set(8801, (url) => url.endsWith('/api/runtime')
    ? new Promise((resolve) => { releaseRuntime = () => resolve({ ok: true, status: 200, json: async () => f.owned(100) }) })
    : Promise.resolve({ ok: true, json: async () => ({ info: { title: 'spider-gd 控制接口' } }) }))
  await f.tick(); assert.equal(typeof releaseRuntime, 'function')
  await f.backend.stopBackend(); releaseRuntime(); await flush()
  assert.deepEqual(f.writes, [])
  assert.equal(f.statuses.at(-1).status, 'stopped')
  assert.ok(!f.statuses.some((s) => s.status === 'ready'))
})

test('readiness timeout rejects waiters without publishing a candidate port', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  for (let i = 0; i < 45; i++) await f.tick()
  await assert.rejects(f.backend.whenBackendPort(), /超时/)
  assert.deepEqual(f.writes, [])
  assert.equal(f.statuses.at(-1).status, 'error')
  assert.ok(!f.statuses.some((s) => s.base))
  assert.deepEqual(f.kills, [100])
  await f.backend.startBackend(f.win)
  assert.equal(f.spawned.length, 2)
  f.spawned[0].proc.emit('exit', 0, null)
  f.listen(8801, f.owned(101)); await f.tick()
  assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
  await f.backend.stopBackend()
})

test('old build and legacy missing runtime replace only the reverified expected installation PID', async () => {
  for (const legacy of [false, true]) {
    const f = fixture(); f.listen(8801, f.owned(40, legacy ? { runtime: 'missing' } : { build_id: 'old' }))
    f.hooks.onKill = (pid) => { if (pid === 40) { f.listeners.delete(8801); f.runtimes.delete(8801) } }
    await f.backend.startBackend(f.win)
    assert.deepEqual(f.kills, [40]); assert.equal(f.spawned[0].options.env.WEB__PORT, '8801')
    assert.deepEqual(f.writes, [])
  }
})

test('replacement does not chase a later same-name PID; query failure is not successful exit', async () => {
  for (const next of [{ pid: 99, executable: 'C:\\安装\\resources\\backend\\spider-gd-service-beta.exe' }, new Error('query denied')]) {
    const f = fixture(); f.listen(8801, f.owned(40, { build_id: 'old' }))
    f.hooks.onKill = () => f.listeners.set(8801, next)
    await f.backend.startBackend(f.win)
    assert.deepEqual(f.kills, [40]); assert.equal(f.spawned.length, 0)
    await assert.rejects(f.backend.whenBackendPort())
    assert.deepEqual(f.writes, [])
  }
})

test('stop cancels an in-flight replacement before kill and prevents a stale error status', async () => {
  for (const queryError of [false, true]) {
    const f = fixture(); f.listen(8801, f.owned(40, { build_id: 'old' }))
    let releaseQuery
    f.hooks.onQuery = (port, _pid, callback) => {
      if (port !== 8801 || f.queries.filter((q) => q.port === port).length !== 2) return false
      releaseQuery = () => callback(queryError ? new Error('denied') : null, queryError ? '' : JSON.stringify(f.listeners.get(port)))
      return true
    }
    f.hooks.onKill = () => { f.listeners.delete(8801); f.runtimes.delete(8801) }
    const starting = f.backend.startBackend(f.win); await flush()
    assert.equal(typeof releaseQuery, 'function')
    await f.backend.stopBackend(); releaseQuery(); await starting
    assert.deepEqual(f.kills, [])
    assert.deepEqual(f.writes, [])
    assert.equal(f.statuses.at(-1).status, 'stopped')
  }
})

test('quit rechecks ready PID/path and does not kill a changed listener', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  f.listen(8801, f.owned(100)); await f.tick()
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  f.listeners.set(8801, { pid: 999, executable: f.exe })
  await f.backend.stopBackend(); assert.deepEqual(f.kills, [])
})

test('Windows junction and case resolve to one installation identity', async () => {
  const f = fixture({ aliases: {
    'C:\\安装\\resources\\backend\\spider-gd-service-beta.exe': 'D:\\实际\\backend\\spider-gd-service-beta.exe',
    'C:\\安装\\resources\\backend': 'D:\\实际\\backend',
    'C:\\安装\\用户\\beta': 'D:\\实际\\用户\\beta'
  } })
  f.listen(8801); await f.backend.startBackend(f.win)
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  assert.equal(f.spawned.length, 0)
})

test('preferred owned listener wins over saved foreign runtime and saved foreign port is never read', async () => {
  const f = fixture({ saved: 9944 }); f.listen(8801)
  f.listen(9944, f.owned(66, { executable: 'D:\\foreign\\spider-gd-service-beta.exe' }))
  await f.backend.startBackend(f.win)
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  assert.ok(!f.http.includes(9944)); assert.deepEqual(f.kills, [])
})

test('development lifecycle checks repository, HOME and channel even when the interpreter matches', async () => {
  for (const extra of [{ cwd: '/another-repo' }, { data_home: '/formal-home' }, { channel: 'stable' }]) {
    const f = fixture({ platform: 'darwin', packaged: false })
    f.listen(8801, f.owned(40, extra)); await f.backend.startBackend(f.win)
    assert.equal(f.spawned[0]?.options.env.WEB__PORT, '9900')
    assert.deepEqual(f.kills, []); assert.deepEqual(f.writes, [])
  }
  const f = fixture({ platform: 'darwin', packaged: false, home: '  "relative/资料"  ' })
  f.listen(8801, f.owned(40, { data_home: '/repo/relative/资料' }))
  await f.backend.startBackend(f.win)
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  assert.equal(f.backend.currentBackendDataDir(), '/repo/relative/资料/data')
})

test('macOS lsof/ps and Linux ss/proc use actual ports and interpreter symlink ownership', async () => {
  for (const platform of ['darwin', 'linux']) {
    const python = '/repo/.venv/bin/python'
    const f = fixture({ platform, packaged: false, saved: 9933, aliases: { [python]: '/opt/miniconda3/bin/python3.13' } })
    f.listen(9933)
    await f.backend.startBackend(f.win)
    assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 9933)
    assert.equal(f.spawned.length, 0)
    assert.ok(f.queries.some((q) => q.port === 9933))
    assert.ok(f.queries.some((q) => q.cmd === (platform === 'darwin' ? 'lsof' : 'ss')))
  }
})

test('replacement retry stays blocked until the original stopped PID actually exits', async () => {
  for (const platform of ['win32', 'darwin', 'linux']) {
    const f = fixture({ platform })
    f.listen(8801, f.owned(40, { build_id: 'old' }))
    f.processes.set(40, { pid: 40, executable: f.exe, parentPid: 1 })
    const releasePort = () => { f.listeners.delete(8801); f.runtimes.delete(8801) }
    f.hooks.onSignal = releasePort
    f.hooks.onKill = (_pid, callback) => {
      releasePort()
      callback(new Error('process still alive'), '')
      return true
    }
    const first = f.backend.startBackend(f.win)
    for (let i = 0; i < 25; i++) await f.tick()
    await first
    assert.equal(f.processes.has(40), true)
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned.length, 0)
    await assert.rejects(f.backend.whenBackendPort())
    assert.deepEqual(f.kills, [40])
    f.processes.delete(40)
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned.length, 1)
    f.hooks.onSignal = null
    f.hooks.onKill = null
    await f.backend.stopBackend()
  }
})

test('a parent exit during its OS query does not permanently block a cleaned-up run', async () => {
  const f = fixture()
  await f.backend.startBackend(f.win)
  f.listen(8801, f.owned(101), 100)
  f.runtimes.delete(8801)
  f.hooks.onQuery = (_port, pid, callback) => {
    if (pid !== 100 || !f.kills.includes(101)) return false
    f.processes.delete(100)
    f.spawned[0].proc.emit('exit', 0, null)
    callback(new Error('parent already exited'), '')
    return true
  }
  for (let i = 0; i < 45; i++) await f.tick()
  assert.deepEqual(f.kills, [101])
  await f.backend.startBackend(f.win)
  assert.equal(f.spawned.length, 2)
  await f.backend.stopBackend()
})

test('timeout cleanup can retry after its owned child and spawned parent both exit', async () => {
  for (const platform of ['win32', 'darwin', 'linux']) {
    const f = fixture({ platform })
    await f.backend.startBackend(f.win)
    f.listen(8801, f.owned(101), 100)
    f.runtimes.delete(8801)
    const exitParent = (pid) => {
      if (pid === 101) {
        f.processes.delete(100)
        f.spawned[0].proc.emit('exit', 0, null)
      }
    }
    f.hooks.onKill = (pid) => { exitParent(pid); return false }
    f.hooks.onSignal = (pid) => {
      exitParent(pid)
      f.processes.delete(pid)
      f.listeners.delete(8801)
    }
    for (let i = 0; i < 45; i++) await f.tick()
    await assert.rejects(f.backend.whenBackendPort(), /超时/)
    assert.deepEqual(f.kills, [101])
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned.length, 2)
    f.hooks.onSignal = null
    await f.backend.stopBackend()
  }
})

test('a second start cannot spawn while an old POSIX backend is still exiting', async () => {
  for (const platform of ['darwin', 'linux']) {
    const f = fixture({ platform })
    f.listen(8801, f.owned(40, { build_id: 'old' }))
    f.processes.set(40, { pid: 40, executable: f.exe, parentPid: 1 })
    f.hooks.onSignal = () => { f.listeners.delete(8801); f.runtimes.delete(8801) }
    const first = f.backend.startBackend(f.win)
    try {
      await flush()
      assert.deepEqual(f.kills, [40])
      await f.backend.startBackend(f.win)
      assert.equal(f.processes.has(40), true)
      assert.equal(f.spawned.length, 0)
    } finally {
      f.processes.delete(40)
      await f.tick()
      await first
      f.hooks.onSignal = null
      await f.backend.stopBackend()
    }
  }
})

test('timeout cleanup cannot retry while a POSIX process ignores SIGTERM', async () => {
  for (const platform of ['darwin', 'linux']) {
    const f = fixture({ platform })
    f.hooks.onSignal = () => {}
    await f.backend.startBackend(f.win)
    for (let i = 0; i < 70; i++) await f.tick()
    assert.deepEqual(f.kills, [100])
    assert.equal(f.processes.has(100), true)
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned.length, 1)
    await assert.rejects(f.backend.whenBackendPort(), /超时/)
  }
})

test('timeout cleanup does not allow a retry when child ownership could not be verified', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  f.listen(8801, f.owned(101), 100)
  f.runtimes.delete(8801)
  let parentReads = 0
  f.hooks.onQuery = (_port, pid, callback) => {
    if (pid !== 100 || ++parentReads !== 1) return false
    callback(new Error('parent identity unavailable'), '')
    return true
  }
  for (let i = 0; i < 45; i++) await f.tick()
  await assert.rejects(f.backend.whenBackendPort(), /超时/)
  assert.deepEqual(f.kills, [100])
  await f.backend.startBackend(f.win)
  assert.equal(f.spawned.length, 1)
  assert.equal(f.listeners.get(8801).pid, 101)
  await f.backend.stopBackend()
})

test('concurrent stop calls wait for the same in-flight process cleanup', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  let finishKill
  f.hooks.onKill = (_pid, callback) => { finishKill = () => callback(null, ''); return true }
  let firstDone = false, secondDone = false
  const first = f.backend.stopBackend().then(() => { firstDone = true })
  await flush()
  assert.equal(typeof finishKill, 'function')
  const second = f.backend.stopBackend().then(() => { secondDone = true })
  await flush()
  assert.equal(firstDone, false)
  assert.equal(secondDone, false)
  finishKill()
  await Promise.all([first, second])
  assert.equal(firstDone, true)
  assert.equal(secondDone, true)
  assert.deepEqual(f.kills, [100])
})

test('stop before readiness rechecks and terminates only a direct spawned listener child', async () => {
  const f = fixture(); await f.backend.startBackend(f.win)
  f.listen(8801, f.owned(101), 100)
  assert.ok(!f.statuses.some((s) => s.status === 'ready'))
  await f.backend.stopBackend()
  assert.deepEqual(f.kills, [101, 100])
  assert.throws(() => f.backend.currentBackendBase())
  await f.tick()
  assert.ok(!f.statuses.some((s) => s.status === 'ready'))
})

test('attached readiness is revoked when the listener exits, changes owner or cannot be checked', async () => {
  for (const changed of [null, { pid: 999, executable: 'D:\\other\\service.exe', parentPid: 1 }, new Error('OS query denied')]) {
    const f = fixture(); f.listen(8801)
    await f.backend.startBackend(f.win)
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
    if (changed) f.listeners.set(8801, changed)
    else f.listeners.delete(8801)
    // 瞬时失败先重试；连续三次无法核实才撤销已公布的端口。
    for (let i = 0; i < 3; i++) await f.tick()
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort())
    assert.equal(f.statuses.at(-1).status, 'error')
    assert.ok(!f.statuses.at(-1).base)
    assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
    assert.deepEqual(f.kills, [])
  }
})

test('transient OS failures retry only the owned saved port and republish after fresh verification', async () => {
  for (const platform of ['win32', 'darwin', 'linux']) {
    const f = fixture({ platform, saved: 9933 }); f.listen(9933)
    await f.backend.startBackend(f.win)
    await loseInspection(f, 9933)
    const failedPort = f.backend.whenBackendPort()
    const queries = f.queries.length
    for (let i = 0; i < 5; i++) await f.tick()
    assert.ok(f.queries.length > queries, 'degraded connection must keep checking')
    assert.ok(f.queries.slice(queries).every((q) => q.port === 9933))
    assert.equal(f.dialogs.length, 1, 'one dialog per outage, not per retry')
    assert.equal(f.statuses.filter((s) => s.status === 'error').length, 1)
    assert.equal(f.writes.length, 1, 'never persist an unverified port again')
    assert.match(f.dialogs[0].message, /9933/)
    assert.match(f.logs.flat().join(' '), /temporary OS inspection denied/)
    f.listen(9933); await f.tick()
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:9933')
    assert.equal(await f.backend.whenBackendPort(), 9933)
    await assert.rejects(failedPort, /核实/)
    assert.equal(f.statuses.filter((s) => s.status === 'ready').length, 2)
    assert.equal(f.writes.length, 2)
    await f.tick(); assert.equal(f.writes.length, 2)
    await loseInspection(f, 9933)
    assert.equal(f.dialogs.length, 2, 'a new outage must still be reported')
    assert.equal(f.spawned.length, 0); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  }
})

test('spawned owned backend recovers after transient HTTP health failures without restart', async () => {
  for (const endpoint of ['openapi.json', '/api/runtime']) {
    const f = fixture(); await f.backend.startBackend(f.win)
    f.listen(8801, f.owned(101), 100); await f.tick()
    const health = f.owned(101)
    f.runtimes.set(8801, async (url) => {
      if (url.endsWith(endpoint)) throw new Error('temporary HTTP timeout')
      return { ok: true, json: async () => ({ info: { title: 'spider-gd 控制接口' } }) }
    })
    for (let i = 0; i < 5; i++) await f.tick()
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort())
    assert.equal(f.dialogs.length, 1)
    assert.equal(f.writes.length, 1)
    f.listen(8801, health, 100); await f.tick()
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
    assert.equal(await f.backend.whenBackendPort(), 8801)
    assert.equal(f.spawned.length, 1); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend(); assert.deepEqual(f.kills, [101, 100])
  }
})

test('observed foreign owner or listener exit revokes readiness immediately and cancels retries', async () => {
  for (const changed of [null, { pid: 999, executable: 'D:/foreign/service.exe', parentPid: 1 }, { pid: 999, executable: 'C:/安装/resources/backend/spider-gd-service-beta.exe', parentPid: 1 }]) {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    if (changed) f.listeners.set(8801, changed)
    else f.listeners.delete(8801)
    await f.tick()
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort())
    const queries = f.queries.length
    f.listen(8801)
    for (let i = 0; i < 5; i++) await f.tick()
    assert.equal(f.queries.length, queries, 'observed identity loss is not a transient inspection error')
    assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
    assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  }
})

test('reconnect rejects changed PID, executable, channel, build, cwd, HOME and foreign runtime', async () => {
  for (const extra of [{ pid: 99 }, { executable: 'D:/foreign/service.exe' }, { channel: 'stable' }, { build_id: 'old' }, { cwd: 'D:/other' }, { data_home: 'D:/other-home' }, { runtime: 'foreign' }, { pid: 99, runtimePidOnly: true }, { executable: 'D:/other.exe', runtimeExeOnly: true }]) {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    await loseInspection(f)
    f.listen(8801, f.owned(40, extra))
    if (extra.runtimePidOnly || extra.runtimeExeOnly) f.listeners.set(8801, { pid: 40, executable: f.exe, parentPid: 1 })
    const queries = f.queries.length
    await f.tick()
    assert.ok(f.queries.length > queries, 'must actually inspect the recovery candidate')
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort())
    const terminalQueries = f.queries.length
    f.listen(8801)
    for (let i = 0; i < 5; i++) await f.tick()
    assert.equal(f.queries.length, terminalQueries, 'never revive a candidate after observed identity loss')
    assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
    assert.ok(f.logs.length >= 2, 'retain transient and terminal failure evidence')
    assert.deepEqual(f.kills, []); assert.equal(f.spawned.length, 0)
    await f.backend.stopBackend()
  }
})

for (const fault of [
  { name: 'runtime HTTP 404', status: 404, body: '{}', evidence: /404/ },
  { name: 'runtime HTTP 200 empty identity', status: 200, body: '{}', evidence: /pid/ },
  { name: 'openapi HTTP 404', endpoint: '/openapi.json', status: 404, body: '{}', evidence: /404/ },
  { name: 'openapi HTTP 200 missing title', endpoint: '/openapi.json', status: 200, body: '{}', evidence: /info\.title/ }
]) {
  test(`${fault.name} once preserves verified readiness and resumes checking`, async () => {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    probeFault(f, fault)
    await f.tick()
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801', 'one missing-evidence read must not permanently revoke')
    assert.equal(await f.backend.whenBackendPort(), 8801)
    assert.equal(f.dialogs.length, 0); assert.equal(f.writes.length, 1)
    f.runtimes.set(8801, f.owned())
    const queries = f.queries.length
    await f.tick()
    assert.ok(f.queries.length > queries, 'a transient absence must not cancel the watcher')
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
    assert.equal(f.writes.length, 1); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  })

  test(`${fault.name} three times withdraws only the public port and recovers after final OS verification`, async () => {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    probeFault(f, fault)
    for (let i = 0; i < 2; i++) {
      await f.tick()
      assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801', 'retain the existing three-failure threshold')
      assert.equal(f.dialogs.length, 0)
    }
    await f.tick()
    assert.throws(() => f.backend.currentBackendBase())
    const rejectedPort = f.backend.whenBackendPort()
    await assert.rejects(rejectedPort)
    assert.equal(f.statuses.at(-1).status, 'error'); assert.ok(!f.statuses.at(-1).base)
    assert.ok(f.dialogs[0].message.includes(fault.endpoint || '/api/runtime'))
    assert.match(f.dialogs[0].message, fault.evidence)
    const queries = f.queries.length
    for (let i = 0; i < 4; i++) {
      await f.tick()
      assert.throws(() => f.backend.currentBackendBase(), 'retry candidate must remain private')
    }
    assert.ok(f.queries.length > queries, 'keep checking beyond the withdrawal threshold')
    assert.equal(f.dialogs.length, 1); assert.equal(f.writes.length, 1)
    // Even a mostly complete identity must not republish without the missing field.
    f.runtimes.set(8801, f.owned(40, { build_id: undefined }))
    await f.tick()
    assert.throws(() => f.backend.currentBackendBase()); assert.equal(f.writes.length, 1)
    f.runtimes.set(8801, f.owned())
    const held = holdRecovery(f, 'final ownership', f.owned())
    await f.tick(); const release = held()
    assert.throws(() => f.backend.currentBackendBase(), 'HTTP success alone must not publish')
    await assert.rejects(f.backend.whenBackendPort()); assert.equal(f.writes.length, 1)
    release(); await flush()
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
    assert.equal(await f.backend.whenBackendPort(), 8801)
    await assert.rejects(rejectedPort)
    assert.equal(f.statuses.filter((s) => s.status === 'ready').length, 2)
    assert.equal(f.writes.length, 2); assert.equal(f.dialogs.length, 1)
    assert.equal(f.spawned.length, 0); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  })
}

for (const field of ['pid', 'executable', 'channel', 'build_id', 'cwd', 'data_home']) {
  test(`one missing runtime ${field} is retryable without relaxing complete recovery identity`, async () => {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    const incomplete = f.owned(); delete incomplete[field]
    f.runtimes.set(8801, incomplete)
    await f.tick()
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
    assert.equal(f.dialogs.length, 0)
    f.runtimes.set(8801, f.owned())
    const queries = f.queries.length
    await f.tick()
    assert.ok(f.queries.length > queries)
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
    assert.equal(f.writes.length, 1); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  })
}

for (const contradiction of [
  { name: 'PID', body: '{"pid":99}' },
  { name: 'executable', body: '{"executable":"D:/foreign/service.exe"}' },
  { name: 'channel', body: '{"channel":"stable"}' },
  { name: 'build', body: '{"build_id":"old"}' },
  { name: 'cwd', body: '{"cwd":"D:/other"}' },
  { name: 'HOME', body: '{"data_home":"D:/other-home"}' },
  { name: 'foreign title', endpoint: '/openapi.json', body: '{"info":{"title":"foreign API"}}' }
]) {
  for (const degraded of [false, true]) {
    test(`partial identity with contradictory ${contradiction.name} permanently cancels ${degraded ? 'private recovery' : 'ready connection'}`, async () => {
      const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
      if (degraded) await loseInspection(f)
      f.listeners.set(8801, { pid: 40, executable: f.exe, parentPid: 1 })
      probeFault(f, contradiction)
      const queries = f.queries.length
      await f.tick()
      assert.ok(f.queries.length > queries)
      assert.throws(() => f.backend.currentBackendBase(), 'missing other fields must not hide contradictory identity')
      await assert.rejects(f.backend.whenBackendPort())
      const terminalQueries = f.queries.length
      f.runtimes.set(8801, f.owned())
      for (let i = 0; i < 5; i++) await f.tick()
      assert.equal(f.queries.length, terminalQueries, 'observed contradiction permanently cancels this candidate')
      assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
      assert.equal(f.spawned.length, 0); assert.deepEqual(f.kills, [])
      await f.backend.stopBackend()
    })
  }
}

for (const endpoint of ['/openapi.json', '/api/runtime']) {
  for (const fault of [
    { name: 'HTTP error', status: 503, evidence: [/503/] },
    { name: 'timeout', error: Object.assign(new Error('probe timed out'), { name: 'TimeoutError' }), evidence: [/TimeoutError/, /probe timed out/] },
    { name: 'invalid JSON', body: '{"broken":', evidence: [/200/, /JSON/, /SyntaxError/] }
  ]) {
    test(`${endpoint} ${fault.name} retains endpoint and failure diagnostics in the withdrawal log`, async () => {
      const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
      probeFault(f, { ...fault, endpoint })
      for (let i = 0; i < 3; i++) await f.tick()
      assert.throws(() => f.backend.currentBackendBase())
      await assert.rejects(f.backend.whenBackendPort())
      const diagnostic = f.logs.flat().join(' ')
      assert.ok(diagnostic.includes(endpoint), 'withdrawal log must identify the failed HTTP endpoint')
      assert.ok(f.dialogs[0].message.includes(endpoint), 'outage dialog must retain HTTP evidence')
      for (const evidence of fault.evidence) {
        assert.match(diagnostic, evidence)
        assert.match(f.dialogs[0].message, evidence)
      }
      for (let i = 0; i < 4; i++) await f.tick()
      assert.equal(f.dialogs.length, 1); assert.equal(f.writes.length, 1)
      f.runtimes.set(8801, f.owned()); await f.tick()
      assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
      assert.equal(await f.backend.whenBackendPort(), 8801)
      assert.equal(f.spawned.length, 0); assert.deepEqual(f.kills, [])
      await f.backend.stopBackend()
    })
  }
}

test('a later OS query failure cannot mask an already observed foreign same-executable PID', async () => {
  const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
  await loseInspection(f)
  f.listen(8801, f.owned(99))
  let reads = 0
  f.hooks.onQuery = (port, _pid, callback) => {
    if (port !== 8801 || ++reads !== 2) return false
    callback(new Error('later OS query denied'), '')
    return true
  }
  await f.tick()
  assert.throws(() => f.backend.currentBackendBase())
  const queries = f.queries.length
  f.hooks.onQuery = null; f.listen(8801)
  await f.tick()
  assert.equal(f.queries.length, queries, 'observed foreign ownership must cancel the candidate, not retry')
  assert.throws(() => f.backend.currentBackendBase())
  await assert.rejects(f.backend.whenBackendPort())
  assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
  assert.deepEqual(f.kills, [])
  await f.backend.stopBackend()
})

test('explicit stop cancels queued reconnect and stale timer callbacks', async () => {
  const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
  await loseInspection(f)
  const retry = f.timers.find((t) => !t.used && !t.cancelled)
  assert.ok(retry, 'there must be a reconnect to cancel')
  await f.backend.stopBackend()
  f.listen(8801)
  const queries = f.queries.length
  retry.cb(); await flush()
  for (let i = 0; i < 5; i++) await f.tick()
  assert.equal(f.queries.length, queries)
  assert.throws(() => f.backend.currentBackendBase())
  await assert.rejects(f.backend.whenBackendPort(), /停止/)
  assert.equal(f.statuses.at(-1).status, 'stopped')
  assert.equal(f.writes.length, 1); assert.deepEqual(f.kills, [])
})

test('stop cannot be undone by in-flight reconnect OS, runtime or final ownership responses', async () => {
  for (const stage of ['OS', 'runtime', 'final ownership']) {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    await loseInspection(f)
    f.listen(8801)
    const held = holdRecovery(f, stage, f.owned())
    await f.tick(); const release = held()
    await f.backend.stopBackend(); release(); await flush()
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort(), /停止/)
    assert.equal(f.statuses.at(-1).status, 'stopped')
    assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
    const queries = f.queries.length
    await f.tick(); assert.equal(f.queries.length, queries)
    assert.deepEqual(f.kills, [])
  }
})

test('genuine spawned exit cancels queued reconnect and retains code or signal evidence', async () => {
  for (const [code, signal, evidence] of [[17, null, /17/], [null, 'SIGKILL', /SIGKILL/]]) {
    const f = fixture(); await f.backend.startBackend(f.win)
    f.listen(8801, f.owned(100)); await f.tick()
    await loseInspection(f)
    assert.ok(f.timers.some((t) => !t.used && !t.cancelled))
    f.processes.delete(100); f.listeners.delete(8801)
    f.spawned[0].proc.emit('exit', code, signal)
    f.listen(8801, f.owned(100))
    const queries = f.queries.length
    for (let i = 0; i < 5; i++) await f.tick()
    assert.equal(f.queries.length, queries)
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort(), /退出/)
    assert.equal(f.statuses.at(-1).status, 'stopped')
    assert.match(f.logs.flat().join(' '), evidence)
    assert.equal(f.writes.length, 1); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  }
})

test('old reconnect responses cannot revive an exited generation or replace the newer connection', async () => {
  for (const stage of ['OS', 'runtime', 'final ownership']) {
    const f = fixture(); await f.backend.startBackend(f.win)
    f.listen(8801, f.owned(100)); await f.tick()
    await loseInspection(f)
    f.listen(8801, f.owned(100))
    const held = holdRecovery(f, stage, f.owned(100))
    await f.tick(); const release = held()
    f.processes.delete(100); f.listeners.delete(8801); f.runtimes.delete(8801)
    f.spawned[0].proc.emit('exit', 17, null)
    f.hooks.onQuery = null
    await f.backend.startBackend(f.win)
    f.spawned[1].proc.stdout.emit('data', Buffer.from('SPIDER_GD_LISTEN=127.0.0.1:9922\n'))
    f.listen(9922, f.owned(101)); await f.tick()
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:9922')
    const statuses = f.statuses.length, writes = f.writes.length
    release(); await flush()
    f.spawned[0].proc.emit('exit', 17, null)
    assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:9922')
    assert.equal(await f.backend.whenBackendPort(), 9922)
    assert.equal(f.statuses.length, statuses); assert.equal(f.writes.length, writes)
    assert.equal(f.spawned.length, 2); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  }
})

const malformedIdentities = [
  { name: 'PID object', field: 'pid', value: { toString: null } },
  { name: 'PID array', field: 'pid', value: [40] },
  { name: 'numeric PID string', field: 'pid', value: '40' },
  { name: 'empty PID string', field: 'pid', value: '' },
  { name: 'nonnumeric PID string', field: 'pid', value: 'not-a-pid' },
  { name: 'negative PID', field: 'pid', value: -40 },
  { name: 'zero PID', field: 'pid', value: 0 },
  { name: 'fractional PID', field: 'pid', value: 40.5 },
  { name: 'unsafe integer PID', field: 'pid', value: 9007199254740992 },
  { name: 'boolean PID', field: 'pid', value: true },
  { name: 'cwd object', field: 'cwd', value: { toString: null } },
  { name: 'executable array', field: 'executable', value: ['C:\\安装\\resources\\backend\\spider-gd-service-beta.exe'] },
  { name: 'HOME object', field: 'data_home', value: { toString: null } },
  { name: 'channel array', field: 'channel', value: ['beta'] },
  { name: 'build object', field: 'build_id', value: { toString: null } },
  { name: 'identity array', body: '[]' }
]

for (const malformed of malformedIdentities) {
  for (const degraded of [false, true]) {
    test(`malformed ${malformed.name} cancels ${degraded ? 'private recovery' : 'ready connection'} without an unhandled verifier rejection`, async () => {
      for (const complete of [false, true]) {
        const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
        if (degraded) await loseInspection(f)
        f.listeners.set(8801, { pid: 40, executable: f.exe, parentPid: 1 })
        const body = malformed.body || JSON.stringify({ ...(complete ? f.owned() : {}), [malformed.field]: malformed.value })
        await assertTerminalIdentityFault(f, { body }, /类型|无效/)
      }
    })
  }
}

for (const degraded of [false, true]) {
  test(`nonstring OpenAPI title remains foreign during ${degraded ? 'private recovery' : 'ready verification'} with safe diagnostics`, async () => {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    if (degraded) await loseInspection(f)
    f.listeners.set(8801, { pid: 40, executable: f.exe, parentPid: 1 })
    await assertTerminalIdentityFault(f, { endpoint: '/openapi.json', body: '{"info":{"title":{"toString":null}}}' }, /异主标题/)
  })

  for (const body of ['{"pid":{"toString":null}}', '{"cwd":{"toString":null}}']) {
    test(`explicit OS PID change wins over malformed ${body.includes('pid') ? 'PID' : 'cwd'} during ${degraded ? 'private recovery' : 'ready verification'}`, async () => {
      const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
      if (degraded) await loseInspection(f)
      f.listeners.set(8801, { pid: 99, executable: f.exe, parentPid: 1 })
      await assertTerminalIdentityFault(f, { body }, /接口里的进程号和实际监听进程不一致/)
    })
  }
}

test('startup does not adopt or replace an otherwise matching identity with coercible malformed fields', async () => {
  for (const field of ['pid', 'executable', 'cwd', 'data_home']) {
    for (const numericString of field === 'pid' ? [false, true] : [false]) {
      const f = fixture(); f.listen(8801)
      const identity = f.owned()
      identity[field] = numericString ? '40' : [identity[field]]
      probeFault(f, { body: JSON.stringify(identity) })
      await f.backend.startBackend(f.win)
      assert.throws(() => f.backend.currentBackendBase(), 'coercible JSON fields are not valid runtime identity')
      assert.ok(!f.statuses.some((s) => s.status === 'ready' || s.base))
      assert.equal(f.spawned[0]?.options.env.WEB__PORT, '9900')
      assert.deepEqual(f.writes, []); assert.deepEqual(f.kills, [])
      await f.backend.stopBackend()
    }
  }
})

test('null runtime body and null identity fields remain missing evidence with the three-failure threshold', async () => {
  for (const field of [null, 'pid', 'executable', 'channel', 'build_id', 'cwd', 'data_home']) {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    probeFault(f, { body: field === null ? 'null' : JSON.stringify(f.owned(40, { [field]: null })) })
    for (let i = 0; i < 2; i++) {
      await f.tick()
      assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
      assert.equal(f.dialogs.length, 0)
    }
    await f.tick()
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort())
    assert.equal(f.dialogs.length, 1)
    f.runtimes.set(8801, f.owned())
    const held = holdRecovery(f, 'final ownership', f.owned())
    await f.tick(); const release = held()
    assert.throws(() => f.backend.currentBackendBase(), 'complete HTTP identity still requires final OS ownership')
    release(); await flush()
    assert.equal(await f.backend.whenBackendPort(), 8801)
    assert.equal(f.writes.length, 2); assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  }
})

test('changed private HTTP endpoint/status causes are retained once without retry or dialog spam', async () => {
  const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
  await loseInspection(f)
  const rejected = f.backend.whenBackendPort()
  assert.equal(f.logs.length, 1)
  f.listeners.set(8801, { pid: 40, executable: f.exe, parentPid: 1 })
  for (const [endpoint, status] of [['/api/runtime', 503], ['/openapi.json', 502], ['/api/runtime', 504]]) {
    probeFault(f, { endpoint, status })
    const logs = f.logs.length, queries = f.queries.length
    for (let i = 0; i < 4; i++) await f.tick()
    assert.equal(f.logs.length, logs + 1, 'a changed private cause must be logged once, not discarded or logged each retry')
    assert.ok(f.logs.at(-1).flat().join(' ').includes(endpoint))
    assert.ok(f.logs.at(-1).flat().join(' ').includes(String(status)))
    assert.equal(f.queries.length, queries + 4, 'keep the existing single inspection per retry')
    assert.equal(f.dialogs.length, 1)
    assert.equal(f.statuses.filter((s) => s.status === 'error').length, 1)
    assert.equal(f.writes.length, 1)
    assert.throws(() => f.backend.currentBackendBase())
    assert.equal(f.backend.whenBackendPort(), rejected, 'private logging must not replace the withdrawn port promise')
  }
  f.runtimes.set(8801, f.owned()); await f.tick()
  assert.equal(await f.backend.whenBackendPort(), 8801)
  assert.equal(f.writes.length, 2)
  assert.deepEqual(f.kills, []); assert.equal(f.spawned.length, 0)
  await f.backend.stopBackend()
})

test('recovery rechecks OS ownership after HTTP before republishing, and recheck errors remain retryable', async () => {
  for (const changed of [{ pid: 99, executable: 'C:/安装/resources/backend/spider-gd-service-beta.exe', parentPid: 1 }, new Error('final ownership query denied')]) {
    const f = fixture(); f.listen(8801); await f.backend.startBackend(f.win)
    await loseInspection(f)
    f.listen(8801)
    f.runtimes.set(8801, async (url) => {
      if (url.endsWith('/api/runtime')) f.listeners.set(8801, changed)
      return { ok: true, status: 200, json: async () => url.endsWith('openapi.json') ? { info: { title: 'spider-gd 控制接口' } } : f.owned() }
    })
    const http = f.http.length
    await f.tick()
    assert.ok(f.http.length > http, 'must reach recovery runtime inspection')
    assert.throws(() => f.backend.currentBackendBase())
    await assert.rejects(f.backend.whenBackendPort())
    assert.equal(f.writes.length, 1); assert.equal(f.dialogs.length, 1)
    f.listen(8801); await f.tick()
    if (changed instanceof Error) {
      assert.equal(f.backend.currentBackendBase(), 'http://127.0.0.1:8801')
      assert.equal(await f.backend.whenBackendPort(), 8801)
    } else assert.throws(() => f.backend.currentBackendBase())
    assert.deepEqual(f.kills, [])
    await f.backend.stopBackend()
  }
})

// Windows venv 转发启动器：Scripts\python.exe 只是启动器，真实监听映像是基础解释器。
const BASE_PYTHON = 'C:\\Users\\jayden\\AppData\\Local\\Programs\\Python\\Python311\\python.exe'
const devRuntime = (f, pid, extra = {}) => ({ runtime: 'ok', executable: BASE_PYTHON, pid, build_id: 'source', channel: f.release.channel, cwd: f.cwd, data_home: f.userData, ...extra })

test('Windows dev venv redirector publishes only the probed real image and stops listener then launcher', async () => {
  const f = fixture({ packaged: false, image: BASE_PYTHON })
  await f.backend.startBackend(f.win)
  assert.equal(f.spawned.length, 2, 'the probe counts as a real spawned process')
  assert.ok(f.spawned[0].probe)
  assert.equal(f.spawned[0].command, f.exe)
  assert.deepEqual(Array.from(f.spawned[0].args.slice(0, 4)), ['-B', '-X', 'utf8', '-c'])
  assert.match(f.spawned[0].args[4], /runtime_executable/)
  assert.equal(f.spawned[0].options.cwd, f.cwd)
  const launcher = f.spawned[1]
  assert.ok(!launcher.probe)
  assert.equal(launcher.command, f.exe, 'spawn command stays the real launcher')
  assert.equal(launcher.args[0], 'main.py')
  assert.equal(launcher.proc.pid, 100)
  f.listen(8801, devRuntime(f, 101), 100)
  await f.tick()
  assert.equal(await Promise.race([f.backend.whenBackendPort(), flush().then(() => 'pending')]), 8801)
  assert.equal(f.statuses.at(-1).status, 'ready')
  await f.backend.stopBackend()
  assert.deepEqual(f.kills, [101, 100], 'stop the proven listener child, then the launcher parent')
})

test('Windows dev probe failure or invalid output fails closed without spawning or killing', async () => {
  for (const probe of [new Error('probe crashed'), { stdout: '' }, { stdout: 'relative\\python.exe' }, { stdout: Buffer.from([0xff, 0xfe, 0x41]) }, { stdout: 'C:\\a.exe\r\nC:\\b.exe' }]) {
    const f = fixture({ packaged: false, image: BASE_PYTHON, probe })
    await f.backend.startBackend(f.win)
    const result = await Promise.race([f.backend.whenBackendPort().then(() => 'ready', () => 'rejected'), flush().then(() => 'pending')])
    assert.equal(result, 'rejected', 'probe failure must be a terminal config-style failure')
    assert.throws(() => f.backend.currentBackendBase())
    assert.equal(f.spawned.filter((s) => !s.probe).length, 0, 'never spawn on an unverified interpreter image')
    assert.deepEqual(f.kills, [])
    assert.deepEqual(f.writes, [])
    assert.equal(f.statuses.at(-1).status, 'error')
    assert.equal(f.dialogs.length, 1)
  }
})

test('Windows dev redirector never adopts or kills a foreign or non-child base-python listener', async () => {
  const foreign = fixture({ packaged: false, image: BASE_PYTHON })
  foreign.listen(8801, devRuntime(foreign, 40, { data_home: 'C:\\other-home' }))
  await foreign.backend.startBackend(foreign.win)
  assert.equal(foreign.spawned.filter((s) => !s.probe).length, 1)
  assert.equal(foreign.spawned[1].options.env.WEB__PORT, '9900')
  assert.deepEqual(foreign.kills, [])
  await foreign.backend.stopBackend()
  assert.ok(!foreign.kills.includes(40), 'never kill a foreign PID')
  for (const mode of ['parent-mismatch', 'non-child']) {
    const f = fixture({ packaged: false, image: BASE_PYTHON })
    await f.backend.startBackend(f.win)
    if (mode === 'parent-mismatch') f.processes.set(100, { pid: 100, executable: BASE_PYTHON, parentPid: 1 })
    f.listen(8801, devRuntime(f, 101), mode === 'non-child' ? 555 : 100)
    await f.tick()
    assert.deepEqual(f.writes, [])
    assert.ok(!f.statuses.some((s) => s.status === 'ready'))
    await f.backend.stopBackend()
    assert.ok(!f.kills.includes(101), 'never kill a listener without full parent proof')
    assert.ok(!f.kills.includes(555))
    if (mode === 'parent-mismatch') assert.ok(!f.kills.includes(100), 'launcher stop requires the launcher image proof')
  }
})

test('Windows dev stale runtime reporting the venv launcher path is fail-closed', async () => {
  const f = fixture({ packaged: false, image: BASE_PYTHON })
  await f.backend.startBackend(f.win)
  f.listen(8801, devRuntime(f, 101, { executable: f.exe }), 100)
  f.listeners.get(8801).executable = BASE_PYTHON
  await f.tick()
  assert.deepEqual(f.writes, [])
  assert.ok(!f.statuses.some((s) => s.status === 'ready'), 'runtime/OS image contradiction must not publish')
  await f.backend.stopBackend()
  assert.deepEqual(f.kills, [101, 100], 'only the proven own child and launcher may be stopped')
})

test('Windows dev redirector still rejects wrong HOME, channel and cwd', async () => {
  for (const extra of [{ data_home: 'C:\\formal-home' }, { channel: 'stable' }, { cwd: 'C:\\another-repo' }]) {
    const f = fixture({ packaged: false, image: BASE_PYTHON })
    f.listen(8801, devRuntime(f, 40, extra))
    await f.backend.startBackend(f.win)
    assert.equal(f.spawned[1]?.options.env.WEB__PORT, '9900')
    assert.deepEqual(f.kills, [])
    assert.deepEqual(f.writes, [])
    await f.backend.stopBackend()
  }
})

test('stop requested during the Windows dev probe cancels the launch', async () => {
  let releaseProbe
  const f = fixture({ packaged: false, image: BASE_PYTHON, probe: (_cmd, _args, _options, callback) => { releaseProbe = () => callback(null, Buffer.from(BASE_PYTHON)) } })
  const starting = f.backend.startBackend(f.win)
  await flush()
  assert.equal(typeof releaseProbe, 'function', 'startup must reach the probe')
  await f.backend.stopBackend()
  releaseProbe(); await starting; await flush()
  assert.equal(f.spawned.filter((s) => !s.probe).length, 0, 'a cancelled generation must not spawn')
  assert.deepEqual(f.kills, [])
  assert.deepEqual(f.writes, [])
  assert.equal(f.statuses.at(-1).status, 'stopped')
  await assert.rejects(f.backend.whenBackendPort(), /停止/)
})
