import type { DesktopApi } from './index'

export interface ElectronAPI extends DesktopApi {
  getDataDir(): Promise<string>
}

declare global {
  interface Window {
    api: ElectronAPI
  }
}

export {}
