"""任务开始前只留一个工作台，执行结束后关掉本次打开的业务页。"""

from __future__ import annotations

import pytest
from task_runner import TaskRunner
from workbench_tabs import choose_workbench, page_role


class _Tab:
    def __init__(self, url: str, visible: set[str] | None = None) -> None:
        self.url = url
        self.visible = visible or set()
        self.closed = False
        self.front = 0
        self.context = None

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.front += 1

    async def evaluate(self, _script, arg=None):
        if isinstance(arg, dict) and arg.get("op") == "visible":
            return arg.get("selector") in self.visible
        return False


class _Context:
    def __init__(self, pages: list[_Tab]) -> None:
        self.pages = pages
        for page in pages:
            page.context = self


def test_page_role_splits_workbench_business_and_user_pages():
    assert page_role("https://pm.gd.csg.cn/portal/#/powerEnterprise") == "workbench"
    assert page_role("https://pm.gd.csg.cn/pfjs/stc/qctc-settle") == "business"
    assert page_role("https://example.com/notes") == "other"
    assert choose_workbench([("shell", 0), ("menu", 2)]) == "menu"


@pytest.mark.unit
async def test_prepare_keeps_the_logged_in_workbench_and_closes_leftovers():
    good = _Tab(
        "https://pm.gd.csg.cn/portal/#/powerEnterprise",
        {"text=结算管理", "text=退出登录"},
    )
    shell = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    business = _Tab("https://pm.gd.csg.cn/pfxh/stc/index")
    user = _Tab("https://example.com/notes")
    runner = TaskRunner({"name": "公有"})
    runner._context = _Context([shell, business, good, user])
    runner._shared_pages = [business]
    runner._shared_session = type("S", (), {})()

    await runner._prepare_browser()

    assert shell.closed is True
    assert business.closed is True
    assert good.closed is False
    assert user.closed is False
    assert runner._workbench_page is good
    assert runner._shared_pages == [good]
    assert good.front == 1
    assert user in runner._kept_pages
    assert business not in runner._kept_pages


@pytest.mark.unit
async def test_finish_closes_only_pages_opened_by_this_run():
    workbench = _Tab(
        "https://pm.gd.csg.cn/portal/#/powerEnterprise",
        {"text=结算管理", "text=退出登录"},
    )
    user = _Tab("https://pm.gd.csg.cn/portal/#/home/informationNotice")
    opened = _Tab("https://pm.gd.csg.cn/pfjy/stc/index")
    runner = TaskRunner({"name": "公有"})
    runner._context = _Context([workbench, user, opened])
    runner._kept_pages = {workbench, user}
    runner._protected_pages = {workbench, user}
    runner._workbench_page = workbench
    runner._shared_pages = [opened]
    runner._shared_session = type("S", (), {})()

    await runner._finish_indicator_pages()

    assert opened.closed is True
    assert workbench.closed is False
    assert user.closed is False
    assert runner._shared_pages == [workbench]
    assert workbench.front == 1


@pytest.mark.unit
async def test_prepare_does_not_turn_a_business_page_into_the_portal():
    business = _Tab("https://pm.gd.csg.cn/pfjs/stc/daily")
    created: list[_Tab] = []

    class Context(_Context):
        async def new_page(self) -> _Tab:
            page = _Tab("about:blank")
            created.append(page)
            self.pages.append(page)
            page.context = self
            return page

    runner = TaskRunner({"name": "公有"})
    runner._context = Context([business])
    runner._shared_pages = [business]
    runner._shared_session = type("S", (), {})()

    await runner._prepare_browser()

    assert business.closed is True
    assert created
    assert runner._shared_pages == [created[0]]
    assert runner._workbench_page is None
    assert created[0].url == "about:blank"
