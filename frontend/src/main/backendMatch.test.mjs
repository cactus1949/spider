import assert from 'node:assert/strict'
import test from 'node:test'
import { backendDecision, killPlan, portAfterStop, startupPort, normalizePath } from './backendMatch.mjs'

const expected = 'C:\\安装\\resources\\backend\\spider-gd-service.exe'
const input = (changes = {}) => ({
  packaged: true, expectedExe: expected, expectedBuild: 'new', expectedChannel: 'stable',
  expectedCwd: 'C:\\安装\\resources\\backend', expectedHome: 'C:\\用户',
  health: { runtime: 'ok', executable: expected, pid: 40, build_id: 'new', channel: 'stable', cwd: 'C:\\安装\\resources\\backend', data_home: 'C:\\用户' },
  listener: { pid: 40, executable: expected }, ...changes,
})

test('same installation and channel attaches only to matching running build and OS listener', () => {
  assert.equal(backendDecision(input()).action, 'attach')
  assert.equal(backendDecision(input({ health: { ...input().health, build_id: 'old' } })).action, 'replace')
  for (const listener of [null, { pid: 99, executable: expected }, { pid: 40, executable: 'C:\\else\\spider-gd-service.exe' }]) {
    assert.equal(backendDecision(input({ listener })).action, 'error')
  }
})

test('other installations and channels are never attached or replaced regardless of build', () => {
  for (const build of ['new', 'old']) {
    const executable = 'D:\\他安装\\spider-gd-service.exe'
    assert.equal(backendDecision(input({ health: { ...input().health, executable, build_id: build }, listener: { pid: 40, executable } })).action, 'error')
    assert.equal(startupPort(input({ health: { ...input().health, executable, build_id: build }, listener: { pid: 40, executable } })).port, 0)
    assert.equal(backendDecision(input({ health: { ...input().health, channel: 'beta', build_id: build } })).action, 'error')
  }
})

test('legacy missing runtime can only replace the actual expected installation executable', () => {
  assert.equal(backendDecision(input({ health: { runtime: 'missing' } })).action, 'replace')
  for (const listener of [null, { pid: 40, executable: 'D:\\else\\spider-gd-service.exe' }, { pid: 40, executable: 'C:\\Python\\python.exe' }]) {
    assert.equal(backendDecision(input({ health: { runtime: 'missing' }, listener })).action, 'error')
  }
})

test('kill requires exact expected path as well as reported PID/path', () => {
  const plan = { expectedExe: expected, reportedPid: 40, reportedExe: expected, listener: input().listener }
  assert.equal(killPlan(plan).kill, true)
  assert.equal(killPlan({ ...plan, expectedExe: 'D:\\else\\spider-gd-service.exe' }).kill, false)
  assert.equal(killPlan({ ...plan, reportedPid: 99 }).kill, false)
  assert.equal(killPlan({ ...plan, listener: null }).kill, false)
  assert.equal(killPlan({ listener: input().listener }).kill, false)
})

test('a later same-name PID is not evidence of a child and is never chased', () => {
  assert.equal(portAfterStop({ oldPid: 40, expectedExe: expected, listener: null }).state, 'clear')
  assert.equal(portAfterStop({ oldPid: 40, expectedExe: expected, listener: input().listener }).state, 'still-ours')
  assert.equal(portAfterStop({ oldPid: 40, expectedExe: expected, listener: { pid: 41, executable: expected } }).state, 'taken')
  assert.equal(portAfterStop({ oldPid: 40, expectedExe: expected, listener: { pid: 40, executable: 'D:\\else.exe' } }).state, 'taken')
})

test('confirmed free preferred port is used; occupied or failed OS query uses a dynamic port', () => {
  assert.equal(startupPort(input({ preferredPort: 8801, health: null, listener: null })).port, 8801)
  assert.equal(startupPort(input({ preferredPort: 8801, health: null })).port, 0)
  assert.equal(startupPort(input({ preferredPort: 8801, health: null, listener: null, listenerError: 'denied' })).port, 0)
})

test('development requires canonical interpreter, same repository, HOME, channel and OS PID', () => {
  const dev = input({ packaged: false, expectedExe: '/opt/miniconda3/bin/python3.13', expectedBuild: 'source', expectedCwd: '/repo', expectedHome: '/home/beta', expectedChannel: 'beta', listener: { pid: 5, executable: '/opt/miniconda3/bin/python3.13' }, health: { runtime: 'ok', pid: 5, executable: '/opt/miniconda3/bin/python3.13', cwd: '/repo', data_home: '/home/beta', channel: 'beta', build_id: 'source' } })
  assert.equal(backendDecision(dev).action, 'attach')
  assert.equal(backendDecision({ ...dev, listener: null }).action, 'error')
  for (const change of [{ cwd: '/other' }, { data_home: '/stable' }, { channel: 'stable' }, { executable: '/other/python' }]) {
    assert.equal(backendDecision({ ...dev, health: { ...dev.health, ...change } }).action, 'error')
  }
})

test('Windows paths ignore case and separators, POSIX paths retain case', () => {
  assert.equal(normalizePath('C:/安装/Backend/APP.exe'), normalizePath('c:\\安装\\backend\\app.EXE'))
  assert.notEqual(normalizePath('/Repo/App'), normalizePath('/repo/app'))
})
