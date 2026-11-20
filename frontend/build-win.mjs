import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, dirname } from 'node:path'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { buildRelease } from './release-channel.mjs'

const root = dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)

try {
  let channel = 'stable'
  let betaNumber = process.env.SPIDER_GD_BETA_NUMBER || undefined
  const args = process.argv.slice(2).filter((arg) => arg !== '--')
  for (let i = 0; i < args.length; i += 2) {
    if (args[i] === '--channel' && args[i + 1]) channel = args[i + 1]
    else if (args[i] === '--beta-number' && args[i + 1]) betaNumber = args[i + 1]
    else throw new Error(`Invalid channel/build argument: ${args[i]}`)
  }
  const release = buildRelease(readFileSync(join(root, '../VERSION'), 'utf8').trim(), channel, betaNumber)
  const env = { ...process.env, SPIDER_GD_CHANNEL: release.channel, SPIDER_GD_BETA_NUMBER: betaNumber || '' }
  const vite = join(dirname(require.resolve('electron-vite/package.json')), 'bin/electron-vite.js')
  const result = spawnSync(process.execPath, [vite, 'build'], { cwd: root, env, stdio: 'inherit' })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status || 1)
  const { build, Platform, Arch } = require('electron-builder')
  const { createBuildOverrides, applyInstallerIdentity } = require('./electron-builder.config.cjs')
  await build({
    projectDir: root,
    targets: Platform.WINDOWS.createTarget(['nsis'], Arch.x64),
    config: await createBuildOverrides({ channel: release.channel, betaNumber, requireBuilt: true }),
    publish: 'never',
    effectiveOptionComputed: (options) => applyInstallerIdentity(release, options)
  })
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error))
  process.exitCode = 1
}
