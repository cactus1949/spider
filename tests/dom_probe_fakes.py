"""低痕迹输入路径（pick_date / captcha）的页面替身。

真实浏览器里这两条路径已经不走 locator：几何靠 ``page.evaluate`` 读一次，
落点靠 ``page.mouse`` 走轨迹。替身必须同样只开放这两条通道——
``locator`` / ``page.click`` / ``page.fill`` 一律直接断言失败，
否则实现哪天悄悄退回 locator，测试仍然会绿。

``mouse.up()`` 需要知道点到了谁，所以替身让坐标本身承担身份：
每个 (selector, index) 分到一条固定横坐标，按下抬起时反查回来。
"""

from __future__ import annotations


class _FakeMouse:
    def __init__(self, page: DomProbeFakePage) -> None:
        self._page = page

    async def move(self, x: float, y: float) -> None:
        self._page.pointer = (float(x), float(y))
        self._page.mouse_moves.append((float(x), float(y)))

    async def down(self) -> None:
        self._page.mouse_events.append("down")

    async def up(self) -> None:
        self._page.mouse_events.append("up")
        self._page.release_at(*self._page.pointer)

    async def click(self, x: float, y: float) -> None:
        await self.move(x, y)
        await self.down()
        await self.up()


class _FakeKeyboard:
    def __init__(self, page: DomProbeFakePage) -> None:
        self._page = page

    async def press(self, key: str, **_kwargs) -> None:
        self._page.keys.append(key)

    async def type(self, text: str, **_kwargs) -> None:
        self._page.typed.append(text)


class DomProbeFakePage:
    """只认 DOM 探测脚本 + mouse/keyboard 的页面替身。

    子类覆盖 ``probe_visible`` / ``probe_texts`` / ``probe_count`` /
    ``probe_attr`` / ``apply_click`` 描述自己的页面状态。
    """

    # 两个落点之间留足间隔：_press_at 会在中心附近抖动 ±1.2px。
    SLOT_WIDTH = 100.0

    def __init__(self) -> None:
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard(self)
        self.pointer: tuple[float, float] = (0.0, 0.0)
        self.probes: list[tuple[str, str, int | None]] = []
        self.mouse_moves: list[tuple[float, float]] = []
        self.mouse_events: list[str] = []
        self.keys: list[str] = []
        self.typed: list[str] = []
        self._slots: dict[tuple[str, int | None], int] = {}
        self._slot_owners: dict[int, tuple[str, int | None]] = {}

    # ----- 探测通道 -----

    async def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "op" in arg:
            return self._probe(arg)
        if isinstance(script, str) and "visibilityState" in script:
            return "visible"
        raise AssertionError(f"替身只接受 DOM 探测脚本，收到: {script[:60]!r} arg={arg!r}")

    def _probe(self, arg: dict):
        op = arg["op"]
        selector = arg.get("selector", "")
        index = arg.get("index")
        self.probes.append((op, selector, index))
        if op == "count":
            return self.probe_count(selector)
        if op == "texts":
            return self.probe_texts(selector)
        if op == "visible_texts":
            return [value for position, value in enumerate(self.probe_texts(selector))
                    if self.probe_visible(selector, position)]
        if op == "attr":
            return self.probe_attr(selector, arg.get("name", ""))
        if not self.probe_visible(selector, index):
            return None
        return self._box_for(selector, index)

    def _box_for(self, selector: str, index: int | None) -> dict:
        key = (selector, index)
        slot = self._slots.setdefault(key, len(self._slots) + 1)
        self._slot_owners[slot] = key
        x = slot * self.SLOT_WIDTH
        return {
            "x": x, "y": 40.0, "width": 20.0, "height": 20.0,
            "pageX": x, "pageY": 40.0, "count": self.probe_count(selector),
        }

    def release_at(self, x: float, _y: float) -> None:
        key = self._slot_owners.get(round(x / self.SLOT_WIDTH))
        if key is None:
            raise AssertionError(f"鼠标抬起落在空白处: x={x}")
        self.apply_click(*key)

    # ----- 被禁用的通道 -----

    def locator(self, selector: str):
        raise AssertionError(f"低痕迹路径不得解析 locator: {selector}")

    async def click(self, selector: str, **_kwargs) -> None:
        raise AssertionError(f"低痕迹路径不得调用 page.click: {selector}")

    async def fill(self, selector: str, _value: str, **_kwargs) -> None:
        raise AssertionError(f"低痕迹路径不得调用 page.fill: {selector}")

    async def press(self, selector: str, _key: str, **_kwargs) -> None:
        raise AssertionError(f"低痕迹路径不得调用 page.press: {selector}")

    async def hover(self, selector: str, **_kwargs) -> None:
        raise AssertionError(f"低痕迹路径不得调用 page.hover: {selector}")

    async def wait_for_selector(self, selector: str, **_kwargs) -> None:
        raise AssertionError(f"低痕迹路径不得调用 wait_for_selector: {selector}")

    # ----- 子类描述页面 -----

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        return False

    def probe_texts(self, selector: str) -> list[str]:
        return []

    def probe_count(self, selector: str) -> int:
        return 1 if self.probe_visible(selector) else 0

    def probe_attr(self, selector: str, name: str) -> str | None:
        return None

    def apply_click(self, selector: str, index: int | None) -> None:
        return None
