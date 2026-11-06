# Scrapling 调研：和 spider 的关系

结论先说：**不能用 Scrapling 单独替换本项目。** 它覆盖「请求、浏览器抓取、解析、并发爬取、导出」。spider 的主体是专网登录、U-Key、双网卡、录制式工作流、本机落盘和客户上传。两边重叠的是 HTTP 客户端、Playwright/CDP 和「会话」这个词，重叠的实现假设并不相同。

材料来自官方 README 与文档站，不是二手评测。性能和覆盖率数字是项目自述，这里没有复跑。

## Scrapling 是什么

[Scrapling](https://github.com/D4Vinci/Scrapling)（作者 Karim Shoair，BSD-3-Clause，Python 3.10+）是一个 Python 抓取框架。官方 README 的定位是：一次请求到整站爬取，包含快速 HTTP、隐蔽浏览器、解析和并发爬虫，也可从终端或 AI Agent 使用（[README](https://github.com/D4Vinci/Scrapling/blob/main/README.md)，[文档首页](https://scrapling.readthedocs.io/en/latest/)）。

基础安装只有解析器。要用 Fetcher / Spider 得装 `scrapling[fetchers]` 并执行 `scrapling install` 下载浏览器；MCP、RAG、交互 shell 是另外的 extra（[README 安装节](https://github.com/D4Vinci/Scrapling/blob/main/README.md)）。

### 主要能力

**抓取与会话**（[Fetchers basics](https://scrapling.readthedocs.io/en/latest/fetching/choosing.html)，[StealthyFetcher](https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html)）

- `Fetcher` / `FetcherSession`：HTTP，可伪装浏览器 TLS 指纹、请求头，可选 HTTP/3。
- `StealthyFetcher` / `StealthySession`：Playwright 驱动的 Chromium 或本机 Chrome。文档列出的反爬包括：`solve_cloudflare=True` 处理 Cloudflare Turnstile/Interstitial、减弱 CDP 与 WebRTC 泄漏、canvas 噪声、headless 特征修补、时区不一致对策。
- 会话对象跨请求保留 Cookie。`ProxyRotator` 做代理轮换。浏览器会话可挡广告域名，可选 DNS-over-HTTPS。
- `cdp_url` 连接已经开着的浏览器；`executable_path` 指向自己的 Chromium。
- `record_requests=True` 后用 `session.network.search()` 查页面背后的响应体，避免再发一遍请求。

**解析**（[Adaptive scraping](https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html)）

- CSS、XPath、按文本/正则查找、DOM 上下级、`find_similar()`。
- Adaptive：第一次用 `auto_save=True` 把元素特征存进 SQLite（键是域名 + identifier），以后选择器失效时用 `adaptive=True` 按相似度找回。文档强调这不是调用大模型。
- `page.markdown()` 把页面收成 Markdown，给 RAG 用。

**爬虫**（[Spiders architecture](https://scrapling.readthedocs.io/en/latest/spiders/architecture.html)，[README Spiders 一节](https://github.com/D4Vinci/Scrapling/blob/main/README.md)）

- 类似 Scrapy 的 `Spider`：`start_urls`、异步 `parse`、`Request` / `Response`。
- 并发上限、按域名节流、下载延迟、AutoThrottle（按响应快慢调延迟，遇到封锁或 `Retry-After` 就加长）。
- 一个蜘蛛里可以挂多个 session（例如 `"fast"` 用 HTTP、`"stealth"` 用浏览器），用 `Request(..., sid=...)` 分流。
- `crawldir` 在中断时保存待请求和已见 URL，同一目录再启动即续爬。
- `async for item in spider.stream()` 边爬边吐数据和统计。
- 可选遵守 robots.txt。开发模式把响应缓存在磁盘上，方便改解析时不再打目标站。
- 模板：`CrawlSpider`、`SitemapSpider`、`XMLFeedSpider`、`CSVFeedSpider`、`ShopifySpider`、面向 Markdown 语料的 `ContentSpider`。
- 结果可 `to_json` / `to_jsonl` / `to_csv` / `to_xml`。

**Agent 与 CLI**

- MCP server（`scrapling[ai]`）：让对话代理用它的隐蔽浏览器去点页面、截图、带登录态和后台 API 响应。文档首页同时提到压缩送进模型的内容、拦广告、防提示注入（[文档首页](https://scrapling.readthedocs.io/en/latest/)）。
- `scrapling extract` 和 `scrapling shell` 可以不写脚本抽页面（[README CLI](https://github.com/D4Vinci/Scrapling/blob/main/README.md)）。
- 已有 Scrapy 回调可以用 `scrapling_response` 换解析器，而不重写下载。

README 自述解析器在其基准里快于常见 Python 解析库，并写有约 92% 测试覆盖和完整类型标注。这些数字未在本仓库验证。

## 对照 spider 现在要做什么

spider 的验收标准不是「把公开页面抓成 JSON」，而是下面这些已经在代码里的行为。

| spider 的要求 | 代码落点 | Scrapling 单独能否满足 |
|---------------|----------|------------------------|
| U-Key 随身 WiFi 进专网，证书控件连 `127.0.0.1:10443` | `shared_browser._build_launch_args` 关闭 `LocalNetworkAccessChecks`；`ukey_manager.py` | **否。** 没有 USB、证书控件、本机网络访问检查这层 |
| 中文 Windows 上 netsh 的 SSID 十六进制、cp936 控制台 | `wifi_connector.decode_ssid_name`、`infrastructure/proc.py` | **否。** 框架不管理主机网络 |
| 双网卡路由，公网失败仍继续本地采集 | `app.ensure_dual_nic_ready`、`infrastructure/network/supervisor.py`、`deploy/setup_routes.ps1` | **否** |
| 复用真实 Chrome Profile 的登录态（Cookie + 页面内存），而不是隐蔽 headless | `BROWSER_SESSION.md`、`shared_browser.py`；控制接口禁止把客户运行收成真 headless | **部分。** 它有 `cdp_url`，但默认价值在 StealthyFetcher 自己拉浏览器。换掉现有 CDP 会丢掉 UKey 与瑞数定时器的启动参数（非零调试端口、禁止后台节流） |
| 录制的多步 RPA：日期控件、交易日历周、导出、弹窗暂停、验证码刷新 | `workflow_runner.py` 的 `action` 列表；`captcha_solver.py` | **否。** Spider 的 `parse` 是「响应进、item 出」，没有这些步骤，也没有 ddddocr 与「弹窗即暂停整单」 |
| 指标 YAML、任务窗口、路径模板、SQLite 配置 | `task_runner.py`、`core/dates.py`、`workflow_configs` | **否** |
| 桌面壳、本机 8800 控制面、托盘、安装包用户目录 | `frontend/`、`gateway/api/server.py`、`user_home.py` | **否。** 它的 CLI/MCP 是抓取工具，不是现场操作台 |
| 文件版本、台账、多文件打成一个 zip、自定义头上传、切 WiFi 再传 | `file_versions.py`、`data_uploader.py` | **否。** 导出是 JSON/CSV/XML，不是客户 multipart 协议 |
| 企微 / Server酱 / PushPlus / outbox | `app.py` delivery、`gateway/alert/` | **否** |
| 已知 API 的 412 重登与 item 续跑 | `session_keeper.py`、`crawl_orchestrator.py` | **部分。** 有会话和 `crawldir` 续爬，没有电力平台 Token / `HdKGccbL`，也没有 `task_items` 这套状态 |
| 只读查询给外部 Agent | `gateway/mcp/server.py` 的三个 tool | **不相关。** Scrapling MCP 是去浏览别人的网站；spider MCP 是查询自己的任务，且明确不触发采集 |
| TLS 指纹 | `api_fetcher.py` 可选 `curl_cffi` impersonate | **部分，且已有。** 再套一层 Fetcher 不会解决登录和落盘 |

所以：

- **公开站点、链接跟随、反 Cloudflare、把 HTML 收成表** → Scrapling 可以单独做。这不是本仓库的当前范围。
- **本仓库的桌面采集闭环** → **否。**
- **只把「发 HTTP / 开浏览器」那一层换成 Scrapling** → **部分，而且不划算。** `ApiFetcher` 已经能 impersonate；工作流必须附着在带证书的 Chrome 上。Stealth 补丁针对的是「被当成自动化的无头浏览器」，和「故意连接用户真实 Profile」是相反的策略。`BROWSER_SESSION.md` 写明当前实现没有关沙箱、没有做指纹伪装。

## 值得学、不值得换

可以借鉴的是局部手法。接入时保持 `WorkflowRunner` 和 `shared_browser` 为执行内核。

1. **选择器失效后的相似度回退。** 录出来的 CSS 在门户改 class 后整步失败。Adaptive 的做法（域名 + 标识存元素特征，失败时按结构/文本相似度找）适合放进点击和导出前的一次查找，作为显式降级，并在 `error_pause` 之前把「匹配到的新选择器」写进日志。不要默认自动改 YAML。存储用现有 SQLite 即可，不必引入整库 Scrapling。文档：[Adaptive scraping](https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html)。
2. **录制时生成更稳的选择器。** README 写了从元素反推 CSS/XPath。工坊（`tools/build_workflow.py`）若只记绝对路径，门户一改就断。反推时优先角色、文本和稳定属性，和上一条的特征一起存。
3. **把「页面已经发出的 API」当成导出源。** spider 已有 `intercept_api`、`collect_api`、`api_call`。Scrapling 的 `record_requests` 把这件事做成会话上的可查询历史，并限制体积。若现有拦截只覆盖单步，可以对照它的边界：只留文本体、有上限、按 URL 搜索，避免把所有 XHR 都写进执行日志。
4. **开发时回放 HTTP 响应。** Spider 的开发模式把第一次响应落盘再回放。spider 已有 `mock_site/` 和 HAR 侦察（`tools/recon_tool`），对整页 RPA 更合适。对纯 `api_call` 步骤，磁盘回放能减少打到专网的次数，和 mock 站点互补，不替代它。
5. **流式统计的形状。** `spider.stream()` 在爬取中途给出计数。本项目 SSE 已按步骤推事件。缺的是操作员一眼能看的「第几个指标、已落几个文件」，不是再引入一个爬虫循环。

明确不要搬的：

- **用 StealthyFetcher 换专用 Chrome。** 会丢掉非零 CDP 端口、UKey 的本地网络例外、后台节流开关和用户 Profile。Cloudflare 求解对本专网不是当前矛盾。
- **用 Spider/`crawldir` 换 `TaskRunner`。** 续的是 URL 队列，续不了「日历周已经点过、导出尚未确认」。跨重启续跑的风险见产品文档。
- **用它的 MCP 换 `gateway/mcp/server.py`。** 权限模型相反：spider 的 tool 只读且不点击；Scrapling 的 MCP 就是为了点击。
- **代理池、robots.txt、Sitemap/Shopify 模板、Markdown RAG。** 目标是一个需证书的业务系统，不是公开整站。
- **为了 README 里的解析速度换成它的 Selector。** 瓶颈在登录、专网和导出文件，不在从 5000 个节点里取文本。

## 若以后做小范围试验

只在离线单测里试，不进客户包：

- 拿一份已保存的门户 HTML（失败诊断里 `WorkflowRunner` 会落页面），用 Adaptive 看旧选择器能否指到同一按钮。
- 对比结果用人工看截图，不自动写回 `workflow_configs`。
- 依赖加在开发 extra，不放进 `mode_d_offline` 的运行时。

试验失败或选择器飘到相邻按钮，就停。现场路径继续用现有暂停和人工继续。

## 来源

- [Scrapling 仓库 README](https://github.com/D4Vinci/Scrapling/blob/main/README.md)
- [文档首页 Introduction](https://scrapling.readthedocs.io/en/latest/)
- [Fetchers basics](https://scrapling.readthedocs.io/en/latest/fetching/choosing.html)
- [StealthyFetcher](https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html)
- [Spiders architecture](https://scrapling.readthedocs.io/en/latest/spiders/architecture.html)
- [Adaptive scraping](https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html)
