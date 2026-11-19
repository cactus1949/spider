import assert from 'node:assert/strict'
import test from 'node:test'
import { backendDecision, killPlan, portAfterStop, startupPort } from './backendMatch.mjs'

const expected = 'C:\\Users\\admin\\AppData\\Local\\Programs\\spider-gd-desktop\\resources\\backend\\spider-gd-service.exe'

test('same path but a different build is not treated as the new install', () => {
  const decision = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: {
      runtime: 'ok',
      executable: expected,
      pid: 40,
      spider_gd: '0.2.4',
      build_id: 'oldbuild999',
    },
    listener: { pid: 40, executable: expected },
  })
  assert.equal(decision.action, 'replace')
  assert.equal(decision.pid, 40)
})

test('matching build attaches only when the listening process agrees', () => {
  const ok = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: { runtime: 'ok', executable: expected, pid: 40, build_id: 'newbuild123' },
    listener: { pid: 40, executable: expected },
  })
  assert.equal(ok.action, 'attach')
  const mismatch = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: { runtime: 'ok', executable: expected, pid: 40, build_id: 'newbuild123' },
    listener: { pid: 99, executable: 'C:\\Python311\\python.exe' },
  })
  assert.equal(mismatch.action, 'error')
})

test('an old backend without runtime is replaced only from the OS listener', () => {
  const replace = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: { runtime: 'missing' },
    listener: { pid: 7, executable: expected },
  })
  assert.equal(replace.action, 'replace')
  assert.equal(replace.pid, 7)
  const unknown = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: { runtime: 'missing' },
    listener: null,
  })
  assert.equal(unknown.action, 'error')
  const foreign = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: { runtime: 'foreign' },
    listener: { pid: 8, executable: 'C:\\Chrome\\chrome.exe' },
  })
  assert.equal(foreign.action, 'error')
})

test('a reported pid is not killed when the listener is something else', () => {
  const plan = killPlan({
    reportedPid: 40,
    reportedExe: expected,
    listener: { pid: 41, executable: 'C:\\Windows\\System32\\svchost.exe' },
  })
  assert.equal(plan.kill, false)
})

test('after quit, a leftover child can be stopped and a foreign pid cannot', () => {
  assert.deepEqual(portAfterStop({ oldPid: 40, listener: null }), { state: 'clear' })
  assert.equal(portAfterStop({
    oldPid: 40,
    listener: { pid: 40, executable: expected },
  }).state, 'still-ours')
  assert.equal(portAfterStop({
    oldPid: 40,
    listener: { pid: 41, executable: expected },
  }).state, 'child')
  const taken = portAfterStop({
    oldPid: 40,
    listener: { pid: 88, executable: 'C:\\Chrome\\chrome.exe' },
  })
  assert.equal(taken.state, 'taken')
})

test('an openapi title match does not kill python or an unknown listener', () => {
  const python = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: { runtime: 'missing' },
    listener: { pid: 9, executable: 'C:\\Python311\\python.exe' },
  })
  assert.equal(python.action, 'error')
  const samePathOtherBuild = backendDecision({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    health: {
      runtime: 'ok',
      executable: expected,
      pid: 40,
      spider_gd: '0.2.4',
      build_id: 'oldbuild999',
    },
    listener: { pid: 40, executable: 'C:\\Python311\\python.exe' },
  })
  assert.equal(samePathOtherBuild.action, 'error')
})

test('a foreign program on 8800 does not stop startup and is not killed', () => {
  const choice = startupPort({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    preferredPort: 8800,
    health: null,
    listener: { pid: 9, executable: 'C:\\Python311\\python.exe' },
  })
  assert.equal(choice.action, 'start')
  assert.equal(choice.port, 0)
  assert.match(choice.reason, /没有关闭它/)
  const free = startupPort({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    preferredPort: 8800,
    health: null,
    listener: null,
  })
  assert.equal(free.action, 'start')
  assert.equal(free.port, 8800)
  const foreignApi = startupPort({
    packaged: true,
    expectedExe: expected,
    expectedBuild: 'newbuild123',
    preferredPort: 8800,
    health: { runtime: 'foreign' },
    listener: { pid: 8, executable: 'C:\\Chrome\\chrome.exe' },
  })
  assert.equal(foreignApi.action, 'start')
  assert.equal(foreignApi.port, 0)
})

test('development does not require the frozen exe', () => {
  const decision = backendDecision({
    packaged: false,
    expectedExe: '',
    expectedBuild: '',
    health: { runtime: 'ok', executable: 'C:\\repo\\.venv\\Scripts\\python.exe', pid: 3, build_id: 'source' },
    listener: null,
  })
  assert.equal(decision.action, 'attach')
})
