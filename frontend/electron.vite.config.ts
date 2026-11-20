import { resolve } from 'path'
import { readFileSync } from 'fs'
import { execFileSync } from 'node:child_process'
import { buildRelease, channelConfig } from './release-channel.mjs'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ command }) => {
  const version = readFileSync(resolve(__dirname, '../VERSION'), 'utf8').trim()
  const channel = process.env.SPIDER_GD_CHANNEL || 'stable'
  const release = command === 'build'
    ? buildRelease(version, channel, process.env.SPIDER_GD_BETA_NUMBER)
    : { ...channelConfig(channel), version }
  const builtRelease = command === 'build'
    ? { ...release, buildId: execFileSync('git', ['rev-parse', '--short=12', 'HEAD'], {
        cwd: resolve(__dirname, '..'), encoding: 'utf8'
      }).trim() }
    : release
  return {
    main: {
      define: { __SPIDER_GD_RELEASE__: JSON.stringify(release) },
      plugins: [{
        name: 'bind-release-identity',
        generateBundle() {
          this.emitFile({ type: 'asset', fileName: 'release-info.json', source: JSON.stringify(builtRelease) })
        }
      }],
      // 全部打包进 asar，只 externalize electron 内置模块
      // 不 externalize 任何 npm 依赖 → 根除 node_modules 缺失问题
      build: {
        rollupOptions: {
          external: ['electron']
        }
      }
    },
    preload: {
      plugins: [externalizeDepsPlugin()]
    },
    renderer: {
      resolve: {
        alias: {
          '@renderer': resolve('src/renderer/src')
        }
      },
      plugins: [react()]
    }
  }
})
