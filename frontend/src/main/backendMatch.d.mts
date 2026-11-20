export interface Listener {
  pid: number
  executable: string
  parentPid?: number
}
export interface KillInput {
  expectedExe: string
  reportedPid?: number
  reportedExe?: string
  listener?: Listener | null
}
export interface KillResult {
  kill: boolean
  reason?: string
  pid?: number
  executable?: string
}
export interface PortResult {
  state: 'clear' | 'taken' | 'still-ours'
  reason?: string
  pid?: number
  executable?: string
}
export interface RuntimeHealth {
  runtime: 'ok' | 'missing' | 'foreign'
  executable?: string
  pid?: number
  spider_gd?: string
  build_id?: string
  channel?: string
  cwd?: string
  data_home?: string
  frozen?: boolean
}
export interface BackendInput {
  packaged: boolean
  expectedExe: string
  expectedBuild: string
  expectedChannel: string
  expectedCwd: string
  expectedHome: string
  health: RuntimeHealth | null
  listener?: Listener | null
  listenerError?: string
  preferredPort?: number
}
export interface BackendResult {
  action: 'start' | 'attach' | 'replace' | 'error'
  reason?: string
  pid?: number
  executable?: string
}
export function normalizePath(value?: string): string
export function ownServiceName(executable?: string): boolean
export function killPlan(input: KillInput): KillResult
export function portAfterStop(input: { oldPid: number; expectedExe: string; listener?: Listener | null }): PortResult
export function startupPort(input: BackendInput): BackendResult & { port: number }
export function backendDecision(input: BackendInput): BackendResult
