# 400 定位：网关动态令牌拒绝，不是验证码输入问题

材料：`C:\Users\Administrator\Desktop\10281930` 下的两份 pickdate HAR、手动成功 HAR、验证码 400 HAR、`captcha单步400` 性能 trace，以及 `logs/app.log`、`logs/error.log`。时间均为北京时间。

## 一、结论

1. **这些 400 是边缘网关（瑞数动态令牌）返回的空 `text/html`，不是业务接口的错误。** 响应只有 `Connection / Content-Length: 6 / Content-Type: text/html / Date`，没有应用统一加的 `Cache-Control / Expires / Vary / X-Content-Type-Options`，业务代码根本收不到它。
2. **触发点在“程序化点击之后由页面自己发出的 XHR”**：菜单点击、日期点击、验证码刷新、提交前探测。同一台浏览器、同一个页面，人手点击同样的位置不会触发。所以问题不在验证码识别、也不在“一次性输入还是逐字输入”。
3. **令牌本身没有过期。** 同一文档里 200 与 400 的 `HdKGccbL` 解码后有一段 60 字节完全相同的会话段，只有每次请求的签名段不同；说明被拒的是“这一次请求的签名”，不是登录态或会话密钥。
4. **另有一条独立故障：Chrome 156 的本地网络访问检查拦截了证书控件。** 控制台报 `ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`，指向 `wss://127.0.0.1:10443`，随后 `cert/simpleRandom` 400、页面抛 `Unexpected end of JSON input`。这与验证码无关，是“数字证书连接失败”的直接原因。

## 二、证据

### 1. 400 的响应形态对不上业务错误

| 来源 | 状态 | Content-Type | 正文 | 响应头 |
| --- | --- | --- | --- | --- |
| `单步执行pickdate（实时节点电价查询）` 11:50:10 | 400 | `text/html` | 6 字节空白 | 只有 `Connection/Content-Length/Content-Type/Date` |
| 同 HAR 11:49:57 成功 | 200 | `application/json` | `{"code":0,...}` | 有 `Cache-Control/Expires/Vary/X-Content-Type-Options/X-XSS-Protection` |
| `验证码400.txt` 11:36:46 | 400 | `text/html` | 6 字节空白 | 同上，缺应用头 |

业务接口的 400 会带统一响应头并返回 JSON；这里没有，属于网关层直接拒绝。`app.log` 里所有 400 也都是 `xhr ct=text/html bytes=6`，与 `_is_gateway_form()` 的判据一致。

### 2. 同一文档内先成功后失败，且会话段不变

把 query 与同名 header 里的 `HdKGccbL` 做 base64url 解码：

- 11:49:57 的 200 与 11:50:10 的 400 共享同一段 60 字节（`80a961f0…d6856c3`），只有首 4 字节和尾部签名不同。
- 手动成功那份 HAR 里，三次 `getJddjTree` 共享另一段会话密钥，全部 200。

所以不是“令牌缺失、登录过期、cookie 被清”。真正被校验的是每次请求各自的签名载荷。

### 3. 400 总是紧跟在自动化点击之后

| 时间 | 点击动作 | 400 距点击 |
| --- | --- | --- |
| 15:44:29 填入验证码 → 15:44:34 刷新验证码 | 程序化填入 + 点击 | 约 5.1s |
| 19:41:40.444 点“实时节点电价查询” | `locator.click` | 0.18s |
| 19:45:13.751 点日历 27 | `locator.click` | 0.07s |
| 19:50:10.034 点日历 27 | `locator.click` | 0.11s |
| 16:45:55.699 点日历 27 | `locator.click` | 0.10s |

手动那份 HAR（19:53:41–19:53:47）在同样的菜单、同样的日期控件上连续三次 `getJddjTree` 全部 200。差别只有“谁点的”。

### 4. 程序化点击与人手点击在浏览器里是可区分的

`captcha单步400` trace 里：

- 自动化那一次点击：`pointerdown → pointerup` 间隔 **2.9 ms**，落点前只有一次次跳跃式移动。
- trace 末尾（人工接管）那一次：`MouseDown 117208150468 → MouseUp 117208282163`，按压 **131 ms**，前面是连续的 `MouseMove`。

这正是 `_settle_pointer()` 注释里写的现象：手点先移动、停住再按；`locator.click()` 是一步到位、按下即抬起。加 hover 和 0.45s 停顿只能补“移动”，补不了“按压时长 + 轨迹”。

### 5. 证书控件被 Chrome 156 拦掉

`login_diagnostics_20261028_084600.log`：

```
08:45:41 WS open wss://127.0.0.1:10443/
08:45:41 WebSocket connection ... failed: net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS
08:45:50 Failed to load resource: 400 (Bad Request)  × 12
```

`login_diagnostics_20261028_103209.log` 同样在 `WS open 127.0.0.1:10443` 之后出现 `cert/simpleRandom` 400 与 `Unexpected end of JSON input`。专用浏览器是普通方式启动的 Chrome 156，没有关掉这项特性。中国电子口岸 2026-04-15 的通知也说明 Chrome 147+ 需要把 `Local Network Access Checks` 设为 Disabled，否则卡介质/证书控件无法工作。

## 三、为什么“验证码一个一个敲”没有解决 400

400 不在输入路径上：填入验证码本身不发请求，发请求的是之后刷新验证码、证书探测或提交登录的那次 XHR。逐字输入只改变了输入事件，没有改变“程序化点击 → 页面立刻发 XHR”这个链路，也没有改变页面是否在前台、文档是否已经过期。所以现象当然不变。

## 四、本次代码改动

1. `src/spider_gd/core/crawler/shared_browser.py`
   - 专用浏览器启动参数增加 `--disable-features=CalculateNativeWinOcclusion,LocalNetworkAccessChecks`，以及 `--disable-backgrounding-occluded-windows`、`--disable-renderer-backgrounding`、`--disable-background-timer-throttling`。
   - 目的：证书控件能连本机 10443；页面被遮挡时定时器/签名心跳不被节流。
   - **只在下次启动专用浏览器时生效**，需要关掉再重开。
2. `tools/workflow_runner.py`
   - 新增 `_human_click()`：分 6–10 步移动（带抖动、先快后慢）→ 停 150–300ms → 按下 → 按住 70–140ms → 抬起，落点在元素中心 42%–58% 范围内随机。
   - 已用于：普通 `click`、日期面板/日期格子、快捷按钮、验证码刷新、点进输入框。
   - 点击前若 `document.visibilityState != "visible"` 会打警告，下一轮可直接判断页面是否在后台。
   - 新增工作流级开关 `human_input: false` 可退回原生 `locator.click`。
3. `tools/recon_tool/stealth.py`：同步本地网络访问检查参数。
4. `tests/unit/test_workflow_recovery_guards.py`：YAML 读取显式 `encoding='utf-8'`，修复 GBK 机器上的误报。

回归：`tests/unit/test_workflow_click.py`、`test_workflow_error_status.py`、`test_shared_browser.py` 全部通过。`test_api_fetcher.py` / `test_local_landing.py` / `test_workflow_runner_file_versions.py` 的失败是 curl_cffi Proactor 与文件版本环境问题，改动前就存在，与本次无关。

## 五、下一轮怎么验证

1. 关闭并重开专用 Chrome（参数只在启动时读）。
2. 登录页控制台不应再出现 `ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`；证书登录不应再报“数字证书连接失败”。
3. 跑一次实时节点电价，观察是否还有 `页面 visibilityState=hidden` 警告。
4. 若仍有 400，看它前面有没有 `visibilityState` 警告：
   - 有 → 页面在后台/被遮挡，把浏览器窗口保持可见，或用带抗节流参数的浏览器跑。
   - 没有 → 说明仍是点击签名问题，需要继续加大轨迹/按压随机性，或改走“页面内调用业务请求函数”的路径。

## 六、还没做、但建议做的

1. **同一域只保留一个业务文档**：跑之前关掉门户标签和旧现货标签。多个同域文档各自跑动态脚本，旧文档的请求签名会被判失效；`共享浏览器` 现在只警告不处理。
2. **400 后不要在原文档重试**：同一文档确诊会持续 400（19:45 失败后 19:47、19:51 仍失败）。当前“关新标签 → 从点现货交易重放”方向对，但重放前要等新文档自己的签名初始化请求跑完，且不要复用旧文档。
3. **不要用页面 URL 里的 `HdKGccbL` 直连 API**。它是每次请求现算的，页面 URL 上的是历史值；`api_fetcher` 的 `hdkgccbbl_source: page_url` 只适合 mock/无防护站点，真实接口必须让页面自己发请求。
4. **绑定“最右侧/最新文档”**：用户已经观察到标签跟随会滞后。自动化开始前把绑定页切到最新打开的业务文档，能避开大部分“旧文档签名失效”。

## 七、第二轮（20:20–20:35）的新发现

### 1. 上一轮的代码根本没有跑起来（最重要）

`logs/app.log` 里 20:25–20:31 的调用仍是：

```
workflow_runner:_do_click:1509
workflow_runner:_apply_date_pick:1833
workflow_runner:_click_element_day:1754
```

而改过的文件里这三行在 **1576 / 1900 / 1821**。20:15:57 那段启动日志是一个新实例，但它没能接管 8800（旧实例 19:33:36 启动的两个进程仍在监听），新实例随后退出，服务一直是旧代码。所以：

- 20:25、20:27、20:31 的 400 仍然来自“原生 `locator.click`”，不是人形点击；
- 20:31 那份 pickdate trace 里，两次点击的按下到抬起分别是 **9.3ms 和 1.2ms**，落点前只有一次 `mousemove`——确认是旧路径；
- 结论：人形点击到底能不能解决 400，这一轮**还没有被验证**。

已加端口预检：占不到端口时 `app.log` 会明确写“已有后端实例在运行，本次启动不会生效”，不再假装启动成功。

### 2. 验证码“全屏闪一下”是元素截图，不是验证码识别

`captcha` 步骤实际做五件事：

1. `pre_refresh` 次点击验证码图片换新码（本轮日志没有“预刷新”行，说明这几次是 0 次）；
2. 轮询 `img.src` 直到 400ms 不变；
3. `locator.screenshot()` 对 `<img>` 做**元素截图**；
4. ddddocr 识别；
5. 点进输入框，Ctrl+A、Backspace，再逐字输入。

第 3 步是唯一“只画验证码”的动作：Playwright 会先把 `<img>` 滚进视口，再让 Chrome 做一次合成捕获，登录页因此闪一帧放大的验证码。它不发 XHR，不改 cookie，和 400 没有因果关系（pickdate 的 400 完全没经过验证码）。

已改成直接读 `src` 的 `data:image/png;base64,...` 解码，画面完全不动；非 data URL 才回退截图。

### 3. 本轮新增的处置

| 建议 | 处理 |
| --- | --- |
| 用最右侧（最新）业务页 | `follow_active_page()` 在浏览器处于后台（所有页 `hasFocus=false`）时，跟随 `target_origin` 下最新的页；`follow_newest_tab: false` 可关 |
| 400 后不在原文档重试 | 网关 400 的文档加入 `_poisoned_pages`；之后在这份文档上再点/选日期/导出/填验证码会先暂停，要求换新文档 |
| 不要用页面 URL 的 HdKGccbL | `ApiFetcher` 遇到 `hdkgccbl_source: page_url` 会告警说明该值是一次性签名，直连必被网关拒绝 |
| 12 次 400 / 端口占用排查 | 后端启动前预检端口，占用时写 ERROR 并退出 |
| 顺带修复 | `curl_cffi` 0.17 不再支持 `chrome130`，`ApiFetcher` 构造即抛 `ImpersonateError`；改为按可用版本选 Chrome 指纹。文件版本清单里的相对路径改存 POSIX 分隔符 |

回归：`tests/unit` + `tests/test_shared_browser.py` **419 passed**。

### 4. 下一轮才算真正验证

1. 完全退出应用（Electron 与后端都要退），确认 `127.0.0.1:8800` 没有监听，再重新打开；
2. 关掉并重开专用 Chrome；
3. 跑一次实时节点电价，重点看：
   - 日志里是否出现 `visibilityState=hidden`；
   - 400 是否还在，若在，按新的 `[visible/focus]` 标记判断页面状态；
   - 若 400 只出现在某一份文档上，换新文档后是否恢复。

## 八、第三轮（20:53–21:02，新代码已生效）

这一轮的日志行号（`_do_click:1598`、`_do_captcha:2487`）已经是新代码，所以结论有效。

### 1. 先排除一个方向：不是“点击不像人”

`captcha_step` trace 里，导致 400 的那一次刷新点击是：

```
pointerdown  -132ms
pointerup      -8ms   → 按住 124ms
```

前面还有连续 16ms 间隔的 `mousemove`，和手动基线里三次 225ms 的点击已经很接近。**这次 400 说明这份文档在点击之前就已经坏了**，不能反过来当成“点击导致 400”的证据。人形点击这条线到此为止，但“点击被识别”也不能再当结论。

### 2. 400 是“文档在填表时被标记”，之后谁点都拒

时间线（同一份登录文档，没有刷新）：

| 时间 | 事件 |
| --- | --- |
| 20:53:44 | `page.fill` 写入证书密码（**零按键事件**） |
| 20:53:48–50 | 识别验证码并填入（Ctrl+A、Backspace、4 个字符，间隔约 95ms 等距） |
| 20:54:48–49 | 第二次识别并填入 |
| 20:54:53 | **人手点击刷新验证码 → 400**（6 字节 text/html，和之前完全一样） |

之后 20:59 起，门户自己的 `sso-auth/oauth2/authorize` 也开始 400，点“现货交易”打不开新标签。这说明被拒的不是某一个接口，而是**这份文档的动态签名整体失效**。

### 3. 与手动成功基线的差异只剩输入行为

手动登录成功那份 trace 12:58:07–12:58:19：

| 维度 | 手动（成功） | 自动化（400） |
| --- | --- | --- |
| 验证码按键间隔 | 77、79、228、105、236、132、214、2313、1029ms | 约 95ms 等距 |
| 按键按住时长 | 96–140ms | 89–95ms |
| 全选删除 | 无 | 先 `Ctrl+A` + `Backspace` |
| 证书密码 | 逐键输入（有 keydown/keyup） | `page.fill`，**零按键** |

手动那次从验证码到登录用了 11 秒，自动化 9 秒，时间不是差异；差异是**输入事件本身**：一个字段完全没有键盘事件、另一个字段等间距输入还带全选，这正是行为风控要抓的特征。站点在填表时把文档标记掉，后续任何请求（包括人手点击）都返回 400。

### 4. 本轮改动

- `_do_fill`：证书密码改为真实按键输入，不再 `page.fill`。
- `_type_text`：只有输入框里已有内容（或读不到内容）才 `Ctrl+A`+`Backspace`；逐字输入改为随机按住 40–130ms、随机间隔（均值 90–150ms、偶尔停顿 150–450ms），不再等距 70ms。
- 键盘 API 不可用时回退原路径，测试替身不受影响。
- `tests/unit` + `tests/test_shared_browser.py` **419 passed**。

### 5. 如果这一版还 400，用 A/B 定位最后一个信号

准备两个手动回合，跑完 captcha 后手动点一次刷新验证码，看是否 400：

- A：**手动输入**证书密码 + 自动识别填入验证码；
- B：自动填入证书密码 + **手动输入**验证码。

哪个回合之后刷新 400，就说明是那一段输入仍然被识别；两个都不 400，则问题已解决。这个实验比继续猜接口有效。


