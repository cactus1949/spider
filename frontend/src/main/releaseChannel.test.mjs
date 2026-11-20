import assert from 'node:assert/strict'
import { test } from 'node:test'
import { existsSync, readFileSync, mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { runInNewContext } from 'node:vm'

const require = createRequire(import.meta.url)
const builderRequire = createRequire(require.resolve('electron-builder'))
const { getConfig, validateConfig } = builderRequire('app-builder-lib/out/util/config')
const { DebugLogger } = builderRequire('builder-util')
const frontend = fileURLToPath(new URL('../../', import.meta.url))

async function channels() {
  const file = new URL('../../release-channel.mjs', import.meta.url)
  assert.ok(existsSync(file), '缺失显式渠道契约加载与身份选择功能')
  return import(file.href)
}

async function config(channel, betaNumber) {
  const file = new URL('../../electron-builder.config.cjs', import.meta.url)
  if (!existsSync(file)) return getConfig(frontend, `${frontend}electron-builder.yml`, null)
  return require(fileURLToPath(file)).createBuilderConfig({ channel, betaNumber, baseVersion: '0.2.8' })
}

test('beta 安装身份与 stable 隔离，正式配置保持旧值', async () => {
  const stable = await config('stable')
  const beta = await config('beta', '3')
  assert.notEqual(beta.appId, stable.appId, 'beta 不能复用正式安装身份')
  assert.equal(stable.appId, 'com.spidergd.desktop')
  assert.equal(stable.productName, '广东电力数据采集')
  assert.equal(stable.publish.url, 'http://47.101.175.39:20152/releases')
  assert.equal(stable.nsis.shortcutName, '广东电力数据采集')
  assert.equal(beta.appId, 'com.spidergd.desktop.beta')
  assert.equal(beta.productName, '广东电力数据采集－测试版')
  assert.equal(beta.executableName, 'spider-gd-desktop-beta')
  assert.equal(beta.extraMetadata.name, 'spider-gd-desktop-beta')
  assert.equal(beta.extraMetadata.version, '0.2.8-beta.3')
  assert.equal(beta.publish.url, 'http://47.101.175.39:20152/releases/beta')
  assert.equal(beta.publish.channel, 'beta')
  assert.equal(beta.publish.updaterCacheDirName, 'spider-gd-desktop-beta-updater')
  assert.equal(beta.nsis.shortcutName, '广东电力数据采集－测试版')
  await validateConfig(beta, new DebugLogger())
  await validateConfig(stable, new DebugLogger())
})

test('build 只接受 stable 或带正整数编号的 beta，不更改源元数据', async () => {
  const { buildRelease } = await channels()
  assert.equal(buildRelease('0.2.8', 'beta', '12').version, '0.2.8-beta.12')
  assert.equal(buildRelease('0.2.8', 'stable').version, '0.2.8')
  for (const [channel, number] of [['wrong', undefined], ['beta', undefined], ['beta', '0'], ['beta', '-1'], ['beta', '1.5'], ['beta', '01'], ['stable', '1']]) {
    assert.throws(() => buildRelease('0.2.8', channel, number), /channel|BetaNumber/)
  }
  assert.throws(() => buildRelease('0.2.8-beta.1', 'stable'), /VERSION/)
  await config('beta', '12')
  assert.equal(JSON.parse(readFileSync(`${frontend}package.json`, 'utf8')).version, '0.2.8')
  assert.equal(JSON.parse(readFileSync(`${frontend}package.json`, 'utf8')).name, 'spider-gd-desktop')
})

test('packaged stable/beta 忽略相反运行环境，dev 可显式切 beta', async () => {
  const { buildRelease, runtimeRelease } = await channels()
  const stable = buildRelease('0.2.8', 'stable')
  const beta = buildRelease('0.2.8', 'beta', '2')
  assert.equal(runtimeRelease(stable, true, { SPIDER_GD_CHANNEL: 'beta' }).channel, 'stable')
  assert.equal(runtimeRelease(beta, true, { SPIDER_GD_CHANNEL: 'stable' }).version, '0.2.8-beta.2')
  const dev = runtimeRelease(stable, false, { SPIDER_GD_CHANNEL: 'beta' })
  assert.equal(dev.productName, '广东电力数据采集－测试版')
  assert.equal(dev.preferredPort, 8801)
  assert.equal(dev.serviceName, 'spider-gd-service-beta')
  assert.throws(() => runtimeRelease(stable, false, { SPIDER_GD_CHANNEL: 'typo' }), /channel/)
})

test('beta 在申请锁前设置名称和目录，stable 完全保留旧 userData', async () => {
  const { buildRelease, configureAppIdentity } = await channels()
  const directory = mkdtempSync(join(tmpdir(), 'spider-channel-'))
  const betaHome = join(directory, 'spider-gd-desktop-beta')
  try {
    const events = []
    const app = {
      setName: (name) => events.push(['name', name]),
      setPath: (key, value) => {
        assert.ok(existsSync(value), '首次启动必须先创建 userData，否则 Electron setPath 失败')
        events.push([key, value])
      },
      getPath: (key) => { assert.equal(key, 'appData'); return directory },
      requestSingleInstanceLock: () => { events.push(['lock']); return true }
    }
    configureAppIdentity(app, buildRelease('0.2.8', 'stable'))
    assert.deepEqual(events, [])
    assert.equal(existsSync(betaHome), false)
    configureAppIdentity(app, buildRelease('0.2.8', 'beta', '1'))
    app.requestSingleInstanceLock()
    assert.deepEqual(events, [['name', 'spider-gd-desktop-beta'], ['userData', betaHome], ['lock']])
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})

test('beta 数据根被链接到正式目录时拒绝设置 userData，不改动正式文件', async () => {
  const { buildRelease, configureAppIdentity } = await channels()
  const directory = mkdtempSync(join(tmpdir(), 'spider-channel-link-'))
  const formal = join(directory, 'formal')
  const betaHome = join(directory, 'spider-gd-desktop-beta')
  try {
    mkdirSync(formal)
    const marker = join(formal, 'browser-session.json')
    writeFileSync(marker, '{"config":{"auth_policy":"reuse_or_auto"}}')
    symlinkSync(formal, betaHome, process.platform === 'win32' ? 'junction' : 'dir')
    const paths = []
    const app = {
      setName() {},
      getPath: (key) => { assert.equal(key, 'appData'); return directory },
      setPath: (key, value) => paths.push([key, value])
    }

    assert.throws(() => configureAppIdentity(app, buildRelease('0.2.8', 'beta', '1')), /测试版数据目录/)
    assert.deepEqual(paths, [])
    assert.equal(readFileSync(marker, 'utf8'), '{"config":{"auth_policy":"reuse_or_auto"}}')
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})

test('后端 env 保留正式 HOME override，beta 默认独立且传递绑定渠道', async () => {
  const { buildRelease, backendEnvironment } = await channels()
  const stable = buildRelease('0.2.8', 'stable')
  const beta = buildRelease('0.2.8', 'beta', '4')
  assert.equal(backendEnvironment(stable, true, '/stable', { SPIDER_GD_HOME: '/external' }).SPIDER_GD_HOME, '/external')
  assert.equal(backendEnvironment(stable, false, '/stable', {}).SPIDER_GD_HOME, undefined)
  assert.equal(backendEnvironment(stable, true, '/stable', {}).SPIDER_GD_HOME, '/stable')
  assert.equal(backendEnvironment(stable, true, '/stable', { SPIDER_GD_HOME: '  ""  ' }).SPIDER_GD_HOME, '/stable')
  assert.equal(backendEnvironment(beta, false, '/beta', { SPIDER_GD_HOME: '   ' }).SPIDER_GD_HOME, '/beta')
  const env = backendEnvironment(beta, false, '/beta', { SPIDER_GD_CHANNEL: 'stable' })
  assert.equal(env.SPIDER_GD_HOME, '/beta')
  assert.equal(env.SPIDER_GD_CHANNEL, 'beta')
})

test('packaged beta 不继承正式 HOME 和浏览器状态，dev 保留显式沙箱', async () => {
  const { buildRelease, backendEnvironment } = await channels()
  const beta = buildRelease('0.2.8', 'beta', '1')
  const inherited = {
    SPIDER_GD_HOME: '/formal-home',
    SPIDER_BROWSER_CONFIG: '/formal-home/browser-session.json',
    SPIDER_GD_CHANNEL: 'stable',
    PYTHONUTF8: '1'
  }
  const env = backendEnvironment(beta, true, '/isolated-beta', inherited)
  assert.equal(env.SPIDER_GD_HOME, '/isolated-beta')
  assert.equal(env.SPIDER_BROWSER_CONFIG, undefined)
  assert.equal(env.SPIDER_GD_CHANNEL, 'beta')
  assert.equal(env.PYTHONUTF8, '1')
  assert.equal(inherited.SPIDER_GD_HOME, '/formal-home')
  assert.equal(inherited.SPIDER_BROWSER_CONFIG, '/formal-home/browser-session.json')
  const dev = backendEnvironment(beta, false, '/isolated-beta', inherited)
  assert.equal(dev.SPIDER_GD_HOME, '/formal-home')
  assert.equal(dev.SPIDER_BROWSER_CONFIG, '/formal-home/browser-session.json')
})

function loadMainModule(name, mocks, processContext) {
  const { transpileModule, ModuleKind } = require('typescript')
  const exports = {}
  const { outputText } = transpileModule(readFileSync(`${frontend}src/main/${name}.ts`, 'utf8'), {
    compilerOptions: { module: ModuleKind.CommonJS, esModuleInterop: true }
  })
  runInNewContext(outputText, {
    exports,
    require: (id) => Object.hasOwn(mocks, id) ? mocks[id] : require(id),
    process: processContext, console, setTimeout, clearTimeout, Buffer,
    __dirname: `${frontend}out/main`
  })
  return exports
}

test('双渠道数据目录 IPC 按 Python HOME 规则及实际后端 cwd 展示，不等待 healthy', async (t) => {
  const releaseModule = await channels()
  for (const channel of ['stable', 'beta']) {
  const release = releaseModule.buildRelease('0.2.8', channel, channel === 'beta' ? '1' : undefined)
  const root = resolve(tmpdir(), 'spider-home-contract')
  const repo = join(root, 'repo')
  const resourcesPath = join(root, 'resources')
  const userData = join(root, channel === 'beta' ? 'spider-gd-desktop-beta' : 'spider-gd-desktop')
  const executable = `${release.serviceName}${process.platform === 'win32' ? '.exe' : ''}`
  const python = join(repo, '.venv', ...(process.platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python']))
  const custom = join(root, 'custom home')
  for (const [packaged, external, cwd] of [
    [false, false, repo],
    [true, false, join(resourcesPath, 'backend')],
    [true, true, join(root, 'external-service')]
  ]) {
    for (const [raw, expected] of [
      ['~/渠道目录', join(homedir(), '渠道目录', 'data')],
      [`  "${custom}"  `, join(custom, 'data')],
      ['  "relative/渠道目录"  ', join(cwd, 'relative', '渠道目录', 'data')],
      [undefined, join(channel === 'stable' && !packaged ? cwd : userData, 'data')],
      ['  ""  ', join(channel === 'stable' && !packaged ? cwd : userData, 'data')]
    ]) {
      await t.test(`packaged=${packaged}, external=${external}, HOME=${raw}`, () => {
        const env = {}
        if (raw !== undefined) env.SPIDER_GD_HOME = raw
        if (external) env.SPIDER_GD_BACKEND_EXE = join(cwd, executable)
        const handlers = new Map()
        const app = {
          isPackaged: packaged,
          getAppPath: () => join(repo, 'frontend'),
          getPath: (key) => key === 'home' ? homedir() : userData,
          requestSingleInstanceLock: () => true,
          setAppUserModelId() {},
          on() {},
          whenReady: () => ({ then: (callback) => callback() })
        }
        class Window {
          webContents = { setWindowOpenHandler() {} }
          on() {}
          loadFile() {}
          loadURL() {}
        }
        const electron = {
          app, BrowserWindow: Window, shell: {},
          ipcMain: { handle: (name, handler) => handlers.set(name, handler), on() {} },
          net: { fetch: () => assert.fail('目录展示不能请求后端 healthy') }, dialog: {}
        }
        const processContext = { platform: process.platform, resourcesPath, env }
        const files = new Set([python, join(repo, 'main.py'), ...(packaged ? [join(cwd, executable)] : [])])
        const backend = loadMainModule('backend', {
          electron,
          fs: { existsSync: (file) => files.has(file) },
          child_process: { spawn: () => assert.fail('不能启动后端'), execFile: () => assert.fail('不能查询监听进程') },
          './backendMatch.mjs': {},
          '../../release-channel.mjs': {
            ...releaseModule,
            backendEnvironment: (identity, packaged, home) => releaseModule.backendEnvironment(identity, packaged, home, env)
          },
          './release': { releaseIdentity: release }
        }, processContext)
        loadMainModule('index', {
          electron,
          './backend': { ...backend, startBackend() {}, stopBackend() {} },
          './tray': { createTray() {} }, './files': { registerFileIpc() {} }, './updater': { startUpdater() {} },
          './versionCenter': { registerVersionCenter() {} },
          '../../release-channel.mjs': { configureAppIdentity() {} },
          './release': { releaseIdentity: release, getReleaseInfo: () => release }
        }, processContext)
        assert.equal(backend.currentBackendLaunchCwd(), cwd)
        assert.equal(releaseModule.resolveBackendDataDir(raw, cwd, channel === 'stable' && !packaged ? cwd : userData), expected)
        const ipcExpected = channel === 'beta' && packaged ? join(userData, 'data') : expected
        assert.equal(handlers.get('app:get-data-dir')(), ipcExpected)
      })
    }
  }
  }
})

test('Windows 构建入口在启动任何构建工具前拒绝无效渠道/编号', () => {
  for (const args of [['--channel', 'typo'], ['--channel', 'beta'], ['--channel', 'beta', '--beta-number', '0'], ['--channel', 'stable', '--beta-number', '1']]) {
    const result = spawnSync(process.execPath, [`${frontend}build-win.mjs`, ...args], { encoding: 'utf8', env: { ...process.env, SPIDER_GD_BETA_NUMBER: '' } })
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /Invalid channel|BetaNumber/, '必须因选择验证失败，而非缺模块或打包工具错误')
    assert.doesNotMatch(result.stdout, /vite|electron-builder/)
  }
})

test('builder 拒绝混入另一渠道后端，且不删除已有产物', async (t) => {
  for (const channel of ['stable', 'beta']) {
    await t.test(channel, () => {
      const result = spawnSync(process.execPath, ['-e', `
        const assert = require('node:assert/strict')
        const fs = require('node:fs')
        const { join } = require('node:path')
        ;(async () => {
          const { buildRelease } = await import(${JSON.stringify(`${frontend}release-channel.mjs`)})
          const release = buildRelease('0.2.8', ${JSON.stringify(channel)}, ${channel === 'beta' ? "'1'" : 'undefined'})
          const root = ${JSON.stringify(frontend)}
          const backend = join(root, 'resources/backend')
          const read = fs.readFileSync
          const exists = fs.existsSync
          let contaminated = true
          fs.readFileSync = (file, ...args) => {
            if (file === join(root, 'out/main/release-info.json')) return JSON.stringify({ ...release, buildId: '012345abcdef' })
            if (file === join(backend, 'build_stamp.json')) return JSON.stringify({ ...release, build_id: '012345abcdef' })
            return read(file, ...args)
          }
          fs.existsSync = (file) => {
            if (file === join(backend, release.serviceName + '.exe')) return true
            if (file === join(backend, ${JSON.stringify(channel === 'beta' ? 'spider-gd-service.exe' : 'spider-gd-service-beta.exe')})) return contaminated
            return exists(file)
          }
          fs.unlinkSync = fs.rmSync = () => assert.fail('构建门禁不能删除已有产物')
          const { createBuilderConfig } = require(join(root, 'electron-builder.config.cjs'))
          const options = { channel: release.channel, betaNumber: ${channel === 'beta' ? "'1'" : 'undefined'}, baseVersion: '0.2.8', requireBuilt: true }
          await assert.rejects(createBuilderConfig(options), /另一渠道/)
          contaminated = false
          await createBuilderConfig(options)
        })().catch((error) => { console.error(error); process.exitCode = 1 })
      `], { encoding: 'utf8' })
      assert.equal(result.status, 0, result.stderr)
    })
  }
})

test('NSIS 安装与卸载共享契约派生身份，不允许构建器覆盖为另一安装', async () => {
  const { buildRelease } = await channels()
  const { applyInstallerIdentity } = require(`${frontend}electron-builder.config.cjs`)
  assert.equal(typeof applyInstallerIdentity, 'function', '缺失安装器身份注入门禁')
  for (const [channel, ownGuid, otherGuid, mainExe, otherMain] of [
    ['stable', '70b98167-4e39-5386-a586-fa7bc9d82092', 'ed4c8e27-14ea-5acd-9bbc-319487189ebe', '广东电力数据采集', 'spider-gd-desktop-beta'],
    ['beta', 'ed4c8e27-14ea-5acd-9bbc-319487189ebe', '70b98167-4e39-5386-a586-fa7bc9d82092', 'spider-gd-desktop-beta', '广东电力数据采集']
  ]) {
    const release = buildRelease('0.2.8', channel, channel === 'beta' ? '1' : undefined)
    const base = { APP_ID: release.appId, APP_GUID: ownGuid, UNINSTALL_APP_KEY: ownGuid, PRODUCT_FILENAME: mainExe }
    const defines = { ...base }
    const commands = { OutFile: 'setup.exe' }
    assert.equal(await applyInstallerIdentity(release, [defines, commands]), false, '不能让 hook 中止生成安装器')
    const uninstallDefines = { ...defines }
    for (const actual of [defines, uninstallDefines]) {
      assert.equal(actual.SPIDER_CHANNEL, channel)
      assert.equal(actual.SPIDER_OWN_MAIN_EXE, `${mainExe}.exe`)
      assert.equal(actual.SPIDER_OTHER_MAIN_EXE, `${otherMain}.exe`)
      assert.equal(actual.SPIDER_OWN_SERVICE_EXE, channel === 'beta' ? 'spider-gd-service-beta.exe' : 'spider-gd-service.exe')
      assert.equal(actual.SPIDER_OTHER_SERVICE_EXE, channel === 'beta' ? 'spider-gd-service.exe' : 'spider-gd-service-beta.exe')
      assert.equal(actual.SPIDER_OWN_INSTALL_KEY, `Software\\${ownGuid}`)
      assert.equal(actual.SPIDER_OTHER_INSTALL_KEY, `Software\\${otherGuid}`)
      assert.equal(actual.SPIDER_OWN_UNINSTALL_KEY, `Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${ownGuid}`)
      assert.equal(actual.SPIDER_OTHER_UNINSTALL_KEY, `Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${otherGuid}`)
    }
    assert.deepEqual(commands, { OutFile: 'setup.exe' })
    for (const field of ['APP_ID', 'APP_GUID', 'UNINSTALL_APP_KEY', 'PRODUCT_FILENAME']) {
      await assert.rejects(applyInstallerIdentity(release, [{ ...base, [field]: 'foreign' }, {}]), /安装器身份/)
    }
  }
})

test('Windows 构建入口使用带身份 hook 的公开 API，不发布或绕过产物核验', async (t) => {
  for (const channel of ['stable', 'beta']) {
    await t.test(channel, () => {
      const result = spawnSync(process.execPath, ['-e', `
        const assert = require('node:assert/strict')
        const fs = require('node:fs')
        const cp = require('node:child_process')
        const { syncBuiltinESMExports } = require('node:module')
        const { join } = require('node:path')
        ;(async () => {
          const root = ${JSON.stringify(frontend)}
          const { buildRelease } = await import(join(root, 'release-channel.mjs'))
          const release = buildRelease('0.2.8', ${JSON.stringify(channel)}, ${channel === 'beta' ? "'1'" : 'undefined'})
          const read = fs.readFileSync
          const exists = fs.existsSync
          const reads = []
          fs.readFileSync = (file, ...args) => {
            if (file === join(root, 'out/main/release-info.json') || file === join(root, 'resources/backend/build_stamp.json')) {
              reads.push(file)
              return JSON.stringify({ ...release, ...(file.endsWith('release-info.json') ? { buildId: '012345abcdef' } : { build_id: '012345abcdef' }) })
            }
            return read(file, ...args)
          }
          fs.existsSync = (file) => file.startsWith(join(root, 'resources/backend'))
            ? file === join(root, 'resources/backend', release.serviceName + '.exe') : exists(file)
          const tools = []
          cp.spawnSync = (command, args, options) => {
            tools.push(args[0])
            assert.equal(options.env.SPIDER_GD_CHANNEL, release.channel)
            return { status: 0 }
          }
          syncBuiltinESMExports()
          const path = require.resolve(join(root, 'node_modules/electron-builder'))
          const builder = require(path)
          let built = false
          require.cache[path].exports = { ...builder, build: async (options) => {
            built = true
            assert.equal(options.publish, 'never')
            assert.equal(options.projectDir, root.replace(/\\/$/, ''))
            assert.equal(options.config.appId, release.appId)
            assert.deepEqual([...options.targets.get(builder.Platform.WINDOWS)], [[builder.Arch.x64, ['nsis']]])
            assert.equal(typeof options.effectiveOptionComputed, 'function')
            const guid = ${JSON.stringify(channel === 'beta' ? 'ed4c8e27-14ea-5acd-9bbc-319487189ebe' : '70b98167-4e39-5386-a586-fa7bc9d82092')}
            const defines = { APP_ID: release.appId, APP_GUID: guid, UNINSTALL_APP_KEY: guid, PRODUCT_FILENAME: release.executableName }
            assert.equal(await options.effectiveOptionComputed([defines, {}]), false)
            assert.equal(defines.SPIDER_OWN_SERVICE_EXE, release.serviceName + '.exe')
          } }
          process.argv = [process.execPath, join(root, 'build-win.mjs'), '--channel', release.channel${channel === 'beta' ? ", '--beta-number', '1'" : ''}]
          process.env.SPIDER_GD_BETA_NUMBER = ''
          await import(join(root, 'build-win.mjs'))
          assert.equal(built, true, '构建入口仍绕过安装器身份 API 门禁')
          assert.equal(tools.length, 1)
          assert.match(tools[0], /electron-vite/)
          assert.equal(reads.length, 2, '必须核验已编译的前端与后端身份')
        })().catch((error) => { console.error(error); process.exitCode = 1 })
      `], { encoding: 'utf8' })
      assert.equal(result.status, 0, result.stderr)
    })
  }
})

test('builder 拒绝与已经编译的主进程身份不一致的产物', async () => {
  const { buildRelease, assertBuiltRelease } = await channels()
  assert.throws(() => assertBuiltRelease(buildRelease('0.2.8', 'stable'), buildRelease('0.2.8', 'beta', '1')), /身份|release/)
  assert.throws(() => assertBuiltRelease(buildRelease('0.2.8', 'beta', '1'), buildRelease('0.2.8', 'beta', '2')), /身份|release/)
  assert.doesNotThrow(() => assertBuiltRelease(buildRelease('0.2.8', 'beta', '2'), buildRelease('0.2.8', 'beta', '2')))
})

// Execute the real Vite configuration without invoking Vite, Git, or native builds.
function viteConfig(command, channel, execGit) {
  const { transpileModule, ModuleKind } = require('typescript')
  const { outputText } = transpileModule(readFileSync(join(frontend, 'electron.vite.config.ts'), 'utf8'), {
    compilerOptions: { module: ModuleKind.CommonJS, esModuleInterop: true }
  })
  return channels().then((releaseModule) => {
    const exports = {}
    runInNewContext(outputText, {
      exports, __dirname: frontend.replace(/\/$/, ''),
      process: { env: { SPIDER_GD_CHANNEL: channel, SPIDER_GD_BETA_NUMBER: channel === 'beta' ? '1' : undefined } },
      require: (id) => {
        if (id === 'fs' || id === 'node:fs') return { readFileSync: () => '0.2.8\n' }
        if (id === 'child_process' || id === 'node:child_process') return { execFileSync: execGit }
        if (id === './release-channel.mjs') return releaseModule
        if (id === 'electron-vite') return { defineConfig: (fn) => fn({ command }), externalizeDepsPlugin: () => ({}) }
        if (id === '@vitejs/plugin-react') return { default: () => ({}) , __esModule: true }
        if (id === 'path' || id === 'node:path') return require(id)
        assert.fail(`unmocked config port: ${id}`)
      }
    })
    return exports.default
  })
}

test('Vite build asset binds repository commit without changing runtime identity; dev needs no Git', async (t) => {
  for (const channel of ['stable', 'beta']) {
    await t.test(channel, async () => {
      const { buildRelease } = await channels()
      const release = buildRelease('0.2.8', channel, channel === 'beta' ? '1' : undefined)
      let gitCalls = 0
      const built = await viteConfig('build', channel, (command, args, options) => {
        gitCalls++
        assert.equal(command, 'git')
        assert.deepEqual([...args], ['rev-parse', '--short=12', 'HEAD'])
        assert.equal(options.cwd, resolve(frontend, '..'))
        assert.equal(options.encoding, 'utf8')
        return '012345abcdef\n'
      })
      const assets = []
      built.main.plugins[0].generateBundle.call({ emitFile: (asset) => assets.push(asset) })
      assert.equal(gitCalls, 1)
      assert.deepEqual(JSON.parse(assets[0].source), { ...release, buildId: '012345abcdef' })
      assert.equal(assets[0].fileName, 'release-info.json')
      assert.deepEqual(JSON.parse(built.main.define.__SPIDER_GD_RELEASE__), release, 'runtime identity must remain unchanged')
      await viteConfig('serve', channel, () => assert.fail('dev must not require Git'))
    })
  }
})

test('public builder rejects old/missing/invalid build identities without modifying artifacts', async (t) => {
  for (const channel of ['stable', 'beta']) {
    await t.test(channel, () => {
      const result = spawnSync(process.execPath, ['-e', `
        const assert = require('node:assert/strict')
        const fs = require('node:fs')
        const { join } = require('node:path')
        ;(async () => {
          const root = ${JSON.stringify(frontend)}
          const { buildRelease } = await import(join(root, 'release-channel.mjs'))
          const release = buildRelease('0.2.8', ${JSON.stringify(channel)}, ${channel === 'beta' ? "'1'" : 'undefined'})
          let built = { ...release, buildId: '012345abcdef' }
          let stamp = { channel: release.channel, version: release.version, build_id: '012345abcdef' }
          const read = fs.readFileSync
          const exists = fs.existsSync
          fs.readFileSync = (file, ...args) => {
            if (file === join(root, 'out/main/release-info.json')) return JSON.stringify(built)
            if (file === join(root, 'resources/backend/build_stamp.json')) return JSON.stringify(stamp)
            return read(file, ...args)
          }
          fs.existsSync = (file) => file.startsWith(join(root, 'resources/backend'))
            ? file === join(root, 'resources/backend', release.serviceName + '.exe') : exists(file)
          fs.writeFileSync = fs.unlinkSync = fs.rmSync = () => assert.fail('identity gate must not modify artifacts')
          const builder = require(join(root, 'electron-builder.config.cjs'))
          process.env.SPIDER_GD_CHANNEL = release.channel
          process.env.SPIDER_GD_BETA_NUMBER = ${channel === 'beta' ? "'1'" : "''"}
          for (const [frontId, backId] of [
            ['012345abcdef', 'ffffffffffff'], [undefined, '012345abcdef'],
            ['012345abcdef', undefined], ['', ''], ['not-hex-id!!', 'not-hex-id!!'],
            ['abcd', 'abcd'], ['012345abcdef0', '012345abcdef0'], [12, 12]
          ]) {
            built = { ...release, buildId: frontId }
            stamp = { channel: release.channel, version: release.version, build_id: backId }
            await assert.rejects(builder(), /build.?id|构建/i)
          }
          built = { ...release, buildId: '012345abcdef' }
          stamp = { channel: release.channel, version: release.version, build_id: '012345abcdef' }
          const config = await builder()
          assert.equal(config.extraMetadata.version, release.version)
          assert.equal(config.appId, release.appId)
        })().catch((error) => { console.error(error); process.exitCode = 1 })
      `], { encoding: 'utf8' })
      assert.equal(result.status, 0, result.stderr)
    })
  }
})
