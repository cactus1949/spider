function versionParts(release, version) {
  const base = '(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)'
  const pattern = release.channel === 'stable' ? `^${base}$` : `^${base}-beta\\.([1-9]\\d*)$`
  const match = typeof version === 'string' && version.length <= 80 && version.match(new RegExp(pattern))
  if (!match || !match.slice(1).every(part => Number.isSafeInteger(Number(part)))) {
    throw new Error('版本编号与当前渠道不一致或格式无效')
  }
  return match.slice(1).map(Number)
}

function releaseBase(release) {
  const base = new URL(`${release.updateUrl.replace(/\/+$/, '')}/`)
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw new Error('发布地址无效')
  }
  return base
}

export function historicalInstallerUrl(release, version) {
  versionParts(release, version)
  const fileName = `${release.productName}-${version}-setup.exe`
  return new URL(encodeURIComponent(fileName), releaseBase(release)).href
}

export function parseVersionCatalog(release, data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)
      || data.schemaVersion !== 1 || data.channel !== release.channel
      || !Array.isArray(data.releases) || data.releases.length > 200) {
    throw new Error('版本记录格式或渠道不正确')
  }
  const seen = new Set()
  const releases = data.releases.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)
        || Object.keys(item).some(key => !['version', 'publishedAt', 'notes'].includes(key))) {
      throw new Error('版本记录格式不正确，不能指定外部地址或文件名')
    }
    versionParts(release, item.version)
    if (seen.has(item.version)) throw new Error('版本记录包含重复版本')
    seen.add(item.version)
    if (typeof item.publishedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(item.publishedAt)
        || !Number.isFinite(Date.parse(item.publishedAt))
        || new Date(item.publishedAt).toISOString().slice(0, 10) !== item.publishedAt
        || !Array.isArray(item.notes) || item.notes.length > 100
        || item.notes.some(note => typeof note !== 'string' || note.length > 4000)) {
      throw new Error('版本日期或更新说明格式不正确')
    }
    return { version: item.version, publishedAt: item.publishedAt, notes: [...item.notes] }
  })
  releases.sort((a, b) => {
    const left = versionParts(release, a.version)
    const right = versionParts(release, b.version)
    for (let i = 0; i < left.length; i++) {
      if (left[i] !== right[i]) return left[i] < right[i] ? 1 : -1
    }
    return 0
  })
  return { channel: release.channel, releases }
}

export async function readVersionCatalog(release, fetcher = fetch) {
  const url = new URL('history.json', releaseBase(release)).href
  let response
  try {
    response = await fetcher(url, {
      redirect: 'error', signal: AbortSignal.timeout(15000), headers: { Accept: 'application/json' }
    })
  } catch (error) {
    // 现场机器常年离线，版本记录只是锦上添花：断网/超时要给人话，而不是底层网络异常。
    const name = error && typeof error === 'object' && 'name' in error ? String(error.name) : ''
    if (name === 'TimeoutError' || name === 'AbortError') {
      throw new Error('联网获取版本记录超时；离线不影响本地采集，联网后请点「刷新版本记录」')
    }
    throw new Error('当前无法联网获取版本记录；离线不影响本地采集，联网后请点「刷新版本记录」')
  }
  if (response.status === 404) throw new Error('当前渠道的版本记录尚未发布，请联系维护人员提供安装包')
  if (!response.ok) throw new Error(`版本记录读取失败（HTTP ${response.status}），请稍后刷新`)
  if (response.redirected || (response.url && response.url !== url)) {
    throw new Error('版本记录响应地址与当前渠道不一致')
  }
  if (!response.body) throw new Error('版本记录响应为空')
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let size = 0
  let text = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > 512 * 1024) {
        await reader.cancel()
        throw new Error('版本记录响应过大')
      }
      text += decoder.decode(value, { stream: true })
    }
    text += decoder.decode()
  } finally {
    reader.releaseLock()
  }
  let data
  try {
    data = JSON.parse(text)
  } catch {
    throw new Error('版本记录格式不正确')
  }
  return parseVersionCatalog(release, data)
}
