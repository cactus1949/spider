"""把五份已验证流程同步到界面实际读取的数据库，默认只检查。

例：python tools/sync_verified_workflows.py --apply
更新前导出原配置；按现有 id 更新，不创建重复条目，不覆盖其他流程。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "登陆流程测试", "结算管理-日清算概览导出", "日前节点电价查询导出",
    "实时节点电价查询导出", "现货交易-完整采集(含披露中心)",
)
_FORBIDDEN_INDICATOR_ACTIONS = ("login", "navigate", "reset_site_storage", "captcha", "fill")


def _expect_verified_workflow(name: str, cfg: dict) -> None:
    if cfg.get("name") != name:
        raise ValueError(f"{name}: 名称不符合预期")
    steps = cfg.get("steps") or []
    actions = [step.get("action") for step in steps if isinstance(step, dict)]
    if name == "登陆流程测试":
        if cfg.get("type") != "login":
            raise ValueError(f"{name}: 必须是可执行的登录流程")
        if not {"login", "click", "fill", "captcha"} <= set(actions):
            raise ValueError(f"{name}: 缺少打开登录页、点击、填写或验证码步骤")
        if "reset_site_storage" in actions:
            raise ValueError(f"{name}: 不能在登录链里清站点存储")
        captcha = next(step for step in steps if step.get("action") == "captcha")
        if captcha.get("submit_selector"):
            raise ValueError(f"{name}: 验证码步骤只负责识别和填写")
        captcha_index = steps.index(captcha)
        after_captcha = steps[captcha_index + 1:]
        if not any(step.get("action") == "click" and step.get("selector") == "button:has-text('立即登录')"
                   for step in after_captcha):
            raise ValueError(f"{name}: 验证码之后必须单独点击立即登录")
        if not any(step.get("action") == "dismiss_modal" and step.get("report_error") is True
                   for step in after_captcha):
            raise ValueError(f"{name}: 提交后必须检测错误弹窗并暂停")
        if not any(step.get("action") == "ensure_login" for step in after_captcha):
            raise ValueError(f"{name}: 提交后必须验证登录结果")
        return
    if cfg.get("auth_policy") != "manual":
        raise ValueError(f"{name}: 指标必须保持手工登录复用")
    if not steps or steps[0].get("action") != "ensure_login":
        raise ValueError(f"{name}: 缺少已有登录检查入口")
    if any(action in _FORBIDDEN_INDICATOR_ACTIONS for action in actions):
        raise ValueError(f"{name}: 指标不能再插入自动登录或直接导航")


def synchronize(source: Path, api: str, *, apply: bool = False, backup_dir: Path) -> list[dict]:
    if urlsplit(api).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("只允许同步本机服务")
    with httpx.Client(base_url=api.rstrip('/'), timeout=10, trust_env=False) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        status = get('/api/browser/status')
        if status.get('busy'):
            raise RuntimeError('任务正在执行或暂停，不能替换运行配置')
        catalog = get('/api/workflows')
        planned = []
        for name in NAMES:
            text = (source / f'{name}.yml').read_text(encoding='utf-8')
            cfg = yaml.safe_load(text)
            _expect_verified_workflow(name, cfg)
            matches = [item for item in catalog if item.get('name') == name]
            if len(matches) != 1:
                raise ValueError(f'{name}: 当前数据库应恰好有一条同名配置，实际 {len(matches)} 条')
            workflow_id = matches[0]['id']
            path = '/api/workflows/' + quote(workflow_id, safe='')
            current = get(path)
            planned.append({'id': workflow_id, 'name': name, 'path': path,
                            'old': current, 'yaml_text': text,
                            'changed': current.get('yaml_text') != text,
                            'sha256': hashlib.sha256(text.encode()).hexdigest()})
        if apply:
            backup_dir.mkdir(parents=True, exist_ok=False)
            (backup_dir / 'workflow_configs.json').write_text(
                json.dumps([item['old'] for item in planned], ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            for item in planned:
                # 检查后有人编辑过时停止，不覆盖这份新修改；备份包含此前所有条目。
                if get('/api/browser/status').get('busy'):
                    raise RuntimeError('同步期间出现运行任务，停止后续更新')
                if get(item['path']).get('yaml_text') != item['old'].get('yaml_text'):
                    raise RuntimeError(f"{item['name']}: 检查后已被编辑，请重新检查")
                if item['changed']:
                    response = client.put(item['path'], json={
                        'name': item['name'], 'yaml_text': item['yaml_text'],
                    })
                    response.raise_for_status()
                    if response.json().get('status') != 'ok':
                        raise RuntimeError(f"{item['name']}: 保存未成功")
                if get(item['path']).get('yaml_text') != item['yaml_text']:
                    raise RuntimeError(f"{item['name']}: 保存后的内容不一致")
        return [{key: item[key] for key in ('id', 'name', 'changed', 'sha256')} for item in planned]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / '参考/workflows_export_已验证单个运行通过')
    parser.add_argument('--api', default='http://127.0.0.1:8800')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    backup = ROOT / 'data/backups' / ('verified_workflows_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    result = synchronize(args.source, args.api, apply=args.apply, backup_dir=backup)
    print(json.dumps({'applied': args.apply, 'backup': str(backup) if args.apply else None,
                      'workflows': result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
