#!/usr/bin/env python3
"""N.E.K.O 插件运行时沙箱 —— 隔离启动官方插件服务器，用于验证插件真实行为。

设计依据（全部来自 Project-N-E-K-O/N.E.K.O 源码 @ 645da54，2026-11-07 复核，非推测）：
  - 入口：plugin/user_plugin_server.py（uvicorn，standalone 时 manage_lifecycle=True，
    即由它自己扫描 plugin/plugins/ 并 autostart）。autostart 由该参数决定，
    与 NEKO_PLUGIN_HOSTED_BY_AGENT 无关（该变量在 http_app.py 已无读取点）
  - 端口：环境变量 NEKO_USER_PLUGIN_SERVER_PORT，默认取 config.USER_PLUGIN_SERVER_PORT
    （config/network.py:193 = 48916）；standalone 入口被占用时仍会自行向后找并回写
  - 健康检查：GET /health -> {"status": "ok", "time": <iso>}
  - 插件源目录：plugin/plugins/<plugin_id>/，含 plugin.toml（[plugin] id / version）
  - autostart 还需 [plugin_runtime] auto_start = true（省略默认 false），
    且该 id 不在 plugin_autostart_pending.json 的待审批集合里

用法：
  python sandbox.py doctor
  python sandbox.py prepare  --root D:/neko-sandbox
  python sandbox.py install  --root D:/neko-sandbox
  python sandbox.py plugin-add --root D:/neko-sandbox --src D:/path/to/plugin
  python sandbox.py start    --root D:/neko-sandbox
  python sandbox.py status   --root D:/neko-sandbox
  python sandbox.py stop     --root D:/neko-sandbox
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/Project-N-E-K-O/N.E.K.O.git"
# 这两个 -c 只对 Windows 的 schannel 后端有意义。别的平台的 git 会在 http_init() 里直接
# die（fatal: Unsupported SSL backend 'schannel'），clone 和 fetch 全部失败，所以按平台给。
GIT_HTTP_OPTS = (["-c", "http.sslBackend=schannel",
                  "-c", "http.schannelCheckRevoke=false"]
                 if os.name == "nt" else [])
# 刻意不等于上游默认端口（config/network.py:193 USER_PLUGIN_SERVER_PORT=48916）：
# 沙箱要和用户正在跑的 N.E.K.O 并存，撞默认端口只会白等 /health。
DEFAULT_PORT = 43117
# 上游 plugin/settings.py:768-769 对 NEKO_PLUGIN_STARTUP_TIMEOUT > 300 直接 raise ValueError，
# 服务会在 import 阶段就崩，报错离「超时配置写大了」很远。这里按上游上限夹紧。
UPSTREAM_STARTUP_TIMEOUT_MAX = 300.0
STATE_FILE = "sandbox.json"
CONFIG_FILE = "config.json"


def load_config(root: Path) -> dict:
    p = root / CONFIG_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save_config(root: Path, data: dict) -> None:
    (root / CONFIG_FILE).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def repo_dir(root: Path) -> Path:
    """沙箱使用的官方源码目录：优先 config 里登记的外部 checkout，否则 root/repo。"""
    cfg = load_config(root)
    return Path(cfg["repo"]) if cfg.get("repo") else (root / "repo")


def toml_section_value(text: str, section: str, key: str) -> str | None:
    """不依赖 tomllib（3.11+）地读某个 [section] 下的标量值（去引号）。

    存在的理由：早先按「行首是 id」解析插件 id，会误命中 `[plugin.ui]` 或别的段里以 id 开头的键，
    于是把插件复制到错误的目录名下 —— 表现为「服务起来了、/plugins 里没有它」，极难排查。
    """
    current = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current != section or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        value = v.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def toml_section_flag(text: str, section: str, key: str) -> bool | None:
    """读某个 [section] 下的布尔键。

    返回 True/False 表示读到了；None 表示该键没写（上游对 auto_start 的语义是「省略=false」，
    但调用方要能区分「显式 false」和「根本没写」，所以不回落到 False）。
    """
    value = toml_section_value(text, section, key)
    if value is None:
        return None
    return value.lower() in {"true", "1", "yes", "on"}


# ---------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[sandbox] {msg}", flush=True)


def die(msg: str, code: int = 2):
    print(f"[sandbox][ERROR] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def load_state(root: Path) -> dict:
    p = root / STATE_FILE
    if not p.is_file():
        die(f"未找到 {p}，请先 start")
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(root: Path, data: dict) -> None:
    (root / STATE_FILE).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, want: int, tries: int = 40) -> int:
    for p in range(want, want + tries):
        if port_free(host, p):
            return p
    die(f"{host} 上 {want}–{want + tries - 1} 全部被占用")


# 本机代理（HTTP_PROXY=127.0.0.1:xxxx）会把 127.0.0.1 也当外网转发，
# 导致服务明明在跑却报 WinError 10061。探活必须显式绕过代理。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(port: int, path: str, timeout: float = 5.0):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def venv_python(root: Path) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    return root / ".venv" / sub / exe


# ---------------------------------------------------------------- commands
def cmd_doctor(args):
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, ok, detail))

    py_ok = sys.version_info >= (3, 10)
    add("python", py_ok, sys.version.split()[0])
    add("git", bool(shutil.which("git")), shutil.which("git") or "未找到")
    uv = shutil.which("uv")
    add("uv（可选，官方推荐）", uv is not None, uv or "未安装，将回退 pip")
    root = Path(args.root).expanduser().resolve() if args.root else None
    if root:
        free = shutil.disk_usage(root.anchor or root).free / 2**30
        add("磁盘可用", free > 8, f"{free:.1f} GiB（依赖较重，建议 ≥8 GiB）")
    add("端口", port_free("127.0.0.1", args.port), f"127.0.0.1:{args.port}")

    for name, ok, detail in checks:
        print(f"  [{'OK ' if ok else 'WARN'}] {name}: {detail}")
    if not py_ok:                      # 位置耦合曾在这里：checks[0] 一换位就查错项
        die("Python 版本不足（需 ≥3.10）")


def cmd_prepare(args):
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for d in ("state", "logs", "plugins"):
        (root / d).mkdir(exist_ok=True)

    entry_rel = Path("plugin") / "user_plugin_server.py"

    # 模式 A：直接挂载已有的官方 checkout（推荐——用户自己维护、随时 pull）
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        if not (repo / entry_rel).is_file():
            die(f"{repo} 下没有 {entry_rel}，不是 N.E.K.O 源码目录")
        save_config(root, {"repo": str(repo), "mode": "external"})
        log(f"已挂载外部源码: {repo}")
        log("提示：该目录不受沙箱管理，运行前用 `sandbox.py sync` 拉最新")
        if (repo / ".git").is_dir():
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            log(f"当前 HEAD: {head}")
        return

    # 模式 B：沙箱内自建仓库
    repo = root / "repo"
    if (repo / entry_rel).is_file():
        save_config(root, {"repo": str(repo), "mode": "cloned"})
        log(f"仓库已存在，跳过克隆: {repo}")
    else:
        if repo.exists():
            die(f"{repo} 已存在但不是有效仓库，请先清理")
        # 克隆源固定为官方仓库。想挂 fork 或钉版本：自己 clone 到任意目录，再用 --repo 指过来；
        # sync 读的是那个仓库自己的 origin，所以 fork 一样能跟。
        log(f"克隆 {REPO_URL} → {repo}")
        rc = subprocess.call(["git", *GIT_HTTP_OPTS, "clone", "--depth", "1",
                              REPO_URL, str(repo)])
        if rc != 0:
            die("克隆失败（代理/网络）。可先用别的方式取到源码，再用 --repo 指向它")
        save_config(root, {"repo": str(repo), "mode": "cloned"})

    log(f"沙箱根目录: {root}")
    log(f"  仓库  : {repo}")
    log(f"  状态  : {root / 'state'}（HOME 重定向至此，避免污染真实 ~/.neko）")
    log(f"  日志  : {root / 'logs'}")


def _git(repo: Path, *args: str) -> list[str]:
    """Windows 上 schannel 的证书吊销检查会挡住部分镜像源，这里统一关掉。"""
    return ["git", "-C", str(repo), *GIT_HTTP_OPTS, *args]


def cmd_sync(args):
    """拉取官方最新源码。官方迭代快，运行前应同步一次。"""
    root = Path(args.root).expanduser().resolve()
    repo = repo_dir(root)
    if not (repo / ".git").is_dir():
        die(f"{repo} 不是 git 仓库，无法同步（用 --repo 指向有效 checkout）")

    def run(*a: str) -> int:
        return subprocess.call(_git(repo, *a))

    def head():
        return subprocess.run(_git(repo, "rev-parse", "--short", "HEAD"),
                              capture_output=True, text=True).stdout.strip()

    before = head()
    branch = subprocess.run(_git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                            capture_output=True, text=True).stdout.strip() or "main"
    remote = subprocess.run(_git(repo, "remote", "get-url", "origin"),
                            capture_output=True, text=True).stdout.strip()
    log(f"同步 {repo}（分支 {branch}，当前 {before}）")
    if remote:
        log(f"  remote: {remote}")

    if run("fetch", "--all", "--prune") != 0:
        die("git fetch 失败（代理/网络）。源码保持原样，未做改动")

    rc = run("reset", "--hard", f"origin/{branch}") if args.hard else run("pull", "--ff-only")
    if rc != 0:
        die("同步失败：本地有未提交改动或分叉。确认后用 sync --hard 覆盖")

    after = head()
    if before == after:
        log(f"已是最新（{after}）")
    else:
        cnt = subprocess.run(_git(repo, "rev-list", "--count", f"{before}..{after}"),
                             capture_output=True, text=True).stdout.strip()
        log(f"已更新 {before} → {after}（{cnt} 个提交）")
    dirty = subprocess.run(_git(repo, "status", "--porcelain"),
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        log("注意：工作区有未提交改动，沙箱将按当前文件内容运行")


def _uv_env():
    """uv 环境：走 Windows 证书库（等价 truststore），避免代理根证书导致的 SSL 失败。"""
    env = os.environ.copy()
    # UV_SYSTEM_CERTS 是新名，UV_NATIVE_TLS 是旧名（0.13 起弃用），两个都给
    env["UV_SYSTEM_CERTS"] = "1"
    env["UV_NATIVE_TLS"] = "1"
    # 跨盘符（C: -> E:）硬链接会失败，强制拷贝
    env["UV_LINK_MODE"] = "copy"
    env.setdefault("UV_HTTP_TIMEOUT", "120")
    return env


def _uv(*argv, cwd=None):
    return subprocess.call(["uv", *argv], cwd=cwd, env=_uv_env())


def cmd_install(args):
    root = Path(args.root).expanduser().resolve()
    repo = repo_dir(root)
    if not (repo / "requirements.txt").is_file():
        die("仓库不完整，先 prepare")

    py = venv_python(root)
    mode = load_config(root).get("mode", "cloned")
    where = "沙箱 venv（不动用户源码目录）" if mode == "external" else "仓库内 venv"
    uv_ok = bool(shutil.which("uv")) and not args.no_uv

    # 1) 建环境：uv 优先（自动带 pip），回退标准库 venv
    if not py.is_file():
        if uv_ok:
            log(f"创建沙箱 venv（uv --python {args.py_version} --seed）: {root / '.venv'}")
            rc = _uv("venv", "--python", args.python or args.py_version,
                     "--seed", str(root / ".venv"))
            if rc != 0:
                log("uv venv 失败，回退 python -m venv")
                uv_ok = False
        if not py.is_file():
            interp = args.python or sys.executable
            log(f"创建沙箱 venv（解释器 {interp}）: {root / '.venv'}")
            if subprocess.call([interp, "-m", "venv", str(root / ".venv")]) != 0:
                die("venv 创建失败")
    if not py.is_file():
        die(f"venv 创建后仍找不到 {py}")

    if subprocess.call([str(py), "-m", "pip", "--version"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        log("venv 内无 pip，执行 ensurepip")
        subprocess.call([str(py), "-m", "ensurepip", "--upgrade"])

    req = repo / "requirements.txt"
    log(f"安装依赖到{where}：{req}")

    # 2) 安装：uv 优先。cwd=repo 是必须的——requirements 第 3 行是 `-e .`
    if uv_ok:
        log("使用 uv 安装（并行解析 + 预编译轮子，避免源码编译）")
        rc = _uv("pip", "install", "--python", str(py), "-r", str(req), cwd=str(repo))
        if rc == 0:
            log("依赖安装完成（uv）")
            return
        log("uv 安装失败，回退 pip")

    log("使用 pip 安装（较慢；含源码构建的包会拖很久）")
    subprocess.call([str(py), "-m", "pip", "-q", "--disable-pip-version-check",
                     "install", "--upgrade", "pip"])
    rc = subprocess.call([str(py), "-m", "pip", "install",
                          "--disable-pip-version-check", "-r", str(req)], cwd=str(repo))
    if rc != 0:
        if not args.insecure_pip:
            die("依赖安装失败（多为代理根证书导致的 SSL 校验失败）。这里不静默关校验：\n"
                "  首选修证书链，或给 pip / uv 指定 CA（PIP_CERT、UV_NATIVE_TLS）；\n"
                "  确认这台机器可以接受不校验 TLS 的下载后，再显式加 --insecure-pip 重跑。")
        log("警告：--insecure-pip 已指定，对 PyPI 关闭 TLS 校验重试一次（中间人风险自负）")
        rc = subprocess.call([str(py), "-m", "pip", "install",
                              "--disable-pip-version-check",
                              "--trusted-host", "pypi.org",
                              "--trusted-host", "files.pythonhosted.org",
                              "--trusted-host", "pypi.python.org",
                              "-r", str(req)], cwd=str(repo))
    if rc != 0:
        die("依赖安装失败，见上方输出")
    log("依赖安装完成（pip）")


def cmd_plugin_add(args):
    root = Path(args.root).expanduser().resolve()
    src = Path(args.src).expanduser().resolve()
    repo = repo_dir(root)
    if not (src / "plugin.toml").is_file():
        die(f"{src} 下没有 plugin.toml，不是合法插件目录")

    text = (src / "plugin.toml").read_text(encoding="utf-8", errors="replace")
    pid = toml_section_value(text, "plugin", "id")
    if not pid:
        die("plugin.toml 里没解析到 [plugin] id")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pid):
        die(f"plugin.toml 的 [plugin] id「{pid}」不合法：只允许 ^[A-Za-z_][A-Za-z0-9_]*$"
            "（与上游 generator.py 同规则），拒绝以防 rmtree 删到 plugins 之外")

    dest = repo / "plugin" / "plugins" / pid
    if args.link and os.name != "nt":
        if dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src, target_is_directory=True)
        log(f"已软链 {src} -> {dest}")
    else:
        # 先软链装、再不带 --link 重装时，dest 是符号链接：rmtree 会直接报错。
        # 与上面 --link 分支同款处理，cmd_plugin_rm 也是这样判的。
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        log(f"已复制 {src} -> {dest}")
    log(f"插件 id = {pid}")

    # 上游 core/registry.py:1290-1296 —— 没写 [plugin_runtime] auto_start 时默认 false，
    # 于是插件会被扫描到、出现在 /plugins 里，但永远不会 autostart。
    # 这是「服务起来了、插件没跑」最隐蔽的一种成因，在这里提前说清楚。
    auto_start = toml_section_flag(text, "plugin_runtime", "auto_start")
    if auto_start:
        log("[OK] plugin.toml 声明 [plugin_runtime] auto_start = true，会随服务 autostart")
    elif auto_start is None:
        log("[WARN] plugin.toml 没有 [plugin_runtime] auto_start，上游默认按 false 处理 —— "
            "插件会被注册但不会被 autostart；要验 autostart 就在插件里显式写 auto_start = true")
    else:
        log("[WARN] plugin.toml 显式写了 [plugin_runtime] auto_start = false —— "
            "插件会被注册但不会被 autostart；只能走 /plugin/<id>/start 手动拉起")


def classify_plugin_path(target):
    """判断 plugins/<id> 的安装形态。悬空软链必须先认出来。

    旧版先问 is_dir()：源目录被删后，悬空软链的 is_dir() 是 False，命令报「不存在」，
    后面那条 is_symlink() 分支永远走不到，软链清不掉。
    """
    if target.is_symlink():
        return "symlink"
    if target.is_dir():
        return "tree"
    return "absent"


def cmd_plugin_rm(args):
    """撤掉 plugin-add 放进 checkout 的插件目录，保持你的 git 工作区干净。"""
    root = Path(args.root).expanduser().resolve()
    repo = repo_dir(root)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.id):
        die(f"--id「{args.id}」不合法：只允许 ^[A-Za-z_][A-Za-z0-9_]*$，"
            "拒绝以防 rmtree 删到 plugins 之外")
    target = repo / "plugin" / "plugins" / args.id
    kind = classify_plugin_path(target)
    if kind == "absent":
        die(f"{target} 不存在")
    if args.dry_run:
        log(f"[dry-run] 将删除 {target}（{kind}）")
        return
    if kind == "symlink":
        target.unlink()          # shutil.rmtree 拒绝删符号链接，--link 装的要走这条路
        log(f"已删除软链 {target}")
        return
    shutil.rmtree(target)
    log(f"已删除 {target}")
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "plugin/plugins"],
                           capture_output=True, text=True).stdout.strip()
    log("plugin/plugins 现状：" + (dirty.replace("\n", " | ") if dirty else "干净"))


# 宿主环境里的凭据不该进被测第三方插件的进程。按名字形状删，不按名字白名单加：
# 缺一个必需变量会让沙箱以一个看不出关联的错误崩掉，而漏删只影响纵深防御的一层。
# 2026-11-15 扩充：COOKIE / AUTH / SESSION / BEARER / JWT / DSN / SSHKEY / LICEN[CS]E 这类名字以前会被放过。
# 代价是 SSH_AUTH_SOCK 会被命中，所以把它写进 KEEP_EXACT 显式保留，靠 git over SSH 的插件不受影响。
# 按词边界判，所以 LICENSES、LICENSING 不被删；粘连的 LICEN[CS]E（MYLICENCE）仍不删，那是已知残留。
# 粘连的 KEY（PRIVATEKEY、MASTERKEY、SESSIONKEY）2026-11-15 起照删：词表里补了 KEY。
# 代价是 KEYBOARD / KEYMAP 这类键盘变量也会被命中，所以显式写进 KEEP_EXACT 保住。
# 注意 SESSION 一类会被剔掉：插件若靠 SESSION_ID / WT_SESSION 做会话关联，在沙箱里读不到，
# 要关联就把值当参数显式传，别依赖环境继承。
SECRET_ENV_RE = re.compile(
    r"(?i)(^|_)(TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|KEY|APIKEY|CREDENTIAL|CREDENTIALS"
    r"|COOKIE|AUTH|AUTHORIZATION|SESSION|BEARER|JWT|DSN|SSHKEY|KEYCHAIN|LICEN[CS]E)($|_)")
# PWD 单独放行：shell 的当前目录变量，删了会让插件里的相对路径调试摸不着头脑。
# 但 MYSQL_PWD / DB_PWD 这类「前缀 + PWD」是真口令，照删。
# SSH_AUTH_SOCK 是 ssh-agent 的 socket 路径，不是口令本身；删了 git+ssh 就找不到 agent。
KEEP_EXACT = {"PWD", "SSH_AUTH_SOCK", "KEYBOARD", "KEYMAP"}
# 上面按分隔符切词，会漏两类真实写法：粘连名（PGPASSWORD、MYCOOKIE）与口令在值里（DATABASE_URL）。
# KEY 单独说：PRIVATEKEY、MASTERKEY、SIGNINGKEY 这类粘连写法以前漏，现在按子串删。
SECRET_ENV_SUB = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "CREDENTIAL", "APIKEY",
                  "COOKIE", "BEARER", "JWT", "SSHKEY", "KEYCHAIN", "KEY")
URL_CRED_RE = re.compile(r"://[^/\s:@]*:(?![\"\'<$])([^\s@\"\'<>{}$]{8,})@")
# 连接串不一定长成 URL：`Server=x;Password=...` 这种也得删。值里只认标记名，不认短占位串。
CONN_SECRET_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|accesstoken|apikey)\s*[=:]\s*(?![\"\'<$])([^\s;,\"']{6,})")
# 整段私钥直接躺在值里时，名字可以长得很无辜（DEPLOY_BLOB、X）。PEM 头就是凭据证据。
PEM_PRIV_RE = re.compile(r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def env_is_secret(name, value):
    """名字形状或值形状像凭据就删。宁可多删一个可选变量，也不把宿主 token 交给第三方插件。"""
    n = (name or "").upper()
    if n in KEEP_EXACT:
        return False
    if SECRET_ENV_RE.search(n):
        return True
    if any(s in n for s in SECRET_ENV_SUB):
        return True
    v = value or ""
    return bool(PEM_PRIV_RE.search(v) or URL_CRED_RE.search(v) or CONN_SECRET_RE.search(v))


def _server_env(root, args):
    """构造隔离后的服务环境。LOCALAPPDATA 才是 Windows 上 N.E.K.O 的数据根。"""
    env = os.environ.copy()
    dropped = [k for k, v in env.items() if env_is_secret(k, v)]
    for k in dropped:
        env.pop(k, None)
    log("已从沙箱环境删除 %d 个凭据形状变量（%s）" %
        (len(dropped), ", ".join(sorted(dropped)[:6]) + ("…" if len(dropped) > 6 else "")))
    env["NEKO_USER_PLUGIN_SERVER_PORT"] = str(args.port_resolved)
    # 兜底：即便有人从外部环境带进 NEKO_PLUGIN_HOSTED_BY_AGENT 也清掉。注意这一条现在已经
    # 不是 autostart 的开关了——上游 http_app.py 的 startup 段只看 build_plugin_server_app(manage_lifecycle=)
    # 参数，而 user_plugin_server.py:81 硬编码 manage_lifecycle=True。留着清理是为了不往宿主日志里
    # 注入一个「嵌入式」的假身份，避免误导排障。
    env.pop("NEKO_PLUGIN_HOSTED_BY_AGENT", None)
    if args.isolate_home:
        # 只改 HOME/USERPROFILE 没用——N.E.K.O 在 Windows 上取 LOCALAPPDATA 作为数据根
        # （utils/config_manager/storage_roots.py）。不改就会读到用户真实的
        # %LOCALAPPDATA%\N.E.K.O，把用户已装的插件全部拉进来 autostart。
        state = root / "state"
        home = state / "home"
        local = state / "LocalAppData"
        appdata = state / "AppData"
        tmp = state / "Temp"
        for d in (home, local, appdata, tmp):
            d.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["LOCALAPPDATA"] = str(local)
        env["APPDATA"] = str(appdata)
        env["TEMP"] = str(tmp)
        env["TMP"] = str(tmp)
        # PLUGIN_CONFIG_ROOT 上游已标 legacy（env 仍生效，新代码用 PLUGIN_CONFIG_ROOTS /
        # USER_PLUGIN_CONFIG_ROOT）。这里当双保险继续钉死，行为不变。
        env["PLUGIN_CONFIG_ROOT"] = str(local / "N.E.K.O" / ".neko-plugin-installations" / "plugins")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 上游默认 10s 对冷 venv 太短，首次 import 重依赖会超时判 failed。
    # 但上游 plugin/settings.py:766-769 对 >300 直接 raise ValueError，服务会在 import 阶段就崩，
    # 报错和「超时配大了」毫无关系。这里对最终生效值夹紧（含外部环境带进来的值）。
    want = args.plugin_startup_timeout
    try:
        inherited = float(env["NEKO_PLUGIN_STARTUP_TIMEOUT"])
        if inherited != args.plugin_startup_timeout:
            log(f"[WARN] 环境里已有 NEKO_PLUGIN_STARTUP_TIMEOUT={inherited:g}，"
                f"它优先于命令行的 {args.plugin_startup_timeout:g}（上游只读环境变量）")
            want = inherited
    except (KeyError, ValueError):
        pass
    if want > UPSTREAM_STARTUP_TIMEOUT_MAX:
        log(f"[WARN] NEKO_PLUGIN_STARTUP_TIMEOUT={want:g} 超过上游上限 "
            f"{UPSTREAM_STARTUP_TIMEOUT_MAX:g}s（plugin/settings.py:768 会 raise ValueError），已夹紧")
        want = UPSTREAM_STARTUP_TIMEOUT_MAX
    env["NEKO_PLUGIN_STARTUP_TIMEOUT"] = str(want)
    return env


def _wait_health(proc, port, log_file, timeout, fh=None):
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            if fh:
                fh.close()
            tail = "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
            die(f"服务进程提前退出（code={proc.returncode}）\n{tail}")
        status, body = http_get(port, "/health", timeout=2)
        if status == 200:
            return status
        time.sleep(1.0)
    return status


def cmd_serve(args):
    """前台运行（供受管后台任务使用，进程随任务生命周期）。"""
    root = Path(args.root).expanduser().resolve()
    repo = repo_dir(root)
    entry = repo / "plugin" / "user_plugin_server.py"
    if not entry.is_file():
        die("找不到 plugin/user_plugin_server.py，先 prepare")
    py = Path(args.python).expanduser() if args.python else venv_python(root)
    if not py.is_file():
        py = Path(sys.executable)
    args.port_resolved = pick_port("127.0.0.1", args.port)
    env = _server_env(root, args)
    log_file = root / "logs" / f"server-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log(f"前台服务  port={args.port_resolved}  log={log_file}")
    log("就绪后可用 status 检查；结束用 stop 或终止本任务")
    fh = open(log_file, "w", encoding="utf-8", errors="replace")
    # 不用 os.execve：Windows 上直接 SIGSEGV。用子进程前台等待，
    # 进程与本任务同生命周期——由受管后台任务托管，TaskStop 会连树一起收。
    proc = subprocess.Popen([str(py), str(entry)], cwd=str(repo), env=env,
                            stdout=fh, stderr=subprocess.STDOUT, close_fds=True)
    save_state(root, {"pid": proc.pid, "port": args.port_resolved, "root": str(root),
                      "repo": str(repo), "log": str(log_file), "mode": "serve",
                      "started_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    rc = proc.wait()
    fh.close()
    log(f"服务已退出（code={rc}）")
    raise SystemExit(rc)


def cmd_start(args):
    root = Path(args.root).expanduser().resolve()
    repo = repo_dir(root)
    entry = repo / "plugin" / "user_plugin_server.py"
    if not entry.is_file():
        die("找不到 plugin/user_plugin_server.py，先 prepare")

    py = Path(args.python).expanduser() if args.python else venv_python(root)
    if not py.is_file():
        py = Path(sys.executable)
        log(f"未找到沙箱 venv，回退当前解释器: {py}")

    args.port_resolved = pick_port("127.0.0.1", args.port)
    port = args.port_resolved
    env = _server_env(root, args)

    log_file = root / "logs" / f"server-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log(f"启动插件服务器  port={port}  log={log_file}")
    fh = open(log_file, "w", encoding="utf-8", errors="replace")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([str(py), str(entry)], cwd=str(repo), env=env,
                            stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)
    save_state(root, {"pid": proc.pid, "port": port, "root": str(root),
                      "repo": str(repo), "log": str(log_file), "mode": "start",
                      "started_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    status = _wait_health(proc, port, log_file, args.timeout, fh)
    fh.close()
    if status != 200:
        proc.terminate()
        die(f"{args.timeout}s 内 /health 未就绪，见 {log_file}")
    log(f"就绪  http://127.0.0.1:{port}/health")
    log(f"pid={proc.pid}（用 stop 停止）")
    if os.name == "nt":
        log("提示：若本环境用 job object 回收进程树，服务会随命令结束一起终止；此时改用 serve + 后台任务")


_PS_QUERY = ('if ($null -eq ($p = Get-CimInstance Win32_Process -Filter "ProcessId=%d")) '
             "{ '##GONE##' } elseif ([string]::IsNullOrWhiteSpace($p.CommandLine)) "
             "{ '##NOLINE##' } else { $p.CommandLine }")


def _pid_cmdline(pid):
    """读 pid 的命令行，返回 (cmd 或 None, 进程还在不在)。

    读不到的两种情形必须分开（A571 第 2 条）。旧版都算「读不到」，于是服务崩了以后
    stop 也要 --force 才清得掉状态文件：
    - 进程已不存在：cmd=None, alive=False。
    - 进程还在但命令行拿不到（PowerShell 缺失、被挡、超时、权限不足）：cmd=None, alive=True。
    macOS 没有 /proc，所以非 Windows 分支要有 ps 兜底。旧版只读 /proc，判定恒为 None，
    修好之后的拒绝逻辑因此让 macOS 上每一次 stop 都要 --force。
    pid 先过 int() 再拼进 PowerShell 与 ps 的参数，不是命令注入面。
    """
    if os.name == "nt":
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_QUERY % int(pid)],
                               capture_output=True, timeout=20)
        except Exception:
            return None, True
        if r.returncode != 0:
            return None, True
        cmd = r.stdout.decode("utf-8", "replace").strip()
        if cmd == "##GONE##":
            return None, False
        if cmd == "##NOLINE##":
            return None, True
        return (cmd or None), True
    return _posix_cmdline(int(pid), _read_proc_cmdline, _run_ps)


def _read_proc_cmdline(pid):
    with open("/proc/%s/cmdline" % pid, "rb") as f:
        return f.read()


def _run_ps(pid):
    """返回 (返回码, stdout 字节)。ps 的 -o command= 不带表头，整行就是命令行。"""
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                       capture_output=True, timeout=20)
    return r.returncode, r.stdout


def _posix_cmdline(pid, read_proc, run_ps):
    """先读 /proc，读不到再用 ps 分辨。两个取数动作由调用方注入，自检才能演 macOS。

    报告 A571 第 2 条说的就是这个分支：旧版只读 /proc，而 macOS 根本没有 /proc，
    于是判定永远是「读不到」，修好之后的拒绝逻辑反倒让每次 stop 都要 --force。
    「读不到 /proc」不等于「进程不存在」，必须再问一次 ps 才能分账。
    """
    try:
        raw = read_proc(pid)
    except Exception:
        raw = None            # FileNotFoundError（没有 /proc）与权限错误都走到这里，再问 ps
    if raw and raw.strip():
        return raw.decode("utf-8", "replace").replace(chr(0), " "), True
    try:
        code, out = run_ps(pid)
    except Exception:
        return None, True     # 连 ps 都跑不了（没装、被挡、超时）：按「读不到但进程可能还在」
    if code != 0 or not out.strip():
        return None, False    # ps 明确说这个 pid 不存在
    return out.decode("utf-8", "replace").strip(), True


def pid_verdict(cmd):
    """True = 命令行里有 user_plugin_server.py；False = 确认不是；None = 读不到，未知。

    机器重启后 pid 会被复用，只看 sandbox.json 的 pid 会误杀无关进程。
    """
    if not cmd:
        return None
    return "user_plugin_server.py" in cmd


def stop_action(verdict, alive, force):
    """cmd_stop 的四种走向。判定与执行分开，判定才能在 CI 里跑满全部组合。

    kill   = 动手杀：命令行确认是沙箱服务，或读不到但用户明确 --force。
    gone   = pid 已不存在：没有进程可杀，直接清状态文件，不要 --force。
    reuse  = pid 属于别人：不杀，只清状态。--force 也不越这条——force 的意思是
             「读不到命令行也要杀」，不是「确认是别人的进程还照杀」。
    refuse = 命令行读不到、进程还在：停下，让用户核对端口归属后带 --force 重跑。
             旧版在这里 fail-open，伪造 pid 照样被 taskkill。
    """
    if not alive:
        return "gone"
    if verdict is False:
        return "reuse"
    if verdict is True:
        return "kill"
    return "kill" if force else "refuse"


KILL_WAIT = 3.0        # stop 发完信号后等多久算「真的停了」
WAIT_STEPS = (0.2,) * 16          # POSIX：读 /proc 几乎免费，格子可以密
NT_WAIT_STEPS = (0.4, 1.0, 2.0)   # Windows：一次探测要起一个 PowerShell，密格子纯属白等


def _state_from_stat(text):
    """从 /proc/<pid>/stat 里取进程状态那一个字符。

    第二个字段是括号包住的命令名，名字里能带空格也能带括号，所以只能按最后一个 ") " 切。
    Z = 已退出、父进程还没回收。它还占着 pid 目录，ps 也还列得出来，但对「服务停没停」
    这个问句就是停了。不认这一条，SIGTERM 成功之后仍会读成「还活着」。
    """
    tail = text.rsplit(") ", 1)
    if len(tail) < 2:
        return None
    parts = tail[1].split()
    if not parts:
        return None
    state = parts[0]
    # 状态只有一个字母（R S D Z T X）。形状不对就返回 None，让调用方退回 ps，
    # 不要拿一个解析不出来的字段当结论。
    return state if len(state) == 1 and state.isalpha() else None


def _alive_from_state(state):
    """把进程状态字符翻成「还在不在」。None = 认不出来，交给下一条判据。

    Z 是僵尸：进程已经退出，只剩父进程没回收。它不占端口也不占内存，
    对「服务停没停」这个问句就是停了。不认这一条，SIGTERM 成功后仍会读成还活着，
    stop 会白等三轮再谎报「没退」。2026-11-16 在 WSL Debian 上实测到的形状。
    """
    if state is None:
        return None
    return state != "Z"


def _pid_really_alive(pid, nt_probe=None, read_stat=None, run_ps=None, on_nt=None):
    """进程还在不在，僵尸算「不在」。四个口子可注入，自检才能在 CI 与本机都跑满分支。

    2026-11-17 加接缝（与 _posix_cmdline、_wait_gone 同一口径）：这是 stop 路径上最后一个
    没有用例的探针，而它本月改过两次（Z 态、稀疏格子）。on_nt 让自检能在 ubuntu 上验 Windows
    那一支，也能在 Windows 上验 /proc 那一支；不传就按真实平台走。
    """
    pid = int(pid)
    if (os.name == "nt" if on_nt is None else on_nt):
        return (nt_probe or (lambda p: _pid_cmdline(p)[1]))(pid)

    def _stat(p):
        with open("/proc/%d/stat" % p, encoding="utf-8", errors="replace") as f:
            return f.read()

    def _ps(p):
        r = subprocess.run(["ps", "-o", "state=", "-p", str(p)],
                           capture_output=True, timeout=20)
        return r.returncode, r.stdout

    read_stat = read_stat or _stat
    run_ps = run_ps or _ps
    try:
        verdict = _alive_from_state(_state_from_stat(read_stat(pid)))
        if verdict is not None:
            return verdict
    except FileNotFoundError:
        return False            # /proc/<pid> 整目录没了 = 已经回收
    except OSError:
        return True             # 读不动就当作还在：宁可多说一次「没停」，不可谎报已停
    try:
        rc, out = run_ps(pid)
    except Exception:
        return True
    out = out.decode("utf-8", "replace").strip()
    if rc != 0 or not out:
        return False
    verdict = _alive_from_state(out[0].upper())   # ps 会给 "S"、"Z"、"S+"、"Z+" 这类
    return True if verdict is None else verdict


def _wait_gone(pid, timeout, probe=None, nap=None, steps=None):
    """等进程真的消失。True = 确认停了。probe、nap、steps 可注入，自检才能不真睡觉。

    steps 是每次探测之间的等待秒表。Windows 的探测要起一个 PowerShell，一次约 0.9 秒，
    拿 0.2 秒的格子轮询等于白等十几秒；taskkill /F 又是同步强杀，一次确认就够。
    POSIX 的探测读 /proc，成本近乎零，才用密格子。
    """
    if probe is None:
        probe = _pid_really_alive
    if nap is None:
        nap = time.sleep
    grid = list(steps or WAIT_STEPS)
    waited, i, alive = 0.0, 0, probe(pid)
    while alive and waited < timeout:
        step = grid[i % len(grid)]
        nap(min(step, timeout - waited))
        waited += step
        i += 1
        alive = probe(pid)
    return not alive


def _kill_pid(pid, force=False):
    """停下来，并回报有没有确认停掉。

    Windows 的 taskkill 本来就带 /F。POSIX 先发 SIGTERM，那是服务能抓住做收尾的信号。
    抓住后又挂住不退的进程，旧版照样打印「已停止」，读的人会以为端口空出来了。
    现在等 KILL_WAIT 秒确认，只有明确 --force 才升级到 SIGKILL。
    """
    if os.name == "nt":
        subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _wait_gone(pid, KILL_WAIT, steps=NT_WAIT_STEPS)
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return True
    if _wait_gone(pid, KILL_WAIT):
        return True
    if not force:
        return False
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return True
    return _wait_gone(pid, KILL_WAIT)

def cmd_stop(args):
    root = Path(args.root).expanduser().resolve()
    st = load_state(root)
    pid = st.get("pid")
    if not pid:
        die("状态文件里没有 pid")
    # 状态文件是磁盘上的旧 JSON，可能被手改过或被别的版本写过。
    # 非数字的 pid 以前一路走到 "%d" 格式化里抛 ValueError，整轮 stop 没有一句人话。
    # float 也拒：int(1.5) 会静默截成 1，然后拿 pid=1 去问操作系统，那不是任何人想要的行为。
    if isinstance(pid, bool) or not isinstance(pid, (int, str)):
        die("状态文件里的 pid 类型不对（%r）。先核对端口归属，再手工删 %s" % (pid, STATE_FILE))
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        die("状态文件里的 pid 不是数字（%r）。先核对端口归属，再手工删 %s" % (pid, STATE_FILE))
    if pid <= 0:
        die("状态文件里的 pid 不合法（%d）。先核对端口归属，再手工删 %s" % (pid, STATE_FILE))
    cmd, alive = _pid_cmdline(pid)
    verdict = pid_verdict(cmd)
    action = stop_action(verdict, alive, args.force)
    if action == "refuse":
        die("读不到 pid=%s 的命令行（PowerShell 缺失、被挡、超时），而该进程还在，"
            "拒绝杀进程：重启后 pid 会被复用，开枪会动到宿主的进程。"
            "先核对端口归属，确认是沙箱服务再带 --force 重跑。" % pid)
    state_file = root / STATE_FILE
    if action == "kill":
        if verdict is None:
            log("读不到 pid 的命令行，按 --force 的明确要求停止")
        if _kill_pid(pid, args.force):
            state_file.unlink(missing_ok=True)
            log(f"已停止 pid={pid}")
        else:
            log(f"pid={pid} 收到信号后没退出（多半是它抓住了 SIGTERM 又没收尾）。"
                "状态文件留着，端口大概率还被它占着。确认要强行停就带 --force 重跑，"
                "那一步会补发 SIGKILL")
    elif action == "gone":
        state_file.unlink(missing_ok=True)
        log(f"pid={pid} 的进程已不存在（服务已退出或中途崩了），只清理状态文件")
    else:
        state_file.unlink(missing_ok=True)
        log(f"pid={pid} 的命令行里没有 user_plugin_server.py，判定为 pid 复用，"
            "不杀该进程；状态文件已清理。若沙箱服务仍在，请手工核对端口后处理")


def _plugin_rows(payload):
    """把 /plugins 或 /plugin/status 的返回统一成 [(plugin_id, 状态摘要)]。

    /plugins        -> {"plugins": [ {...}, ... ], "message": str}   （query_service.py:887-890）
    /plugin/status  -> {"plugins": { "<id>": {...} }, "time": iso}   （query_service.py:833-836）
    这两种形状一个是 list、一个是 dict，早先的版本只处理了 list，导致 /plugin/status 一条都打不出来。
    """
    rows = []
    if not isinstance(payload, dict):
        return rows
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        for it in plugins:
            if isinstance(it, dict):
                pid = it.get("plugin_id") or it.get("id") or "?"
                pst = it.get("status") or it.get("state") or it.get("runtime_load_state") or "?"
                rows.append((str(pid), str(pst)))
    elif isinstance(plugins, dict):
        for pid, it in plugins.items():
            if isinstance(it, dict):
                pst = (it.get("status") or it.get("state")
                       or it.get("runtime_load_state") or "?")
            else:
                pst = it if isinstance(it, str) else "?"
            rows.append((str(pid), str(pst)))
    return rows


# 上游实际路由（plugin/server/routes/ 下的 @router.get）：
#   /plugins          plugins.py:42    插件列表
#   /plugin/status    plugins.py:35    逐插件运行状态（验证 autostart 用这条最直接）
#   /plugin/messages  messages.py:22   消息队列
# 早先这里探的是 /runs 和 /messages —— 两个 GET 都不能当探活路由：GET /runs（集合）405，
# 集合只有 POST（runs.py:35）；单个 run 虽有 GET /runs/{run_id}（runs.py:44），但要先有 run_id。
# /messages 根本没这个路径，会 404。看的人会以为是插件的问题。
# 每行的第二个字段是上游返回体里放列表的键。三条都实测过：
# /plugins 是 {plugins, message}，/plugin/status 是 {plugins, time}，/plugin/messages 是
# {messages, count, time}。计数只按这个键取，不再猜 items/data 这类不存在的外形。
STATUS_ROUTES = (
    ("/plugins", "plugins"),
    ("/plugin/status", "plugins"),
    ("/plugin/messages", "messages"),
)


def cmd_status(args):
    """打印运行状态。插件列表从 /plugins 与 /plugin/status 解析，失败也照实打印。"""
    root = Path(args.root).expanduser().resolve()
    st = load_state(root)
    if not st:
        die(f"未找到 {root / STATE_FILE}，请先 start 或 serve")

    port = st.get("port")
    code, body = http_get(port, "/health", timeout=3)
    info = {"sandbox": st, "health_http": code, "health_body": body}
    if code != 200:
        print()
        log("[提示] 服务无响应，可能已退出；用 stop 清理后重启")
    print(json.dumps(info, ensure_ascii=False, indent=2))

    for path, label in STATUS_ROUTES:
        c, b = http_get(port, path, timeout=5)
        n = "?"
        if isinstance(b, (list, dict)):
            items = b if isinstance(b, list) else (b.get(label) or [])
            n = len(items) if isinstance(items, (list, dict)) else "?"
        print(f"  {path} -> HTTP {c}  items={n}")
        for pid, pst in _plugin_rows(b)[:20]:
            print(f"      - {pid:<24} {pst}")


# ---------------------------------------------------------------- main
def _symlink_cases(bad):
    """检查 classify_plugin_path 对四种安装形态的判断，返回实际跑掉的用例数。

    悬空软链是外部深审 A472 第 6 条：旧代码先问 is_dir()，悬空链接被当成「不存在」，
    清不掉。造不了符号链接的机器（Windows 无权限）跳过并说明，不算失败。
    """
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="sb-symlink-"))
    n = 0
    try:
        (td / "gone").symlink_to(td / "missing")
        (td / "lnk").symlink_to(td, target_is_directory=True)
        (td / "tree").mkdir()
        cases = [("symlink", td / "gone"), ("symlink", td / "lnk"),
                 ("tree", td / "tree"), ("absent", td / "nothing")]
        for want, path in cases:
            got = classify_plugin_path(path)
            n += 1
            if got != want:
                bad.append("安装形态 %s 判成 %s（%s）" % (want, got, path.name))
    except OSError as e:
        print("  SKIP: 这台机器造不了符号链接（%s），形态判断 %d 例未跑" % (type(e).__name__, n))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return n


def _self_test():
    """环境变量过滤、插件 id 校验与安装形态判断的回归自检（CI 跑，见 sync-check.yml）。"""
    keep = ["PATH", "SYSTEMROOT", "HOME", "LOCALAPPDATA", "APPDATA", "TEMP", "COMSPEC",
            "SSH_AUTH_SOCK", "GIT_SSL_CAINFO", "PYTHONUTF8", "NEKO_USER_PLUGIN_SERVER_PORT",
            "NEKO_PLUGIN_STARTUP_TIMEOUT", "PWD", "SESSIONNAME", "OAUTH_SCOPE", "AUTHOR_NAME",
            "CONNECTION_POOL", "LICENSES", "LICENSING", "KEYBOARD", "KEYMAP"]
    drop = ["MYSQL_PWD", "DB_PWD", "GITHUB_TOKEN", "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY", "OPENAI_APIKEY",
            "MY_SECRET", "SESSION_TOKEN_X", "DB_PASS", "PGPASSWORD", "ADMIN_PASSWORD",
            "DATABASE_URL", "db_credential",
            # 2026-11-15 补：这些名字以前会被放过（外部审 A472 之后的讨论）
            "COOKIE", "SESSION_COOKIE", "AUTH", "AUTHORIZATION", "SESSION_ID", "WT_SESSION",
            "BEARER", "JWT", "MYCOOKIE", "SSHKEY", "KEYCHAIN_PASSWORD", "CONN_STR", "REDIS_URL",
            "LICENCE", "LICENSE", "OPEN_LICENSE_DIR",
            # 2026-11-15 补：粘连 KEY 以前只靠词边界正则，PRIVATEKEY 一类整批漏
            "SESSIONKEY", "PRIVATEKEY", "TLS_PRIVATEKEY", "ENCRYPTIONKEY",
            "SIGNINGKEY", "MASTERKEY", "DEPLOY_BLOB"]
    vals = {"DATABASE_URL": "postgres://u:sup3rs3cr3tpw@db/x", "db_credential": "x",
            "CONN_STR": "Server=db.internal;Password=Sup3rS3cret!;Database=app",
            "REDIS_URL": "redis://:r3d1spassw0rd@cache:6379/0",
            "AUTH": "Basic YWxpZGVjOjEyMzQ1Njc4", "SESSION_ID": "abcd1234efgh5678ijkl",
            # PEM 头拆写：本文件会被自己扫，整串会把自己否决（A472 第 4 条口径）
            "DEPLOY_BLOB": "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADAN\n-----END " + "PRIVATE KEY-----"}
    bad = []
    for k in keep:
        if env_is_secret(k, vals.get(k, "")):
            bad.append("运行变量被误删: " + k)
    for k in drop:
        if not env_is_secret(k, vals.get(k, "abcdefghijkl12")):
            bad.append("凭据形状没被删: " + k)
    for bad_id in ("../../x", ".git", "a/b", "abs:C", "", "1abc"):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bad_id):
            bad.append("id 校验放过了 " + repr(bad_id))
    for good_id in ("my_plugin", "GalgamePlugin", "a1"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", good_id):
            bad.append("id 校验误杀 " + good_id)
    # 2026-11-16 补（A571 第 2 条）：stop 判定的全组合。判定与执行分开以后，才能在 CI 里
    # 跑满 verdict(3) × alive(2) × force(2) = 12 组。旧表只有 6 组，而且没有 alive 这一维，
    # 所以「进程已不存在」与「命令行读不到」两种情形混在一起，macOS 全红也没人发现。
    stop_cases = [
        (True, True, False, "kill"), (True, True, True, "kill"),
        (True, False, False, "gone"), (True, False, True, "gone"),
        (False, True, False, "reuse"), (False, True, True, "reuse"),
        (False, False, False, "gone"), (False, False, True, "gone"),
        (None, True, False, "refuse"), (None, True, True, "kill"),
        (None, False, False, "gone"), (None, False, True, "gone"),
    ]
    for verdict, alive, force, want in stop_cases:
        got = stop_action(verdict, alive, force)
        if got != want:
            bad.append("stop 判定 verdict=%s alive=%s force=%s 得到 %s 期望 %s"
                       % (verdict, alive, force, got, want))
    # 命令行 → 判定：读不到是 None（要 --force 或按 gone 处理），身份不符才是 False
    for cmd, want in ((None, None), ("", None),
                      ("/usr/bin/python3 /srv/user_plugin_server.py", True),
                      ("C:" + chr(92) + "Windows" + chr(92) + "explorer.exe", False)):
        got = pid_verdict(cmd)
        if got is not want:
            bad.append("pid_verdict(%r) 得到 %s 期望 %s" % (cmd, got, want))
    # 2026-11-16 补（A571 第 2 条）：POSIX 分支的平台形状。macOS 没有 /proc，旧版只读 /proc，
    # 于是判定恒为「读不到」。这里用注入的取数函数演三种平台，Windows 上也能跑这一条。
    ZERO = b"\0"

    def _no_proc(pid):
        raise FileNotFoundError(2, "No such file or directory")

    def _no_ps(pid):
        raise OSError("ps 不可用")
    posix_cases = [
        (lambda pid: b"/usr/bin/python3" + ZERO + b"/srv/user_plugin_server.py" + ZERO,
         None, True, True),                                        # Linux：/proc 读到了
        (_no_proc, lambda pid: (0, b"/usr/bin/python3 /srv/user_plugin_server.py"),
         True, True),                                             # macOS：没有 /proc，退回 ps
        (_no_proc, lambda pid: (1, b""), None, False),            # ps 明确说这个 pid 不存在
        (_no_proc, lambda pid: (0, b"/usr/sbin/cron -n"), False, True),  # 进程在，但不是沙箱服务
    ]
    for read_proc, run_ps, want_verdict, want_alive in posix_cases:
        cmd, alive = _posix_cmdline(4321, read_proc, run_ps)
        if pid_verdict(cmd) is not want_verdict or alive is not want_alive:
            bad.append("POSIX 分支：verdict=%s alive=%s 期望 verdict=%s alive=%s（cmd=%r）"
                       % (pid_verdict(cmd), alive, want_verdict, want_alive, cmd))
    # 连 ps 都跑不了（没装、被挡、超时）：算「读不到但进程可能还在」，不能算「已不存在」
    cmd, alive = _posix_cmdline(4321, _no_proc, _no_ps)
    if pid_verdict(cmd) is not None or alive is not True:
        bad.append("ps 跑不了时应为「读不到但进程可能还在」，实得 verdict=%s alive=%s"
                   % (pid_verdict(cmd), alive))
    # 2026-11-16 补（Linux/WSL 实测发现的缺口）：stop 不许在进程没退的情况下说「已停止」。
    # 状态字符解析要有用例：命令名里带括号与空格是 /proc/<pid>/stat 的常见形状。
    stat_cases = [
        ("242 (python3) S 1 242 242 0 -1 4202752 1 1", "S"),
        ("242 (python3) Z 1 242 242 0 -1 4202752 1 1", "Z"),
        ("242 (my prog (x)) Z 1 242 242 0", "Z"),
        ("242 (weird) 1 2 3", None),
        ("", None),
    ]
    for text, want in stat_cases:
        got = _state_from_stat(text)
        if got != want:
            bad.append("_state_from_stat(%r) 得到 %s 期望 %s" % (text[:28], got, want))
    # 状态字符到「还在不在」的翻译。Z 算不在，是 2026-11-16 WSL 实测钉下来的口径。
    alive_cases = [(None, None), ("Z", False), ("S", True), ("R", True), ("D", True), ("T", True)]
    for state, want in alive_cases:
        got = _alive_from_state(state)
        if got is not want:
            bad.append("_alive_from_state(%s) 得到 %s 期望 %s" % (state, got, want))
    seen = []

    def _probe_gone_third(p):
        seen.append(p)
        return len(seen) < 3          # 前两次还在，第三次没了

    if not _wait_gone(7, 1.0, probe=_probe_gone_third, nap=lambda s: None):
        bad.append("第三次探测就没了，_wait_gone 却报没停")
    if _wait_gone(7, 0.6, probe=lambda p: True, nap=lambda s: None):
        bad.append("一直活着的进程，_wait_gone 报成已停")
    nap_calls = []
    _wait_gone(7, 0.6, probe=lambda p: True, nap=lambda s: nap_calls.append(s))
    if len(nap_calls) != 3:
        bad.append("timeout 0.6 应该睡 3 次 0.2，实得 %d 次" % len(nap_calls))
    # Windows 的格子必须稀：一次探测要起一个 PowerShell，密格子等于白等十几秒
    nt_calls = []
    _wait_gone(7, KILL_WAIT, probe=lambda p: nt_calls.append(p) or True,
               nap=lambda s: None, steps=NT_WAIT_STEPS)
    if len(nt_calls) > 5:
        bad.append("Windows 的探测格子太密：跑了 %d 次 PowerShell 形状的探测" % len(nt_calls))
    # A464 第 13 条欠的三组用例：toml 读段、_plugin_rows、超时夹紧。本轮全补上。
    # 第一组钉的是「别按行首是 id 就认」这个老坑：[plugin.ui] 的键不能替 [plugin] 回答。
    x_tomls = [
        ('[plugin]\nid = "demo"\n', "plugin", "id", "demo"),
        ("[plugin]\nid = 'sq'\n", "plugin", "id", "sq"),
        ('[plugin]\nid = "demo"  # 尾巴注释\n', "plugin", "id", "demo"),
        ('[other]\nid = "wrong"\n[plugin]\nid = "right"\n', "plugin", "id", "right"),
        ('[plugin]\nid = "x"\n[plugin.ui]\nid_panel = "p"\n', "plugin.ui", "id", None),
        ('[plugin]\nid = "x"\n[plugin.ui]\nid_panel = "p"\n', "plugin", "id_panel", None),
        ('[plugin_runtime]\nauto_start = true\n', "plugin_runtime", "auto_start", "true"),
        ('[plugin_runtime]\nauto_start = false\n', "plugin_runtime", "auto_start", "false"),
        ('[plugin]\nversion = "1"\n', "plugin", "id", None),
    ]
    for x_txt, x_sec, x_key, x_want in x_tomls:
        x_got = toml_section_value(x_txt, x_sec, x_key)
        if x_got != x_want:
            bad.append("toml_section_value(%s.%s) 期望 %r，得到 %r" % (x_sec, x_key, x_want, x_got))
    x_flags = [("[plugin_runtime]\nauto_start = true\n", True),
               ("[plugin_runtime]\nauto_start = True\n", True),
               ("[plugin_runtime]\nauto_start = 1\n", True),
               ("[plugin_runtime]\nauto_start = false\n", False),
               ("[plugin_runtime]\nauto_start = 0\n", False),
               ('[plugin]\nid = "x"\n', None)]      # 没写要回 None，调用方要分得开「显式 false」
    for x_txt, x_wf in x_flags:
        x_gf = toml_section_flag(x_txt, "plugin_runtime", "auto_start")
        if x_gf != x_wf:
            bad.append("auto_start 形状 %r 期望 flag=%r，得到 %r" % (x_txt.strip(), x_wf, x_gf))
    # 第二组用真机抓回来的两种外形：/plugins 是 list，/plugin/status 是以 id 为键的 dict
    x_rows = [
        ({"plugins": [{"plugin_id": "a", "status": "running"}], "message": ""},
         [("a", "running")]),
        ({"plugins": {"b": {"status": "stopped"}}, "time": "t"}, [("b", "stopped")]),
        ({"plugins": {"c": {"runtime_load_state": "failed"}}}, [("c", "failed")]),
        ({"plugins": [{"id": "d", "state": "running"}]}, [("d", "running")]),
        ({"plugins": {"e": "plain string"}}, [("e", "plain string")]),
        ({"plugins": [{}]}, [("?", "?")]),
        ({"plugins": None}, []),
        ([], []),
        ("不是 dict", []),
    ]
    for x_payload, x_wr in x_rows:
        x_gr = _plugin_rows(x_payload)
        if x_gr != x_wr:
            bad.append("_plugin_rows(%r) 期望 %r，得到 %r" % (x_payload, x_wr, x_gr))
    # 第三组：上游 plugin/settings.py:768 对 >300 直接 raise，服务在 import 阶段就崩。
    x_env0 = os.environ.get("NEKO_PLUGIN_STARTUP_TIMEOUT")
    x_hosted0 = os.environ.get("NEKO_PLUGIN_HOSTED_BY_AGENT")
    os.environ["NEKO_PLUGIN_HOSTED_BY_AGENT"] = "1"   # 不带进来就验不出「清掉」这一步
    import tempfile                      # 模块层不引 tempfile，只有自检用得到（与 _symlink_cases 同一口径）
    x_root = Path(tempfile.mkdtemp(prefix="sb-env-"))
    x_n = 0
    try:
        x_a = argparse.Namespace(plugin_startup_timeout=360.0, port_resolved=43117,
                                 isolate_home=True)
        x_e = _server_env(x_root, x_a)
        if float(x_e["NEKO_PLUGIN_STARTUP_TIMEOUT"]) != 300.0:
            bad.append("360 应夹到 300，实得 %s" % x_e["NEKO_PLUGIN_STARTUP_TIMEOUT"])
        x_n += 1
        if not (x_root / "state" / "LocalAppData").is_dir():
            bad.append("isolate_home=True 没建出 LocalAppData 数据根")
        x_n += 1
        if x_e.get("NEKO_PLUGIN_HOSTED_BY_AGENT") is not None:
            bad.append("NEKO_PLUGIN_HOSTED_BY_AGENT 没被清掉")
        x_n += 1
        for x_in, x_want2 in (("45", 45.0), ("600", 300.0), ("abc", 300.0)):
            os.environ["NEKO_PLUGIN_STARTUP_TIMEOUT"] = x_in
            x_g2 = float(_server_env(x_root, x_a)["NEKO_PLUGIN_STARTUP_TIMEOUT"])
            if x_g2 != x_want2:
                bad.append("环境带进 %s 时应生效 %g，实得 %g" % (x_in, x_want2, x_g2))
            x_n += 1
    finally:
        if x_hosted0 is None:
            os.environ.pop("NEKO_PLUGIN_HOSTED_BY_AGENT", None)
        else:
            os.environ["NEKO_PLUGIN_HOSTED_BY_AGENT"] = x_hosted0
        if x_env0 is None:
            os.environ.pop("NEKO_PLUGIN_STARTUP_TIMEOUT", None)
        else:
            os.environ["NEKO_PLUGIN_STARTUP_TIMEOUT"] = x_env0
        shutil.rmtree(x_root, ignore_errors=True)
    # _wait_health 三例：就绪要立刻返回，服务提前退要把日志尾巴与退出码带出来。
    # 本轮真机跑的是 serve 那条路，start 这条以前只有读证。取数函数在这里临时替换，
    # 不改动 _wait_health 本身，跑完在 finally 里换回来。
    hw_root = Path(tempfile.mkdtemp(prefix="sb-wait-"))
    hw_log = hw_root / "server.log"
    hw_log.write_text("\n".join("第%d行" % i for i in range(1, 41)), encoding="utf-8")
    _real_get = globals()["http_get"]

    class _Exited(object):
        returncode = 1

        def poll(self):
            return 1

    try:
        globals()["http_get"] = lambda port, path, timeout=2: (200, '{"status": "ok"}')
        if _wait_health(None, 1, hw_log, 5) != 200:
            bad.append("服务就绪时 _wait_health 没返回 200")
        globals()["http_get"] = lambda port, path, timeout=2: (503, "")
        if _wait_health(None, 1, hw_log, 0) is not None:
            bad.append("timeout=0 应一次都不探，直接返回 None")
        hw_exit = None
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                _wait_health(_Exited(), 1, hw_log, 30)
            except SystemExit as e:
                hw_exit = (e.code, buf.getvalue())
        if hw_exit is None:
            bad.append("服务提前退出时 _wait_health 没停手")
        elif "第40行" not in hw_exit[1] or "code=1" not in hw_exit[1]:
            bad.append("提前退出的诊断没带出日志尾巴与退出码：%r" % (hw_exit[1][-120:],))
        elif hw_exit[0] == 0:
            bad.append("提前退出要走 die，退出码不该是 0")
    finally:
        globals()["http_get"] = _real_get
        shutil.rmtree(hw_root, ignore_errors=True)
    # 2026-11-17 补：_pid_really_alive 是 stop 路径上最后一个没用例的探针，本月改过两次。
    # 三条分支（nt、/proc、ps）都靠注入演，Windows 与 ubuntu 上跑同一组断言。
    ra_n = 0
    ra_cases = [
        (lambda p: "242 (python3) S 1 242 242 0 -1 42 1 1", None, True),     # 正常睡眠 = 还在
        (lambda p: "242 (python3) Z 1 242 242 0 -1 42 1 1", None, False),    # 僵尸 = 已退
        (lambda p: "242 (python3) D 1 242 242 0 -1 42 1 1", None, True),     # 不可中断 IO 仍算在
        (lambda p: (_ for _ in ()).throw(FileNotFoundError()), None, False),  # /proc 整目录没了
        (lambda p: (_ for _ in ()).throw(PermissionError()), None, True),     # 读不动就当作还在
        ("242 broken-stat-line", lambda p: (0, b"S"), True),                  # stat 解析不出→退回 ps
        ("242 broken-stat-line", lambda p: (0, b"Z"), False),
        ("242 broken-stat-line", lambda p: (0, b"Z+"), False),
        ("242 broken-stat-line", lambda p: (0, b"S+"), True),
        ("242 broken-stat-line", lambda p: (1, b""), False),                  # ps 说没有这个 pid
        ("242 broken-stat-line", lambda p: (0, b""), False),
        ("242 broken-stat-line",
         lambda p: (_ for _ in ()).throw(OSError("ps 没了")), True),          # ps 跑不了，不判已退
    ]
    for stat_src, ps_src, want_ra in ra_cases:
        ra_n += 1
        ra = stat_src if callable(stat_src) else (lambda p, _t=stat_src: _t)
        got_ra = _pid_really_alive(242, read_stat=ra, run_ps=ps_src, on_nt=False)
        if got_ra is not want_ra:
            bad.append("_pid_really_alive(stat=%r, ps=%r) 期望 %r，得到 %r"
                       % (stat_src if isinstance(stat_src, str) else "callable", ps_src,
                          want_ra, got_ra))
    # nt 那一支同样用注入演，两边平台都能验（on_nt=True 时不碰 PowerShell）
    ra_n += 1
    if _pid_really_alive(242, on_nt=True, nt_probe=lambda p: False) is not False:
        bad.append("nt 分支没把 nt_probe 的 False 传出来")
    ra_n += 1
    if _pid_really_alive(242, on_nt=True, nt_probe=lambda p: True) is not True:
        bad.append("nt 分支没把 nt_probe 的 True 传出来")

    # 状态文件里的 pid 不是数字时，cmd_stop 要说人话，不许抛 ValueError 让整轮 stop 消失。
    stop_root = Path(tempfile.mkdtemp(prefix="sb-badpid-"))
    try:
        for bad_pid in ("abc", 0, -7, {"n": 1}, 1.5, True):
            ra_n += 1
            (stop_root / STATE_FILE).write_text(json.dumps(
                {"pid": bad_pid, "port": 43117, "root": str(stop_root),
                 "mode": "start"}), encoding="utf-8")
            cap = io.StringIO()
            code = None
            with contextlib.redirect_stderr(cap):
                try:
                    cmd_stop(argparse.Namespace(root=str(stop_root), force=False))
                except SystemExit as e:
                    code = e.code
            msg = cap.getvalue()
            if code == 0 or "pid" not in msg:
                bad.append("坏 pid %r 没被人话拦住（退出码 %r，消息 %r）" % (bad_pid, code, msg[:80]))
            if not (stop_root / STATE_FILE).exists():
                bad.append("坏 pid %r 那一步不该顺手删掉状态文件" % (bad_pid,))
    finally:
        shutil.rmtree(stop_root, ignore_errors=True)
    total = (len(keep) + len(drop) + 9 + len(stop_cases) + 4 + len(posix_cases) + 1
             + len(stat_cases) + len(alive_cases) + 4 + len(x_tomls) + len(x_flags)
             + len(x_rows) + x_n + 5 + ra_n) + _symlink_cases(bad)
    print("SANDBOX SELF-TEST: %s（%d 项）" % ("PASS" if not bad else "FAIL", total))
    for m in bad:
        print("  FAIL:", m)
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser(description="N.E.K.O 插件运行时沙箱")
    ap.add_argument("--self-test", action="store_true",
                   help="内置自检（CI 用），不需要 --root")
    if "--self-test" in sys.argv:
        return _self_test()
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="检查环境前置条件")
    d.add_argument("--root", default=None)
    d.add_argument("--port", type=int, default=DEFAULT_PORT)
    d.set_defaults(func=cmd_doctor)

    p = sub.add_parser("prepare", help="挂载已有源码或克隆官方仓库，并建隔离目录")
    p.add_argument("--root", required=True)
    p.add_argument("--repo", default=None, help="已有的 N.E.K.O 源码目录（推荐，免克隆）；fork 与钉版本也走这条路")
    p.set_defaults(func=cmd_prepare)

    y = sub.add_parser("sync", help="拉取官方最新源码（官方迭代快，跑前先同步）")
    y.add_argument("--root", required=True)
    y.add_argument("--hard", action="store_true", help="本地有改动时强制对齐 origin（会丢改动）")
    y.set_defaults(func=cmd_sync)

    i = sub.add_parser("install", help="安装依赖到沙箱 venv")
    i.add_argument("--root", required=True)
    i.add_argument("--python", default=None,
                   help="建 venv 用的解释器路径。给了就用它。没给时 uv 走 --py-version（默认 3.11），"
                        "pip 走当前解释器")
    i.add_argument("--no-uv", action="store_true", help="禁用 uv，强制用 pip（排障用）")
    i.add_argument("--insecure-pip", dest="insecure_pip", action="store_true",
                   help="pip 首次失败后允许用 --trusted-host 关闭 TLS 校验重试（有中间人风险，"
                        "默认不允许）")
    i.add_argument("--py-version", default="3.11", help="uv 建 venv 时的 Python 版本（默认 3.11）")
    i.set_defaults(func=cmd_install)

    a = sub.add_parser("plugin-add", help="把待测插件放进沙箱")
    a.add_argument("--root", required=True)
    a.add_argument("--src", required=True)
    a.add_argument("--link", action="store_true", help="软链而非复制（仅类 Unix）")
    a.set_defaults(func=cmd_plugin_add)

    r = sub.add_parser("plugin-rm", help="移除 plugin-add 放进源码目录的插件")
    r.add_argument("--root", required=True)
    r.add_argument("--id", required=True, help="插件 id（= plugin.toml 里的 name）")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_plugin_rm)

    v = sub.add_parser("serve", help="前台运行服务（配合受管后台任务，进程常驻）")
    v.add_argument("--root", required=True)
    v.add_argument("--port", type=int, default=DEFAULT_PORT)
    v.add_argument("--python", default=None)
    v.add_argument("--plugin-startup-timeout", type=float, default=60.0)
    v.add_argument("--no-isolate-home", dest="isolate_home", action="store_false", default=True)
    v.set_defaults(func=cmd_serve)

    s = sub.add_parser("start", help="启动插件服务器并等待 /health")
    s.add_argument("--root", required=True)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--timeout", type=int, default=300, help="等待 /health 就绪的秒数（首启较慢）")
    s.add_argument("--plugin-startup-timeout", type=float, default=60.0,
                   help="单个插件启动就绪的等待秒数，透传 NEKO_PLUGIN_STARTUP_TIMEOUT（默认 60，上游默认 10 对冷 venv 太短；"
                        f"上游对 >{UPSTREAM_STARTUP_TIMEOUT_MAX:g} 直接 ValueError，本脚本会夹紧）")
    s.add_argument("--python", default=None, help="指定解释器（默认 <root>/.venv）")
    s.add_argument("--no-isolate-home", dest="isolate_home", action="store_false", default=True,
                   help="不重定向 HOME（默认重定向到 <root>/state/home）")
    s.set_defaults(func=cmd_start)

    t = sub.add_parser("status", help="查看运行状态与基础路由")
    t.add_argument("--root", required=True)
    t.set_defaults(func=cmd_status)

    x = sub.add_parser("stop", help="停止沙箱服务")
    x.add_argument("--root", required=True)
    x.add_argument("--force", action="store_true",
                   help="读不到 pid 命令行时也照杀。只在确认 pid 仍属于沙箱服务后用")
    x.set_defaults(func=cmd_stop)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    # 丢掉返回值会让 CI 的「Sandbox self-test」这一步永远不会红（A472 的反向对照抓到）。
    # 只把整数当退出码：命令路径里 cmd_* 若返回非零值，不该被当成失败码。
    _rc = main()
    sys.exit(_rc if isinstance(_rc, int) else 0)
