# 元素句柄预览初始化：400 隔离结果

日期：2026-10-29。环境：Playwright 1.60.0、Chrome 156.0.8078.49。

## 结论

本轮已把稳定触发路径缩小到 Playwright 元素句柄的异步预览初始化
`ElementHandle._initializePreview()`。不是几何读取普遍有问题。

原版调用出现 400；仅在临时 driver 副本中关闭构造函数对该方法的调用，
保留原来的元素定位和 bounding_box 后，三组共 9 次全部成功；
切回未修改的原版，再做两组三轮，6 次全部再次出现相同 400。

这证明当前现场下该预览初始化链路是关键触发条件。该方法包含主世界
InjectedScript 初始化和 previewNode 调用；尚未区分这两个内部环节，
也没有证明站点具体使用哪条检测规则。

## 实验条件

用户确认可以使用软件的“重置刷新”按钮恢复现场，已明确授权清 Cookie。
本轮各个正式对照均使用相同接口 `POST /api/browser/reset`，
清 Cookie 并刷新后，等待验证码图片就绪；主动刷新一次并确认 HTTP 200，
再执行单项动作，最后共用同一套坐标点击方法刷新验证码检查结果。

没有填写凭据或提交登录。测试对象固定为登录页第二个 `span.title`（证书登录）。
所有请求均由真实页面产生，不重放签名或自行构造业务请求。

仅将“200 基线后出现 400 + text/html + 6 字节空白正文”计为复现。
图片未就绪、连接中断和重置失败都记为不可判定，停止当批实验，不计入 400。
每轮记录文档 loader_id。三轮仅说明此现场重复性，不代表所有站点与版本。

## 原版对照结果

| 动作 | 有效轮次 | 动作后验证码请求 |
| --- | --- | --- |
| 无动作对照 | 1（另有预检成功） | 200 |
| JS getBoundingClientRect | 1 | 200 |
| 原始 DOM 查询 | 1 | 200 |
| 原始 DOM.resolveNode | 1 | 200 |
| 原始 DOM.getBoxModel(nodeId) | 3 | 全部 200 |
| 原始 DOM.getBoxModel(objectId) | 3 | 全部 200 |
| 原始 Runtime.evaluate 返回元素，再 getBoxModel | 3 | 全部 200 |
| 原始隔离世界 resolveNode，再 getBoxModel | 3 | 全部 200 |
| Playwright ElementHandle.bounding_box | 3，另有补测 1 次 | 全部 400 |
| Playwright locator.bounding_box | 3 | 全部 400 |
| Playwright evaluate_handle 返回元素，再 bounding_box | 3 | 全部 400 |
| Playwright evaluate_handle 返回普通对象，等待 500ms | 3 | 全部 200 |
| Playwright evaluate_handle 返回元素，等待 500ms，不读几何 | 3 | 全部 400 |
| Playwright element_handle，等待 500ms，不读几何 | 3 | 全部 400 |

此前 isolate_results.json 中“只取句柄为 200”不能继续作为排除条件。
本轮不读几何、仅等待预览初始化完成，也能稳定复现；早期立即释放句柄的结果
可能受异步时序影响，尚未逐一复现当时的所有条件。

## 最小改动与恢复原版对照

从已安装 Playwright 复制独立 driver，只移除 ElementHandle 构造函数中的：

```js
this._initializePreview().catch((e) => {
});
```

不更改 boundingBox、DOM.getBoxModel、鼠标轨迹、站点脚本或已安装的依赖。

| 路径 | 原版 | 关闭预览初始化的临时副本 | 恢复原版复验 |
| --- | --- | --- | --- |
| ElementHandle.bounding_box | 3/3 为 400 | 3/3 为 200 | 原版首轮及协议补测可复现 |
| locator.bounding_box | 3/3 为 400 | 3/3 为 200 | 3/3 为 400 |
| evaluate_handle 返回元素，等待 500ms | 3/3 为 400 | 3/3 为 200 | 3/3 为 400 |

源码中 ElementHandle 构造函数会异步启动 `_initializePreview()`；该方法取得
句柄所在执行环境的 injectedScript，再调用 previewNode。协议摘要也观察到
主世界新增约 307KB 脚本求值。因此“只返回坐标”与“把元素返回成 ElementHandle”
具有不同副作用。不能把该现象简单归因于点击速度、验证码 OCR 或证书服务。

## 可复跑命令

探针：`tools/geometry_probe.py`。浏览器重启后需重新确认 CDP 端口。
本轮开始是 57179，重启后是 61764。下面命令要求仅有一个目标站点标签，
并已处于登录页；不会主动切走其它业务页面。

原版对照：

```sh
.venv/bin/python tools/geometry_probe.py \
  --endpoint http://127.0.0.1:61764 \
  --reset-url http://127.0.0.1:8800/api/browser/reset \
  --cases locatorbox evalhandle --repeat 3 --settle 5 --interval 2 --timeout 20 \
  --output build/diag/geometry_stock_recheck.jsonl
```

仅关闭预览初始化：

```sh
.venv/bin/python tools/geometry_probe.py \
  --endpoint http://127.0.0.1:61764 \
  --reset-url http://127.0.0.1:8800/api/browser/reset \
  --cases handlebox locatorbox evalhandle --repeat 3 --settle 5 --interval 2 --timeout 20 \
  --disable-element-preview \
  --output build/diag/geometry_no_preview_recheck.jsonl
```

新开关使用临时目录复制 driver，精确校验待改调用只出现一次，退出时清理。
它用于诊断对照，不是生产依赖补丁。输出记录 driver_variant；早期手动临时
副本实验的记录以独立文件 geometry_no_preview.jsonl 区分。

退出码：0 表示所有已测轮次为 200；1 表示观察到目标 400；
2 表示实验条件无效，停止；初始化错误也返回非零，须查看错误信息。

## 证据文件

原始实验产物位于忽略 Git 的 build/diag 目录：

- isolate_results.json：上一轮单动作结果。
- geometry_matrix.jsonl：本轮原版各路径结果，保留不可判定轮次。
- geometry_no_preview.jsonl：临时副本三组九次成功记录。
- geometry_stock_recheck.jsonl：恢复原版六次失败记录。
- geometry_preview_cli_check.jsonl：新诊断开关的现场校验。
- protocol_methods.jsonl：脱敏的协议方法、脚本长度和指纹，不保存凭据。
- preview_driver.json：早期独立 driver 副本位置和唯一修改说明。

开场曾有连接中断、TLS 失败和图片未就绪；恢复后才开始有效对照，
不据此断言网站维护，也不把这些轮次归因于几何操作。

## 隔离阶段的工程修复方向与边界

受保护路径应优先返回普通数据（坐标、文字、属性）并用鼠标/键盘输入，
避免返回 Playwright ElementHandle。仅仅换成 evaluate_handle 仍会触发该链路。

现有日期、验证码已有 JS 数据探针；普通 click 的准备路径仍调用滚动及
bounding_box，需要接入同类数据探针。还应移除异常时悄悄退回危险路径的行为，
并逐项核对 selector、滚动、遮挡、新标签和错误暂停语义。

以上描述的是隔离实验结束时的状态。后续业务修复及验证见下文。

## 本地验证

```sh
.venv/bin/pytest -q tests/integration/test_geometry_probe.py
.venv/bin/ruff check tools/geometry_probe.py tests/integration/test_geometry_probe.py
```

真实本地 Chromium 验证全部 18 条探针路径；另外验证基线失败停止、传输失败
与 400 分类、重置失败不继续、非零退出码、敏感地址脱敏、临时 driver 不改
已安装依赖且退出清理。真实站点结果与本地工具验证分别呈现。

## 已实施的业务修复（2026-10-29）

`tools/dom_probe.py` 统一返回坐标、可见性、文本和属性等普通数据；
支持五份 YAML 实际使用的文字选择器、嵌套 `:has` 和组合选择器。
不创建元素句柄、不改页面属性。默认 click、wait、fill、check、export、
验证码取图/输入和日期选择使用此路径，失败不再悄悄退回 locator 或元素截图。
鼠标点击前检查遮挡、禁用状态，并在 hover 后复测坐标。
保留显式 `low_profile_input: false` 作为旧路径诊断开关。
没有修改已安装的 Playwright SDK。

日期选择保留现有控件导航逻辑，修复前置 wait 的句柄创建与失败回退；
选不到日期现在明确失败。验证码请求异常会暂停，停止自动刷新和重试。
登录 YAML 的预刷新次数由 4 改成 0；诊断 YAML 使用 0 次预刷新、1 次尝试。
登录失败弹窗继续告警并暂停整个任务，不会标成完成。
等待期间才收到的 HTTP 错误也会进入原有暂停/恢复机制，
普通的无请求错误超时仍然失败，不会忽略后继续。

### 真实登录页验证

使用原版 SDK 调用改后的实际 WorkflowRunner，分别执行 click、wait、fill、
check、captcha，每类 3 轮。每轮先重置、确认验证码刷新 200，再执行动作，
最后再次刷新验证码。15/15 组前后均为 200，动作期间没有记录到 HTTP 错误。
fill 仅向验证码框输入测试字符，captcha 使用真实 OCR；没有提交登录。

证据：`build/diag/fixed_workflow_live.jsonl`、
`build/diag/fixed_workflow_summary.json`、`build/diag/fixed_workflow_live_run.log`。
这些是当前登录页的结果，不能替代登录后实时电价页面的现场验收。

### 本地回归验证

五份实际 YAML 均通过本地模拟站点执行；另覆盖三种日期控件。
CDP Debugger 监听主世界脚本编译，确认默认路径没有加载元素句柄预览所用的
InjectedScript。覆盖探测失败禁止回退、遮挡/禁用按钮、验证码 HTTP 错误暂停、
证书失败弹窗暂停、新标签、下载及原有的页面恢复。
模拟站点只能验证流程与兼容性，不能证明真实站点所有业务入口都不再出现 400。

最终相关测试集：458 passed、1 skipped（缺少现场验证码样本），耗时 250.20 秒。
命令：

```sh
.venv/bin/pytest -q tests/unit tests/test_shared_browser.py \
  tests/integration/test_workflow_data_only_input.py \
  tests/integration/test_workflow_runner_mocksite_e2e.py \
  tests/integration/test_geometry_probe.py --tb=short
```

日志：`build/diag/final_input_suite.log`。新增文件 lint、Python 编译及
`git diff --check` 通过；已有大文件没有引入新的 lint 问题。

修改后需要重启软件后端，运行中的 Python 进程不会自动替换已导入的执行器。
