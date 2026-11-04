"""通过 CDP 观察主世界元素预览初始化，供真实浏览器回归使用。"""

import asyncio


class MainWorldPreviewDetector:
    def __init__(self, session):
        self.session = session
        self.main_contexts = set()
        self.tasks = []
        self.scripts = []

    async def start(self):
        def context_created(event):
            context = event["context"]
            if context.get("auxData", {}).get("isDefault"):
                self.main_contexts.add(context["id"])

        def script_parsed(event):
            if (
                event.get("executionContextId") in self.main_contexts
                and event.get("length", 0) > 100000
            ):
                self.tasks.append(asyncio.create_task(self.inspect_script(event["scriptId"])))

        self.session.on("Runtime.executionContextCreated", context_created)
        self.session.on("Debugger.scriptParsed", script_parsed)
        await self.session.send("Runtime.enable")
        await self.session.send("Debugger.enable")

    async def inspect_script(self, script_id):
        source = await self.session.send("Debugger.getScriptSource", {"scriptId": script_id})
        if "module.exports.InjectedScript" in source["scriptSource"]:
            self.scripts.append(script_id)

    async def assert_clean(self):
        await asyncio.sleep(0.1)
        if self.tasks:
            await asyncio.gather(*self.tasks)
        assert not self.scripts, "业务操作创建了主世界元素预览脚本，命中现场已复现的 400 路径"
