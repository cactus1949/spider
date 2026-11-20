import assert from 'node:assert/strict'
import test from 'node:test'
import { createRequire } from 'node:module'
import { loadProduction, flush } from './boundaryFixtures.mjs'
const require = createRequire(import.meta.url)
const React = require('react')

function desktopFixture(settings = {}) {
  const requests = [], errors = [], dirs = []
  let status, resolveBase, rejectBase
  const base = new Promise((resolve, reject) => { resolveBase = resolve; rejectBase = reject })
  const window = { api: {
    getBackendBase: () => base,
    onBackendStatus: (callback) => { status = callback; return () => {} },
    getDataDir: async () => { dirs.push('get'); return '/beta/data' }, listDir: async () => [], pickDir: async () => '/picked'
  } }
  const globals = { window, URL, fetch: async (url, init) => {
    requests.push({ url, method: init?.method || 'GET', body: init?.body })
    if (url.endsWith('/api/settings') && !init?.method && settings instanceof Error) throw settings
    return { ok: true, json: async () => url.endsWith('/api/settings') && !init?.method ? { settings } : {} }
  } }
  const module = loadProduction('../renderer/src/services/api.ts', {}, globals)
  return { module, window, globals, requests, errors, dirs, resolveBase, rejectBase, status: (info) => status(info) }
}

test('starting/error bases never authorize renderer requests; only verified ready does', async () => {
  const f = desktopFixture()
  f.status({ status: 'starting', base: 'http://127.0.0.1:8800' })
  const pending = f.module.api.health()
  assert.equal(await Promise.race([pending.then(() => 'fetched'), flush().then(() => 'waiting')]), 'waiting')
  assert.deepEqual(f.requests, [])
  f.status({ status: 'ready', base: 'http://127.0.0.1:9911' })
  await pending
  assert.equal(f.requests[0].url, 'http://127.0.0.1:9911/api/health')
})

test('ready followed by error/stopped/failed invalidates old base and causes no fetch', async () => {
  for (const status of ['error', 'stopped', 'failed']) {
    const f = desktopFixture(); f.resolveBase('http://127.0.0.1:9911'); await flush()
    await f.module.api.health(); assert.equal(f.requests.length, 1)
    f.status({ status, base: 'http://127.0.0.1:8800' })
    await assert.rejects(f.module.backendUrl('/api/export'))
    await assert.rejects(f.module.api.health())
    assert.equal(f.requests.length, 1)
    assert.throws(() => f.module.backendBase())
  }
})

test('IPC failure rejects URLs and API calls instead of falling back to stable port', async () => {
  const f = desktopFixture(); f.rejectBase(new Error('backend failed')); await flush()
  await assert.rejects(f.module.backendUrl('/api/export'), /backend failed/)
  await assert.rejects(f.module.api.health(), /backend failed/)
  assert.deepEqual(f.requests, [])
})

test('fire-and-forget browser cancellation reports failed readiness without fetching a fallback', async () => {
  const f = desktopFixture()
  assert.equal(typeof f.module.cancelBrowserRun, 'function')
  f.rejectBase(new Error('backend failed')); await flush()
  f.module.cancelBrowserRun('run-1', (error) => f.errors.push(String(error)))
  await flush()
  assert.equal(f.errors.length, 1); assert.match(f.errors[0], /backend failed/)
  assert.deepEqual(f.requests, [])
})

test('browser cancellation uses the verified ready base and preserves run identity', async () => {
  const f = desktopFixture(); f.resolveBase('http://127.0.0.1:9911'); await flush()
  f.module.cancelBrowserRun('run-1', (error) => f.errors.push(String(error))); await flush()
  assert.deepEqual(f.requests, [{ url: 'http://127.0.0.1:9911/api/browser/cancel', method: 'POST', body: '{"run_id":"run-1"}' }])
  assert.deepEqual(f.errors, [])
})

test('late IPC success cannot resurrect a stopped renderer connection', async () => {
  const f = desktopFixture(); f.status({ status: 'stopped' })
  f.resolveBase('http://127.0.0.1:8800'); await flush()
  await assert.rejects(f.module.api.health()); assert.deepEqual(f.requests, [])
})

test('invalid ready bases fail closed without throwing from the event callback', async () => {
  for (const base of ['http://127.0.0.1:99999', 'http://127.0.0.1:0', 'http://elsewhere:8800', 'http://127.0.0.1:8800/foreign']) {
    const f = desktopFixture()
    assert.doesNotThrow(() => f.status({ status: 'ready', base }))
    await assert.rejects(f.module.api.health())
    assert.deepEqual(f.requests, [])
  }
})

test('browser-only renderer uses same-origin URLs rather than a desktop stable port', async () => {
  const requests = []
  const module = loadProduction('../renderer/src/services/api.ts', {}, { window: { location: { origin: 'http://localhost:5173' } }, fetch: async (url) => { requests.push(url); return { ok: true, json: async () => ({}) } } })
  assert.equal(await module.backendUrl('/api/export'), '/api/export')
  await module.api.health(); assert.deepEqual(requests, ['/api/health'])
})

// Execute the real Files function with React's dispatcher at the render/effect boundary.
// No DOM or Electron is needed; JSX is kept real, and API/settingsStore remain production.
function renderFiles(f) {
  const states = [], deps = [], cleanups = [], effects = [], memos = []
  let cursor = 0, dirty = false
  const dispatcher = {
    useState(initial) {
      const i = cursor++; if (!(i in states)) states[i] = typeof initial === 'function' ? initial() : initial
      return [states[i], (value) => { const next = typeof value === 'function' ? value(states[i]) : value; if (!Object.is(next, states[i])) { states[i] = next; dirty = true } }]
    },
    useEffect(callback, next) {
      const i = cursor++
      if (!deps[i] || next.some((d, j) => !Object.is(d, deps[i][j]))) {
        deps[i] = next; effects.push(() => { cleanups[i]?.(); cleanups[i] = callback() })
      }
    },
    useMemo(fn, next) {
      const i = cursor++
      if (!deps[i] || next.some((d, j) => !Object.is(d, deps[i][j]))) { deps[i] = next; memos[i] = fn() }
      return memos[i]
    },
    useCallback(fn, next) { return this.useMemo(() => fn, next) }
  }
  const Component = Object.assign(() => null, { Compact: () => null })
  const appContext = { message: { error: (text) => f.errors.push(text), success() {}, info() {}, warning() {} }, modal: {} }
  const ui = new Proxy({ App: { useApp: () => appContext } }, { get: (object, key) => object[key] || Component })
  const Files = loadProduction('../renderer/src/pages/Files.tsx', {
    react: React, antd: ui, '@ant-design/icons': ui, '../services/api': f.module, '../components/UploadProgress': { startDataUpload() {} }
  }, f.globals).default
  const render = () => {
    cursor = 0; dirty = false
    const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
    const previous = internals.ReactCurrentDispatcher.current
    internals.ReactCurrentDispatcher.current = dispatcher
    try { return Files() } finally { internals.ReactCurrentDispatcher.current = previous }
  }
  return async () => {
    let tree = render()
    for (let i = 0; i < 10; i++) {
      effects.splice(0).forEach((effect) => effect()); await flush()
      if (!dirty) return tree
      tree = render()
    }
    throw new Error('Files effect failed to settle')
  }
}

test('Files failed settings load shows error and never PUTs a default download directory', async () => {
  const f = desktopFixture(new Error('settings offline')); f.resolveBase('http://127.0.0.1:9911'); await flush()
  await renderFiles(f)()
  assert.equal(f.module.settingsStore.ready(), false)
  assert.deepEqual(f.requests.map((r) => r.method), ['GET'])
  assert.deepEqual(f.dirs, [])
  assert.ok(f.errors.some((error) => /设置.*加载|加载.*设置/.test(error)))
})

test('Files successful load preserves stored directory; only truly absent directory initializes channel data', async () => {
  for (const settings of [{ downloadDir: '/saved/formal' }, {}]) {
    const expectedDir = settings.downloadDir || '/beta/data'
    const expectedMethods = settings.downloadDir ? ['GET'] : ['GET', 'PUT']
    const f = desktopFixture(settings); f.resolveBase('http://127.0.0.1:9911'); await flush()
    await renderFiles(f)()
    assert.equal(f.module.settingsStore.get('downloadDir'), expectedDir)
    assert.deepEqual(f.requests.map((r) => r.method), expectedMethods)
  }
})
