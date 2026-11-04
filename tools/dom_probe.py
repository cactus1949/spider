"""只返回普通数据的 DOM 探针；不创建 Playwright ElementHandle。"""

DOM_PROBE_JS = r"""(arg) => {
  const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  const textOf = (node) => normalize(
    node.matches('input[type=button],input[type=submit]') ? node.value : node.textContent
  );
  const allElements = () => Array.from(document.querySelectorAll('*')).filter(
    (node) => !node.matches('script,style,noscript,head')
  );
  const paths = new Map();
  const pathOf = (node) => {
    if (paths.has(node)) return paths.get(node);
    const parts = [];
    let current = node;
    while (current.parentElement) {
      const parent = current.parentElement;
      const index = Array.prototype.indexOf.call(parent.children, current) + 1;
      parts.unshift(':nth-child(' + index + ')');
      current = parent;
    }
    const path = ':root' + (parts.length ? ' > ' + parts.join(' > ') : '');
    paths.set(node, path);
    return path;
  };
  const textMatches = (text, exact, smallest) => {
    const want = normalize(text);
    const matches = (node) => exact ? textOf(node) === want
      : textOf(node).toLowerCase().includes(want.toLowerCase());
    return allElements().filter((node) => matches(node) && (!smallest
      || !Array.from(node.children).some(matches)));
  };
  const asSelector = (nodes) => nodes.length
    ? ':is(' + nodes.map(pathOf).join(',') + ')' : ':not(*)';
  const unquote = (value) => {
    const quote = value[0];
    if ((quote !== '"' && quote !== "'") || value[value.length - 1] !== quote)
      throw new Error('Text predicate requires a quoted string');
    return value.slice(1, -1).replace(/\\([\\'"])/g, '$1');
  };
  // 用结构路径表达文字匹配集，再交给浏览器处理 CSS 的嵌套 :has / :not / 组合符。
  // 不给页面写临时属性或全局变量，也不把 DOM 节点返回给调用者。
  const queryAll = (selector) => {
    if (!selector || !selector.trim()) throw new Error('Empty selector');
    if (selector.startsWith('text=')) {
      let text = selector.slice(5).trim();
      const exact = text.startsWith('"') || text.startsWith("'");
      if (exact) text = unquote(text);
      return textMatches(text, exact, true);
    }
    let output = '', quote = '', escaped = false;
    for (let i = 0; i < selector.length; i += 1) {
      const ch = selector[i];
      if (quote) {
        output += ch;
        if (escaped) escaped = false;
        else if (ch === '\\') escaped = true;
        else if (ch === quote) quote = '';
        continue;
      }
      if (ch === '"' || ch === "'") { quote = ch; output += ch; continue; }
      const matched = selector.slice(i).match(/^:(has-text|text-is)\(/);
      if (!matched) { output += ch; continue; }
      const start = i + matched[0].length;
      let end = start, innerQuote = '', innerEscape = false;
      for (; end < selector.length; end += 1) {
        const c = selector[end];
        if (innerQuote) {
          if (innerEscape) innerEscape = false;
          else if (c === '\\') innerEscape = true;
          else if (c === innerQuote) innerQuote = '';
        } else if (c === '"' || c === "'") innerQuote = c;
        else if (c === ')') break;
      }
      if (end === selector.length) throw new Error('Unclosed text predicate');
      const text = unquote(selector.slice(start, end).trim());
      output += asSelector(textMatches(text, matched[1] === 'text-is', false));
      i = end;
    }
    return Array.from(document.querySelectorAll(output));
  };
  const rectOf = (node) => {
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    for (let p = node; p; p = p.parentElement) {
      const style = getComputedStyle(p);
      if (style.visibility === 'hidden' || style.visibility === 'collapse'
          || style.display === 'none') return null;
    }
    return rect;
  };
  const all = queryAll(arg.selector);
  const nodes = arg.index === undefined || arg.index === null
    ? all : (all[arg.index] ? [all[arg.index]] : []);
  if (arg.op === 'count') return all.length;
  if (arg.op === 'texts') return nodes.map((node) => normalize(node.innerText || node.textContent));
  if (arg.op === 'visible_texts') return nodes.filter((node) => rectOf(node))
    .map((node) => normalize(node.innerText || node.textContent));
  if (arg.op === 'attr') return nodes.length ? nodes[0].getAttribute(arg.name) : null;
  if (arg.op === 'value') return nodes.length ? nodes[0].value : null;
  const node = nodes.find((candidate) => rectOf(candidate));
  if (arg.op === 'visible') return !!node;
  if (!node) return null;
  if (arg.op === 'checked') return !!node.checked || node.getAttribute('aria-checked') === 'true';
  if (arg.op === 'has_value') return !!node.value;
  if (arg.op === 'value_matches') return node.value === arg.value;
  if (arg.op === 'editable') return !node.disabled && !node.readOnly;
  if (arg.op === 'focused')
    return document.activeElement === node || node.contains(document.activeElement);
  if (arg.op !== 'box') throw new Error('Unknown probe operation');
  if (node.closest(':disabled,[aria-disabled="true"]')) return null;
  let rect = rectOf(node);
  if (rect.top < 0 || rect.bottom > innerHeight || rect.left < 0 || rect.right > innerWidth) {
    node.scrollIntoView({block:'center', inline:'center', behavior:'instant'});
    rect = rectOf(node);
  }
  if (!rect) return null;
  const x = Math.max(0, rect.left), y = Math.max(0, rect.top);
  const right = Math.min(innerWidth, rect.right), bottom = Math.min(innerHeight, rect.bottom);
  if (right <= x || bottom <= y) return null;
  const hit = document.elementFromPoint((x + right) / 2, (y + bottom) / 2);
  if (!hit || (hit !== node && !node.contains(hit))) return null;
  return {x, y, width:right-x, height:bottom-y, pageX:x+scrollX, pageY:y+scrollY, count:all.length};
}"""
