# Mock 广东电力市场交易系统

模拟 `pm.gd.csg.cn` 的极简站点，用于测试侦察工具（HAR 录制、流量分析、导出抓取等）。

## 启动

```bash
cd mock_site
pip install -r requirements.txt
python main.py
```

服务监听 `http://localhost:8000`。

## 测试账号

| 字段   | 值          |
| ------ | ----------- |
| 用户名 | `admin`     |
| 密码   | `admin123`  |
| 验证码 | 任意 4 位数字（不校验值） |

## 路由清单

| 方法 | 路径               | 说明                                              |
| ---- | ------------------ | ------------------------------------------------- |
| GET  | `/`                | 根路径，自动跳转登录页或概览页                    |
| GET  | `/login`           | 登录页面                                          |
| POST | `/login`           | 登录提交（成功后写入多个 Cookie + 重定向并带 `HdKGccbL` 参数） |
| GET  | `/captcha`         | PNG 验证码图（4 位数字）                          |
| GET  | `/daily_overview`  | 日清算概览页（需登录），URL 携带 `HdKGccbL` 参数  |
| POST | `/api/export`      | 导出 Excel（需 `Authorization: Bearer <token>`）  |
| GET  | `/logout`          | 退出登录                                          |

## 关键约定

- **Session Cookie**：`VShPPFZUwMakO`（Starlette SessionMiddleware）
- **附加 Cookie**：`SF_cookie_82`、`SF_cookie_159`、`acw_tc`、`X-Auth-Token`（仅做模拟，并不参与服务端鉴权）
- **Bearer Token**：登录后写入 session，并通过 `localStorage.token` 暴露给前端 JS
- **URL 参数**：登录跳转后 URL 必带 `?HdKGccbL=<随机串>`，用于侦察工具捕获
- **导出 API**：表单参数 `pdate=YYYYMMDD`；header `X-Force-Expire: true` 可触发 412 模拟 token 过期

## 使用 pandas 校验导出文件

```python
import pandas as pd
df = pd.read_excel("daily_settlement_20260622.xlsx")
print(df.head())
```

## 离线特性

- 不引入任何 CDN（无 Vue/jQuery/Bootstrap 远程依赖）
- 全部 CSS 内联，验证码用 Pillow 本地生成，Excel 用 openpyxl 本地生成
