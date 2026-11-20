import type { ReleaseIdentity } from '../../release-channel.mjs'

export interface VersionRecord {
  version: string
  publishedAt: string
  notes: string[]
}

export interface VersionCatalog {
  channel: 'stable' | 'beta'
  releases: VersionRecord[]
}

export function historicalInstallerUrl(release: ReleaseIdentity, version: string): string
export function parseVersionCatalog(release: ReleaseIdentity, data: unknown): VersionCatalog
export function readVersionCatalog(
  release: ReleaseIdentity,
  fetcher?: (url: string, options: RequestInit) => Promise<Response>
): Promise<VersionCatalog>
