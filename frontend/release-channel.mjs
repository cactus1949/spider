import { join, resolve, sep } from 'node:path'
import { mkdirSync, realpathSync } from 'node:fs'
import { homedir } from 'node:os'
import channels from '../src/spider_gd/release-channels.json' with { type: 'json' }

export function channelConfig(channel = 'stable') {
  if (channel !== 'stable' && channel !== 'beta') throw new Error(`Invalid channel: ${channel}`)
  return { ...channels[channel], channel }
}

export function buildRelease(baseVersion, channel = 'stable', betaNumber) {
  const identity = channelConfig(channel)
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(baseVersion)) throw new Error('VERSION must be x.y.z')
  const hasNumber = betaNumber !== undefined && betaNumber !== null && betaNumber !== ''
  if (channel === 'beta' && (!hasNumber || !/^[1-9]\d*$/.test(String(betaNumber)))) throw new Error('BetaNumber must be a positive integer for beta channel')
  if (channel === 'stable' && hasNumber) throw new Error('BetaNumber is not allowed for stable channel')
  return { ...identity, version: channel === 'beta' ? `${baseVersion}-beta.${betaNumber}` : baseVersion }
}

export function runtimeRelease(built, packaged, env = process.env) {
  if (packaged) return { ...built }
  return { ...channelConfig(env.SPIDER_GD_CHANNEL || built.channel), version: built.version.split('-')[0] }
}

export function configureAppIdentity(app, release) {
  if (release.channel === 'beta') {
    const appData = app.getPath('appData')
    const home = join(appData, release.userDataDirName)
    const expected = join(realpathSync(appData), release.userDataDirName)
    mkdirSync(home, { recursive: true })
    const actual = realpathSync(home)
    if (process.platform === 'win32' ? actual.toLowerCase() !== expected.toLowerCase() : actual !== expected) {
      throw new Error('测试版数据目录指向其他位置，请移除目录链接后重试')
    }
    app.setName(release.packageName)
    app.setPath('userData', home)
  }
}

export function backendEnvironment(release, packaged, userData, inherited = process.env) {
  const env = { ...inherited, SPIDER_GD_CHANNEL: release.channel }
  if (packaged && release.channel === 'beta') {
    env.SPIDER_GD_HOME = userData
    delete env.SPIDER_BROWSER_CONFIG
  } else if ((packaged || release.channel === 'beta') && !(env.SPIDER_GD_HOME || '').trim().replace(/^"+|"+$/g, '')) {
    env.SPIDER_GD_HOME = userData
  }
  return env
}

export function resolveBackendDataDir(rawHome, launchCwd, defaultHome, userHome = homedir()) {
  const home = (rawHome || '').trim().replace(/^"+|"+$/g, '')
  const expanded = home === '~' || home.startsWith('~/') || home.startsWith(`~${sep}`)
    ? join(userHome, home.slice(2))
    : home
  return join(resolve(launchCwd, expanded || defaultHome), 'data')
}

export function assertBuiltRelease(expected, built) {
  for (const field of ['channel', 'version', 'appId', 'packageName', 'productName', 'serviceName']) {
    if (expected[field] !== built[field]) throw new Error(`编译产物 release 身份不一致: ${field}`)
  }
}
