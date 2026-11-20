import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildRelease } from '../../release-channel.mjs'
import { parseVersionCatalog, readVersionCatalog, historicalInstallerUrl } from './versionCatalog.mjs'

const release = (channel = 'stable') => buildRelease('0.2.8', channel, channel === 'beta' ? '1' : undefined)
const entry = (version = '0.2.7') => ({
  version, publishedAt: '2026-11-01', notes: ['改善登录诊断', '<script>仅作为文本展示</script>']
})
const catalog = (channel = 'stable', releases = [entry()]) => ({ schemaVersion: 1, channel, releases })

test('历史记录按数字版本倒序排列，不丢失更新说明', () => {
  const result = parseVersionCatalog(release(), catalog('stable', [entry('0.2.9'), entry('0.2.10')]))
  assert.deepEqual(result.releases.map(item => item.version), ['0.2.10', '0.2.9'])
  assert.deepEqual(result.releases[0].notes, entry().notes)
  assert.equal(result.channel, 'stable')
})

test('beta 只接受 beta 记录，预发布编号按数字排序', () => {
  const result = parseVersionCatalog(release('beta'), catalog('beta', [entry('0.2.8-beta.2'), entry('0.2.8-beta.10')]))
  assert.deepEqual(result.releases.map(item => item.version), ['0.2.8-beta.10', '0.2.8-beta.2'])
})

test('合法空目录与读取失败区分', () => {
  assert.deepEqual(parseVersionCatalog(release(), catalog('stable', [])).releases, [])
})

for (const [name, value] of [
  ['另一渠道', catalog('beta', [entry('0.2.8-beta.1')])],
  ['混入预发布版本', catalog('stable', [entry('0.2.8-beta.1')])],
  ['重复版本', catalog('stable', [entry(), entry()])],
  ['非映射', []],
  ['不支持的schema', { ...catalog(), schemaVersion: 2 }],
  ['缺少发布说明', catalog('stable', [{ ...entry(), notes: null }])],
  ['发布说明非文本', catalog('stable', [{ ...entry(), notes: [42] }])],
  ['无效日期', catalog('stable', [{ ...entry(), publishedAt: '2026-02-30' }])],
  ['超长说明', catalog('stable', [{ ...entry(), notes: ['x'.repeat(4001)] }])],
  ['超大记录数', catalog('stable', Array.from({ length: 201 }, (_, i) => entry(`0.2.${i}`)))],
  ['外部URL字段', catalog('stable', [{ ...entry(), url: 'https://example.invalid/foreign.exe' }])],
  ['任意文件名', catalog('stable', [{ ...entry(), fileName: '../foreign.exe' }])]
]) {
  test(`拒绝非法历史目录：${name}`, () => {
    assert.throws(() => parseVersionCatalog(release(), value))
  })
}

for (const version of ['../x', 'https://example.invalid/x.exe', 'file:///tmp/x', '0.2.7/../x', '0.2.07', '0.2.8-beta.1', '0.2.9007199254740992']) {
  test(`正式版拒绝非法安装包版本：${version}`, () => {
    assert.throws(() => historicalInstallerUrl(release(), version))
  })
}

test('安装包地址由已绑定渠道和版本生成，文件名安全编码', () => {
  for (const channel of ['stable', 'beta']) {
    const identity = release(channel)
    const version = channel === 'beta' ? '0.2.7-beta.3' : '0.2.7'
    const url = new URL(historicalInstallerUrl(identity, version))
    assert.equal(url.origin, new URL(identity.updateUrl).origin)
    assert.equal(decodeURIComponent(url.pathname), `${new URL(identity.updateUrl).pathname}/${identity.productName}-${version}-setup.exe`)
    assert.equal(url.search, '')
    assert.equal(url.hash, '')
  }
})

test('读取当前渠道固定 history.json，禁止跟随重定向', async () => {
  const calls = []
  const identity = release('beta')
  const result = await readVersionCatalog(identity, async (url, options) => {
    calls.push([url, options])
    return new Response(JSON.stringify(catalog('beta', [entry('0.2.8-beta.1')])))
  })
  assert.equal(calls[0][0], `${identity.updateUrl}/history.json`)
  assert.equal(calls[0][1].redirect, 'error')
  assert.equal(calls[0][1].signal instanceof AbortSignal, true)
  assert.equal(result.releases[0].version, '0.2.8-beta.1')
})

test('断网读取给出离线提示，不把底层网络异常抛给界面', async () => {
  await assert.rejects(
    readVersionCatalog(release(), async () => { throw new TypeError('fetch failed') }),
    /离线不影响本地采集/
  )
  await assert.rejects(
    readVersionCatalog(release(), async () => { throw Object.assign(new Error('timeout'), { name: 'TimeoutError' }) }),
    /超时/
  )
})

for (const [name, response, message] of [
  ['未发布', () => new Response('', { status: 404 }), /尚未发布/],
  ['服务失败', () => new Response('', { status: 503 }), /503/],
  ['非法JSON', () => new Response('not-json'), /格式/],
  ['超大响应', () => new Response('x'.repeat(512 * 1024 + 1)), /过大/],
  ['跨地址响应', () => {
    const response = new Response(JSON.stringify(catalog()))
    Object.defineProperty(response, 'url', { value: 'https://example.invalid/history.json' })
    return response
  }, /地址/]
]) {
  test(`目录读取失败不伪造历史：${name}`, async () => {
    await assert.rejects(readVersionCatalog(release(), async () => response()), message)
  })
}
