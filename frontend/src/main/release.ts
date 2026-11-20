import { app } from 'electron'
import { runtimeRelease, type ReleaseIdentity, type ReleaseInfo } from '../../release-channel.mjs'

declare const __SPIDER_GD_RELEASE__: ReleaseIdentity

export const releaseIdentity = runtimeRelease(__SPIDER_GD_RELEASE__, app.isPackaged)

export function getReleaseInfo(): ReleaseInfo {
  const { version, channel, channelLabel, productName } = releaseIdentity
  return { version, channel, channelLabel, productName }
}
