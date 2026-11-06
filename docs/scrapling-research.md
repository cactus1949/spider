# Scrapling 调研：可借鉴的手法

这篇只记值得看的做法。它覆盖请求与浏览器抓取，也覆盖解析、并发爬取与导出。spider 的主体是专网登录与 U-Key，加上双网卡与录制式工作流，以及本机落盘与客户上传。两边重叠的是 HTTP 客户端、Playwright/CDP 和「会话」这个词，重叠的实现假设并不相同。

材料来自官方 README 与文档站，不是二手评测。性能和覆盖率数字是项目自述，这里没有复跑。

## Scrapling 是什么

[Scrapling](https://github.com/D4Vinci/Scrapling)（作者 Karim Shoair，BSD-3-Clause，Python 3.10+）是一个 Python 抓取框架。官方 README 的定位是：一次请求到整站爬取，包含快速 HTTP 和隐蔽浏览器，也包含解析和并发爬虫，也可从终端或 AI Agent 使用（[README](https://github.com/D4Vinci/Scrapling/blob/main/README.md)，[文档首页](https://scrapling.readthedocs.io/en/latest/)）。

基础安装只有解析器。要用 Fetcher / Spider 得装 `scrapling[fetchers]` 并执行 `scrapling install` 下载浏览器；MCP 和 RAG，以及交互 shell 是另外的 extra（[README 安装节](https://github.com/D4Vinci/Scrapling/blob/main/README.md)）。

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

## 和现场路径并排放

下面是 spider 已经在做的事。第三列只摘文档里对不上的事实，方便对着学。

| spider 已经在做的事 | 代码落点 | 文档里对不上的事实 |
|---------------|----------|------------------------|
| U-Key 随身 WiFi 进专网，证书控件连 `127.0.0.1:10443` | `shared_browser._build_launch_args` 关闭 `LocalNetworkAccessChecks`；`ukey_manager.py` | 没有 USB、证书控件、本机网络访问检查这层 |
| 中文 Windows 上 netsh 的 SSID 十六进制、cp936 控制台 | `wifi_connector.decode_ssid_name`、`infrastructure/proc.py` | 框架不管理主机网络 |
| 双网卡路由，公网失败仍继续本地采集 | `app.ensure_dual_nic_ready`、`infrastructure/network/supervisor.py`、`deploy/setup_routes.ps1` | 文档不涉及路由和降级 |
| 复用真实 Chrome Profile 的登录态（Cookie + 页面内存） | `BROWSER_SESSION.md`、`shared_browser.py`；控制接口不把客户运行收成真 headless | 它有 `cdp_url`。StealthyFetcher 默认自己拉浏览器。现有启动参数用非零调试端口，并禁止后台节流，保住 UKey 与瑞数定时器 |
| 录制的多步 RPA：日期控件、交易日历周、导出、弹窗暂停、验证码刷新 | `workflow_runner.py` 的 `action` 列表；`captcha_solver.py` | Spider 的 `parse` 是「响应进、item 出」，没有这些步骤，也没有 ddddocr 与「弹窗即暂停整单」 |
| 指标 YAML、任务窗口、路径模板、SQLite 配置 | `task_runner.py`、`core/dates.py`、`workflow_configs` | 文档没有这套任务配置 |
| 桌面壳、本机 8800 控制面、托盘、安装包用户目录 | `frontend/`、`gateway/api/server.py`、`user_home.py` | 它的 CLI/MCP 是抓取工具。spider 这边是现场操作台 |
| 文件版本、台账、多文件打成一个 zip、自定义头上传、切 WiFi 再传 | `file_versions.py`、`data_uploader.py` | 导出是 JSON/CSV/XML。客户上传是 multipart |
| 企微 / Server酱 / PushPlus / outbox | `app.py` delivery、`gateway/alert/` | 文档没有这些发送通道 |
| 已知 API 的 412 重登与 item 续跑 | `session_keeper.py`、`crawl_orchestrator.py` | 有会话和 `crawldir` 续爬。没有电力平台 Token / `HdKGccbL`，也没有 `task_items` |
| 只读查询给外部 Agent | `gateway/mcp/server.py` 的三个 tool | Scrapling MCP 用来浏览外部网站。spider MCP 只查询自己的任务，且不触发采集 |
| TLS 指纹 | `api_fetcher.py` 可选 `curl_cffi` impersonate | `ApiFetcher` 已经能 impersonate。Fetcher 再套一层，仍不处理登录和落盘 |

公开站点上的链接跟随和反 Cloudflare，不在本仓库当前范围里。`BROWSER_SESSION.md` 写明当前实现没有关沙箱，也没有做指纹伪装。可借鉴的手法在下一节。

## 值得学的手法

可以借鉴的是局部手法。现场执行仍是 `WorkflowRunner` 和 `shared_browser`。

1. **选择器失效后的相似度回退。** 录出来的 CSS 在门户改 class 后整步失败。Adaptive 的做法（域名 + 标识存元素特征，失败时按结构/文本相似度找）适合放进点击和导出前的一次查找，作为显式降级，并在 `error_pause` 之前把「匹配到的新选择器」写进日志。不要默认自动改 YAML。存储用现有 SQLite 即可，不必引入整库 Scrapling。文档：[Adaptive scraping](https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html)。
2. **录制时生成更稳的选择器。** README 写了从元素反推 CSS/XPath。工坊（`tools/build_workflow.py`）若只记绝对路径，门户一改就断。反推时优先角色、文本和稳定属性，和上一条的特征一起存。
3. **把「页面已经发出的 API」当成导出源。** spider 已有 `intercept_api`、`collect_api`、`api_call`。Scrapling 的 `record_requests` 把这件事做成会话上的可查询历史，并限制体积。若现有拦截只覆盖单步，可以对照它的边界：只留文本体、有上限、按 URL 搜索，避免把所有 XHR 都写进执行日志。
4. **开发时回放 HTTP 响应。** Spider 的开发模式把第一次响应落盘再回放。spider 已有 `mock_site/` 和 HAR 侦察（`tools/recon_tool`），对整页 RPA 更合适。对纯 `api_call` 步骤，磁盘回放能减少打到专网的次数，和 mock 站点互补，不替代它。
5. **流式统计的形状。** `spider.stream()` 在爬取中途给出计数。本项目 SSE 已按步骤推事件。缺的是操作员一眼能看的「第几个指标、已落几个文件」，不是再引入一个爬虫循环。

下面这些和现场步骤对不上，笔记里只作边界：

- StealthyFetcher 默认自己拉浏览器。非零 CDP 端口、UKey 本地网络例外，以及后台节流和用户 Profile，都在现有专用 Chrome 的启动参数里。Cloudflare 求解对本专网不是当前矛盾。
- `crawldir` 续的是 URL 队列。它续不了「日历周已经点过、导出尚未确认」。跨重启续跑的风险见产品文档。
- 两个 MCP 的权限相反。spider 的 tool 只读且不点击。Scrapling 的 MCP 用来点击。
- 代理池和 robots.txt，以及 Sitemap/Shopify 模板和 Markdown RAG，面向公开整站。本仓库的目标是要证书的业务系统。
- README 里的解析速度解决不了登录、专网和导出文件。

## 样本上核对 Adaptive

- 拿一份已保存的门户 HTML（失败诊断里 `WorkflowRunner` 会落页面），用 Adaptive 看旧选择器能否指到同一按钮。
- 对比结果用人工看截图，不自动写回 `workflow_configs`。
- 这类核对留在开发样本上，不放进 `mode_d_offline` 的运行配置。

选择器飘到相邻按钮时就停。现场仍用现有暂停和人工继续。

## 来源

- [Scrapling 仓库 README](https://github.com/D4Vinci/Scrapling/blob/main/README.md)
- [文档首页 Introduction](https://scrapling.readthedocs.io/en/latest/)
- [Fetchers basics](https://scrapling.readthedocs.io/en/latest/fetching/choosing.html)
- [StealthyFetcher](https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html)
- [Spiders architecture](https://scrapling.readthedocs.io/en/latest/spiders/architecture.html)
- [Adaptive scraping](https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html)
