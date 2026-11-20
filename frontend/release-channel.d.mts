export type ReleaseChannel = 'stable' | 'beta'
export interface ReleaseInfo {
  version: string
  channel: ReleaseChannel
  channelLabel: string
  productName: string
}
export interface ReleaseIdentity extends ReleaseInfo {
  appId: string
  aumid: string
  packageName: string
  userDataDirName: string | null
  executableName: string
  serviceName: string
  preferredPort: number
  updateUrl: string
  updateChannel: string
  updaterCacheDirName: string
}
export function channelConfig(channel?: string): Omit<ReleaseIdentity, 'version'>
export function buildRelease(baseVersion: string, channel?: string, betaNumber?: string | number): ReleaseIdentity
export function runtimeRelease(built: ReleaseIdentity, packaged: boolean, env?: NodeJS.ProcessEnv): ReleaseIdentity
export function configureAppIdentity(app: {
  setName(name: string): void
  setPath(name: 'userData', path: string): void
  getPath(name: 'appData'): string
}, release: ReleaseIdentity): void
export function backendEnvironment(release: ReleaseIdentity, packaged: boolean, userData: string, inherited?: NodeJS.ProcessEnv): NodeJS.ProcessEnv
export function resolveBackendDataDir(rawHome: string | undefined, launchCwd: string, defaultHome: string, userHome?: string): string
export function assertBuiltRelease(expected: ReleaseIdentity, built: ReleaseIdentity): void
