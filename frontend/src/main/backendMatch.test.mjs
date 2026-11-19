import assert from 'node:assert/strict'
import test from 'node:test'
import { backendDecision, ownServiceName } from './backendMatch.mjs'

test('packaged app attaches only when the exe path matches', () => {
  const expected = 'C:\\Users\\admin\\AppData\\Local\\Programs\\spider-gd-desktop\\resources\\backend\\spider-gd-service.exe'
  const decision = backendDecision({
    packaged: true,
    expectedExe: expected,
    health: { executable: expected, pid: 12, spider_gd: '0.2.4' },
  })
  assert.equal(decision.action, 'attach')
})

test('packaged app replaces another copy of its own service and does not touch python', () => {
  const old = backendDecision({
    packaged: true,
    expectedExe: 'C:\\new\\resources\\backend\\spider-gd-service.exe',
    health: {
      executable: 'C:\\old\\resources\\backend\\spider-gd-service.exe',
      pid: 44,
      spider_gd: '0.2.0',
    },
  })
  assert.equal(old.action, 'replace')
  assert.equal(old.pid, 44)
  const foreign = backendDecision({
    packaged: true,
    expectedExe: 'C:\\new\\resources\\backend\\spider-gd-service.exe',
    health: { executable: 'C:\\Python311\\python.exe', pid: 9, spider_gd: '0.2.0' },
  })
  assert.equal(foreign.action, 'error')
  assert.equal(ownServiceName('C:\\Python311\\python.exe'), false)
})

test('development does not require the frozen exe', () => {
  const decision = backendDecision({
    packaged: false,
    expectedExe: '',
    health: { executable: 'C:\\repo\\.venv\\Scripts\\python.exe', pid: 3, spider_gd: '0.2.4' },
  })
  assert.equal(decision.action, 'attach')
})
