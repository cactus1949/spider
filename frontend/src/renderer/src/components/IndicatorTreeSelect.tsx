import { useMemo, useState } from 'react'
import { TreeSelect } from 'antd'
import type { WorkflowGroup, WorkflowInfo } from '../services/api'
import { filterWorkflowTree, workflowTree } from './workflowTree'

interface Props {
  workflows: WorkflowInfo[]
  groups: WorkflowGroup[]
  value?: string[]
  onChange?: (value: string[]) => void
  placeholder?: string
}

export default function IndicatorTreeSelect({ workflows, groups, value, onChange, placeholder }: Props): JSX.Element {
  const [search, setSearch] = useState('')
  const nodes = useMemo(() => filterWorkflowTree(workflowTree(workflows, groups, 'indicator'), search), [workflows, groups, search])
  const expanded = useMemo(() => {
    const keys: string[] = []
    const visit = (items: typeof nodes): void => items.forEach((item) => { if (item.children) { keys.push(item.key); visit(item.children) } })
    visit(nodes)
    return keys
  }, [nodes])
  return <TreeSelect
    multiple
    showSearch
    allowClear
    value={value}
    onChange={(next) => onChange?.(next as string[])}
    onSearch={setSearch}
    onDropdownVisibleChange={(open) => { if (!open) setSearch('') }}
    filterTreeNode={false}
    treeData={nodes}
    treeExpandedKeys={search ? expanded : undefined}
    treeNodeLabelProp="label"
    placeholder={placeholder || '选择指标'}
    maxTagCount="responsive"
    style={{ width: '100%' }}
  />
}
