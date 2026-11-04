# 浏览器会话与 400 恢复审查

审查日期：2026-10-27。
范围：`9125189`、`7012329`、`9b543a7`，以及当前已暂存未提交的 6 个文件。
本次只审查与复现，没有修改业务源码、SQLite 或运行中的浏览器配置，没有访问真实业务站点执行登录、改日期或导出。

## 结论

尚未完善，不能认定真实站点的登录失败和日期请求 400 已解决。

本次未提交修改确实移除了恢复函数的 reload 退路，移除了半登录后的自动刷新，增加了同域旧标签暂停，以及“关本次新标签再进入”的模拟站点测试。这些方向正确。

但“错误必定暂停”“暂停后不能误放行”“界面运行的是已修订流程”三项仍不成立。当前版本不适合据此宣称无人值守采集已经可靠。

## 高优先级问题

### 1. 重试额度耗尽后继续执行，甚至返回成功

位置：`tools/workflow_runner.py:768`、`:781`、`:363`。

每检查点重放一次后再次出现错误，代码 acknowledge 后返回 False；总重试次数达到上限则直接返回 False。调用者将 False 理解为“不需要重放”，继续原步骤、导出并发出 workflow_done。关闭自动恢复同样没有保留独立的错误暂停检查。

最小复现实际输出：

- checkpoint_exhausted：actions = [click, export]，failed = false，最后事件 workflow_done。
- total_exhausted：actions = [click, export]，failed = false，最后事件 workflow_done。

应将“错误检测/暂停”与“是否允许自动恢复”分离。额度只限制恢复动作，不取消错误状态。第二次失败必须停住，禁止发 step_done/workflow_done。

### 2. 半登录可以经“继续”或行前检查被误放行

位置：`tools/workflow_runner.py:2180`，`src/spider_gd/core/crawler/shared_browser.py:554`。

captcha 使用 require_storage_keys 检查 token；暂停恢复却使用 SharedBrowser.check_auth，它只检查站点、菜单和可选身份元素，不验证该 token。行前复用同样采用宽松判定。

复现同一页：有 .el-menu 和 pfxh-userInfo，没有 pfxh-token。严格登录检查返回“半登录”，全局检查返回 authenticated；调用暂停处理后选择继续，函数正常返回并设置 skip_remaining_login = true。

应统一登录后置条件；门户登录、现货子应用认证可分别定义，不能把某个应用的 token 机械要求在所有页面上，但同一阶段的行前、恢复、结束判定必须一致。

### 3. 任意同源 4xx/5xx 被误当成可自动恢复的网关错误

位置：`tools/workflow_runner.py:547`、`:558`。

错误记录没有 Content-Type、响应体长度/形态，也没有页面标识。复现 application/json 的业务校验 400，仍然关闭了当前子标签并触发恢复。错误来自门户页还是现货页也无法从记录关联到恢复对象。

应记录 run_id/page_id/文档代次/步骤/请求类型/状态码/Content-Type/响应体大小及安全摘要。已观察到的“业务接口 HTTP 400 + text/html + 极短空白正文”可单独分类；JSON 业务错误、401、403、404、429、5xx 应分别处理。未知错误暂停诊断，不能自动断言签名失效。

### 4. 无安全恢复路径时实际是失败结束，不是暂停保留现场

位置：`tools/workflow_runner.py:803`、`:647`、`:369`。

缺检查点、绑定页已关闭、没有可关新页时，函数设置 block_reason，随后抛异常，执行器发 step_error/aborted；未调用 error_pause/wait_continue。同域旧标签暂停后没有处理完再次继续，也落入失败结束。

复现：绑定页 400、没有新标签。failed = true，wait_continue 调用次数 0；事件为 step_start → gateway_rejected → step_error → aborted。

这能阻止后续点击，但不满足“整个任务暂停，修复后再检查”的产品要求。需要明确暂停、取消、失败三种状态及各自恢复入口。

### 5. 实际运行配置没有跟上仓库修改

位置：`src/spider_gd/gateway/api/server.py:667`，`tools/task_runner.py:172`。

接口先读 SQLite，缺配置才回退 YAML。只读检查本机 `data/spider.db`：

- “日前节点电价查询导出”仍含 navigate 到 `https://pm.gd.csg.cn/portal/#/powerEnterprise`；没有显式 page_recovery/max_page_recoveries 配置。
- “登陆流程测试”的 captcha 没有 require_storage_keys；数据库版本没有新增 reset_site_storage。

因此仓库 YAML 里的修改并不能证明界面正在执行相同版本。工作流/任务的登录注入来源也应统一，避免指标来自数据库、注入登录来自文件，拼成另一份未验收流程。

只读查询运行中后端 `/api/browser/status`：mode = managed，auth_policy = reuse_or_auto，target_origin = `https://pm.gd.csg.cn/portal/#/home`。

`shared_browser.py:411` 仍拼接 `/portal/#/home`，结果会成为 `...#/home/portal/#/home`；“只手工登录”也没有成为当前有效策略。

应把配置迁移作为修复的一部分：预先备份、生成可核对的差异、更新数据库中的对应流程及持久化连接配置；运行日志记录实际配置来源和版本摘要。不能覆盖用户自定义步骤或把改仓库文件当成发布完成。

### 6. 存储清理的兜底越过了指定范围和保留键

位置：`tools/workflow_runner.py:1511`。

used_cdp = false 时无条件 localStorage.clear() 与 sessionStorage.clear()。即使只指定 session_storage 或指定 keep_keys，也会清空两种存储；CDP 失败时也没有完整实现宣称的 Cookie 清理。

真实本地浏览器页复现：初始 localStorage 有 token；sessionStorage 有 keep 与 drop。执行 types=[session_storage]、keep_keys=[keep]、reload=false 后，两种存储均为空。

默认采集/登录链不应自动清站点存储；清理应作为独立的人工维护动作，并严格遵守作用范围和保留键。若必要能力不可用，应明确报错，不能用“清更多”当兜底。

## 其他需要修正的问题

- `workflow_runner.py:624`：网络等待按“在途请求数没有减少多久”返回，并非等待请求完成。复现仍有 1 个请求时约 0.81 秒即返回。日期和原有 wait 数值不需要为此调整；应增加绑定此次查询的响应/页面就绪验证，超时暂停而不是直接导出。
- `workflow_runner.py:470`：页面观测器没有在结束时移除。真实本地浏览器同页运行两次已完成流程，再发一个请求，两份已结束 runner 都记录该请求。长期复用会积累监听器、日志和旧运行对象。
- `workflow_runner.py:998`：action: login 在全局认证检查没有通过时仍 goto；skip_if_loaded 只保护 navigate。手工复用模式应在调度与动作执行边界共同约束自动登录/重载行为。
- `workflow_runner.py:2097`：HTTP 200 HTML 或无 Content-Type 也被诊断为疑似网关拦截；该分类不检查错误状态和正文。不能用此诊断倒推故障原因。
- `workflow_runner.py:836`：恢复将执行列表设为 [binding]，保护页仍开着，但后续 switch_tab 的索引含义可能改变。应使用稳定 page_id，而不是依赖重新排列后的下标。
- `_same_origin_stale_tabs` 遍历所有 Context，且只比较 netloc。独立 Context 的同域页面不应自动等同于共享会话的冲突页面；绑定页自身不能重开时，也应先提供相关旧页诊断。
- 前端进度组件没有处理 stale_tabs_warning、page_recovery、gateway_rejected。部分事件进入后台日志，但并不等于用户在进度窗口看到了操作指引。
- `workflows/dayahead_node_price.yml` 注释仍写“或刷新文档”；BROWSER_SESSION.md 仍称 4 份 YAML 已加 skip_if_loaded，而当前 workflows 中没有该键。文档必须对齐实际配置。

## 对根因的判断

HTTP 400、text/html、极短空白响应以及换页后有时成功，支持“请求被前置防护或会话检查拒绝”的假设。它们尚不足以证明具体是哪段脚本写坏状态，也不足以证明所有同域标签必然竞争 Cookie，或字符串中固定片段就是某种密钥。

将一个请求的签名参数放到另一接口后得到相同错误，只能说明这种构造也会被拒，不能证明正常点击日期的失败与它具有同一原因。应把文档中的观测事实、推测解释、已验证恢复路径分开。

不应通过重放/拼接防护参数来修复自动化。让真实页面产生自己的请求，软件负责保存会话、限定动作、识别失败并停止。

## 若由我全权实施

1. **先收敛有效运行配置。** 备份并迁移 SQLite 中正在使用的流程；默认仅复用/手工登录；业务 origin 规范化，入口 URL 独立保存。自动采集中移除自动登录、清存储和强制门户跳转。
2. **先完成可靠暂停。** 请求异常被记录后立即禁止后续业务动作。无恢复路径、额度耗尽、重复 400、状态未知都进入 paused，保持全局占用；取消明确结束。恢复检查仍失败则继续暂停。
3. **再实现最窄的恢复动作。** 只针对已标识的失败现货子页，且属于本次运行、由指定门户点击打开、门户仍可用、没有同 Context 中待处理的冲突页；关闭这一个子页，重新点击同一入口。只重放白名单中的导航/查询步骤，一次仍失败即暂停。默认先由用户选择执行恢复，待现场验收后再开放自动模式。
4. **统一认证与数据完成条件。** 行前、暂停后和任务结束用同一套分阶段认证规则；查询必须拿到本次目标日期对应的有效结果后才允许导出。保留原日期算法和 wait 参数。
5. **从反例验证，再做真实站点验收。** 必测第二次 400、总额度耗尽、JSON 400、缺 token 点继续、无可关页、旧页未处理、迟到响应、查询超时、源页面已变更、监听器释放及数据库实际配置。最后在同一专用 Profile 中比较“完全手工”“仅连接但手工操作”“自动点击查询”；不混入清 Cookie、刷新和多次重试，逐项定位差异。

交付验收：错误后没有新的业务点击/导出；恢复不会关闭保护页；不会刷新失败文档；失败记录不能变成成功；导出数据与所选日期一致。连续多次现场流程和故障恢复验收通过，才声称可稳定使用。

## 本次验证结果

原有测试重新运行：

```
.venv/bin/pytest -q tests/integration/test_workflow_runner_mocksite_e2e.py tests/unit/test_workflow_login_guard.py tests/unit/test_workflow_navigate.py
25 passed in 88.33s
```

额外最小复现放在 `/tmp/spider_review_repro.py` 和 `/tmp/spider_review_browser_repro.py`，未改仓库已有测试：

- 每检查点额度耗尽、总额度耗尽：均继续 export 并 workflow_done。
- 无安全恢复路径：直接失败，无等待继续。
- JSON 400：触发关闭子页。
- 在途请求未完成：0.81 秒返回。
- 半登录：严格判定失败，全局判定成功，继续后放行。
- 两个已完成 runner：仍同时观察后续响应。
- 只清 sessionStorage 且保留一个键：localStorage/sessionStorage 全空。

以上证明具体控制流缺陷；没有把模拟站点测试通过等同于真实站点故障已经解决。
