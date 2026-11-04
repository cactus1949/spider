"""隔离元素定位和几何读取；只刷新验证码，不填写凭据或提交登录。

每轮重载现有登录页；显式传入 --reset-url 时先清 Cookie 再刷新。
动作前必须收到验证码 HTTP 200，
否则停止整个实验；动作后异常也停止当前轮，下一轮重新验证基线。
结果只记录协议方法、响应形态和页面路径，不保存签名、cookie 或正文。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

CASES = (
    "noop",
    "handle",
    "evalbox",
    "rawquery",
    "rawresolve",
    "rawbox",
    "rawobjectbox",
    "handlebox",
    "locatorbox",
    "rawscroll",
    "locatorscroll",
    "hover",
    "evalhandlebox",
    "rawjsbox",
    "rawisolatedbox",
    "handlewait",
    "evalhandle",
    "evalobject",
)


@contextmanager
def diagnostic_driver(disable_preview: bool):
    """仅在临时 driver 副本中关闭预览初始化；不改已安装的依赖。"""
    if not disable_preview:
        yield "stock"
        return
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    source = Path(cli).parent
    bundle = source / "lib" / "coreBundle.js"
    original = bundle.read_text(encoding="utf-8")
    invocation = "this._initializePreview().catch((e) => {\n        });"
    if original.count(invocation) != 1:
        raise RuntimeError(
            "Unsupported driver: expected exactly one preview constructor invocation"
        )
    with tempfile.TemporaryDirectory(prefix="spider-geometry-driver-") as directory:
        destination = Path(directory) / "package"
        shutil.copytree(source, destination)
        (destination / "lib" / "coreBundle.js").write_text(
            original.replace(invocation, "/* diagnostic: skip element preview */", 1),
            encoding="utf-8",
        )
        with patch(
            "playwright._impl._transport.compute_driver_executable",
            return_value=(node, str(destination / "cli.js")),
        ):
            yield "temporary_copy_without_element_preview"


TARGET_JS = """({selector, index}) => {
    const node = document.querySelectorAll(selector)[index];
    if (!node) throw new Error('target_missing');
    const r = node.getBoundingClientRect();
    return {x:r.x, y:r.y, width:r.width, height:r.height};
}"""


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


async def apply_probe(page, session, case: str, selector: str, index: int) -> list[str]:
    """返回本轮主动使用的 API；原始 CDP 路径不解析 Playwright locator。"""
    if case not in CASES:
        raise ValueError(f"Unknown case: {case}")
    if case == "noop":
        return []
    if case == "evalbox":
        await page.evaluate(TARGET_JS, {"selector": selector, "index": index})
        return ["page.evaluate(getBoundingClientRect)"]
    if case in ("evalhandlebox", "evalhandle", "evalobject"):
        handle = await page.evaluate_handle(
            (
                "() => ({probe:true})"
                if case == "evalobject"
                else "({selector,index}) => document.querySelectorAll(selector)[index]"
            ),
            {"selector": selector, "index": index},
        )
        try:
            if case == "evalhandlebox":
                element = handle.as_element()
                if element is None:
                    raise RuntimeError("target_missing")
                await element.bounding_box()
            else:
                await asyncio.sleep(0.5)
        finally:
            await handle.dispose()
        return (
            ["page.evaluate_handle", "ElementHandle.bounding_box"]
            if case == "evalhandlebox"
            else ["page.evaluate_handle", "wait_500ms"]
        )
    if case.startswith("raw"):
        methods = []

        async def send(method, **params):
            methods.append(method)
            return await session.send(method, params)

        if case == "rawjsbox":
            resolved = await send(
                "Runtime.evaluate",
                expression=(f"document.querySelectorAll({json.dumps(selector)})[{index}]"),
            )
            object_id = resolved["result"]["objectId"]
            try:
                await send("DOM.getBoxModel", objectId=object_id)
            finally:
                await send("Runtime.releaseObject", objectId=object_id)
            return methods

        root = await send("DOM.getDocument", depth=0)
        found = await send("DOM.querySelectorAll", nodeId=root["root"]["nodeId"], selector=selector)
        if len(found["nodeIds"]) <= index:
            raise RuntimeError("target_missing")
        node_id = found["nodeIds"][index]
        if case == "rawquery":
            return methods
        if case in ("rawresolve", "rawobjectbox", "rawisolatedbox"):
            params = {"nodeId": node_id}
            if case == "rawisolatedbox":
                tree = await send("Page.getFrameTree")
                world = await send(
                    "Page.createIsolatedWorld",
                    frameId=tree["frameTree"]["frame"]["id"],
                    worldName="spider-gd-geometry-probe",
                )
                params["executionContextId"] = world["executionContextId"]
            resolved = await send("DOM.resolveNode", **params)
            object_id = resolved["object"]["objectId"]
            try:
                if case in ("rawobjectbox", "rawisolatedbox"):
                    await send("DOM.getBoxModel", objectId=object_id)
            finally:
                await send("Runtime.releaseObject", objectId=object_id)
        elif case == "rawbox":
            await send("DOM.getBoxModel", nodeId=node_id)
        elif case == "rawscroll":
            await send("DOM.scrollIntoViewIfNeeded", nodeId=node_id)
        return methods
    target = page.locator(selector).nth(index)
    if case in ("handle", "handlebox", "handlewait"):
        handle = await target.element_handle(timeout=3000)
        if handle is None:
            raise RuntimeError("target_missing")
        try:
            if case == "handlebox":
                await handle.bounding_box()
            elif case == "handlewait":
                await asyncio.sleep(0.5)
        finally:
            await handle.dispose()
    elif case == "locatorbox":
        await target.bounding_box(timeout=3000)
    elif case == "locatorscroll":
        await target.scroll_into_view_if_needed(timeout=3000)
    elif case == "hover":
        await target.hover(timeout=3000)
    return [case]


async def refresh_captcha(page, session, selector: str, response_path: str, timeout: float) -> dict:
    """所有实验共用 JS 定位 + 固定坐标输入，不通过 locator 发起验证请求。"""
    box = await page.evaluate(TARGET_JS, {"selector": selector, "index": 0})
    if box["width"] <= 0 or box["height"] <= 0:
        return {"state": "captcha_not_visible"}
    loop = asyncio.get_running_loop()
    finished = loop.create_future()
    pending: set[str] = set()
    responses: dict[str, dict] = {}

    def matches(url):
        return urlsplit(url).path == response_path

    def on_request(event):
        if matches(event["request"]["url"]):
            pending.add(event["requestId"])

    def on_response(event):
        response = event["response"]
        if event["requestId"] in pending and matches(response["url"]):
            responses[event["requestId"]] = {
                "state": "response",
                "status": response["status"],
                "mime": response.get("mimeType"),
                "header_names": sorted(name.lower() for name in response.get("headers", {})),
            }

    def on_finished(event):
        request_id = event["requestId"]
        if request_id in responses and not finished.done():
            finished.set_result((request_id, responses[request_id]))

    def on_failed(event):
        if event["requestId"] in pending and not finished.done():
            finished.set_result((None, {"state": "network_error", "error": event.get("errorText")}))

    listeners = {
        "Network.requestWillBeSent": on_request,
        "Network.responseReceived": on_response,
        "Network.loadingFinished": on_finished,
        "Network.loadingFailed": on_failed,
    }
    for event, callback in listeners.items():
        session.on(event, callback)
    try:
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await asyncio.sleep(0.25)
        await session.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        try:
            await asyncio.sleep(0.2)
        finally:
            await session.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": 0,
                    "clickCount": 1,
                },
            )
        try:
            request_id, result = await asyncio.wait_for(finished, timeout)
        except TimeoutError:
            return {"state": "timeout", "request_seen": bool(pending)}
        if request_id:
            try:
                body = await asyncio.wait_for(
                    session.send("Network.getResponseBody", {"requestId": request_id}), timeout
                )
                raw = (
                    base64.b64decode(body["body"])
                    if body.get("base64Encoded")
                    else body["body"].encode("utf-8")
                )
                result.update(body_size=len(raw), body_blank=not raw.strip())
            except Exception as exc:
                result["body_error"] = type(exc).__name__
        return result
    finally:
        for event, callback in listeners.items():
            session.remove_listener(event, callback)


async def run_round(page, session, case: str, args) -> dict:
    result = {
        "at": datetime.now(UTC).isoformat(),
        "case": case,
        "page": safe_url(page.url),
        "outcome": "inconclusive",
    }

    def observe_captcha(event):
        response = event["response"]
        if urlsplit(response["url"]).path == args.response_path:
            result.setdefault("captcha_responses", []).append(
                {
                    "status": response["status"],
                    "mime": response.get("mimeType"),
                    "stage": result.get("stage"),
                }
            )

    session.on("Network.responseReceived", observe_captcha)
    try:
        if getattr(args, "reset_url", None):
            result["stage"] = "reset"

            def reset():
                request = urllib.request.Request(args.reset_url, method="POST")
                with urllib.request.urlopen(request, timeout=40) as response:
                    return json.load(response)

            reset_result = await asyncio.to_thread(reset)
            if reset_result.get("status") != "ok":
                result["stop"] = "reset_failed"
                return result
            result["reset"] = "cookies_and_reload"
        else:
            result["stage"] = "reload"
            await page.reload(wait_until="domcontentloaded", timeout=args.timeout * 1000)
        result["stage"] = "front"
        await page.bring_to_front()
        result["stage"] = "captcha_ready"
        # 使用与测量相同的 evaluate 路径，不引入 wait_for_function 的页面内轮询。
        deadline = asyncio.get_running_loop().time() + args.timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("captcha_not_ready")
            ready = await asyncio.wait_for(
                page.evaluate(
                    "s => {const i=document.querySelector(s);"
                    "return !!(i && i.complete && i.naturalWidth>0)}",
                    args.captcha_selector,
                ),
                remaining,
            )
            if ready:
                break
            await asyncio.sleep(min(0.25, remaining))
        await asyncio.sleep(args.settle)
        tree = await session.send("Page.getFrameTree")
        result["loader_id"] = tree["frameTree"]["frame"]["loaderId"]
        result["stage"] = "baseline"
        result["baseline"] = await refresh_captcha(
            page, session, args.captcha_selector, args.response_path, args.timeout
        )
        if result["baseline"].get("status") != 200:
            result["stop"] = "baseline_unavailable"
            return result
        await asyncio.sleep(args.interval)
        result["stage"] = "probe"
        result["methods"] = await apply_probe(page, session, case, args.selector, args.index)
        result["stage"] = "after"
        result["after"] = await refresh_captcha(
            page, session, args.captcha_selector, args.response_path, args.timeout
        )
        after = result["after"]
        if after.get("state") != "response":
            result["stop"] = "network_unavailable"
        elif after.get("status") == 200:
            result["outcome"] = "200_after_probe"
        elif (
            after.get("status") == 400
            and after.get("mime") == "text/html"
            and after.get("body_blank") is True
            and after.get("body_size") == 6
        ):
            result["outcome"] = "observed_400_shape"
        else:
            result["stop"] = "unexpected_response"
    except Exception as exc:
        # 异常正文可能包含完整请求地址，只记录异常类，现场诊断另行保留。
        result.update(stop="probe_error", error=type(exc).__name__)
    finally:
        session.remove_listener("Network.responseReceived", observe_captcha)
    return result


async def main(args) -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(args.endpoint, timeout=args.timeout * 1000)
        candidates = [
            page
            for context in browser.contexts
            for page in context.pages
            if urlsplit(page.url).netloc == args.host
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"Expected exactly one target page; found {len(candidates)}")
        page = candidates[0]
        if args.host == "pm.gd.csg.cn" and urlsplit(page.url).fragment != "/user/login":
            raise RuntimeError("Geometry probes must start on the login page")
        session = await page.context.new_cdp_session(page)
        await session.send("Network.enable")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        observed_rejection = False
        with args.output.open("a", encoding="utf-8") as output:
            try:
                for repeat in range(args.repeat):
                    for case in args.cases:
                        result = await run_round(page, session, case, args)
                        result["repeat"] = repeat + 1
                        result["environment"] = {
                            "playwright": version("playwright"),
                            "browser": browser.version,
                            "selector": args.selector,
                            "index": args.index,
                            "driver_variant": getattr(args, "driver_variant", "stock"),
                        }
                        line = json.dumps(result, ensure_ascii=False)
                        output.write(line + "\n")
                        output.flush()
                        print(line, flush=True)
                        observed_rejection |= result["outcome"] == "observed_400_shape"
                        if result.get("stop"):
                            return 2
            finally:
                await session.detach()
        # 不调用 browser.close()，保留用户的专用浏览器。
    return 1 if observed_rejection else 0


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--endpoint", required=True)
    cli.add_argument(
        "--disable-element-preview",
        action="store_true",
        help="诊断专用：在临时 driver 副本中仅关闭元素预览初始化",
    )
    cli.add_argument("--reset-url", help="显式使用软件的清 Cookie 并刷新按钮接口")
    cli.add_argument("--host", default="pm.gd.csg.cn")
    cli.add_argument("--selector", default="span.title")
    cli.add_argument("--index", type=int, default=1, help="CSS 匹配序号，必须与对照对象一致")
    cli.add_argument("--captcha-selector", default="img[src^='data:image']")
    cli.add_argument("--response-path", default="/portal/xhr/sso-auth/verify/captcha")
    cli.add_argument(
        "--cases",
        nargs="+",
        choices=CASES,
        default=[
            "noop",
            "evalbox",
            "rawquery",
            "rawresolve",
            "rawbox",
            "rawobjectbox",
            "handle",
            "handlebox",
            "locatorbox",
        ],
    )
    cli.add_argument("--repeat", type=int, default=3)
    cli.add_argument("--timeout", type=float, default=15)
    cli.add_argument("--settle", type=float, default=3)
    cli.add_argument("--interval", type=float, default=1)
    cli.add_argument("--output", type=Path, default=Path("build/diag/geometry_results.jsonl"))
    return cli


if __name__ == "__main__":
    args = parser().parse_args()
    with diagnostic_driver(args.disable_element_preview) as variant:
        args.driver_variant = variant
        raise SystemExit(asyncio.run(main(args)))
