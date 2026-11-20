import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import { buildRelease } from '../../release-channel.mjs'
import * as catalogModule from './versionCatalog.mjs'

function register(channel = 'stable') {
  const handlers = new Map()
  const opened = []
  const requests = []
  let response = () => new Response(JSON.stringify({
    schemaVersion: 1, channel,
    releases: [{ version: channel === 'beta' ? '0.2.7-beta.1' : '0.2.7', publishedAt: '2026-11-01', notes: ['更新记录'] }]
  }))
  let openError = null
  const identity = buildRelease('0.2.8', channel, channel === 'beta' ? '1' : undefined)
  const electron = {
    ipcMain: { handle: (key, callback) => handlers.set(key, callback) },
    net: { fetch: async (url, options) => { requests.push([url, options]); return response() } },
    shell: { openExternal: async url => {
      if (openError) throw openError
      opened.push(url)
    } }
  }
  const source = readFileSync(new URL('./versionCenter.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}
  vm.runInNewContext(compiled, {
    exports, structuredClone,
    require: name => {
      if (name === 'electron') return electron
      if (name === './release') return { releaseIdentity: identity }
      if (name === './versionCatalog.mjs') return catalogModule
      assert.fail(`Unexpected dependency: ${name}`)
    }
  })
  exports.registerVersionCenter()
  return {
    identity, opened, requests,
    list: () => handlers.get('versions:list')({}),
    download: version => handlers.get('versions:download')({}, version),
    respondWith: callback => { response = callback },
    failRead: () => { response = () => new Response('', { status: 503 }) },
    failOpen: () => { openError = new Error('fixture browser unavailable') }
  }
}

for (const channel of ['stable', 'beta']) {
  test(`${channel} IPC 仅为已验证历史版本打开本渠道安装包，不能接受URL`, async () => {
    const app = register(channel)
    assert.equal(app.requests.length, 0, '注册时不得自动访问发布服务器')
    const version = channel === 'beta' ? '0.2.7-beta.1' : '0.2.7'
    await assert.rejects(app.download(version), /读取|刷新/)
    const catalog = await app.list()
    assert.equal(catalog.channel, channel)
    for (const value of [null, 42, {}, '../x', 'https://example.invalid/x.exe', 'file:///tmp/x.exe', '9.9.9']) {
      await assert.rejects(app.download(value))
    }
    assert.equal(app.opened.length, 0)
    const result = await app.download(version)
    assert.equal(result.ok, true)
    assert.deepEqual(app.opened, [catalogModule.historicalInstallerUrl(app.identity, version)])
    assert.equal(app.requests.length, 1, '下载不能接受另一渠道或renderer提供的地址')
  })
}

test('刷新失败废弃旧历史，不能凭过期缓存继续下载', async () => {
  const app = register()
  await app.list()
  app.failRead()
  await assert.rejects(app.list(), /503/)
  await assert.rejects(app.download('0.2.7'), /读取|刷新/)
  assert.equal(app.opened.length, 0)
})

test('返回目录副本不能修改主进程允许下载的版本', async () => {
  const app = register()
  const data = await app.list()
  data.releases[0].version = '9.9.9'
  await assert.rejects(app.download('9.9.9'))
  assert.equal((await app.download('0.2.7')).ok, true)
})

test('迟到的旧读取不能恢复刷新失败后已失效的下载目录', async () => {
  const app = register()
  let resolveRead
  app.respondWith(() => new Promise(resolve => { resolveRead = resolve }))
  const oldRead = app.list()
  app.failRead()
  await assert.rejects(app.list(), /503/)
  resolveRead(new Response(JSON.stringify({ schemaVersion: 1, channel: 'stable', releases: [
    { version: '0.2.7', publishedAt: '2026-11-01', notes: [] }
  ] })))
  await oldRead
  await assert.rejects(app.download('0.2.7'), /读取|刷新/)
  assert.equal(app.opened.length, 0)
})

test('浏览器打开失败不报告下载成功', async () => {
  const app = register()
  await app.list()
  app.failOpen()
  await assert.rejects(app.download('0.2.7'), /browser unavailable/)
  assert.equal(app.opened.length, 0)
})
