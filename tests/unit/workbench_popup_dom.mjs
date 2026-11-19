import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(
  new URL('../../src/spider_gd/core/crawler/workbench_popup.py', import.meta.url),
  'utf8',
)
const found = source.match(/POPUP_READ_JS = r"""([\s\S]*?)"""/)
if (!found) throw new Error('没有从 workbench_popup.py 取出弹窗脚本')

function box(left, top, width, height) {
  return { left, top, width, height, right: left + width, bottom: top + height, x: left, y: top }
}

class El {
  constructor(props = {}) {
    this.nodeType = 1
    this.parentElement = null
    this.children = []
    this.tag = props.tag || 'div'
    this.className = props.className || ''
    this.attrs = props.attrs || {}
    this.innerText = props.innerText || ''
    this.style = {
      display: 'block',
      visibility: 'visible',
      opacity: '1',
      pointerEvents: 'auto',
      position: 'static',
      zIndex: 'auto',
      ...(props.style || {}),
    }
    this.rect = props.rect || box(0, 0, 0, 0)
  }

  append(child) {
    child.parentElement = this
    this.children.push(child)
    return child
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null
  }

  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name)
  }

  getBoundingClientRect() {
    return this.rect
  }

  contains(node) {
    if (node === this) return true
    return this.children.some((child) => child.contains(node))
  }

  querySelectorAll(selector) {
    const parts = selector.split(',').map((item) => item.trim())
    const out = []
    const visit = (node) => {
      const matched = parts.some((part) => {
        if (part === '*') return true
        if (part === 'button') return node.tag === 'button'
        if (part === 'a') return node.tag === 'a'
        if (part === '[role="button"]') return node.attrs.role === 'button'
        return false
      })
      if (matched) out.push(node)
      node.children.forEach(visit)
    }
    this.children.forEach(visit)
    return out
  }
}

function read(build) {
  const body = new El({ tag: 'body', rect: box(0, 0, 1280, 720) })
  build(body)
  const all = []
  const visit = (node) => {
    all.push(node)
    node.children.forEach(visit)
  }
  body.children.forEach(visit)
  const document = {
    body,
    elementFromPoint(x, y) {
      const hits = all.filter((node) => {
        const rect = node.rect
        return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom
      })
      hits.sort((a, b) => Number(b.style.zIndex || 0) - Number(a.style.zIndex || 0))
      return hits[0] || body
    },
  }
  const sandbox = {
    window: { innerWidth: 1280, innerHeight: 720 },
    document,
    getComputedStyle: (el) => el.style,
    result: null,
  }
  vm.runInNewContext(`result = (${found[1]})()`, sandbox)
  return sandbox.result
}

test('workbench header and page body are not popups', () => {
  const header = read((body) => {
    body.append(new El({
      className: 'WorkBenchHeaderWrap',
      innerText: '首页 权限管理 档案管理 信息披露',
      style: { position: 'fixed', zIndex: '100' },
      rect: box(0, 0, 1280, 80),
    }))
  })
  assert.equal(header, null)
  const page = read((body) => {
    body.append(new El({
      className: 'layout-content',
      innerText: '交易日历 周明细 现货交易',
      style: { position: 'absolute', zIndex: '1' },
      rect: box(0, 80, 1280, 560),
    }))
  })
  assert.equal(page, null)
})

test('hidden, faded, off-screen and non-interactive leftovers are not popups', () => {
  assert.equal(read((body) => {
    const wrap = body.append(new El({ style: { opacity: '0' }, rect: box(0, 0, 1280, 720) }))
    wrap.append(new El({
      innerText: '已经关掉的协议',
      style: { position: 'fixed', zIndex: '20' },
      rect: box(400, 180, 480, 240),
    }))
  }), null)
  assert.equal(read((body) => {
    body.append(new El({
      innerText: '屏幕外',
      style: { position: 'fixed', zIndex: '20' },
      rect: box(-2400, 100, 400, 200),
    }))
  }), null)
  assert.equal(read((body) => {
    body.append(new El({
      className: 'ant-modal',
      attrs: { role: 'dialog' },
      innerText: '动画残留',
      style: { position: 'fixed', zIndex: '20', pointerEvents: 'none' },
      rect: box(400, 180, 480, 240),
    }))
  }), null)
  assert.equal(read((body) => {
    body.append(new El({
      className: 'ant-modal',
      attrs: { role: 'dialog', 'aria-hidden': 'true' },
      innerText: '隐藏残留',
      style: { position: 'fixed', zIndex: '20' },
      rect: box(400, 180, 480, 240),
    }))
  }), null)
})

test('a real dialog is still reported and a text card is not', () => {
  const dialog = read((body) => {
    body.append(new El({
      className: 'ant-modal-mask',
      style: { position: 'fixed', zIndex: '1000' },
      rect: box(0, 0, 1280, 720),
    }))
    const modal = body.append(new El({
      className: 'ant-modal',
      attrs: { role: 'dialog' },
      innerText: '贵单位仍有协议待签署',
      style: { position: 'fixed', zIndex: '1001' },
      rect: box(400, 180, 480, 240),
    }))
    modal.append(new El({
      tag: 'button',
      innerText: '关闭',
      style: { position: 'static', zIndex: '1002' },
      rect: box(760, 360, 64, 32),
    }))
  })
  assert.equal(dialog.text.includes('协议待签署'), true)
  assert.equal(dialog.button.label, '关闭')
  assert.equal(dialog.button.safe, true)

  const risky = read((body) => {
    body.append(new El({
      className: 'ant-modal-mask',
      style: { position: 'fixed', zIndex: '1000' },
      rect: box(0, 0, 1280, 720),
    }))
    body.append(new El({
      attrs: { role: 'dialog' },
      innerText: '请去签署协议',
      style: { position: 'fixed', zIndex: '1001' },
      rect: box(400, 180, 480, 240),
    }))
  })
  assert.equal(risky.text, '请去签署协议')
  assert.equal(risky.button, null)

  assert.equal(read((body) => {
    body.append(new El({
      innerText: '页面中间的说明卡片',
      style: { position: 'fixed', zIndex: '5' },
      rect: box(400, 180, 480, 240),
    }))
  }), null)
})
