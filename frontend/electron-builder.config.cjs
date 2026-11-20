const { readFileSync, existsSync } = require('node:fs')
const { join } = require('node:path')
const { createRequire } = require('node:module')
const builderRequire = createRequire(require.resolve('electron-builder'))
const { getConfig } = builderRequire('app-builder-lib/out/util/config')

/** 深合并普通对象（数组按覆盖处理，绝不拼接）。 */
function mergeConfig(base, overrides) {
  const result = { ...base }
  for (const [key, value] of Object.entries(overrides)) {
    const existing = result[key]
    if (value && typeof value === 'object' && !Array.isArray(value)
      && existing && typeof existing === 'object' && !Array.isArray(existing)) {
      result[key] = mergeConfig(existing, value)
    } else {
      result[key] = value
    }
  }
  return result
}

/** 渠道身份覆盖：appId / productName / 可执行文件名 / 安装器与更新源。 */
function identityOverrides(release) {
  return {
    appId: release.appId,
    productName: release.productName,
    executableName: release.executableName,
    extraMetadata: { version: release.version, name: release.packageName },
    nsis: { shortcutName: release.productName },
    publish: {
      provider: 'generic',
      url: release.updateUrl,
      channel: release.updateChannel,
      updaterCacheDirName: release.updaterCacheDirName
    },
    generateUpdatesFilesForAllChannels: false
  }
}

async function resolveRelease({ channel = 'stable', betaNumber, baseVersion }) {
  const { buildRelease } = await import('./release-channel.mjs')
  return buildRelease(baseVersion || readFileSync(join(__dirname, '../VERSION'), 'utf8').trim(), channel, betaNumber)
}

function verifyBuilt(release, { assertBuiltRelease, channelConfig }) {
  const built = JSON.parse(readFileSync(join(__dirname, 'out/main/release-info.json'), 'utf8'))
  assertBuiltRelease(release, built)
  const stamp = JSON.parse(readFileSync(join(__dirname, 'resources/backend/build_stamp.json'), 'utf8'))
  if (stamp.channel !== release.channel || stamp.version !== release.version) throw new Error('后端 release 身份与前端构建不一致')
  if (typeof built.buildId !== 'string' || !/^[0-9a-f]{12}$/i.test(built.buildId)
    || stamp.build_id !== built.buildId) throw new Error('后端 build_id 与前端构建 buildId 缺失、非法或不一致')
  if (!existsSync(join(__dirname, 'resources/backend', `${release.serviceName}.exe`))) throw new Error('未找到对应渠道的后端 exe')
  const other = channelConfig(release.channel === 'beta' ? 'stable' : 'beta')
  if (existsSync(join(__dirname, 'resources/backend', `${other.serviceName}.exe`))) throw new Error('后端产物混入另一渠道 exe，请重新生成对应渠道的后端')
}

/**
 * 传给 electron-builder.build() 的“增量覆盖”。
 *
 * 只给覆盖项、不给整份 electron-builder.yml：程序化 config 会与 yml 再做一次深合并，
 * 若把 yml 的数组（extraResources/files 等）也传进去会翻倍，同一目录被并发复制，
 * Windows 上会报 EBUSY（copyfile .env）。
 */
async function createBuildOverrides({ channel = 'stable', betaNumber, baseVersion, requireBuilt = false } = {}) {
  const release = await resolveRelease({ channel, betaNumber, baseVersion })
  if (requireBuilt) {
    const { assertBuiltRelease, channelConfig } = await import('./release-channel.mjs')
    verifyBuilt(release, { assertBuiltRelease, channelConfig })
  }
  return identityOverrides(release)
}

/**
 * 完整配置（yml + 渠道覆盖）。给测试、validateConfig 和 electron-builder 的配置文件入口使用；
 * 注意不要直接把它传给 build()，否则数组会被 yml 再合并一次。
 */
async function createBuilderConfig({ channel = 'stable', betaNumber, baseVersion, requireBuilt = false } = {}) {
  const release = await resolveRelease({ channel, betaNumber, baseVersion })
  if (requireBuilt) {
    const { assertBuiltRelease, channelConfig } = await import('./release-channel.mjs')
    verifyBuilt(release, { assertBuiltRelease, channelConfig })
  }
  const config = await getConfig(__dirname, join(__dirname, 'electron-builder.yml'), null)
  return mergeConfig(config, identityOverrides(release))
}

async function applyInstallerIdentity(release, [defines]) {
  const { channelConfig } = await import('./release-channel.mjs')
  const { UUID } = builderRequire('builder-util-runtime')
  // electron-builder 24 的默认安装 GUID 命名空间。
  const namespace = UUID.parse('50e065bc-3134-11e6-9bab-38c9862bdaf3')
  const guid = UUID.v5(release.appId, namespace)
  const other = channelConfig(release.channel === 'beta' ? 'stable' : 'beta')
  const otherGuid = UUID.v5(other.appId, namespace)
  if (defines.APP_ID !== release.appId || defines.APP_GUID !== guid
    || defines.UNINSTALL_APP_KEY !== guid || defines.PRODUCT_FILENAME !== release.executableName) {
    throw new Error('安装器身份与渠道契约不一致')
  }
  Object.assign(defines, {
    SPIDER_CHANNEL: release.channel,
    SPIDER_OWN_MAIN_EXE: `${release.executableName}.exe`,
    SPIDER_OTHER_MAIN_EXE: `${other.executableName}.exe`,
    SPIDER_OWN_SERVICE_EXE: `${release.serviceName}.exe`,
    SPIDER_OTHER_SERVICE_EXE: `${other.serviceName}.exe`,
    SPIDER_OWN_INSTALL_KEY: `Software\\${guid}`,
    SPIDER_OTHER_INSTALL_KEY: `Software\\${otherGuid}`,
    SPIDER_OWN_UNINSTALL_KEY: `Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${guid}`,
    SPIDER_OTHER_UNINSTALL_KEY: `Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${otherGuid}`
  })
  return false
}

module.exports = () => createBuilderConfig({ channel: process.env.SPIDER_GD_CHANNEL || 'stable', betaNumber: process.env.SPIDER_GD_BETA_NUMBER, requireBuilt: true })
module.exports.createBuilderConfig = createBuilderConfig
module.exports.createBuildOverrides = createBuildOverrides
module.exports.applyInstallerIdentity = applyInstallerIdentity
