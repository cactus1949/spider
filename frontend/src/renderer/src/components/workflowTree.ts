import type { WorkflowGroup, WorkflowInfo } from '../services/api'

export interface WorkflowNode {
  key: string
  title: string
  label: string
  value: string
  selectable: boolean
  searchText: string
  children?: WorkflowNode[]
}

const compare = (a: { title: string }, b: { title: string }): number =>
  a.title.localeCompare(b.title, 'zh-CN')

export function workflowPath(item: WorkflowInfo, groups: WorkflowGroup[]): string {
  const byId = new Map(groups.map((group) => [group.id, group]))
  const parts = [item.label || item.name]
  const seen = new Set<string>()
  let parent = item.group_id
  while (parent && byId.has(parent) && !seen.has(parent)) {
    seen.add(parent)
    const group = byId.get(parent)!
    parts.unshift(group.name)
    parent = group.parent_id
  }
  return parts.join(' / ')
}

export function workflowTree(workflows: WorkflowInfo[], groups: WorkflowGroup[], type?: 'indicator' | 'login'): WorkflowNode[] {
  const nodes = new Map<string, WorkflowNode>()
  const roots: WorkflowNode[] = []
  for (const group of groups) {
    nodes.set(group.id, { key: `group:${group.id}`, value: `group:${group.id}`, title: group.name,
      label: group.name, selectable: false, searchText: group.name.toLowerCase(), children: [] })
  }
  for (const group of groups) {
    const node = nodes.get(group.id)!
    const parent = group.parent_id && nodes.get(group.parent_id)
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  for (const item of workflows) {
    if (type && item.type !== type) continue
    const path = workflowPath(item, groups)
    const node: WorkflowNode = { key: `workflow:${item.name}`, value: item.name, title: path,
      label: path, selectable: true, searchText: path.toLowerCase() }
    const parent = item.group_id && nodes.get(item.group_id)
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  const sort = (items: WorkflowNode[]): void => {
    items.sort((a, b) => Number(Boolean(b.children)) - Number(Boolean(a.children)) || compare(a, b))
    items.forEach((item) => { if (item.children) sort(item.children) })
  }
  sort(roots)
  return roots
}

export function filterWorkflowTree(nodes: WorkflowNode[], keyword: string): WorkflowNode[] {
  const needle = keyword.trim().toLowerCase()
  if (!needle) return nodes
  return nodes.flatMap((node) => {
    if (node.selectable) return node.searchText.includes(needle) ? [node] : []
    if (node.label.toLowerCase().includes(needle)) return [node]
    const children = filterWorkflowTree(node.children || [], needle)
    return children.length ? [{ ...node, children }] : []
  })
}
