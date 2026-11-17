# -*- coding: utf-8 -*-
"""dry_load 的回归：坏样例必须失败，好样例必须通过，降级必须明说。

这一组用例的意义在于「执行层」：以前所有门禁都不运行插件代码，
agent 自报「测过了」没法证伪。
"""
import io
import os

import pytest

TOML_OK = ("[plugin]\n"
           'id = "demo_plugin"\n'
           'version = "1.0.0"\n'
           'entry = "main:DemoPlugin"\n'
           'author = "t"\n'
           'name = "n"\n'
           'description = "d"\n'
           "\n[plugin_runtime]\n"
           "auto_start = true\n")
SRC_OK = """from plugin.sdk.plugin import NekoPluginBase, neko_plugin, llm_tool


@neko_plugin
class DemoPlugin(NekoPluginBase):
    async def on_load(self):
        return 1

    @llm_tool(name='t')
    async def t(self):
        return 2
"""


def _pkg(root, name, toml, files):
    d = os.path.join(str(root), name)
    os.makedirs(d, exist_ok=True)
    io.open(os.path.join(d, "plugin.toml"), "w", encoding="utf-8", newline="\n").write(toml)
    for fn, body in files.items():
        p = os.path.join(d, fn.replace("/", os.sep))
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        io.open(p, "w", encoding="utf-8", newline="\n").write(body)
    return d


def test_好样例必须退_0(dry_load, tmp_path):
    d = _pkg(tmp_path, "demo_plugin", TOML_OK, {"main.py": SRC_OK})
    code, rep = dry_load.run(d)
    assert code == 0, rep["problems"]
    assert rep["mode"] == "ast-fallback"
    assert rep.get("note"), "降级模式必须明写它证明不了能 import"


def test_仓内对照样例保持一好一坏(dry_load, repo_root):
    base = os.path.join(repo_root, "tests", "fixtures", "plugins")
    ok_code, _r1 = dry_load.run(os.path.join(base, "demo_ok"))
    bad_code, r2 = dry_load.run(os.path.join(base, "demo_broken"))
    assert ok_code == 0
    assert bad_code == 1
    assert any("GhostPlugin" in x for x in r2["problems"]), r2


@pytest.mark.parametrize("kind", [
    "syntax", "no_symbol", "not_async", "bad_entry_shape", "type_removed",
    "host_section", "id_mismatch", "missing_toml", "relative_import",
])
def test_九种坏形状各自必须失败(dry_load, tmp_path, kind):
    d = None
    if kind == "syntax":
        d = _pkg(tmp_path, "demo_plugin", TOML_OK, {"main.py": "def broken(:\n"})
    elif kind == "no_symbol":
        d = _pkg(tmp_path, "demo_plugin", TOML_OK,
                 {"main.py": SRC_OK.replace("class DemoPlugin", "class ElsePlugin")})
    elif kind == "not_async":
        d = _pkg(tmp_path, "demo_plugin", TOML_OK,
                 {"main.py": SRC_OK.replace("    async def t(self)", "    def t(self)")})
    elif kind == "bad_entry_shape":
        d = _pkg(tmp_path, "demo_plugin", TOML_OK.replace('entry = "main:DemoPlugin"',
                                                          'entry = "main.DemoPlugin"'),
                 {"main.py": SRC_OK})
    elif kind == "type_removed":
        d = _pkg(tmp_path, "demo_plugin",
                 TOML_OK.replace('version = "1.0.0"', 'version = "1.0.0"\ntype = "extension"'),
                 {"main.py": SRC_OK})
    elif kind == "host_section":
        d = _pkg(tmp_path, "demo_plugin", TOML_OK + "\n[plugin.host]\nfoo = 1\n",
                 {"main.py": SRC_OK})
    elif kind == "id_mismatch":
        d = _pkg(tmp_path, "other_dir", TOML_OK, {"main.py": SRC_OK})
    elif kind == "relative_import":
        d = _pkg(tmp_path, "demo_plugin", TOML_OK,
                 {"main.py": SRC_OK + "\nfrom . import ghost\n"})
    else:  # missing_toml
        d = str(tmp_path / "empty_dir")
        os.makedirs(d, exist_ok=True)
        io.open(os.path.join(d, "main.py"), "w", encoding="utf-8").write("x = 1\n")
    code, rep = dry_load.run(d)
    assert code == 1, (kind, code, rep.get("problems"))
    assert rep["problems"], kind


def test_目录不存在是用法错误不是发现问题(dry_load, tmp_path):
    code, rep = dry_load.run(os.path.join(str(tmp_path), "nope"))
    assert code == 2
    assert rep["mode"] == "usage"


def test_内置插件的包写法不能被误判(dry_load, tmp_path):
    """本机 11 个内置件都用 plugin.plugins.<id>:Class；旧判据把它们全判成坏插件。"""
    toml = TOML_OK.replace('entry = "main:DemoPlugin"',
                           'entry = "plugin.plugins.demo_plugin:DemoPlugin"')
    d = _pkg(tmp_path, "demo_plugin", toml,
             {"__init__.py": SRC_OK, "main.py": SRC_OK})
    code, rep = dry_load.run(d)
    assert code == 0, rep["problems"]


def test_包写法第二段与目录名不符必须失败(dry_load, tmp_path):
    toml = TOML_OK.replace('entry = "main:DemoPlugin"',
                           'entry = "plugin.plugins.other_name:DemoPlugin"')
    d = _pkg(tmp_path, "demo_plugin", toml, {"main.py": SRC_OK})
    code, rep = dry_load.run(d)
    assert code == 1
    assert any("host.py:664" in x or "目录名" in x for x in rep["problems"]), rep["problems"]


def test_真装载那条腿在_CI_上确实跑到(dry_load, tmp_path, monkeypatch):
    """假 SDK 里放一个同签名的 _import_plugin_module，真装载分支就有覆盖。"""
    import importlib
    import subprocess
    import sys
    import tempfile
    import shutil as _sh

    fake = tempfile.mkdtemp(prefix="dryload-pytest-")
    try:
        core = os.path.join(fake, "plugin", "core")
        sdk = os.path.join(fake, "plugin", "sdk", "plugin")
        os.makedirs(core)
        os.makedirs(sdk)
        for p in (os.path.join(fake, "plugin", "__init__.py"),
                  os.path.join(core, "__init__.py"),
                  os.path.join(fake, "plugin", "sdk", "__init__.py")):
            io.open(p, "w", encoding="utf-8").write("")
        io.open(os.path.join(sdk, "__init__.py"), "w", encoding="utf-8", newline="\n").write(
            "class NekoPluginBase(object):\n    pass\n\n\n"
            "def neko_plugin(cls=None, **k):\n"
            "    return cls if cls is not None else (lambda c: c)\n\n\n"
            "def llm_tool(*a, **k):\n    return lambda f: f\n")
        io.open(os.path.join(core, "host.py"), "w", encoding="utf-8", newline="\n").write(
            "import importlib, os, sys\n\n\n"
            "def _import_plugin_module(module_path, config_path, logger, *, source_only=False):\n"
            "    sys.path.insert(0, str(os.path.dirname(str(config_path))))\n"
            "    return importlib.import_module(module_path)\n")
        d = _pkg(fake, "demo_plugin", TOML_OK, {"main.py": SRC_OK})
        code, rep = dry_load.run(d, py=sys.executable, repo=fake)
        assert rep["mode"] == "real-import", rep
        assert code == 0, rep["problems"]
        # 入口符号被改名时，AST 层就该拦住
        io.open(os.path.join(d, "main.py"), "w", encoding="utf-8", newline="\n").write(
            SRC_OK.replace("class DemoPlugin", "class GonePlugin"))
        code2, rep2 = dry_load.run(d, py=sys.executable, repo=fake)
        assert code2 == 1, rep2
    finally:
        _sh.rmtree(fake, ignore_errors=True)
