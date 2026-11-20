import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import { fileURLToPath } from 'node:url'
const require = createRequire(import.meta.url)
const ts = require('typescript')

// Transpile the actual production module; substitute only explicitly listed boundaries.
export function loadProduction(relative, mocks = {}, globals = {}) {
  const filename = fileURLToPath(new URL(relative, import.meta.url))
  const { outputText } = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true }
  })
  const exports = {}
  runInNewContext(outputText, {
    exports, require: (id) => Object.hasOwn(mocks, id) ? mocks[id] : require(id),
    console: { log() {}, error() {}, warn() {} }, Buffer, AbortSignal,
    setTimeout, clearTimeout, __dirname: fileURLToPath(new URL('../../out/main/', import.meta.url)), ...globals
  }, { filename })
  return exports
}

export async function flush() {
  for (let i = 0; i < 30; i++) await Promise.resolve()
}
