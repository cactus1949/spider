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

test('backup import invalidates cached settings and reload reads imported values', async () => {
  let stored = { siteUrl: 'original' }
  const requests = []
  const module = loadProduction('../renderer/src/services/api.ts', {}, {
    window: { location: { origin: 'http://localhost:5173' } },
    fetch: async (url, init) => {
      requests.push(url)
      if (url.endsWith('/api/data/import')) {
        stored = { siteUrl: 'imported-site' }
        return { ok: true, json: async () => ({ status: 'ok', imported: ['settings'], errors: [], warnings: [] }) }
      }
      return { ok: true, json: async () => ({ settings: { ...stored } }) }
    }
  })
  await module.settingsStore.load()
  assert.equal(module.settingsStore.get('siteUrl'), 'original')

  await module.api.importAllData({ settings: { siteUrl: 'imported-site' } })

  assert.equal(module.settingsStore.ready(), false)
  const settings = await module.settingsStore.load()
  assert.equal(settings.siteUrl, 'imported-site')
  assert.equal(module.settingsStore.get('siteUrl'), 'imported-site')
  assert.deepEqual(requests, ['/api/settings', '/api/data/import', '/api/settings'])
})

test('late pre-import settings response cannot replace imported settings', async () => {
  let resolveOld
  let reads = 0
  const oldResponse = new Promise((resolve) => { resolveOld = resolve })
  const module = loadProduction('../renderer/src/services/api.ts', {}, {
    window: { location: { origin: 'http://localhost:5173' } },
    fetch: async (url) => {
      if (url.endsWith('/api/data/import')) {
        return { ok: true, json: async () => ({ status: 'ok', imported: [], errors: [], warnings: [] }) }
      }
      reads += 1
      if (reads === 1) return { ok: true, json: () => oldResponse }
      return { ok: true, json: async () => ({ settings: { siteUrl: 'imported-site' } }) }
    }
  })
  const pending = module.settingsStore.load()
  await flush()
  await module.api.importAllData({})
  await module.settingsStore.load()
  resolveOld({ settings: { siteUrl: 'original' } })
  const oldCallerResult = await pending

  assert.equal(oldCallerResult.siteUrl, 'imported-site')
  assert.equal(module.settingsStore.get('siteUrl'), 'imported-site')
})

test('late pre-import settings failure cannot clear imported settings', async () => {
  let rejectOld
  let reads = 0
  const oldResponse = new Promise((_resolve, reject) => { rejectOld = reject })
  const module = loadProduction('../renderer/src/services/api.ts', {}, {
    window: { location: { origin: 'http://localhost:5173' } },
    fetch: async (url) => {
      if (url.endsWith('/api/data/import')) {
        return { ok: true, json: async () => ({ status: 'ok', imported: [], errors: [], warnings: [] }) }
      }
      reads += 1
      if (reads === 1) return { ok: true, json: () => oldResponse }
      return { ok: true, json: async () => ({ settings: { siteUrl: 'imported-site' } }) }
    }
  })
  const pending = module.settingsStore.load()
  const result = pending.catch(() => null)
  await flush()
  await module.api.importAllData({})
  await module.settingsStore.load()
  rejectOld(new Error('old request failed'))
  const oldCallerResult = await result

  assert.equal(oldCallerResult?.siteUrl, 'imported-site')
  assert.equal(module.settingsStore.get('siteUrl'), 'imported-site')
})

test('failed backup upload preserves previously loaded settings', async () => {
  const module = loadProduction('../renderer/src/services/api.ts', {}, {
    window: { location: { origin: 'http://localhost:5173' } },
    fetch: async (url) => {
      if (url.endsWith('/api/data/import')) throw new Error('upload failed')
      return { ok: true, json: async () => ({ settings: { siteUrl: 'original' } }) }
    }
  })
  await module.settingsStore.load()
  await assert.rejects(module.api.importAllData({}), /upload failed/)
  assert.equal(module.settingsStore.get('siteUrl'), 'original')
})

function renderFiles(f, relative = '../renderer/src/pages/Files.tsx', mocks = {}, props = {}) {
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
    useCallback(fn, next) { return this.useMemo(() => fn, next) },
    useRef(initial) {
      const i = cursor++; if (!(i in states)) states[i] = { current: initial }
      return states[i]
    }
  }
  const Component = Object.assign(() => null, { Compact: () => null, Item: () => null, Password: () => null })
  const appContext = { message: {
    error: (text) => f.errors.push(text),
    success: (text) => f.messages?.push({ type: 'success', text }),
    warning: (text) => f.messages?.push({ type: 'warning', text }), info() {}
  }, modal: {} }
  const ui = new Proxy({ App: { useApp: () => appContext } }, { get: (object, key) => object[key] || Component })
  const Files = loadProduction(relative, {
    react: React, antd: ui, '@ant-design/icons': ui, '../services/api': f.module, '../components/UploadProgress': { startDataUpload() {} }, ...mocks
  }, f.globals).default
  const render = () => {
    cursor = 0; dirty = false
    const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
    const previous = internals.ReactCurrentDispatcher.current
    internals.ReactCurrentDispatcher.current = dispatcher
    try { return Files(props) } finally { internals.ReactCurrentDispatcher.current = previous }
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

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements)
  if (!tree || typeof tree !== 'object' || !tree.props) return []
  return [tree, ...elements(tree.props.children)]
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function jsonResponse(data) {
  return { ok: true, json: async () => data }
}

function button(tree, text) {
  const found = elements(tree).find(e => e.props.children === text)
  assert.ok(found, `actual ${text} button is rendered`)
  return found
}

function settingsAlert(tree) {
  return elements(tree).find(e => e.props.message === '设置没有加载成功')
}

async function settingsModalFixture(transport, backup = {}) {
  const f = desktopFixture()
  f.resolveBase('http://127.0.0.1:9911'); await flush()
  f.messages = []
  f.events = []
  f.window.api.getReleaseInfo = async () => ({ channel: 'beta', version: 'fixture' })
  f.window.dispatchEvent = (event) => f.events.push(event.type)
  f.globals.Event = Event
  f.globals.Error = Error
  let change
  f.globals.document = { createElement: () => ({
    files: [{ text: async () => JSON.stringify(backup) }],
    click() { change = this.onchange() }
  }) }
  f.globals.fetch = async (url, init) => {
    f.requests.push({ url, method: init?.method || 'GET', body: init?.body })
    return transport(url, init)
  }
  f.module = loadProduction('../renderer/src/services/api.ts', {}, f.globals)
  const values = {}
  f.form = {
    setFieldsValue: (next) => Object.assign(values, next),
    setFieldValue: (key, value) => { values[key] = value },
    getFieldsValue: () => ({ ...values }), getFieldValue: (key) => values[key],
    validateFields: async () => ({ ...values })
  }
  const Form = Object.assign(() => null, {
    Item: () => null, useForm: () => [f.form], useWatch: () => undefined
  })
  const appContext = { message: {
    error: text => f.errors.push(text),
    success: text => f.messages.push({ type: 'success', text }),
    warning: text => f.messages.push({ type: 'warning', text }), info() {}
  } }
  const App = Object.assign(() => null, { useApp: () => appContext })
  const ui = new Proxy({ Form, App }, { get: (obj, key) => obj[key] || Object.assign(() => null, { Password: () => null, Compact: () => null }) })
  f.props = { open: true, onClose() {} }
  f.render = renderFiles(f, '../renderer/src/components/SettingsModal.tsx', {
    antd: ui, './UploadProfilesPanel': () => null, './VersionCenterPanel': () => null
  }, f.props)
  f.section = async (tree, key) => {
    const menu = elements(tree).find(e => e.props.items)
    assert.ok(menu, 'actual settings menu is rendered')
    menu.props.onClick({ key })
    return f.render()
  }
  f.importData = async (tree) => {
    tree = await f.section(tree, 'section-about')
    button(tree, '导入数据').props.onClick()
    await change
    return f.render()
  }
  return f
}

function stationName(tree) {
  const input = elements(tree).find(e => e.props.placeholder?.startsWith('场站名称'))
  assert.ok(input, 'actual station name input is rendered')
  return input
}

const stationA = '[{"id":"a","name":"Station A","deviceId":"","orgName":"","wifiSsid":"","wifiPassword":"","certPassword":""}]'
const stationB = '[{"id":"b","name":"Station B","deviceId":"","orgName":"","wifiSsid":"","wifiPassword":"","certPassword":""}]'

// Only transport and React/Ant render boundaries are substituted; API/cache/modal stay real.
test('adopted post-import settings failure makes no third GET and keeps Save disabled', async () => {
  const old = deferred(), current = deferred()
  let reads = 0
  const f = await settingsModalFixture(async (url) => {
    if (url.endsWith('/api/data/import')) return jsonResponse({ status: 'ok', imported: ['settings'], errors: [], warnings: [] })
    if (!url.endsWith('/api/settings')) return jsonResponse({})
    reads += 1
    if (reads === 1) return { ok: true, json: () => old.promise }
    if (reads === 2) return { ok: true, json: () => current.promise }
    return jsonResponse({ settings: { siteUrl: 'unintended-recovery' } })
  })
  const oldResult = f.module.settingsStore.load().then(value => ({ value }), error => ({ error }))
  await flush()
  await f.module.api.importAllData({})
  await f.render()
  old.resolve({ settings: { siteUrl: 'old' } }); await flush()
  current.reject(new Error('current settings unavailable'))
  const result = await oldResult
  const tree = await f.render()
  assert.equal(reads, 2, 'a rejected adopted promise must not trigger an implicit retry')
  assert.equal(result.error?.message, 'current settings unavailable')
  assert.equal(f.module.settingsStore.ready(), false)
  assert.equal(settingsAlert(tree)?.props.description, 'current settings unavailable')
  assert.equal(button(tree, '保存').props.disabled, true)
})

test('older modal completions cannot enable Save over the newest failure banner', async () => {
  const responses = []
  let reads = 0
  const f = await settingsModalFixture(async (url) => {
    if (!url.endsWith('/api/settings')) return jsonResponse({})
    reads += 1
    if (reads === 1) throw new Error('initial load failed')
    if (reads === 6) throw new Error('newest load failed')
    const response = deferred(); responses.push(response)
    return { ok: true, json: () => response.promise }
  })
  let tree = await f.render()
  const retry = settingsAlert(tree).props.action.props.onClick
  // Each superseded request adopts the next real API promise. Their UI continuations
  // then finish after a newer invocation has already reported its failure.
  for (let i = 0; i < 4; i++) {
    retry(); await f.render()
    if (i > 0) {
      responses[i - 1].resolve({ settings: { siteUrl: 'superseded' } })
      await flush()
    }
  }
  responses[3].resolve({ settings: { siteUrl: 'older-success' } })
  for (let i = 0; i < 30 && !f.module.settingsStore.ready(); i++) await Promise.resolve()
  assert.equal(f.module.settingsStore.ready(), true, 'transport has completed the older read')
  retry()
  tree = await f.render()
  assert.equal(reads, 6)
  assert.equal(settingsAlert(tree)?.props.description, 'newest load failed')
  assert.equal(button(tree, '保存').props.disabled, true, 'obsolete success cannot authorize Save')
})

for (const failure of [false, true]) {
  test(`closed modal ignores late settings ${failure ? 'failure' : 'success'}`, async () => {
    const response = deferred()
    const f = await settingsModalFixture(async () => ({ ok: true, json: () => response.promise }))
    await f.render()
    f.props.open = false
    await f.render()
    if (failure) response.reject(new Error('obsolete closed failure'))
    else response.resolve({ settings: { siteUrl: 'obsolete closed success' } })
    const tree = await f.render()
    assert.equal(settingsAlert(tree), undefined, 'closed load must not publish an error')
    assert.equal(f.form.getFieldValue('siteUrl'), undefined, 'closed load must not populate a draft')
    assert.equal(button(tree, '保存').props.disabled, true)
  })
}

for (const readback of [false, true]) {
  test(`delayed pre-import PUT cannot ${readback ? 'replace imported cache' : 'make invalidated cache ready'}`, async () => {
    const put = deferred()
    let stored = { siteUrl: 'original' }
    const f = await settingsModalFixture(async (url, init) => {
      if (url.endsWith('/api/data/import')) {
        stored = { siteUrl: 'imported' }
        return jsonResponse({ status: 'ok', imported: ['settings'], errors: [], warnings: [] })
      }
      if (init?.method === 'PUT') {
        stored = { ...stored, ...JSON.parse(init.body).settings }
        return { ok: true, json: () => put.promise }
      }
      return jsonResponse({ settings: { ...stored } })
    })
    await f.module.settingsStore.load()
    const saved = f.module.settingsStore.save({ siteUrl: 'pre-import-save' })
    await flush()
    await f.module.api.importAllData({})
    if (readback) await f.module.settingsStore.load()
    put.resolve({ status: 'ok', settings: { siteUrl: 'pre-import-save' } })
    await saved
    assert.equal(f.module.settingsStore.ready(), readback)
    assert.equal(f.module.settingsStore.get('siteUrl'), readback ? 'imported' : '')
  })
}

test('dirty station A is replaced by imported B and Save retains B through the real API', async () => {
  let stored = { siteUrl: 'before', stationProfiles: stationA }
  const f = await settingsModalFixture(async (url, init) => {
    if (url.endsWith('/api/data/import')) {
      stored = { ...JSON.parse(init.body).settings }
      return jsonResponse({ status: 'ok', imported: ['settings'], errors: [], warnings: [] })
    }
    if (!url.endsWith('/api/settings')) return jsonResponse({})
    if (init?.method === 'PUT') {
      // /api/settings replaces the incoming station list; absent IDs are deleted,
      // not unioned with imported stations. These synthetic rows have no secrets.
      stored = { ...stored, ...JSON.parse(init.body).settings }
      return jsonResponse({ status: 'ok', settings: { ...stored } })
    }
    return jsonResponse({ settings: { ...stored } })
  }, { settings: { siteUrl: 'imported', stationProfiles: stationB } })
  let tree = await f.section(await f.render(), 'section-site')
  stationName(tree).props.onChange({ target: { value: 'Dirty Station A' } })
  tree = await f.render()
  tree = await f.importData(tree)
  tree = await f.section(tree, 'section-site')
  f.form.setFieldValue('siteUrl', 'saved-after-import')
  await button(tree, '保存').props.onClick()
  tree = await f.render()
  const persisted = await f.module.api.getSettings()
  assert.deepEqual(JSON.parse(persisted.settings.stationProfiles).map(row => row.id), ['b'], 'Save must not delete imported B by resubmitting dirty A')
  assert.equal(stationName(tree).props.value, 'Station B')
  assert.equal(persisted.settings.siteUrl, 'saved-after-import')
  assert.deepEqual(f.errors, [])
  assert.ok(f.messages.some(m => m.type === 'success' && m.text === '设置已保存'))
})

test('failed import readback and ordinary retry preserve dirty station drafts', async () => {
  let imported = false, fail = true
  const f = await settingsModalFixture(async (url) => {
    if (url.endsWith('/api/data/import')) {
      imported = true
      return jsonResponse({ status: 'ok', imported: ['settings'], errors: [], warnings: [] })
    }
    if (!url.endsWith('/api/settings')) return jsonResponse({})
    if (imported && fail) throw new Error('readback failed')
    return jsonResponse({ settings: { siteUrl: 'before', stationProfiles: imported ? stationB : stationA } })
  })
  let tree = await f.section(await f.render(), 'section-site')
  stationName(tree).props.onChange({ target: { value: 'Dirty Station A' } })
  tree = await f.importData(await f.render())
  tree = await f.section(tree, 'section-site')
  assert.equal(stationName(tree).props.value, 'Dirty Station A')
  assert.equal(button(tree, '保存').props.disabled, true)
  fail = false
  settingsAlert(tree).props.action.props.onClick()
  tree = await f.render()
  assert.equal(stationName(tree).props.value, 'Dirty Station A')
  assert.equal(button(tree, '保存').props.disabled, false)
})

for (const failReadback of [false, true]) {
  test(`import includes errors and migration warnings with ${failReadback ? 'failed' : 'successful'} readback`, async () => {
    let imported = false
    const f = await settingsModalFixture(async (url) => {
      if (url.endsWith('/api/data/import')) {
        imported = true
        return jsonResponse({ status: 'ok', imported: ['settings'], errors: ['workflow import failed'], warnings: ['migration needs review'] })
      }
      if (imported && failReadback && url.endsWith('/api/settings')) throw new Error('readback unavailable')
      return jsonResponse({ settings: { siteUrl: 'before' } })
    })
    const tree = await f.importData(await f.render())
    const warnings = f.messages.filter(m => m.type === 'warning').map(m => m.text).join('\n')
    assert.match(warnings, /workflow import failed/)
    assert.match(warnings, /migration needs review/, 'migration warning must survive the error branch')
    if (failReadback) assert.match(warnings, /设置刷新失败/)
    assert.equal(button(tree, '保存').props.disabled, failReadback)
    assert.ok(!f.messages.some(m => m.type === 'success'))
  })
}

test('Settings import warns instead of reporting completion when settings readback fails', async () => {
  const f = desktopFixture({ siteUrl: 'before' })
  f.resolveBase('http://127.0.0.1:9911'); await flush()
  f.messages = []
  let failReadback = false, change
  const events = []
  f.window.api.getReleaseInfo = async () => ({ channel: 'beta', version: 'fixture' })
  f.window.dispatchEvent = (event) => events.push(event.type)
  f.globals.Event = Event
  f.globals.Error = Error
  f.globals.document = { createElement: () => ({
    files: [{ text: async () => JSON.stringify({ settings: { siteUrl: 'imported' } }) }],
    click() { change = this.onchange() }
  }) }
  f.globals.fetch = async (url, init) => {
    f.requests.push({ url, method: init?.method || 'GET' })
    if (url.endsWith('/api/data/import')) {
      failReadback = true
      return { ok: true, json: async () => ({ status: 'ok', imported: ['settings'], errors: [], warnings: [] }) }
    }
    if (url.endsWith('/api/settings') && failReadback) throw new Error('readback unavailable')
    return { ok: true, json: async () => ({ settings: { siteUrl: 'before' } }) }
  }
  f.module = loadProduction('../renderer/src/services/api.ts', {}, f.globals)
  const Form = Object.assign(() => null, {
    Item: () => null, useForm: () => [form], useWatch: () => undefined
  })
  const form = { setFieldsValue() {}, getFieldsValue: () => ({}) }
  const App = Object.assign(() => null, { useApp: () => ({ message: {
    error: text => f.errors.push(text),
    success: text => f.messages.push({ type: 'success', text }),
    warning: text => f.messages.push({ type: 'warning', text })
  } }) })
  const ui = new Proxy({ Form, App }, { get: (obj, key) => obj[key] || Object.assign(() => null, { Password: () => null, Compact: () => null }) })
  const render = renderFiles(f, '../renderer/src/components/SettingsModal.tsx', {
    antd: ui, './UploadProfilesPanel': () => null, './VersionCenterPanel': () => null
  }, { open: true, onClose() {} })
  let tree = await render()
  elements(tree).find(e => e.props.items)?.props.onClick({ key: 'section-about' })
  tree = await render()
  const button = elements(tree).find(e => e.props.children === '导入数据')
  assert.ok(button, 'actual import button is rendered')
  button.props.onClick(); await change
  tree = await render()
  assert.deepEqual(events, ['settings-updated'])
  assert.equal(f.module.settingsStore.ready(), false)
  assert.ok(elements(tree).some(e => e.props.description === 'readback unavailable'))
  assert.ok(!f.messages.some(m => m.type === 'success'))
  assert.ok(f.messages.some(m => /数据已导入.*设置刷新失败/.test(m.text)))
})

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
