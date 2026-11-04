import { useEffect, useState } from 'react'
import { Form, Select, Switch, Tooltip } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'
import { api, type UploadProfile } from '../services/api'

const uploadLabel = (
  <span>
    自动上传
    <Tooltip title="开启自动上传会切换到上传配置中设置的 WiFi，上传完成后切回">
      <QuestionCircleOutlined style={{ marginLeft: 6, color: '#8A8F95' }} />
    </Tooltip>
  </span>
)

/** 任务基本信息和补数据共用的上传配置、自动上传开关。 */
export default function TaskUploadFields({
  active,
  profileId,
  autoUpload,
  onChange
}: {
  active: boolean
  profileId?: string
  autoUpload?: boolean
  onChange?: (next: { profileId: string; autoUpload: boolean }) => void
}): JSX.Element {
  const [profiles, setProfiles] = useState<UploadProfile[]>([])

  useEffect(() => {
    if (!active) return
    let cancelled = false
    void api.listUploadProfiles().then((res) => {
      if (!cancelled) setProfiles(res.profiles || [])
    }).catch(() => {
      if (!cancelled) setProfiles([])
    })
    return () => { cancelled = true }
  }, [active])

  const options = profiles.map((item) => ({ label: item.name, value: item.id }))
  if (onChange) {
    return (
      <div className="task-form-grid">
        <div>
          <div className="catchup-field-label">上传配置</div>
          <Select
            allowClear
            placeholder="不上传"
            style={{ width: '100%' }}
            value={profileId || undefined}
            options={options}
            onChange={(value) => onChange({ profileId: value || '', autoUpload: Boolean(autoUpload) })}
          />
        </div>
        <div>
          <div className="catchup-field-label">{uploadLabel}</div>
          <Switch
            checked={Boolean(autoUpload)}
            checkedChildren="开"
            unCheckedChildren="关"
            onChange={(checked) => onChange({ profileId: profileId || '', autoUpload: checked })}
          />
        </div>
      </div>
    )
  }

  return (
    <>
      <Form.Item label="上传配置" name="uploadProfileId">
        <Select allowClear placeholder="不上传" options={options} />
      </Form.Item>
      <Form.Item label={uploadLabel} name="autoUpload" valuePropName="checked">
        <Switch checkedChildren="开" unCheckedChildren="关" />
      </Form.Item>
    </>
  )
}
