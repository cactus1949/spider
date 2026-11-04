# 本机微信发送 PoC

## 目标

验证系统能否在任务完成后，通过本机已登录的微信客户端发送消息或 zip 文件。

生产稳定方案走企业微信群机器人、Server酱或 PushPlus：任务结束且产生下载文件后，系统自动压缩并发送文件或下载链接。本机微信桌面自动化仍保留为 PoC/兜底验证，不作为稳定链路。

## 稳定方案：企业微信群机器人

推荐在应用“设置 → 结果发送”中配置：

- 启用任务产物发送
- 发送通道：企业微信群机器人 / Server酱（个人微信链接） / PushPlus（个人微信链接） / 本机微信 PoC（Windows 实验）
- 企业微信群机器人 Webhook，或 Server酱 SendKey，或 PushPlus Token
- 文件发送方式：自动 / 企业微信文件直发 / 远程上传后发链接
- 远程上传地址和 Token 可以留空，默认复用“录制工坊”的远程上传配置
- 重试次数和重试间隔可按现场网络稳定性调整
- 填好后可直接点击“检查发送计划”或“真实发送测试”验证链路
- “补发队列”会显示待补发/失败/已发送数量，可刷新、恢复失败、恢复并补发或清理历史

也可以在配置文件中开启 `delivery`：

```yaml
delivery:
  enabled: true
  env_file: "/c/secure/spider-gd-delivery.env" # 推荐：Webhook/Token 放这里，不写进 YAML
  backend: wecom_webhook          # wecom_webhook / serverchan / pushplus / desktop_wechat
  wecom_webhook: ""               # 可留空，由 env_file 提供 SPIDER_GD_WECOM_WEBHOOK
  serverchan_send_key: ""
  serverchan_channel: ""
  serverchan_openid: ""
  pushplus_token: ""
  pushplus_channel: ""
  desktop_wechat_target: ""       # backend=desktop_wechat 时必填，如 文件传输助手
  desktop_wechat_backend: powershell
  desktop_wechat_select_mode: current
  desktop_wechat_profile_path: "./data/wechat_targets.json"
  desktop_wechat_app_name: ""
  desktop_wechat_pause_sec: 0.7
  desktop_wechat_hash_threshold: 12
  desktop_wechat_allow_unstable_search: false
  send_message: true
  send_file: true
  file_delivery: auto
  wecom_file_max_mb: 20
  remote_upload_url: ""          # 可留空，自动读取设置里的 workshopUploadUrl
  remote_upload_token: ""        # 可留空，自动读取设置里的 workshopUploadToken
  remote_upload_relative_path: "/publicData"
  remote_upload_verify_url: true # 上传后验证下载链接可访问
  retry_attempts: 2
  retry_delay_sec: 0.5
  title: "spider-gd 采集完成"
```

离线采集链路生成 zip 后，会调用稳定发送：

1. 发送一条 markdown 完成消息，包含任务 ID、成功数、失败数和压缩包名。
2. 小 zip 直接上传到企业微信机器人临时素材接口，再发送文件消息到群里。
3. 大 zip 使用远程上传地址 + token 上传，再把下载链接发到企业微信群。

`file_delivery: auto` 时，小于等于 `wecom_file_max_mb` 的 zip 会直接作为企业微信文件发送；超过该大小会使用远程上传地址 + token 上传 zip，然后把下载链接发到企业微信群。也可以把 `file_delivery` 固定为 `remote_link`，始终上传后发链接。

如果已配置远程上传地址，小 zip 直发企业微信文件失败时会自动降级：先把 zip 上传到外网服务器，再把下载链接发送到微信/企微消息里。现场如果追求更稳，建议保留远程上传配置作为兜底，或者直接使用 `file_delivery: remote_link`。

远程链接默认会在上传成功后做一次轻量验证：先尝试 `HEAD`，不支持时再用 `Range: bytes=0-0` 的 `GET` 只取响应头。outbox 补发复用旧 `remote_url` 前也会重新验证。验证失败时不会把坏链接发到微信里，会留在 outbox 等待修复后补发；如果现场服务器不支持这种验证，可关闭 `remote_upload_verify_url`。

发送成功后，outbox 会记录 `delivery_note`，前端补发队列里会显示“送达方式”：能区分最终是企业微信文件直发、远程下载链接，还是只发送了完成消息。

如果需要进入个人微信再转发，建议把 `backend` 设为 `serverchan` 或 `pushplus`。这两种通道不做文件直发，会把 zip 上传到外网服务器后，把下载链接推送到个人微信通知里，稳定性比控制本机微信窗口高很多。

如果现场必须验证控制本机 Windows 微信，可在“设置 → 结果发送”里把发送通道切到“本机微信 PoC（Windows 实验）”，填写“微信目标会话”，默认使用 `PowerShell UIA + 校验当前会话`。这条链路会被结果发送和异常告警共用：任务失败、登录失败等告警也会发到同一个微信目标。`file_delivery=auto` 或 `wecom_file` 时会直接把本地文件发给微信；`file_delivery=remote_link` 时会先上传再发送下载链接。

PoC 真实发送要求 Windows 值守机微信已登录、桌面未锁屏，并且当前会话或绑定会话能被校验。macOS/Linux 上只适合做 dry-run 计划检查，真实发送会被安全策略拒绝。

也可以不改配置，直接用命令行临时传 PushPlus 或 Server酱凭据。PushPlus 示例：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-test \
  --sample-zip \
  --pushplus-token "your_pushplus_token" \
  --remote-upload-url "https://your-public-server.example.com" \
  --send \
  --download-remote
```

Server酱示例：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-test \
  --sample-zip \
  --serverchan-send-key "SCTxxxxxxxx" \
  --remote-upload-url "https://your-public-server.example.com" \
  --send \
  --download-remote
```

前端/定时任务使用的 `TaskRunner` 路径也已接入同一套稳定发送：任务运行前会记录下载目录快照，任务结束后只要有新增或变更下载文件，就打包成 zip，然后走企业微信/远程链接发送。`storage_state.json`、已有 zip、失败截图目录和内部 `_stable_delivery` 目录不会被打进产物包。

旧采集编排器路径也按“有成功产物就发送”处理：任务全部成功时发送完整 zip，任务部分失败但已经下载了文件时，也会压缩已成功产物并进入稳定发送，回执和消息摘要里会保留 `success` / `failed` 数量，方便客户知道这次包是部分成功结果。

稳定发送会在真正发送时读取最新设置；如果程序启动时没启用，后面在设置页打开或修改 Webhook/Token，后续任务发送和 outbox 补发都会使用新配置，不需要重启。

无人值守部署时，推荐在 `delivery.env_file` 中配置固定凭据文件路径，或设置进程环境变量 `SPIDER_GD_DELIVERY_ENV_FILE=/c/secure/spider-gd-delivery.env`。这样定时任务、任务完成自动发送和启动后的 outbox 自动补发都能读取同一份 Webhook/Token，不需要把密钥写进 YAML 或命令历史。

如果在设置页填写了“凭据文件路径”，对应的 Webhook/Token 输入框可以留空；空输入不会遮蔽凭据文件里的 `SPIDER_GD_WECOM_WEBHOOK`、`SPIDER_GD_PUSHPLUS_TOKEN` 或 `SPIDER_GD_SERVERCHAN_SEND_KEY`。

outbox 的幂等判断会包含发送通道和目标指纹；更换 Webhook、Token、通道或文件发送方式后，同一个 zip 不会被旧目标的“已发送”状态误跳过。历史记录缺少目标指纹时会按未知目标处理，保守重新发送。

企业微信消息、企业微信文件上传、远程上传都会使用 `retry_attempts` 和 `retry_delay_sec` 做有限重试。现场网络偶发抖动时，建议先把 `retry_attempts` 调到 3 或 4，间隔保持 0.5 到 1 秒。

如果重试耗尽后仍然失败，系统会把任务 ID、zip 路径和汇总信息写入 SQLite 的 `delivery_outbox` 待补发队列。主程序启动时会先补发一次；调度器开启时，每 5 分钟自动补发一次。也可以手动执行：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-retry \
  --env-file "/c/secure/spider-gd-delivery.env"
```

查看队列明细：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-outbox --status pending
spider-gd --config configs/mode_a_wired.yaml delivery-outbox --status failed
spider-gd --config configs/mode_a_wired.yaml delivery-outbox --status all
```

补发结果中的 `blocked` 表示本地 zip 已不存在且没有可复用的远程链接，系统会把这类记录转为 `failed`，命令退出码为 1，避免误以为已经发送成功。

如果前端看到 `发送中` 长时间不变化，点击“检查发送中”或运行 `delivery-retry`；超过 10 分钟的发送中断记录会自动恢复为待补发并重试。

启动补发和每 5 分钟调度补发也会自动恢复可继续发送的 `failed` 记录：只要本地 zip 仍存在，或 outbox 里已有可复用的 `remote_url`，就会重新放回待补发并尝试发送。若本地 zip 已丢失且没有远程链接，记录会继续保持 `failed`，避免无人值守时反复无效重试。

如果记录已经进入 `failed`，例如之前文件缺失、配置错误或外网服务长时间不可用，修复原因后可先恢复失败记录再补发：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-retry \
  --env-file "/c/secure/spider-gd-delivery.env" \
  --include-failed
```

也可以只恢复 failed 记录、不立即发送：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-outbox --requeue-failed
```

清理历史记录：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-outbox --cleanup sent
spider-gd --config configs/mode_a_wired.yaml delivery-outbox --cleanup failed
```

控制接口也提供：

- `GET /api/delivery/outbox`：查看待补发/失败/已发送记录
- `POST /api/delivery/retry`：立即补发 pending 队列；传 `includeFailed=true` 时先恢复 failed 再补发
- `POST /api/delivery/outbox/requeue`：把 failed 记录恢复为 pending

先做只读体检：

```bash
conda activate spider-gd
spider-gd --config configs/mode_a_wired.yaml delivery-doctor \
  --file "/path/to/result.zip"
```

如果还没有写入配置，也可以临时传凭据做只读体检，例如 PushPlus + 远程链接：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-doctor \
  --file "/path/to/result.zip" \
  --pushplus-token "your_pushplus_token" \
  --remote-upload-url "https://your-public-server.example.com"
```

`delivery-doctor` 只读检查配置、预计发送策略和 outbox 状态，不会真实发送。如果输出里有 `pending` 或 `failed`，按 `recommendations` 里的命令补发；前端设置页的“恢复并补发”会先恢复 failed 再发送。

现场推荐直接做一键验收。未传 `--file` 时会自动生成一个样例 zip，每次生成新的验收任务 ID，避免被历史“已发送”记录跳过；加 `--require-remote-url` 时，命令会自动切到远程链接模式，要求本次必须产出外网下载链接，并下载远端 zip 比对大小和 SHA256：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-acceptance \
  --env-file "/c/secure/spider-gd-delivery.env" \
  --require-remote-url
```

验收成功时看 `ok: true`、`sent: true`、`outbox.status: sent`、`outbox.remote_url`、`outbox.forwarding.ready` 和 `verification.ok: true`。命令输出会包含 `receipt_path`，回执里记录本次 zip 的路径、大小、SHA256、远程链接、送达方式、可转发摘要和完整核验结果，可直接作为现场验收报告。命令行临时传入的 Webhook/Token 只用于发送，JSON 输出和回执里都会脱敏。

`delivery-test`、`delivery-acceptance`、`delivery-send`、`delivery-doctor`、`delivery-retry` 和 `wechat-poc` 都支持用 `--env-file` 或环境变量代替命令行凭据，避免 Webhook/Token 进入 shell history。env 文件内容示例：

```bash
SPIDER_GD_WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
SPIDER_GD_REMOTE_UPLOAD_URL="https://your-public-server.example.com"
SPIDER_GD_REMOTE_UPLOAD_TOKEN="your_upload_token"
```

支持的 key 包括：`SPIDER_GD_DELIVERY_BACKEND`、`SPIDER_GD_WECOM_WEBHOOK`、`SPIDER_GD_SERVERCHAN_SEND_KEY`、`SPIDER_GD_SERVERCHAN_CHANNEL`、`SPIDER_GD_SERVERCHAN_OPENID`、`SPIDER_GD_PUSHPLUS_TOKEN`、`SPIDER_GD_PUSHPLUS_CHANNEL`、`SPIDER_GD_DESKTOP_WECHAT_TARGET`、`SPIDER_GD_DESKTOP_WECHAT_BACKEND`、`SPIDER_GD_DESKTOP_WECHAT_SELECT_MODE`、`SPIDER_GD_DESKTOP_WECHAT_PROFILE_PATH`、`SPIDER_GD_DESKTOP_WECHAT_APP_NAME`、`SPIDER_GD_DESKTOP_WECHAT_PAUSE_SEC`、`SPIDER_GD_DESKTOP_WECHAT_HASH_THRESHOLD`、`SPIDER_GD_DESKTOP_WECHAT_ALLOW_UNSTABLE_SEARCH`、`SPIDER_GD_FILE_DELIVERY`、`SPIDER_GD_REMOTE_UPLOAD_URL`、`SPIDER_GD_REMOTE_UPLOAD_TOKEN`、`SPIDER_GD_REMOTE_UPLOAD_RELATIVE_PATH`。显式命令行参数优先于 `--env-file`，`--env-file` 优先于系统环境变量。不要把这类 env 文件提交到 git。

如果 `--env-file` 路径不存在、不可读或不是 UTF-8 文本，命令会返回结构化 JSON 错误和退出码 1，不会继续尝试桌面微信发送。Git Bash 中建议使用 `/c/secure/spider-gd-delivery.env` 这类路径写法。

如果要验收个人微信通知链路，用 Server酱或 PushPlus 加远程链接：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-acceptance \
  --env-file "/c/secure/spider-gd-delivery.env" \
  --require-remote-url
```

PushPlus 场景下，env 文件里把 `SPIDER_GD_WECOM_WEBHOOK` 换成 `SPIDER_GD_PUSHPLUS_TOKEN`；Server酱场景下换成 `SPIDER_GD_SERVERCHAN_SEND_KEY`。

再做 dry-run 发送计划检查：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-test \
  --file "/path/to/result.zip"
```

上面命令默认 dry-run，只检查配置和打印会走“企业微信文件直发”还是“远程上传后发送下载链接”。如果企业微信文件直发没有远程上传兜底，输出会带 `warnings` 提醒。确认无误后再真实发送：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-test \
  --file "/path/to/result.zip" \
  --send
```

如果需要使用底层测试命令，也可以直接生成一个样例 zip 做真实链路测试：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-test \
  --sample-zip \
  --wecom-webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx" \
  --force-remote-link \
  --remote-upload-url "https://your-public-server.example.com" \
  --send \
  --download-remote
```

样例文件会写到数据目录的 `_stable_delivery/samples/delivery_sample_*.zip`，同样会进入 `delivery_outbox` 并返回大小与 SHA256。命令输出会包含 `receipt_path`。上面命令不要求先修改 YAML，会临时使用传入的 Webhook 和远程上传地址，并下载远端 zip 比对 SHA256。前端设置页“发送测试”在测试文件路径为空时也会使用样例 zip 逻辑，并在发送后自动核验本地 zip 和远程链接。

带文件的真实发送测试也会先写入 `delivery_outbox`。如果附件或远程链接已经送达、后续说明消息失败，补发时会复用已完成进度，避免重复发送 zip。

正式人工发送某个任务 zip 时，使用 `delivery-send`，不要长期借用测试命令：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-send \
  --file "/path/to/result.zip" \
  --task-id "TASK_20260630_001" \
  --verify
```

`--task-id` 可以不传；系统会按文件路径生成稳定 ID。同一路径重复执行时，如果该 zip 已经按当前 Webhook/Token 和发送方式送达，会直接返回已有回执，不会重复上传或重复发送附件；如果同一路径文件内容变了，会重新发送新的 zip。

`delivery-send` 成功或失败后会输出并落盘 `receipt_path`。任务自动发送和 `delivery-retry` 补发也会写同一类回执。默认位置是数据目录的 `_stable_delivery/receipts/<task_id>.json`，里面包含 `outbox.status`、`delivery_note`、`remote_url`、`zip_size`、`zip_sha256` 和发送汇总，便于现场留档或排查“到底发的是哪个 zip”。加 `--verify` 时，输出里还会包含 `verification`，命令会立即核验 outbox 状态、本地 zip 大小和 SHA256，以及远程链接可访问性。

发送后可做一次回执核验：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-verify \
  --task-id "TASK_20260630_001"
```

或直接核验回执文件：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-verify \
  --receipt "/path/to/_stable_delivery/receipts/TASK_20260630_001.json"
```

`delivery-verify` 会检查 `outbox.status`、本地 zip 大小和 SHA256；如果回执里有 `remote_url`，还会验证下载链接可访问。只想离线核验本地文件时可加 `--no-remote`。

如果要确认远程链接下载到的文件也和本次 zip 完全一致，可做深度核验：

```bash
spider-gd --config configs/mode_a_wired.yaml delivery-send \
  --file "/path/to/result.zip" \
  --task-id "TASK_20260630_001" \
  --force-remote-link \
  --remote-upload-url "https://your-public-server.example.com" \
  --download-remote
```

`--force-remote-link` 会让本次命令强制远程上传后发送下载链接，不需要改 YAML；也可以用 `--file-delivery remote_link` 达到同样效果。`--download-remote` 会自动开启发送后核验，下载 `remote_url` 并比对远程文件大小和 SHA256，默认最多下载 200MB；需要调整上限时加 `--max-remote-mb 500`。它要求回执里必须有 `remote_url`；如果本次走的是企业微信文件直发、没有远程链接，命令会失败并提示没有可下载的远端文件。需要强制可下载核验时，使用 `--force-remote-link` 或把 `file_delivery` 设为 `remote_link`。如果只想对已有回执做离线复查，也可以继续使用 `delivery-verify --receipt ... --download-remote`。

前端设置页的“补发队列”每条记录也提供“核验”按钮，会调用同一套逻辑检查 outbox、本地 zip 和远程链接。

`delivery-acceptance`、`delivery-test --send` 和 `delivery-send` 的 JSON 输出都会包含 `task_id` 和 `outbox` 摘要。现场验收重点看：

- `sent: true`
- `outbox.status: sent`
- `outbox.delivery_note`：例如“企业微信群机器人；远程下载链接；完成消息”
- `outbox.remote_url`：走远程链接或兜底时会返回下载地址
- `outbox.zip_size` / `outbox.zip_sha256`：用于核对转发出去的链接下载到的就是本次生成的 zip
- `outbox.forwarding`：给现场操作员看的可转发摘要；`ready=true` 表示已经送达，`can_forward_link=true` 时直接转发 `remote_url`，否则按 `instruction` 转发企微附件或处理错误

`delivery-test --send --file` 默认会为每次测试生成新的任务 ID，方便连续做真实收件验收；如果需要复用同一条 outbox 记录，可显式传 `--task-id YOUR_ID`。

也可以临时传入某个企业微信机器人 webhook。`wechat-poc --backend auto` 只使用稳定发送配置；只要配置文件、前端设置、环境变量或 `--env-file` 里有可用的企业微信/Server酱/PushPlus 凭据，就直接走稳定通道，不控制本机微信窗口，也不使用搜索结果。只发文本时这是轻量稳定推送；带 `--file` 时会自动走可靠 outbox，写入回执，并按配置里的远程上传地址做大文件或失败兜底。推荐用 env 文件传 Webhook/Token，避免进入命令历史：

```bash
spider-gd wechat-poc \
  --env-file "/c/secure/spider-gd-delivery.env" \
  --target "文件传输助手" \
  --message "spider-gd 发送测试" \
  --file "/path/to/result.zip" \
  --verify \
  --send
```

如果要指定回执文件或稳定任务 ID：

```bash
spider-gd wechat-poc \
  --env-file "/c/secure/spider-gd-delivery.env" \
  --message "任务完成，见附件" \
  --file "/path/to/result.zip" \
  --task-id "WECHAT_SEND_001" \
  --receipt "/path/to/receipt.json" \
  --force-remote-link \
  --download-remote \
  --send
```

如果没有稳定发送配置，`wechat-poc --backend auto` 会直接失败并提示配置 Webhook/Token，不会退回本机桌面 PoC。要强制走桌面微信，请显式指定 `--backend powershell` 或 `--backend wxauto`。

## 本机微信 PoC

这是桌面 UI 自动化，不是微信官方 API。它依赖真实桌面会话、微信窗口、剪贴板和键盘事件，只适合 PoC 或临时兜底。

macOS 脚本只能通过搜索式 UI 操作选择会话，真实发送默认禁止；如需稳定链路，请优先使用上面的企业微信/Server酱/PushPlus 通道。Windows 才支持 `bound` / `current` 这类带会话校验的桌面 PoC。

## 使用前准备

1. 打开本机微信客户端，并确认已经登录。
2. 准备一个测试会话，建议先用“文件传输助手”或测试群。
3. 保持桌面未锁屏，运行期间不要操作鼠标键盘。

## Dry-run

默认不会真实操作微信，只打印计划：

```bash
python -m spider_gd wechat-poc --backend powershell --target "文件传输助手" --message "spider-gd 微信发送 PoC"
```

发送文件的 dry-run：

```bash
python -m spider_gd wechat-poc --backend powershell --target "文件传输助手" --file "/path/to/result.zip"
```

## 真实发送

桌面微信真实发送不要依赖搜索结果位置。微信搜索会按账号、版本、网络状态展示“网络结果/搜一搜/功能/联系人”等分组，搜索式自动点击即使做了过滤，也不能作为稳定链路。

Windows 上真实发送默认只允许两种更稳的方式：

- `bound`：先绑定固定会话截图指纹，后续发送前复核指纹，不匹配就停止。
- `current`：先手动打开目标会话，命令只校验当前主聊天区是目标，再发送。

Git Bash + conda 环境示例：

```bash
conda activate spider-gd
spider-gd wechat-poc \
  --backend powershell \
  --target "文件传输助手" \
  --bind
spider-gd wechat-poc \
  --backend powershell \
  --select-mode bound \
  --target "文件传输助手" \
  --message "hello" \
  --send
```

如果不想绑定，也可以先手动点开“文件传输助手”，再用 `current` 模式发送 zip 文件：

```bash
conda activate spider-gd
spider-gd wechat-poc \
  --backend powershell \
  --select-mode current \
  --target "文件传输助手" \
  --message "任务完成，见附件" \
  --file "/path/to/result.zip" \
  --send
```

`wxauto` / `wxautox4` 仍可用于测试，但它们内部可能走搜索并回车；当微信搜索结果第一块是“网络结果”时，可能会选错。先安装其中一个库：

```bash
pip install wxauto
```

或按 `wxautox4` 官方文档安装并完成授权。确认 dry-run 输出的目标和文件都正确后，加 `--send`：

```bash
python -m spider_gd wechat-poc \
  --target "文件传输助手" \
  --message "spider-gd 微信发送 PoC" \
  --file "/path/to/result.zip" \
  --backend wxauto \
  --send
```

`--backend auto` 不再控制本机微信，也不会打开搜索框；它只尝试稳定通道。需要桌面微信 PoC 时，必须显式传 `--backend powershell` 或 `--backend wxauto`。

## 固定会话绑定模式

如果现场不能使用 `wxauto`，但只需要几个固定微信号/固定会话传输文件，推荐绑定模式：

1. 手动打开微信，切到目标会话，例如“文件传输助手”。
2. 运行绑定命令，记录当前窗口标题区域截图指纹和输入框位置：

```bash
python -m spider_gd wechat-poc --target "文件传输助手" --bind --backend powershell
```

3. 后续发送使用 `bound` 模式。发送前会重新比对标题区域指纹，不匹配就停止，避免误发：

```bash
python -m spider_gd wechat-poc \
  --target "文件传输助手" \
  --message "hello" \
  --select-mode bound \
  --backend powershell \
  --send
```

如果微信窗口大小、字体缩放、主题发生变化，需要重新运行 `--bind`。如果误判过严，可以适当调大 `--hash-threshold`，例如 `--hash-threshold 20`。

Windows 默认使用 `--select-mode current`：不打开搜索框、不输入目标、不回车，只校验当前主聊天区能看到目标名，校验通过才发送。`--select-mode exact` 仍可用于 dry-run 或临时 PoC：它会通过 UI Automation 在微信窗口或搜索结果中精确寻找目标。搜索结果存在“网络结果/搜一搜”和“功能”分组时，会跳过网络结果，优先点击“功能”分组下的目标项，并尽量点击整行结果而不是文字子控件；找不到可信分组就停止，不会输入搜索词后按回车选择第一条。由于现场 UI 不固定，真实发送默认会拒绝 `exact` 和 `legacy-search`，要求改用 `bound` / `current`。

最稳的保守模式是 `current`：先手动打开目标会话，命令只校验当前微信窗口能看到目标名，再发送：

```bash
python -m spider_gd wechat-poc --target "文件传输助手" --message "hello" --backend powershell --send
```

`legacy-search` 只保留为兼容旧命令参数。新版实现不再按方向键后回车，因为微信搜索第一块常常是“网络结果/搜一搜”；它会和 `exact` 一样只在“功能/联系人/群聊/聊天记录/公众号/小程序”等可信分组里选择，找不到就停止。`--search-down` 现在会被忽略，不再用于真实选择。

```bash
python -m spider_gd wechat-poc --target "文件传输助手" --message "hello" --backend powershell --select-mode legacy-search --search-down 3 --allow-unstable-search --send
```

同理，如果确实要把 `exact` 搜索式选择用于临时 PoC，也必须显式加 `--allow-unstable-search`。不加这个参数时，程序会在发送前退出，避免误发。

如果微信客户端名称不是默认的 `WeChat` / `微信`，可以指定：

```bash
python -m spider_gd wechat-poc --target "文件传输助手" --message "hello" --backend powershell --select-mode current --app-name "WeChat" --send
```

## 验收建议

1. 稳定链路先跑 `delivery-acceptance --require-remote-url`，确认 `ok: true`、`verification.ok: true`，并人工在微信/企微消息里打开链接下载一次。
2. 再用真实任务产物跑 `delivery-send --file ... --force-remote-link --download-remote`，确认回执里的 `zip_sha256` 和远端下载核验一致。
3. 将网络断开或让上传服务临时失败一次，确认任务进入 `delivery_outbox`，修复后 `delivery-retry --include-failed` 能补发。
4. 桌面微信自动化只作为 PoC：如仍需演示，再测试窗口最小化、未最小化、当前焦点在别的程序三种状态。最终稳定交付以 Webhook/远程链接回执为准。

## 风险

- 微信更新、窗口焦点变化、桌面锁屏都会影响发送。
- 发送个人微信消息没有官方稳定接口，不建议高频或大规模群发。
- 涉及客户数据时，建议 zip 加密，或只发送远程下载链接。
