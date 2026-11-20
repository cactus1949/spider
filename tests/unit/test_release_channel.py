"""正式/测试身份、冻结元数据与构建渠道拒绝策略。"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from spider_gd.runtime_info import runtime_identity

ROOT = Path(__file__).resolve().parents[2]


def release_module():
    assert (ROOT / 'src/spider_gd/release_channel.py').is_file(), '缺失跨构建/运行时的发布渠道契约'
    return importlib.import_module('spider_gd.release_channel')


def test_source_runtime_reports_explicit_stable_channel(monkeypatch):
    monkeypatch.delenv('SPIDER_GD_CHANNEL', raising=False)
    assert runtime_identity().get('channel') == 'stable', '健康身份必须报告渠道而不是仅有版本'


def test_build_version_validation_and_contract():
    module = release_module()
    beta = module.build_release('0.2.8', 'beta', 3)
    assert beta['version'] == '0.2.8-beta.3'
    assert beta['serviceName'] == 'spider-gd-service-beta'
    assert beta['preferredPort'] == 8801
    assert module.build_release('0.2.8')['version'] == '0.2.8'
    for channel, number in [('wrong', None), ('beta', None), ('beta', 0), ('beta', -1), ('beta', '1.5'), ('beta', '01'), ('beta', True), ('beta', '1٢'), ('stable', 1)]:
        with pytest.raises(ValueError, match='channel|BetaNumber'):
            module.build_release('0.2.8', channel, number)
    for version in ('0.2.8-beta.1', '0.٢.8', '01.2.8'):
        with pytest.raises(ValueError, match='VERSION'):
            module.build_release(version)


@pytest.mark.parametrize('channel,version,opposite', [('stable', '0.2.8', 'beta'), ('beta', '0.2.8-beta.7', 'stable')])
def test_frozen_identity_ignores_runtime_channel_env(channel, version, opposite, monkeypatch):
    module = release_module()
    from spider_gd import build_stamp
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(build_stamp, 'RELEASE_CHANNEL', channel, raising=False)
    monkeypatch.setattr(build_stamp, 'RELEASE_VERSION', version, raising=False)
    monkeypatch.setenv('SPIDER_GD_CHANNEL', opposite)
    assert module.runtime_release()['channel'] == channel
    assert module.runtime_release()['version'] == version


def test_beta_home_defaults_independently_without_migrating_stable_data(tmp_path, monkeypatch):
    from spider_gd.config.user_home import prepare_user_home, user_home
    monkeypatch.setenv('SPIDER_GD_CHANNEL', 'beta')
    monkeypatch.delenv('SPIDER_GD_HOME', raising=False)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'appdata'))
    monkeypatch.setattr(sys, 'platform', 'win32')
    home = user_home()
    assert home == tmp_path / 'appdata' / 'spider-gd-desktop-beta', 'beta 未设置 HOME 时也不能读正式数据'
    stable = tmp_path / 'stable-install'
    (stable / 'data').mkdir(parents=True)
    (stable / 'data' / 'spider.db').write_text('stable secret', encoding='utf-8')
    assert prepare_user_home(stable) == home
    assert not (home / 'data' / 'spider.db').exists()


@pytest.mark.parametrize('channel,port', [('stable', 8800), ('beta', 8801)])
def test_noneditable_wheel_loads_channel_and_settings_outside_repository(tmp_path, channel, port):
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copy2(ROOT / 'pyproject.toml', source)
    shutil.copy2(ROOT / 'README.md', source)
    shutil.copytree(ROOT / 'src', source / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    built = subprocess.run(
        [sys.executable, '-c', 'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])', str(wheels)],
        cwd=source, capture_output=True, env={**os.environ, 'PIP_NO_INDEX': '1'},
    )
    assert built.returncode == 0, built.stderr.decode('utf-8', errors='replace')
    wheel, = wheels.glob('*.whl')
    unpacked = tmp_path / 'unpacked'
    with ZipFile(wheel) as archive:
        assert 'spider_gd/release-channels.json' in archive.namelist(), '普通 wheel 必须携带包内渠道契约'
        assert archive.namelist().count('spider_gd/release-channels.json') == 1
        assert 'release-channels.json' not in archive.namelist()
        archive.extractall(unpacked)
    cwd = tmp_path / 'outside-repository'
    cwd.mkdir()
    (cwd / 'mode.yaml').write_text('web:\n  host: 127.0.0.1\n', encoding='utf-8')
    (cwd / 'override.yaml').write_text('web:\n  port: 9123\n', encoding='utf-8')
    env = {key: value for key, value in os.environ.items() if key not in ('WEB__PORT', 'PORT')}
    env.update(PYTHONPATH=str(unpacked), SPIDER_GD_CHANNEL=channel, SPIDER_GD_HOME=str(tmp_path / 'home'))
    code = '''
import json
import os
import sys
from pathlib import Path
import spider_gd
import spider_gd.config.settings as settings
from spider_gd.release_channel import runtime_release
unpacked = Path(sys.argv[1])
assert Path(spider_gd.__file__).is_relative_to(unpacked)
assert Path(settings.__file__).is_relative_to(unpacked)
release = runtime_release()
default = settings.load_config('mode.yaml').web.port
override = settings.load_config('override.yaml').web.port
os.environ['WEB__PORT'] = '9234'
env_port = settings.load_config('mode.yaml').web.port
print(json.dumps({'channel': release['channel'], 'preferredPort': release['preferredPort'], 'default': default, 'yaml': override, 'env': env_port}))
'''
    loaded = subprocess.run([sys.executable, '-P', '-c', code, str(unpacked)], cwd=cwd, env=env, capture_output=True)
    assert loaded.returncode == 0, loaded.stderr.decode('utf-8', errors='replace')
    assert json.loads(loaded.stdout) == {'channel': channel, 'preferredPort': port, 'default': port, 'yaml': 9123, 'env': 9234}


def test_freeze_stages_version_and_channel_without_changing_source(tmp_path, monkeypatch):
    import build_backend
    release = release_module().build_release('0.2.8', 'beta', 5)
    stage = getattr(build_backend, 'stage_release_data', None)
    assert callable(stage), '冻结必须暂存选定 VERSION 和渠道 JSON，不能复用正式 VERSION'
    source_version = (ROOT / 'VERSION').read_bytes()
    pairs = stage(release, tmp_path)
    files = {Path(dest) / Path(src).name: Path(src) for src, dest in pairs}
    assert set(files) == {Path('VERSION'), Path('spider_gd/release-channels.json')}, '冻结契约必须放在 spider_gd 包资源目录'
    assert files[Path('VERSION')].read_text(encoding='utf-8').strip() == '0.2.8-beta.5'
    assert json.loads(files[Path('spider_gd/release-channels.json')].read_text(encoding='utf-8'))['beta']['serviceName'] == 'spider-gd-service-beta'
    assert (ROOT / 'VERSION').read_bytes() == source_version
    original = build_backend.install_build_stamp('abc123def456', release=release)
    try:
        spec = importlib.util.spec_from_file_location('frozen_stamp_fixture', build_backend.PACKAGE / 'build_stamp.py')
        stamp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stamp)
        assert stamp.BUILD_ID == 'abc123def456'
        assert stamp.RELEASE_CHANNEL == 'beta'
        assert stamp.RELEASE_VERSION == '0.2.8-beta.5'
    finally:
        build_backend.restore_build_stamp(original)


@pytest.mark.parametrize('channel,version', [('stable', '0.2.8'), ('beta', '0.2.8-beta.7')])
def test_frozen_package_version_uses_bound_stamp_not_replaced_version(channel, version, monkeypatch, tmp_path):
    import spider_gd
    from spider_gd import build_stamp
    (tmp_path / 'VERSION').write_text('9.9.9', encoding='utf-8')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(build_stamp, 'RELEASE_CHANNEL', channel)
    monkeypatch.setattr(build_stamp, 'RELEASE_VERSION', version)
    assert spider_gd.read_version() == version, '冻结版本不能被磁盘 VERSION 替换'


@pytest.mark.parametrize('args', [
    ['--channel', 'typo'], ['--channel', 'beta'],
    ['--channel', 'beta', '--beta-number', '0'],
    ['--channel', 'stable', '--beta-number', '1'],
])
def test_invalid_backend_channel_is_rejected_before_dependency_checks(args):
    proc = subprocess.run([sys.executable, str(ROOT / 'build_backend.py'), *args], capture_output=True)
    assert proc.returncode != 0
    assert b'channel' in proc.stderr.lower(), '错误渠道应在调用 PyInstaller/安装依赖前拒绝'
    assert b'PyInstaller' not in proc.stderr


@pytest.mark.parametrize('channel,number,name,version', [
    ('stable', None, 'spider-gd-service', '0.2.8'),
    ('beta', 4, 'spider-gd-service-beta', '0.2.8-beta.4'),
])
def test_freeze_command_uses_selected_name_and_runs_checks(tmp_path, monkeypatch, channel, number, name, version):
    import build_backend
    release = release_module().build_release('0.2.8', channel, number)
    output = tmp_path / 'output'
    monkeypatch.setattr(build_backend, 'OUTPUT', output)
    monkeypatch.setattr(build_backend, 'BUILD', tmp_path / 'work')
    monkeypatch.setattr(build_backend, 'IS_WIN', False)
    monkeypatch.setattr(build_backend, 'EXE_SUFFIX', '')
    monkeypatch.setattr(build_backend, 'current_build_id', lambda: 'abc123def456')
    checked = []
    monkeypatch.setattr(build_backend, 'assert_frozen_modules', lambda exe: checked.append(('modules', exe.name)))
    monkeypatch.setattr(build_backend, 'assert_frozen_ocr', lambda exe: checked.append(('ocr', exe.name)))
    original = (build_backend.PACKAGE / 'build_stamp.py').read_bytes()

    def freeze(cmd, *, check, env):
        assert cmd[cmd.index('--name') + 1] == name
        data = [cmd[i + 1].split(build_backend.os.pathsep)[0] for i, arg in enumerate(cmd) if arg == '--add-data']
        versions = [Path(path) for path in data if Path(path).name == 'VERSION']
        assert len(versions) == 1
        assert versions[0].read_text(encoding='utf-8').strip() == version
        assert ROOT / 'VERSION' not in versions
        (output / name).write_bytes(b'fake executable')
    monkeypatch.setattr(build_backend.subprocess, 'run', freeze)
    build_backend.run_pyinstaller(release)
    assert checked == [('modules', name), ('ocr', name)]
    assert json.loads((output / 'build_stamp.json').read_text(encoding='utf-8')) == {
        'build_id': 'abc123def456', 'channel': channel, 'version': version,
    }
    assert (build_backend.PACKAGE / 'build_stamp.py').read_bytes() == original


@pytest.mark.parametrize('channel,number,version,opposite', [
    ('stable', None, '0.2.8', 'beta'), ('beta', 6, '0.2.8-beta.6', 'stable'),
])
def test_real_frozen_runtime_keeps_channel_and_version(tmp_path, channel, number, version, opposite):
    """本机最小冻结验证，不代表 Windows exe/安装包验证。"""
    import os
    import build_backend
    pytest.importorskip('PyInstaller')
    release = release_module().build_release('0.2.8', channel, number)
    entry = tmp_path / 'entry.py'
    entry.write_text('import json\nfrom spider_gd.runtime_info import runtime_identity\nprint(json.dumps(runtime_identity()))\n', encoding='utf-8')
    cmd = [sys.executable, '-m', 'PyInstaller', '--onefile', '--name', str(release['serviceName']),
           '--distpath', str(tmp_path / 'dist'), '--workpath', str(tmp_path / 'work'),
           '--specpath', str(tmp_path), '--paths', str(build_backend.SRC)]
    for src, dest in build_backend.stage_release_data(release, tmp_path / 'release'):
        cmd += ['--add-data', f'{src}{os.pathsep}{dest}']
    cmd.append(str(entry))
    original = build_backend.install_build_stamp('abc123def456', release=release)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        build_backend.restore_build_stamp(original)
    suffix = '.exe' if sys.platform == 'win32' else ''
    executable = tmp_path / 'dist' / (str(release['serviceName']) + suffix)
    result = subprocess.run([str(executable)], check=True, capture_output=True, env={**os.environ, 'SPIDER_GD_CHANNEL': opposite})
    identity = json.loads(result.stdout)
    assert identity['frozen'] is True
    assert identity['channel'] == channel
    assert identity['spider_gd'] == version
    assert identity['build_id'] == 'abc123def456'
    assert identity['executable'] == str(executable)


def test_failed_freeze_restores_source_stamp(tmp_path, monkeypatch):
    import build_backend
    release = release_module().build_release('0.2.8', 'beta', 9)
    monkeypatch.setattr(build_backend, 'OUTPUT', tmp_path / 'output')
    monkeypatch.setattr(build_backend, 'BUILD', tmp_path / 'work')
    original = (build_backend.PACKAGE / 'build_stamp.py').read_bytes()

    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(build_backend.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_backend.run_pyinstaller(release)
    assert (build_backend.PACKAGE / 'build_stamp.py').read_bytes() == original

