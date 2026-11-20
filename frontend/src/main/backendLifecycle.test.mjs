import assert from 'node:assert/strict'
import test from 'node:test'
import { EventEmitter } from 'node:events'
import { win32, posix } from 'node:path'
import * as channels from '../../release-channel.mjs'
import contract from '../../../src/spider_gd/release-channels.json' with { type: 'json' }
import { loadProduction, flush } from './boundaryFixtures.mjs'

function fixture({ platform = 'win32', channel = 'beta', packaged = true, saved = 0, missing = false, home, aliases = {} } = {}) {
  const path = platform === 'win32' ? win32 : posix
  const root = platform === 'win32' ? 'C:\\安装' : '/repo'
  const resourcesPath = path.join(root, 'resources')
  const cwd = packaged ? path.join(resourcesPath, 'backend') : root
  const release = channels.buildRelease('0.2.8', channel, channel === 'beta' ? '7' : undefined)
  const exe = packaged ? path.join(cwd, `${release.serviceName}${platform === 'win32' ? '.exe' : ''}`) : path.join(root, '.venv', 'bin', 'python')
  const userData = path.join(root, '用户', channel)
  const env = home ? { SPIDER_GD_HOME: home } : {}
  const listeners = new Map(), runtimes = new Map(), processes = new Map()
  const queries = [], http = [], kills = [], writes = [], statuses = [], spawned = [], timers = []
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
    } else assert.fail(`unexpected system command ${cmd}`)
  }
  const spawn = (command, args, options) => {
    const proc = new EventEmitter()
    proc.pid = 100 + spawned.length
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
    return { ok: true, status: runtime.runtime === 'missing' && url.endsWith('/api/runtime') ? 404 : 200,
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
    electron: { app, net, dialog: { showErrorBox() {} } }, child_process: { execFile, spawn }, fs, 'node:fs': fs,
    path: pathModule, 'node:path': pathModule, './backendMatch.mjs': match,
    '../../release-channel.mjs': releaseModule,
    './release': { releaseIdentity: release },
    net: { createServer: () => ({ once() {}, listen(_port, _host, cb) { cb() }, address: () => ({ port: 9900 }), close(cb) { cb() } }) }
  }, { process: processContext, Date: Clock, setTimeout: (cb, ms) => { const timer = { cb, ms }; timers.push(timer); return timer }, clearTimeout: (timer) => { timer.cancelled = true } })
  const win = { webContents: { send: (_event, info) => statuses.push(info) }, isVisible: () => true, once() {} }
  const owned = (pid = 40, extra = {}) => ({ runtime: 'ok', executable: canonical(exe), pid, build_id: packaged ? 'new' : 'source', channel, cwd: canonical(cwd), data_home: canonical(home || (packaged || channel === 'beta' ? userData : cwd)), ...extra })
  const listen = (port, runtime = owned(), parentPid = 1) => { listeners.set(port, { pid: runtime.pid, executable: runtime.executable, parentPid }); runtimes.set(port, runtime) }
  const tick = async () => { await flush(); const timer = timers.find((t) => !t.used && !t.cancelled); if (timer) { timer.used = true; clock += timer.ms; timer.cb() }; await flush() }
  return { backend, win, owned, listen, exe, cwd, userData, release, listeners, runtimes, processes, queries, http, kills, writes, statuses, spawned, timers, tick, hooks }
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
