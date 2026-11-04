# 交易日历：识别复盘、工作流与 CSV 结构

日期：2026-10-30。对应文件：

- `workflows/交易日历.yml`：月视图完整快照（整月日期/事件/状态）。
- `workflows/交易日历-周明细.yml`：周视图明细，按天累积成月。
- `output/交易日历/交易日历2026-10.csv`：2026-10 月视图样本（78 行）。
- `output/交易日历/交易日历2026-10-周明细.csv`：2026-10-26 那一周的明细样本（15 行）。
- `output/交易日历/交易日历2026-03_参考归一.csv`：客户参考 CSV 按同一列结构归一（53 行）。
- `tools/calendar_data.py`：CSV 整形、空日期、合并。
- `tools/workflow_runner.py` 的 `extract_calendar`：页面日历 → CSV。

## 1. 识别复盘：录制结果为什么不能用

录制包 `交易日历.zip` 的原始捕获只有 3 个 click，生成的 YAML 有 6 步：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| “中长期交易”被点了两次 | 注入脚本在页面/重载时重复绑定监听，两次 click 的 `_time` 完全相同 | 录制器对“同动作 + 同选择器 + 同文本 + 400ms 内”的相邻步骤去重；Python 清洗再兜底一次 |
| 没有读取日历的步骤 | 录制器只记录了点击，没有记录“要什么数据” | 新增 `extract_calendar` 动作；录制器新增 `L 日历` 模式 |
| 选择器是 `div.slideItem___3DyvC ...` | CSS Module 的 hash 后缀，站点发版就变 | 换成精确文本 `text="中长期交易"`；离线核对：精确匹配 1 个，子串匹配会命中“中长期交易（灾备）”2 个 |
| 最后停在“周”按钮 | 录制的是视图切换，不是数据 | 工作流显式切视图，再用 `extract_calendar` 读 DOM |

清洗后的步骤是：`login → click 中长期交易 → wait 日历 → 切视图 → wait → extract_calendar`。

### 关键发现：`交易日历.txt` 才是“有明细”的那份页面

`交易日历.raw.json` 里保存的是点击“周”之前的 **月视图** DOM，只有事件标题；
`交易日历.txt` 保存的是点击之后、页面上同时存在 **周视图 + 月视图** 的完整 DOM：

- 周视图事件卡片 `.calendar-week` 里包含：
  - 标题 `.titleOmit`：`2026年10月周双边协商交易(2026-10-26)（灾备）`
  - `标的时间`：`2026-10-28 ~ 2026-10-31`
  - `申报时间`：`10-26 14:00 ~ 10-26 15:30`
  - 类型 `.typeSizeFormal`：`正式`（空表示非正式/变更类）
  - 状态 `.legend-icon` 的 `greyBg` / `NotBg` / `tradeBg`
- 月视图事件 `.calendar-month` 只有标题和状态。

客户参考 CSV 里的“上午/下午”和“2.25-2.28”正好对应周视图的 **申报时间** 和
**标的时间**，不是月视图能提供的。因此设计成两个工作流，而不是硬从月视图猜。

## 2. 页面结构（两份 HTML 都已核对）

- 入口：工作台卡片 `中长期交易`，点击后新开标签页。
- 日历：`.calendar-box`，FullCalendar 网格；点“周”后页面上会同时有
  `.calendar-box:has(.fc-dayGridWeek-view)` 和
  `.calendar-box:has(.fc-dayGridMonth-view)`，必须用 `:has()` 区分。
- 视图：`.calendar-head-box .el-radio-button`，文字为 `月` / `周` / `日`。
- 日期格：`[data-date="YYYY-MM-DD"]`；非当前视图月份的格子带 `fc-day-other`。
- 事件：`.fc-event`；月视图读 `.fc-event-main`，周视图读 `.calendar-week`。
- 状态：`tradeBg`（交易中）/ `NotBg`（未开始）/ `greyBg`（已结束）。

所以不需要 `extract_table`（它不是 `<table>`），也不需要依赖 hash 类名：

```text
[data-date] + .fc-event
  ├─ .calendar-month  → 日期 / 交易名称 / 状态
  └─ .calendar-week   → + 标的时间 / 申报时间 / 类型
```

## 3. CSV 结构

固定 11 列，UTF-8 BOM，Excel 直接打开：

| 列 | 说明 | 月视图 | 周明细 |
| --- | --- | --- | --- |
| 日期 | 申报日期 `YYYY-MM-DD`（周视图取事件所在日期格） | ✓ | ✓ |
| 星期 | 中文星期 | ✓ | ✓ |
| 时段 | 上午 / 下午 | 空 | 从申报时间推导（12:00 前上午） |
| 交易名称 | 系统完整标题 | ✓ | ✓ |
| 交易时间范围 | 周视图=“标的时间”，如 `2026-10-28 ~ 2026-10-31` | 空 | ✓ |
| 申报时间 | 周视图=“申报时间”，如 `10-26 14:00 ~ 10-26 15:30` | 空 | ✓ |
| 类型 | `正式` / 空（变更类） | 空 | ✓ |
| 状态 | 交易中 / 未开始 / 已结束 / 无交易安排 | ✓ | ✓ |
| 视图 | 月 / 周 / 日 | ✓ | ✓ |
| 月份 | 该行所属 `YYYY-MM` | ✓ | ✓ |
| 采集时间 | 本次读取/合并时间 | ✓ | ✓ |

规则：

- 同一天多个事件 → 多行，保持页面顺序；月视图当天没有事件 → 一行“无交易安排”。
- 月视图跳过 `fc-day-other`；周明细 `include_other=true` 会读跨月周的日期，
  再按 **任务目标月份** 过滤，保证 10-26~11-01 这周里 11-01 进 11 月文件。
- 文件名 `交易日历{month_iso}.csv`（月）和 `交易日历明细{month_iso}.csv`（周），
  月份取页面实际显示值或任务目标月份。
- 空页面不写空 CSV：月视图没有日期直接报错；周明细目标月没有事件时，
  若已有文件则保留，不覆盖。
- 周明细 `merge=true`：以“日期 + 交易名称 + 申报时间 + 交易时间范围”为键，
  同一事件重抓会更新状态，不会重复追加。

### 与客户参考 CSV 的对应关系

| 参考 CSV 列 | 系统字段 |
| --- | --- |
| 第 1 列 日期 | 周视图日期格 `data-date`（申报日期） |
| 第 2 列 上午/下午 | 申报时间的时段，如 `14:00` → 下午 |
| 第 3 列 交易名称 | 系统标题（更完整，含年月、日期、灾备标记） |
| 第 4 列 2.25-2.28 | 标的时间（完整 ISO 区间） |

参考 CSV 归一后 53 行（含 2 月末尾跨月行）。系统月视图 2026-10 为 78 行
（31 天 + 47 个额外事件），10-30、10-31 无安排；周明细 2026-10-26 那周 15 行。

## 4. 两个工作流怎么选

### 交易日历.yml（月快照）

- 固定月视图，一次拿到整月：日期、交易名称、状态。
- 适合“这个月有哪几天、哪些交易”的完整性检查。
- 明细列（标的时间/申报时间/类型）为空。

### 交易日历-周明细.yml（月累积明细）

- 固定周视图，读取当前一周，`month_mode=target` + `merge=true`。
- 建议每天执行一次：每天把本周明细补进 `交易日历明细YYYY-MM.csv`；
  同一事件重抓只更新状态。
- 到月底，文件里就是该月所有周的事件明细，字段与参考 CSV 对齐。
- 局限（2026-10-31 已修）：页面周视图一次只显示一周，且站点 FullCalendar 没有
  fc-prev/next 工具栏，翻周只能靠 `.date-box` 的只读周选择器（`el-date-editor--week`）。
  `select_calendar_week` 现在点开该选择器、在面板里翻月后点目标周一，把周视图
  切到任务目标周；任务按周拆多轮执行即可累积多周明细。

## 5. 怎么用

1. Windows 同步代码后重启后端/前端。
2. 在“指标配置”导入 `workflows/交易日历.yml` 和/或
   `workflows/交易日历-周明细.yml`（也可以导入 `build/exports/交易日历.zip`）。
3. 新建任务，选择指标，日期范围保持默认 `D ~ D`。
4. 首次执行前，在已登录工作台单步跑一遍，确认：
   - 能新开“中长期交易”标签页；
   - 月视图能等到 `.calendar-box:has(.fc-dayGridMonth-view)`；
   - 周明细能等到 `.calendar-box:has(.fc-dayGridWeek-view)`；
   - 生成 `交易日历YYYY-MM.csv` / `交易日历明细YYYY-MM.csv`。
5. 保存位置可在后续“文件保存位置”配置里单独指定（见
   `docs/17_任务文件路径配置设计.md`）。

## 6. 代码改动

- `tools/workflow_runner.py`：新增 `extract_calendar` 动作；支持月/周视图、
  `month_mode`、`include_other`、`include_empty_days`、`merge`；
  新增 `{month_iso}` / `{previous_month_iso}` 占位符和 `calendar_saved` 事件。
- `tools/calendar_data.py`：11 列固定表头、空日期、时段推导、CSV 合并。
- `tools/build_workflow.py`：录制去重；`L` 日历模式；`extract_calendar`
  默认 `save_as`；类型推断把 `extract_calendar` 视为数据操作。
- `src/spider_gd/gateway/api/server.py`、`Indicators.tsx`：日志显示
  “📅 交易日历已保存（月份，行数）”。
- 测试：`tests/unit/test_calendar_data.py`、`tests/unit/test_calendar_workflow_yaml.py`、
  `tests/unit/test_build_workflow_dedupe.py`。
