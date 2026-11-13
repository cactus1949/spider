/** 任务级自定义文件名。规则与后端 custom_filename.py 保持一致。 */

export const FILENAME_EXAMPLE_DATE = '20261112'
export const FILENAME_PLACEHOLDERS = [
  'yyyy',
  'yyyyMM',
  'yyyyMMdd',
  'MM',
  'dd',
  'yyyy-MM',
  'yyyy-MM-dd'
] as const

function placeholderPattern(): RegExp {
  return /\{([^{}]*)\}/g
}
const RESERVED = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`)
])
const ILLEGAL = /[\\/:*?"<>|]/

export interface CustomFileName {
  enabled: boolean
  template: string
}

export type CustomFileNames = Record<string, CustomFileName>

export function fileSuffix(saveAs: string): string {
  const leaf = String(saveAs || '').replace(/\\/g, '/').split('/').pop() || ''
  const match = leaf.match(/(\.[A-Za-z0-9]{1,8})$/)
  const known = ['.xlsx', '.xls', '.csv', '.json', '.zip', '.txt', '.xml', '.html', '.pdf']
  if (match && known.includes(match[1].toLowerCase())) return match[1]
  return '.xlsx'
}

export function validateCustomFilename(template: string): string {
  const raw = String(template || '')
  const text = raw.trim()
  if (!text) return '文件名不能为空'
  if (raw !== text || text.endsWith('.')) return '文件名不能以空格或点开头或结尾'
  if (ILLEGAL.test(text) || [...text].some((char) => char.charCodeAt(0) < 32)) {
    return '文件名不能包含 \\ / : * ? " < > |'
  }
  const opens = text.split('{').length - 1
  const closes = text.split('}').length - 1
  if (opens !== closes) return '花括号要成对'
  const stripped = text.replace(placeholderPattern(), '')
  if (stripped.includes('{') || stripped.includes('}')) return '花括号要成对'
  const tokens = [...text.matchAll(/\{([^{}]*)\}/g)].map((item) => item[1])
  for (const token of tokens) {
    if (!FILENAME_PLACEHOLDERS.includes(token as (typeof FILENAME_PLACEHOLDERS)[number])) {
      return `不认识的占位符 {${token || '空'}}`
    }
  }
  if (/\.[A-Za-z0-9]{1,8}$/.test(text)) return '不要填写扩展名，扩展名会自动沿用原文件'
  const rendered = renderStem(text)
  if (!rendered || rendered === '.' || rendered === '..') return '文件名不能为空'
  if (rendered.endsWith(' ') || rendered.endsWith('.')) return '文件名不能以空格或点结尾'
  if (rendered.length > 180) return '文件名过长'
  if (RESERVED.has(rendered.split('.', 1)[0].toUpperCase())) return '不能使用 Windows 保留名'
  return ''
}

export function previewCustomFilename(template: string, suffix: string): string {
  const error = validateCustomFilename(template)
  if (error) return ''
  const extension = suffix.startsWith('.') ? suffix : `.${suffix || 'xlsx'}`
  return `${renderStem(template.trim())}${extension}`
}

function renderStem(template: string): string {
  const sample: Record<string, string> = {
    yyyy: '2026',
    yyyyMM: '202611',
    yyyyMMdd: FILENAME_EXAMPLE_DATE,
    MM: '11',
    dd: '12',
    'yyyy-MM': '2026-11',
    'yyyy-MM-dd': '2026-11-12'
  }
  return template.replace(placeholderPattern(), (_all, token: string) => sample[token] || '')
}
