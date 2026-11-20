import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync, existsSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { homedir, tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const frontend = fileURLToPath(new URL('../../', import.meta.url))
const require = createRequire(import.meta.url)
const builderRequire = createRequire(require.resolve('electron-builder'))
const templates = join(dirname(builderRequire.resolve('app-builder-lib/package.json')), 'templates/nsis')
const readTemplate = (name) => readFileSync(join(templates, name), 'utf8')
const source = () => readFileSync(join(frontend, 'resources/installer.nsh'), 'utf8')

// A bounded NSH hook interpreter, NOT makensis and NOT a Windows behavior test.
// Execute the real custom macro bodies with injected extraction/env/nsExec/delete boundaries.
const identity = {
  SPIDER_CHANNEL: 'stable', SPIDER_OWN_MAIN_EXE: 'own-main.exe', SPIDER_OTHER_MAIN_EXE: 'other-main.exe',
  SPIDER_OWN_SERVICE_EXE: 'own-service.exe', SPIDER_OTHER_SERVICE_EXE: 'other-service.exe',
  SPIDER_OWN_INSTALL_KEY: 'Software\\own', SPIDER_OTHER_INSTALL_KEY: 'Software\\other',
  SPIDER_OWN_UNINSTALL_KEY: 'Software\\Uninstall\\own', SPIDER_OTHER_UNINSTALL_KEY: 'Software\\Uninstall\\other',
  INSTALL_REGISTRY_KEY: 'Software\\own', UNINSTALL_REGISTRY_KEY: 'Software\\Uninstall\\own',
  APP_EXECUTABLE_FILENAME: 'own-main.exe', UNINSTALL_FILENAME: 'Uninstall own-main.exe'
}
function hookProgram(name, buildUninstaller = false, definitions = identity) {
  const text = source()
  const macros = new Map([...text.matchAll(/^!macro\s+(\w+)([^\n]*)\n([\s\S]*?)^!macroend/gm)]
    .map(([, key, parameters, body]) => [key, { parameters: parameters.trim().split(/\s+/).filter(Boolean), body }]))
  macros.set('compileGuards', { parameters: [], body: text.slice(0, text.indexOf('Var spiderGuardReady')) })
  let counter = 0
  function expand(key, args = []) {
    assert.ok(macros.has(key), `missing lifecycle macro: ${key}`)
    const macro = macros.get(key)
    const substitutions = { ...definitions, ...Object.fromEntries(macro.parameters.map((parameter, index) => [parameter, args[index]])) }
    const active = [true]
    const lines = []
    for (let line of macro.body.split('\n')) {
      line = line.trim().replace(/\$\{(\w+)\}/g, (all, key) => substitutions[key] ?? all)
      const ifdef = line.match(/^!if(n?)def (\w+)$/)
      if (ifdef) {
        const defined = ifdef[2] === 'BUILD_UNINSTALLER' ? buildUninstaller : Object.hasOwn(substitutions, ifdef[2])
        active.push(active.at(-1) && (ifdef[1] ? !defined : defined)); continue
      }
      const comparison = line.match(/^!if "([^"]*)" (!=|==) "([^"]*)"$/)
      if (comparison) {
        active.push(active.at(-1) && (comparison[2] === '!=' ? comparison[1] !== comparison[3] : comparison[1] === comparison[3])); continue
      }
      if (line === '!endif') { active.pop(); continue }
      if (!active.at(-1) || !line || line.startsWith(';')) continue
      if (line.startsWith('!error ')) throw new Error(tokens(line)[1])
      const define = line.match(/^!define (\w+) \$\{__COUNTER__\}$/)
      if (define) { substitutions[define[1]] = String(counter++); continue }
      if (line.startsWith('!undef ')) continue
      line = line.replace(/\$\{(\w+)\}/g, (all, key) => substitutions[key] ?? all)
      const insert = line.match(/^!insertmacro (\w+)(.*)$/)
      if (insert) lines.push(...expand(insert[1], tokens(insert[2])))
      else lines.push(line)
    }
    return lines
  }
  return expand(name)
}
function tokens(text) {
  return [...text.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map((match) => match[1] ?? match[2] ?? match[3])
}
function execute(lines, { codes = [], deleteError = false, envError = false, filesExist = true, extractError = false, initialErrors = false, state = {} } = {}) {
  const events = []
  const variables = { INSTDIR: 'C:\\Install\\own', PLUGINSDIR: 'C:\\Temp\\guard', SYSDIR: 'C:\\Windows\\System32', ...state }
  const env = {}
  const stack = []
  let errors = initialErrors
  let exitCode = 0
  const value = (text) => text.replace(/\$(\w+)/g, (_, key) => variables[key] ?? '')
  const labels = new Map(lines.map((line, index) => [line.replace(/:$/, ''), index]))
  const jump = (label, index) => label === '0' || label === undefined ? index : (assert.ok(labels.has(label), `unknown label ${label}`), labels.get(label))
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]
    const [op, ...args] = tokens(line)
    if (line.endsWith(':') || ['Section', 'SectionEnd', 'SectionIn', 'Var'].includes(op)) continue
    if (op === 'InitPluginsDir') { events.push('extract-init'); continue }
    if (op === 'ClearErrors') { errors = false; continue }
    if (op === 'File') { events.push('extract-guard'); errors ||= extractError; continue }
    if (op === 'StrCpy') { variables[args[0].slice(1)] = value(args[1]); continue }
    if (op === 'StrCmp') { index = jump(value(args[0]) === value(args[1]) ? args[2] : args[3], index); continue }
    if (op === 'IfErrors') { index = jump(errors ? args[0] : args[1], index); continue }
    if (op === 'IfFileExists') { index = jump(filesExist ? args[1] : args[2], index); continue }
    if (op === 'Push') { stack.push(value(args[0])); continue }
    if (op === 'Pop') { assert.ok(stack.length); variables[args[0].slice(1)] = stack.pop(); continue }
    if (op === 'System::Call') {
      // Windows 旧版 System.dll 上 Push + "t s" 会破坏栈/崩溃；实现改为内联参数 + .R8 返回值。
      // 注意 System::Call 输出寄存器区分大小写：.r8 写 $8，.R8 才是 $R8。
      const setting = args[0].match(/SetEnvironmentVariable\(t, t\) i \("([^"]+)", "([^"]*)"\)\.R8/)
      assert.ok(setting, `environment values must use inline args and .R8: ${line}`)
      env[setting[1]] = value(setting[2])
      variables.R8 = envError ? '0' : '1'
      continue
    }
    if (op === 'nsExec::ExecToLog') {
      assert.match(value(args[0]), / -File "C:\\Temp\\guard\\installer-guard\.ps1"$/)
      assert.doesNotMatch(value(args[0]), /-Command|-EncodedCommand|C:\\Install/)
      events.push(`guard:${env.SPIDER_GUARD_ACTION}${env.SPIDER_CLEANUP_TARGET ? ':' + env.SPIDER_CLEANUP_TARGET : ''}`)
      stack.push(String(codes.shift() ?? 0)); continue
    }
    if (op === 'RMDir' || op === 'Delete' || op === 'Rename') {
      events.push(`${op}:${value(args.at(-1))}`); errors ||= deleteError || !filesExist; continue
    }
    if (op === 'SetErrorLevel') { exitCode = Number(args[0]); continue }
    if (op === 'DetailPrint') continue
    if (op === 'Quit' || op === 'Abort') return { events, aborted: true, exitCode, variables }
    assert.fail(`unsupported hook instruction: ${line}`)
  }
  return { events, aborted: false, exitCode, variables }
}

function installEntry() {
  const template = readTemplate('installer.nsi')
  assert.ok(template.indexOf('!insertmacro customHeader') < template.indexOf('Section "install"'))
  const section = hookProgram('customHeader')
  assert.match(section.join('\n'), /^Section "-spiderIsolation"/m, 'must be the first hidden install section')
  assert.ok(section.includes('SectionIn RO'), 'preflight cannot be deselected')
  return section
}

test('lifecycle: assisted outer/inner and silent installs check, stop, forget before legacy uninstall', () => {
  const realInstall = readTemplate('installSection.nsh')
  assert.match(realInstall, /\$\{ifNot\} \$\{UAC_IsInnerInstance\}[\s\S]*?CHECK_APP_RUNNING/)
  assert.ok(realInstall.indexOf('CHECK_APP_RUNNING') < realInstall.indexOf('!insertmacro uninstallOldVersion'))
  for (const mode of ['assisted-outer', 'assisted-inner', 'silent']) {
    const result = execute(installEntry())
    assert.equal(result.aborted, false, mode)
    assert.deepEqual(result.events.filter((event) => event.startsWith('guard:')).slice(0, 3), ['guard:Check', 'guard:Stop', 'guard:Forget'], mode)
    // The legacy command boundary is reached only after Forget; there is no legacy invocation in our hook.
    assert.equal(result.events.some((event) => event.includes('old-uninstaller')), false)
  }
  assert.deepEqual(hookProgram('customHeader', true), [], 'uninstaller build must not create an install preflight section')
})

test('lifecycle: silent uninstall can extract its guard before customUnInit, and Check has no stop/forget', () => {
  const realUninstall = readTemplate('uninstaller.nsh')
  assert.ok(realUninstall.indexOf('call un.checkAppRunning') < realUninstall.indexOf('!insertmacro customUnInit'))
  assert.match(readTemplate('include/allowOnlyOneInstallerInstance.nsh'), /!ifmacrodef customCheckAppRunning[\s\S]*?!insertmacro customCheckAppRunning/)
  const result = execute(hookProgram('customCheckAppRunning', true))
  assert.deepEqual(result.events, ['extract-init', 'extract-guard', 'guard:Check'])
  assert.equal(result.aborted, false)
})

test('cleanup: both install and uninstall gate every program delete, preserve data, and abort on deletion failure', () => {
  for (const entry of ['customHeader', 'customRemoveFiles']) {
    const lines = hookProgram(entry, entry === 'customRemoveFiles')
    const result = execute(lines)
    assert.equal(result.aborted, false)
    const mutations = result.events.filter((event) => /^(RMDir|Delete|Rename):/.test(event))
    assert.ok(mutations.length >= 10, `${entry} must actually remove stale program files`)
    for (let index = 0; index < result.events.length; index++) {
      const mutation = result.events[index].match(/^(RMDir|Delete|Rename):C:\\Install\\own\\(.+)$/)
      if (!mutation) continue
      assert.equal(result.events[index - 1], `guard:CheckCleanup:${mutation[2]}`, 'no ungated cleanup operation')
      assert.doesNotMatch(mutation[2], /^(data|logs|tasks|workflows|downloads|archives|\.env)(\\|$)/i)
    }
    const failedDelete = execute(lines, { deleteError: true })
    assert.equal(failedDelete.aborted, true)
    assert.equal(failedDelete.exitCode, 2)
    assert.equal(failedDelete.events.filter((event) => /^(RMDir|Delete|Rename):/.test(event)).length, 1)
    const beforeCleanup = entry === 'customHeader' ? [0, 0, 0] : [0, 0]
    const refusedGate = execute(lines, { codes: [...beforeCleanup, 2] })
    assert.equal(refusedGate.aborted, true)
    assert.equal(refusedGate.events.some((event) => /^(RMDir|Delete|Rename):/.test(event)), false)
  }
})

test('compile wiring: both builds reject missing identity, data deletion and mismatched framework keys', () => {
  for (const buildUninstaller of [false, true]) {
    assert.doesNotThrow(() => hookProgram('compileGuards', buildUninstaller))
    for (const key of Object.keys(identity).filter((key) => key.startsWith('SPIDER_'))) {
      const definitions = { ...identity }; delete definitions[key]
      assert.throws(() => hookProgram('compileGuards', buildUninstaller, definitions), new RegExp(key))
    }
    for (const key of ['DELETE_APP_DATA_ON_UNINSTALL', 'UNINSTALL_REGISTRY_KEY_2']) {
      assert.throws(() => hookProgram('compileGuards', buildUninstaller, { ...identity, [key]: '' }))
    }
    for (const key of ['INSTALL_REGISTRY_KEY', 'UNINSTALL_REGISTRY_KEY', 'APP_EXECUTABLE_FILENAME']) {
      assert.throws(() => hookProgram('customHeader', buildUninstaller, { ...identity, [key]: 'foreign' }), /identity/)
    }
  }
})

test('cleanup: a fresh empty install skips absent files without masking errors', () => {
  for (const entry of ['customHeader', 'customRemoveFiles']) {
    const result = execute(hookProgram(entry, entry === 'customRemoveFiles'), { filesExist: false })
    assert.equal(result.aborted, false)
    assert.equal(result.events.some((event) => /^(RMDir|Delete|Rename):/.test(event)), false)
  }
})

test('lifecycle: failed extraction, Stop/Forget and legacy uninstall failures never fall through', () => {
  const lines = installEntry()
  assert.equal(execute(lines, { extractError: true }).aborted, true)
  for (const stage of [1, 2]) {
    const result = execute(lines, { codes: [...Array(stage).fill(0), 2] })
    assert.equal(result.aborted, true)
    assert.equal(result.events.filter((event) => event.startsWith('guard:')).length, stage + 1)
    assert.equal(result.events.some((event) => /^(RMDir|Delete|Rename):/.test(event)), false)
  }
  assert.equal(execute(hookProgram('customUnInstallCheck'), { state: { R0: '2' } }).aborted, true)
  assert.equal(execute(hookProgram('customUnInstallCheck'), { state: { R0: '0' } }).aborted, false)
})

test('error measurement: inherited registry-not-found flags do not mask a new file failure', () => {
  const lines = installEntry()
  assert.equal(execute(lines, { initialErrors: true }).aborted, false)
  assert.equal(execute(lines, { initialErrors: true, extractError: true }).aborted, true)
  assert.equal(execute(lines, { initialErrors: true, deleteError: true }).aborted, true)
  for (let index = 0; index < lines.length; index++) {
    if (lines[index] === 'ClearErrors') {
      assert.match(lines[index + 1], /^(File|RMDir|Delete) /, 'reset only BEFORE measuring a new file operation')
      assert.match(lines[index + 2], /^IfErrors /, 'check the new failure immediately; never clear and continue')
    }
  }
})

test('offline makensis: real builder templates compile both identities and fail on missing definitions', async (t) => {
  // Optional local cache only. Never call builder's downloader, Wine or an emitted executable.
  const cache = process.platform === 'darwin' ? join(homedir(), 'Library/Caches/electron-builder/nsis') : undefined
  const nsisDir = process.env.SPIDER_TEST_NSIS_DIR || (cache && join(cache, 'nsis-3.0.4.1'))
  const pluginDir = process.env.SPIDER_TEST_NSIS_PLUGINS || (cache && join(cache, 'nsis-resources-3.4.1/plugins/x86-unicode'))
  const compiler = nsisDir && join(nsisDir, process.platform === 'win32' ? 'Bin/makensis.exe' : process.platform === 'darwin' ? 'mac/makensis' : 'linux/makensis')
  if (!compiler || !existsSync(compiler) || !pluginDir || !existsSync(pluginDir)) {
    t.skip('No cached makensis/plugins: NSIS compile unverified; no download attempted'); return
  }
  const { buildRelease } = await import('../../release-channel.mjs')
  const { applyInstallerIdentity } = require(join(frontend, 'electron-builder.config.cjs'))
  const { UUID } = builderRequire('builder-util-runtime')
  const targetSource = readFileSync(builderRequire.resolve('app-builder-lib/out/targets/nsis/NsisTarget.js'), 'utf8')
  const namespace = UUID.parse(targetSource.match(/ELECTRON_BUILDER_NS_UUID = .*?UUID\.parse\("([^"]+)"\)/)[1])
  const { NsisScriptGenerator } = builderRequire('app-builder-lib/out/targets/nsis/nsisScriptGenerator')
  const { LangConfigurator, createAddLangsMacro, addCustomMessageFileInclude } = builderRequire('app-builder-lib/out/targets/nsis/nsisLang')
  const temp = mkdtempSync(join(tmpdir(), 'spider-installer-compile-'))
  try {
    const payload = join(temp, 'payload'); mkdirSync(payload)
    const dummy = join(payload, 'fixture.bin'); writeFileSync(dummy, Buffer.from([0]))
    const generator = new NsisScriptGenerator()
    generator.include(join(templates, 'include/StdUtils.nsh'))
    generator.addIncludeDir(join(templates, 'include'))
    generator.addPluginDir('x86-unicode', pluginDir)
    generator.flags(['updated', 'force-run', 'keep-shortcuts', 'no-desktop-shortcut', 'delete-app-data', 'allusers', 'currentuser'])
    const languages = new LangConfigurator({ multiLanguageInstaller: false })
    createAddLangsMacro(generator, languages)
    let messageIndex = 0
    const packager = { getTempFile: async () => join(temp, `messages-${messageIndex++}.nsh`) }
    for (const name of ['messages.yml', 'assistedMessages.yml']) await addCustomMessageFileInclude(name, packager, generator, languages)
    generator.include(join(frontend, 'resources/installer.nsh'))
    const script = 'Unicode true\n' + generator.build() + `\n!include "${join(templates, 'installer.nsi')}"\n`
    const compile = (defines, suffix) => spawnSync(compiler, [
      '-V2', '-WX', '-INPUTCHARSET', 'UTF8', ...Object.entries(defines).map(([key, value]) => value === null ? `-D${key}` : `-D${key}=${value}`),
      `-XOutFile "${join(temp, suffix + '.exe')}"`, '-'
    ], { input: script, encoding: 'utf8', cwd: templates, env: { ...process.env, NSISDIR: nsisDir, LC_ALL: 'en_US.UTF-8' }, timeout: 30_000 })
    for (const channel of ['stable', 'beta']) {
      const release = buildRelease('0.2.8', channel, channel === 'beta' ? '1' : undefined)
      const guid = UUID.v5(release.appId, namespace)
      const base = {
        APP_ID: release.appId, APP_GUID: guid, UNINSTALL_APP_KEY: guid,
        PRODUCT_NAME: release.productName, PRODUCT_FILENAME: release.executableName,
        APP_FILENAME: release.executableName, APP_PACKAGE_NAME: release.packageName,
        APP_DESCRIPTION: 'Offline compile fixture', VERSION: release.version,
        SHORTCUT_NAME: release.productName, UNINSTALL_DISPLAY_NAME: release.productName,
        BUILD_RESOURCES_DIR: join(frontend, 'resources'), PROJECT_DIR: frontend,
        APP_BUILD_DIR: payload, UNINSTALLER_OUT_FILE: dummy,
        INSTALL_MODE_PER_ALL_USERS_REQUIRED: null, allowToChangeInstallationDirectory: null
      }
      await applyInstallerIdentity(release, [base])
      for (const uninstall of [false, true]) {
        const defines = { ...base, ...(uninstall ? { BUILD_UNINSTALLER: null } : {}) }
        const result = compile(defines, `${channel}-${uninstall}`)
        assert.equal(result.status, 0, `${channel}/${uninstall}: ${result.error || ''}\n${result.stdout}\n${result.stderr}`)
        for (const key of Object.keys(defines).filter((key) => key.startsWith('SPIDER_'))) {
          const missing = { ...defines }; delete missing[key]
          const refused = compile(missing, 'must-not-build')
          assert.notEqual(refused.status, 0)
          assert.match(refused.stderr, new RegExp(`Missing ${key}`))
        }
        const refused = compile({ ...defines, DELETE_APP_DATA_ON_UNINSTALL: null }, 'must-not-build')
        assert.notEqual(refused.status, 0)
        assert.match(refused.stderr, /preserve user data/)
      }
    }
  } finally { rmSync(temp, { recursive: true, force: true }) }
})

test('lifecycle: every nonzero/error/timeout guard exit aborts before next stage, including environment failure', () => {
  for (const entry of ['customHeader', 'customCheckAppRunning']) {
    const lines = entry === 'customHeader' ? installEntry() : hookProgram(entry, true)
    for (const code of [1, 2, 'error', 'timeout']) {
      const result = execute(lines, { codes: [code] })
      assert.equal(result.aborted, true, `${entry}:${code}`)
      assert.equal(result.exitCode, 2)
      assert.deepEqual(result.events.filter((event) => event.startsWith('guard:')), ['guard:Check'])
    }
    const result = execute(lines, { envError: true })
    assert.equal(result.aborted, true)
    assert.equal(result.events.some((event) => event.startsWith('guard:')), false)
  }
})
