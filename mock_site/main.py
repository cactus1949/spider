"""Mock 广东电力市场交易系统 (pm.gd.csg.cn) - 用于测试侦察工具

启动: python main.py
访问: http://localhost:8000
账号: admin / admin123
"""
from __future__ import annotations

import asyncio
import io
import json
import random
import secrets
import string
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from markupsafe import Markup

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageFont
from openpyxl import Workbook
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Mock 广东电力市场交易系统")
app.add_middleware(SessionMiddleware, secret_key="mock-spider-gd-secret-key", session_cookie="VShPPFZUwMakO")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------- 工具函数 ---------- #

def _random_token(length: int = 32) -> str:
    return secrets.token_hex(length // 2)


def _random_hd_kg(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_captcha_text(n: int = 4) -> str:
    return "".join(random.choices(string.digits, k=n))


def _generate_captcha_image(text: str) -> bytes:
    width, height = 120, 40
    image = Image.new("RGB", (width, height), color=(245, 245, 245))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("Arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    for i, ch in enumerate(text):
        x = 12 + i * 25
        y = random.randint(2, 8)
        color = (random.randint(0, 120), random.randint(0, 120), random.randint(0, 120))
        draw.text((x, y), ch, fill=color, font=font)
    # 干扰线
    for _ in range(4):
        draw.line(
            [
                (random.randint(0, width), random.randint(0, height)),
                (random.randint(0, width), random.randint(0, height)),
            ],
            fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)),
            width=1,
        )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _generate_settlement_data(pdate: str) -> list[dict]:
    """生成机组结算情况数据。"""
    units = ["华能广州电厂#1", "粤电珠海电厂#2", "深能东部电厂#3", "广州恒运电厂#4", "粤电沙角电厂#5"]
    rows = []
    for unit in units:
        rows.append(
            {
                "unit_name": unit,
                "total_grid_energy": round(random.uniform(1000, 5000), 2),
                "purchase_export_energy": round(random.uniform(100, 500), 2),
                "base_energy": round(random.uniform(200, 800), 2),
                "mid_long_market_energy": round(random.uniform(300, 1000), 2),
                "day_ahead_deviation": round(random.uniform(-50, 50), 2),
                "real_time_deviation": round(random.uniform(-30, 30), 2),
                "user_side_fee": round(random.uniform(1000, 5000), 2),
                "gen_side_fee": round(random.uniform(800, 4000), 2),
                "deviation_fee": round(random.uniform(-100, 100), 2),
                "imbalance_fund": round(random.uniform(-50, 50), 2),
            }
        )
    return rows


def _generate_kpi_data() -> dict:
    return {
        "market_energy": round(random.uniform(20000, 50000), 2),
        "user_fee": round(random.uniform(8000, 20000), 2),
        "gen_fee": round(random.uniform(6000, 18000), 2),
        "market_surplus": round(random.uniform(-500, 1500), 2),
    }


def _generate_monthly_data(year: int) -> list[dict]:
    """生成 12 个月的月度结算汇总数据。"""
    rows = []
    base_energy = random.uniform(800000, 1200000)
    base_fee = random.uniform(40000, 60000)
    for m in range(1, 13):
        seasonal = 1.0 + 0.15 * (1 if m in (1, 7, 8, 12) else -1) * random.random()
        energy = round(base_energy * seasonal, 2)
        fee = round(base_fee * seasonal, 2)
        rows.append(
            {
                "month": f"{year}-{m:02d}",
                "market_energy": energy,
                "user_fee": fee,
                "gen_fee": round(fee * random.uniform(0.78, 0.92), 2),
                "avg_price": round(random.uniform(380, 520), 2),
                "yoy_growth": round(random.uniform(-12, 18), 2),
                "settle_status": random.choice(["已结算", "已结算", "已结算", "结算中"]),
            }
        )
    return rows


def _generate_contract_data() -> list[dict]:
    """生成合同电量列表。"""
    parties = [
        "广州佛山某制造集团",
        "深圳南山高新园区",
        "东莞松山湖科技园",
        "珠海格力电器股份",
        "中山火炬开发区",
        "惠州大亚湾石化",
        "江门台山工业园",
        "肇庆四会金属加工",
    ]
    statuses = ["履行中", "履行中", "已完成", "待生效", "履行中"]
    rows = []
    for idx, party in enumerate(parties, start=1):
        rows.append(
            {
                "contract_no": f"GD-CT-2026-{idx:04d}",
                "party": party,
                "contract_energy": round(random.uniform(5000, 80000), 2),
                "executed_energy": round(random.uniform(1000, 60000), 2),
                "price": round(random.uniform(380, 520), 2),
                "sign_date": f"2026-{random.randint(1, 6):02d}-{random.randint(1, 28):02d}",
                "status": random.choice(statuses),
            }
        )
    return rows


# ---- 新增数据：现货出清电量 / 市场用电需求 ---- #

SPOT_AVAILABLE_DATES = {
    "2026-06-20",
    "2026-06-21",
    "2026-06-22",
    "2026-06-23",
    "2026-06-24",
}


def _parse_iso_date(date_str: Optional[str]) -> str:
    """解析 ?date=YYYY-MM-DD，为空则返回今天；格式错误报 400。"""
    if not date_str:
        return date.today().isoformat()
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date 参数格式需为 YYYY-MM-DD") from exc


def _generate_spot_clearance(date_str: str) -> dict:
    """生成现货出清电量（3列 × 25行：24 小时 + 合计）。使用 random.seed(date_str) 保证可重复。"""
    rng = random.Random(date_str)
    rows: list[dict] = []
    total_energy = 0.0
    total_units = 0
    for hour in range(24):
        # 基础 80000 MWh ±30% 随机波动
        energy = 80000.0 * (1.0 + rng.uniform(-0.3, 0.3))
        # 开机台数 = 250 + (电量 - 50000) / 1000
        units = int(round(250 + (energy - 50000) / 1000))
        rows.append({"time": f"{hour:02d}:00", "energy": round(energy, 2), "units": units})
        total_energy += energy
        total_units += units
    rows.append({"time": "合计", "energy": round(total_energy, 2), "units": total_units})
    return {"date": date_str, "available": date_str in SPOT_AVAILABLE_DATES, "rows": rows}


def _generate_market_demand(date_str: str) -> dict:
    """生成市场用电需求（3行 × 25列：类型 + 24 小时）。seed 为 date_str + 'market'。"""
    rng = random.Random(date_str + "market")
    demand: list[float] = []
    actual: list[float] = []
    deviation: list[float] = []
    for hour in range(24):
        # 日间系数：10-12点、17-19点 ×1.15，2-5点 ×0.85，其他 ×1.0
        if hour in (10, 11, 12, 17, 18, 19):
            day_factor = 1.15
        elif hour in (2, 3, 4, 5):
            day_factor = 0.85
        else:
            day_factor = 1.0
        # 基础 100000 MWh ±15%
        base = 100000.0 * (1.0 + rng.uniform(-0.15, 0.15)) * day_factor
        # 实际用电 = 需求 × 0.87~0.93
        act = base * rng.uniform(0.87, 0.93)
        dev = (act - base) / base * 100 if base else 0.0
        demand.append(round(base, 2))
        actual.append(round(act, 2))
        deviation.append(round(dev, 2))
    return {
        "date": date_str,
        "available": date_str in SPOT_AVAILABLE_DATES,
        "hours": [f"{h:02d}:00" for h in range(24)],
        "demand": demand,
        "actual": actual,
        "deviation": deviation,
    }


def _require_login(request: Request) -> Optional[RedirectResponse]:
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=302)
    return None


# ---------- 路由 ---------- #

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if request.session.get("user"):
        hd = _random_hd_kg()
        return RedirectResponse(url=f"/daily_overview?HdKGccbL={hd}", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.get("/captcha")
async def captcha(request: Request) -> Response:
    text = _random_captcha_text()
    request.session["captcha"] = text
    png = _generate_captcha_image(text)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    captcha: str = Form(...),
) -> Response:
    if username != "admin" or password != "admin123":
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误"},
            status_code=401,
        )

    # 验证码任意输入都通过（方便测试），但要求非空
    if not captcha.strip():
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "验证码不能为空"},
            status_code=400,
        )

    token = _random_token(32)
    hd = _random_hd_kg()
    request.session["user"] = username
    request.session["token"] = token
    request.session["login_time"] = datetime.now().isoformat()

    response = RedirectResponse(url=f"/daily_overview?HdKGccbL={hd}", status_code=302)
    # 模拟真实系统的多个 Cookie
    response.set_cookie("SF_cookie_82", secrets.token_hex(8), httponly=False, path="/")
    response.set_cookie("SF_cookie_159", secrets.token_hex(8), httponly=False, path="/")
    response.set_cookie("acw_tc", secrets.token_hex(16), httponly=False, path="/")
    response.set_cookie("X-Auth-Token", token, httponly=False, path="/")
    return response


@app.get("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


def ukey_pick_message(name: str, wifi_ssid: str) -> str:
    """生成多 UKey 演练里进入场站后的说明。

    Args:
        name: 选中的场站名称。
        wifi_ssid: 该场站要连接的 WiFi 名称。

    Returns:
        str: 给页面展示的一句话，不含密码。

    Examples:
        >>> ukey_pick_message("阳江明阳海上风电", "YJ-UKEY")
        '已进入场站 阳江明阳海上风电，将连接 WiFi YJ-UKEY'
    """
    return f"已进入场站 {name}，将连接 WiFi {wifi_ssid}"


def _load_station_picker():
    """加载场站选择模块，保证从 mock_site 目录启动时也能找到源码。

    Returns:
        tuple: 异常类型、选择函数、示例场站、公开字段函数。

    Examples:
        >>> error_type, pick, demo, public = _load_station_picker()
        >>> demo()[0].name
        '阳江明阳海上风电'
    """
    src = Path(__file__).resolve().parents[1] / "src"
    src_text = str(src)
    if src.is_dir() and src_text not in sys.path:
        sys.path.insert(0, src_text)
    from spider_gd.core.station_picker import StationPickError, pick_station
    from spider_gd.core.station_profile import demo_station_profiles, public_station

    return StationPickError, pick_station, demo_station_profiles, public_station


@app.get("/mock/ukey-login", response_class=HTMLResponse)
async def ukey_login_mock_page(request: Request) -> Response:
    """打开多 UKey 假登录页。

    Args:
        request: 当前 HTTP 请求。

    Returns:
        Response: 证书登录演练页面。

    Examples:
        >>> ukey_pick_message("阳江", "YJ").startswith("已进入场站")
        True
    """
    return templates.TemplateResponse(request, "ukey_login_mock.html", {})


@app.get("/mock/ukey-login/stations")
async def ukey_login_mock_stations() -> dict[str, object]:
    """列出演练用场站，不含密码。

    Returns:
        dict[str, object]: ``stations`` 为编号、名称和 WiFi。

    Examples:
        >>> ukey_pick_message("阳西海上风电", "YX-UKEY").endswith("YX-UKEY")
        True
    """
    _error_type, _pick, demo, public = _load_station_picker()
    return {"stations": [public(item) for item in demo()]}


@app.post("/mock/ukey-login/pick")
async def ukey_login_mock_pick(request: Request) -> dict[str, object]:
    """点击立即登录后等待本机窗口，返回选中的场站。

    Args:
        request: JSON 里可选 ``choice``，用来直接指定场站编号或名称。

    Returns:
        dict[str, object]: ``ok`` 为真时含场站名称和 WiFi，不含密码。

    Raises:
        HTTPException: 指定的场站不存在，或选择窗口无法打开。

    Examples:
        >>> "WiFi" in ukey_pick_message("阳江", "YJ-UKEY")
        True
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    choice = str(payload.get("choice") or "").strip()
    error_type, pick, demo, public = _load_station_picker()
    try:
        selected = await asyncio.to_thread(pick, demo(), choice=choice or None)
    except error_type as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if selected is None:
        return {"ok": False, "cancelled": True, "message": "已取消选择场站"}
    request.session["station_id"] = selected.id
    request.session["station_name"] = selected.name
    request.session["station_wifi"] = selected.wifi_ssid
    station = public(selected)
    return {
        "ok": True,
        "cancelled": False,
        "station": station,
        "message": ukey_pick_message(station["name"], station["wifiSsid"]),
    }


@app.get("/portal", response_class=HTMLResponse)
@app.get("/portal/", response_class=HTMLResponse)
async def portal_mock(request: Request, mock: str = "") -> Response:
    """真实门户 URL 形态的工作流 mock。

    浏览器不会把 hash 片段发送给服务端，因此 /portal/#/home 与
    /portal/#/powerEnterprise 都落到这个路由。
    mock=stale 时工作台只提供“签名失效演练”的现货交易入口。
    """
    return templates.TemplateResponse(
        request, "workflow_mock.html", {"app": "portal", "mock_mode": mock}
    )


@app.get("/pfxh/stc/check", response_class=HTMLResponse)
async def spot_workflow_mock(request: Request) -> Response:
    """现货交易页 mock，保留参考 YAML 依赖的 Element UI DOM 类名。"""
    return templates.TemplateResponse(request, "workflow_mock.html", {"app": "spot"})


@app.get("/pfjs/stc/check", response_class=HTMLResponse)
async def settlement_workflow_mock(request: Request) -> Response:
    """结算管理页 mock，保留账单中心/日清算概览工作流选择器。"""
    return templates.TemplateResponse(request, "workflow_mock.html", {"app": "settlement"})


@app.get("/pfxh/stc/stale", response_class=HTMLResponse)
async def stale_document_mock(request: Request, doc: str = "") -> Response:
    """模拟“文档级请求签名失效”的子应用页面。

    同一份文档（相同 doc）刷新仍然 400。只有另一个 doc 的新标签才 200。
    工作台点击「现货交易」时会给每次新开的标签换一个 doc。
    """
    identity = doc or "default"
    issued = request.cookies.get("mock_stale_doc")
    if not issued:
        valid = False
        remember = identity
    else:
        valid = identity != issued
        remember = None
    response = templates.TemplateResponse(
        request,
        "workflow_mock.html",
        {"app": "stale", "doc_valid": "1" if valid else "0"},
    )
    if remember is not None:
        response.set_cookie("mock_stale_doc", remember, path="/")
    return response


@app.get("/pfxh/qctc-pm-trade-market-out-zzzx/daNodePriceQuery/getJddjTree")
async def stale_document_tree(valid: str = "1") -> Response:
    """签名失效时由网关拒绝：400 + text/html，而不是业务 JSON。"""
    if valid != "1":
        return Response(content=b"\r\n\r\n\r\n", media_type="text/html", status_code=400)
    return {
        "code": 200,
        "data": [{"id": "gd-01", "label": "广东电网", "children": [{"id": "nd-01", "label": "节点A"}]}],
    }


@app.get("/pfxh/qctc-pm-trade-market-out-zzzx/daNodePriceQuery/getFirstAndLastTen")
async def stale_document_grid(valid: str = "1", date_str: str = "") -> Response:
    if valid != "1":
        return Response(content=b"\r\n\r\n\r\n", media_type="text/html", status_code=400)
    return {"code": 200, "data": [{"date": date_str, "node": "节点A", "price": 392.15}]}


@app.get("/pfxh/qctc-pm-trade-market-out-zzzx/daNodePriceQuery/getGrid")
async def stale_document_rows(valid: str = "1", date_str: str = "") -> Response:
    if valid != "1":
        return Response(content=b"\r\n\r\n\r\n", media_type="text/html", status_code=400)
    return {"code": 200, "data": [{"date": date_str, "node": "节点A", "price": 392.15}]}


@app.get("/date-picker-lab/{kind}", response_class=HTMLResponse)
async def date_picker_lab(request: Request, kind: str) -> Response:
    """日期选择器兼容性实验页：element-ui / element-plus / antd。"""
    supported = {"element-ui", "element-plus", "antd"}
    if kind not in supported:
        raise HTTPException(status_code=404, detail="date picker kind not found")
    return templates.TemplateResponse(request, "date_picker_lab.html", {"kind": kind})


@app.get("/widget-lab", response_class=HTMLResponse)
async def widget_lab(request: Request) -> Response:
    """下拉选择 + Element daterange 实验页，带输入事件记录（window.__inputLog）。"""
    return templates.TemplateResponse(request, "widget_lab.html", {})


@app.api_route(
    "/pfxh/qctc-pm-trade-market-out-plzx/DaMarkjsc/homeXhMarketAllInfo",
    methods=["GET", "POST"],
)
async def spot_api_fallback(request: Request) -> dict:
    """现货完整采集的 API 降级 mock。"""
    return {
        "code": 200,
        "data": [
            {"name": "负荷信息", "date": date.today().isoformat(), "value": 12345.67, "unit": "MWh"},
            {"name": "现货分时分类型出清电量", "date": date.today().isoformat(), "value": 7654.32, "unit": "MWh"},
        ],
    }


@app.api_route(
    "/pfjs/qctc-pm-jsgl-electricity-month-query/sbs/calDayStatQuery/exportItemsWithoutCalVersion",
    methods=["GET", "POST"],
)
async def settlement_api_fallback(request: Request) -> Response:
    """结算管理日清算概览的 API 降级 mock，返回 Excel。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "日清算概览"
    ws.append(["结算日", "市场化电量", "电费"])
    ws.append([date.today().isoformat(), 12345.67, 8901.23])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="daily_settlement_fallback.xlsx"'},
    )


@app.get("/daily_overview", response_class=HTMLResponse)
async def daily_overview(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect

    pdate = datetime.now().strftime("%Y%m%d")
    context = {
        "username": request.session.get("user"),
        "token": request.session.get("token"),
        "pdate": pdate,
        "kpi": _generate_kpi_data(),
        "rows": _generate_settlement_data(pdate),
        "hd_kg": request.query_params.get("HdKGccbL", ""),
    }
    return templates.TemplateResponse(request, "daily_overview.html", context)


@app.get("/monthly_settlement", response_class=HTMLResponse)
async def monthly_settlement(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    year = int(request.query_params.get("year", datetime.now().year))
    rows = _generate_monthly_data(year)
    summary = {
        "total_energy": round(sum(r["market_energy"] for r in rows), 2),
        "total_user_fee": round(sum(r["user_fee"] for r in rows), 2),
        "total_gen_fee": round(sum(r["gen_fee"] for r in rows), 2),
        "avg_price": round(sum(r["avg_price"] for r in rows) / len(rows), 2),
    }
    context = {
        "username": request.session.get("user"),
        "token": request.session.get("token"),
        "year": year,
        "rows": rows,
        "summary": summary,
    }
    return templates.TemplateResponse(request, "monthly_settlement.html", context)


@app.get("/contract_power", response_class=HTMLResponse)
async def contract_power(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    context = {
        "username": request.session.get("user"),
        "token": request.session.get("token"),
        "contracts": _generate_contract_data(),
    }
    return templates.TemplateResponse(request, "contract_power.html", context)


def _calendar_bootstrap_json() -> str:
    """把交易日历现场样本序列化成可嵌入页面脚本的 JSON。

    Returns:
        str: JSON 文本。``<`` 已转成 Unicode 转义，避免撑破 script 标签。

    Examples:
        >>> "2026-10-26" in _calendar_bootstrap_json()
        True
    """
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    from calendar_fixture import calendar_bootstrap

    return json.dumps(calendar_bootstrap(), ensure_ascii=False).replace("<", "\\u003c")


@app.get("/pfjy/calendar", response_class=HTMLResponse)
async def trading_calendar_mock(request: Request) -> Response:
    """返回不校验 UKey 的中长期交易日历页。

    月、周、日三种视图都在。周选择器按日画格，点中某天会选中那一行，也就是那一周。

    Args:
        request (Request): 当前 HTTP 请求。页面不读取查询参数。

    Returns:
        Response: 交易日历 HTML。

    Examples:
        >>> "2026-10-26" in _calendar_bootstrap_json()
        True
    """
    return templates.TemplateResponse(
        request,
        "trading_calendar.html",
        {"bootstrap_json": Markup(_calendar_bootstrap_json())},
    )


@app.get("/midlong_trade", response_class=HTMLResponse)
async def midlong_trade(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request,
        "midlong_trade.html",
        {"username": request.session.get("user")},
    )


@app.get("/spot_market", response_class=HTMLResponse)
async def spot_market(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request,
        "spot_market.html",
        {"username": request.session.get("user")},
    )


@app.get("/spot_clearance", response_class=HTMLResponse)
async def spot_clearance(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    date_str = _parse_iso_date(request.query_params.get("date"))
    data = _generate_spot_clearance(date_str)
    context = {
        "username": request.session.get("user"),
        "token": request.session.get("token"),
        "date": date_str,
        "available": data["available"],
        "rows": data["rows"],
    }
    return templates.TemplateResponse(request, "spot_clearance.html", context)


@app.get("/spot_clearance/export")
async def spot_clearance_export(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    date_str = _parse_iso_date(request.query_params.get("date"))
    data = _generate_spot_clearance(date_str)
    if not data["available"]:
        raise HTTPException(status_code=404, detail=f"{date_str} 暂无数据")

    wb = Workbook()
    ws = wb.active
    ws.title = "现货出清电量"
    ws.append(["日期", "时刻", "电量(MWh)", "开机台数(台)"])
    for r in data["rows"]:
        ws.append([date_str, r["time"], r["energy"], r["units"]])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"spot_clearance_{date_str}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/market_demand", response_class=HTMLResponse)
async def market_demand(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    date_str = _parse_iso_date(request.query_params.get("date"))
    data = _generate_market_demand(date_str)
    context = {
        "username": request.session.get("user"),
        "token": request.session.get("token"),
        "date": date_str,
        "available": data["available"],
        "hours": data["hours"],
        "demand": data["demand"],
        "actual": data["actual"],
        "deviation": data["deviation"],
    }
    return templates.TemplateResponse(request, "market_demand.html", context)


@app.get("/market_demand/export")
async def market_demand_export(request: Request) -> Response:
    redirect = _require_login(request)
    if redirect is not None:
        return redirect
    date_str = _parse_iso_date(request.query_params.get("date"))
    data = _generate_market_demand(date_str)
    if not data["available"]:
        raise HTTPException(status_code=404, detail=f"{date_str} 暂无数据")

    wb = Workbook()
    ws = wb.active
    ws.title = "市场用电需求"
    header = ["类型"] + data["hours"]
    ws.append(header)
    ws.append(["市场用电需求(MWh)"] + data["demand"])
    ws.append(["市场实际用电(MWh)"] + data["actual"])
    ws.append(["偏差率(%)"] + data["deviation"])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"market_demand_{date_str}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export_monthly")
async def api_export_monthly(request: Request, year: str = Form(...)) -> Response:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth.split(" ", 1)[1].strip()
    expected = request.session.get("token")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        year_int = int(year)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid year")

    rows = _generate_monthly_data(year_int)

    wb = Workbook()
    ws = wb.active
    ws.title = f"{year_int}年月度结算"
    ws.append([
        "月份",
        "市场化电量(兆瓦时)",
        "用户侧电费(万元)",
        "发电侧电费(万元)",
        "平均电价(元/兆瓦时)",
        "同比增长(%)",
        "结算状态",
    ])
    for r in rows:
        ws.append([
            r["month"],
            r["market_energy"],
            r["user_fee"],
            r["gen_fee"],
            r["avg_price"],
            r["yoy_growth"],
            r["settle_status"],
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"monthly_settlement_{year_int}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export")
async def api_export(request: Request, pdate: str = Form(...)) -> Response:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth.split(" ", 1)[1].strip()
    expected = request.session.get("token")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    if request.headers.get("X-Force-Expire", "").lower() == "true":
        raise HTTPException(status_code=412, detail="Token expired")

    rows = _generate_settlement_data(pdate)
    kpi = _generate_kpi_data()

    wb = Workbook()
    ws = wb.active
    ws.title = "日清算数据"
    ws.append(["日期", "指标项", "数值", "单位"])

    ws.append([pdate, "当日市场化电量", kpi["market_energy"], "兆瓦时"])
    ws.append([pdate, "用户侧电费", kpi["user_fee"], "万元"])
    ws.append([pdate, "发电侧电费", kpi["gen_fee"], "万元"])
    ws.append([pdate, "市场盈余", kpi["market_surplus"], "万元"])

    for r in rows:
        ws.append([pdate, f"{r['unit_name']}-机组总上网电量", r["total_grid_energy"], "兆瓦时"])
        ws.append([pdate, f"{r['unit_name']}-代购及跨省外送电量", r["purchase_export_energy"], "兆瓦时"])
        ws.append([pdate, f"{r['unit_name']}-基数电量", r["base_energy"], "兆瓦时"])
        ws.append([pdate, f"{r['unit_name']}-中长期市场化电量", r["mid_long_market_energy"], "兆瓦时"])
        ws.append([pdate, f"{r['unit_name']}-日前偏差电量", r["day_ahead_deviation"], "兆瓦时"])
        ws.append([pdate, f"{r['unit_name']}-实时偏差电量", r["real_time_deviation"], "兆瓦时"])
        ws.append([pdate, f"{r['unit_name']}-用户侧电费", r["user_side_fee"], "万元"])
        ws.append([pdate, f"{r['unit_name']}-发电侧电费", r["gen_side_fee"], "万元"])
        ws.append([pdate, f"{r['unit_name']}-偏差电费", r["deviation_fee"], "万元"])
        ws.append([pdate, f"{r['unit_name']}-不平衡资金", r["imbalance_fund"], "万元"])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"daily_settlement_{pdate}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
