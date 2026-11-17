#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profile 锚点漂移探针（零第三方依赖，Python 3.8+；网络走 GitHub REST 只读接口，
用 urllib 直连，不依赖 gh——CI 里没有交互式登录也能跑，见 api()）

用途
  profile-neko.md 的锚点写法不统一：`(1050 行，读于 a222e50)`、`（id 212、name 213）`、
  `plugins.py:28`、`runs.py:35` 都有。行号会随上游漂移，而这份 profile 是团队的契约来源。
  本脚本把「路径 + 紧跟该路径的行号」抽出来，对**当前 main**逐条核对：记录的那一行上是否还
  出现该行附近的标识符。不在了，就给出该标识符现在在哪。

用法
  python scripts/profile_drift.py [--profile 路径] [--json] [--strict] [--fresh]

  默认只报告：不改文件、不因漂移失败（退出码 0）。--strict 时有漂移退 1，留给以后要 blocking
  的场合；CI 现在按非阻塞接。--fresh 忽略缓存重拉。

缓存
  拉到的上游文件放在 scripts/.drift-cache/（已加进 .gitignore），避免一次跑打上百次 API。
  当前 main 的 sha 也存那里，超过 24 小时自动重取，否则「当前 main」这句话站不住。

匿名限额是一小时 60 次。CI 里给这一步传 GITHUB_TOKEN，否则跑不了几条就限流。
限流与断网不算漂移：那些锚点报 unreachable，脚本退 2，不冒充「上游删了文件」的结论。

退出码：0 报告完成；--strict 且有漂移、或有锚点缺符号可比对（weak）时退 1；
有锚点取不到上游时 2（包括根本取不到 sha）。weak 也算失败是 2026-11-17 改的：
那句「没有可比对的符号」是我们自己的写法问题，让它静默通过就是在门禁里留一格假绿。
"""
import argparse
import base64
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_PROFILE = os.path.join(REPO, "skills", "plugin-forge", "references", "profile-neko.md")
CACHE_DIR = os.path.join(HERE, ".drift-cache")
HEAD_TTL = 24 * 3600
UPSTREAM = "Project-N-E-K-O/N.E.K.O"
NL = chr(10)

# 前缀白名单。2026-11-17 补 utils / brain：profile 早就在引
# utils/config_manager/storage_roots.py:367-373 这类锚点，白名单不含 utils 时探针直接看不见它，
# 于是「71 条全对」这句话漏掉了真在用的锚点。
# 不要加 scripts/：那是我们自己的仓的路径，上游没有对应文件，加进去会让探针把
# 本文提到的 `scripts/profile_drift.py` 当成上游锚点，报出一条假 404。
PATH_RE = re.compile(r"(?<![\w/.\-])((?:plugin|app|config|docs|nekoclaw|memory|launcher_core|"
                     r"main_logic|\.agent|utils|brain)/[\w./\-]*\.(?:py|md|toml|json|js|ts))")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
# 认这四种形状：NNN 行、:NNN、（NNN 或（NNN/NNN、带标签的「id 212、name 213」。
# issue 号带 #、章节号带 §，都不会被吃进来。第四种必须跟在短标签后并以顿号或括号收尾，
# 否则「见 4.2 与自查 5、6、8」这种句子编号会被当成行号——第一版探针就在这里报了 8 条假漂移。
NUM_PATTERNS = (re.compile(r"(?<![\w.\-])(\d{1,5})(?=\s*行)"),
                re.compile(r":(\d{1,5})(?![\d%])"),
                re.compile(r"[（(]\s*(\d{1,5}(?:\s*[/、,]\s*\d{1,5})*)"),
                re.compile(r"(?<=[A-Za-z_] )(\d{1,5})(?=[、，,)）])"))
STOPWORDS = {"plugin", "read", "line", "sha", "fact", "def", "main", "code", "true", "false",
             "none", "str", "int", "bool", "list", "dict", "self", "return", "import", "from",
             "https", "http", "www", "md", "py", "toml", "json", "get", "post", "put", "delete",
             "api", "ctx", "sdk", "app", "pinned", "readme", "guide", "this", "that", "with"}


def classify(status):
    """把 HTTP 状态分成三类，因为它们的含义完全不同。

    missing  = 404，上游真的没有这个文件，这是有效结论（算漂移）。
    limited  = 403 / 429，匿名限额一小时 60 次，这不是结论。
    net      = 其他状态、超时、断连，同样不是结论。
    旧版把三者一律压成 None，限流就被报成「上游取不到该文件」，读起来像上游删了代码。
    """
    if status == 404:
        return "missing"
    if status in (403, 429):
        return "limited"
    return "net"


def api(url):
    """直连 GitHub REST，不依赖 gh：CI 里没有交互式登录也能跑。有 GITHUB_TOKEN 就带上。

    返回 (数据, 故障)。故障取 classify 的三种值，None 表示成功。
    """
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "plugin-forge-profile-drift-probe",
    })
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return None, classify(e.code)
    except (ValueError, TimeoutError, OSError):
        return None, "net"


def resolve_head(use_cache=True, max_age=HEAD_TTL):
    """取上游当前 main 的 sha。缓存超过 max_age 秒就重取，否则「当前 main」这句话不成立。"""
    import time
    mark = os.path.join(CACHE_DIR, "head.txt")
    if use_cache and os.path.isfile(mark) and time.time() - os.path.getmtime(mark) <= max_age:
        with open(mark, encoding="utf-8") as f:
            sha = f.read().strip()
        if sha:
            return sha, True
    meta, err = api("https://api.github.com/repos/" + UPSTREAM)
    branch = (meta or {}).get("default_branch") or "main"
    data, err2 = api("https://api.github.com/repos/%s/commits/%s" % (UPSTREAM, branch))
    sha = (data or {}).get("sha", "")
    if sha:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(mark, "w", encoding="utf-8") as f:
            f.write(sha)
        return sha, False
    return "", err2 or err or "net"


def body_state(data):
    """区分「上游没有这个文件」与「文件太大、API 不给正文」。

    contents API 对 1MB–50MB 的文件返 HTTP 200 但不带 content。旧版把它算成 404，
    于是探针会说「该文件已改名或已删除」——一个不存在的产品结论。
    """
    if data is None:
        return "missing"
    return None if data.get("content") else "big"


def api_bytes(url):
    """取原始正文。Contents API 对 1MB 以上的文件不给正文，这类文件只能走 raw 端点。

    2026-11-17 加：CI 里这一步已经改成阻塞，`plugin/settings.py`（1.1MB）以前会算成
    unreachable 并退 2，等于探针天天红却什么都没说。raw 端点没有大小门槛。
    """
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={
        "Accept": "text/plain",
        "User-Agent": "plugin-forge-profile-drift-probe",
    })
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        return None, classify(e.code)
    except (ValueError, TimeoutError, OSError):
        return None, "net"


def upstream_lines(path, head, get=None, get_raw=None, cache_dir=None):
    """返回 (行列表, 故障)。故障非 None 时不能据此说上游没有该文件。

    cache_dir 可注入，自检用它把夹具写进临时目录，不污染仓库外的 .drift-cache。
    """
    get = get or api
    get_raw = get_raw or api_bytes
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    fn = os.path.join(cache_dir, "%s.%s.txt" % (head[:8], path.replace("/", "__")))
    if os.path.isfile(fn):
        with open(fn, encoding="utf-8", errors="replace") as f:
            return f.read().split(NL), None
    data, err = get("https://api.github.com/repos/%s/contents/%s?ref=%s" % (UPSTREAM, path, head))
    if err:
        return None, err
    state = body_state(data)
    if state == "big":
        raw, rerr = get_raw("https://raw.githubusercontent.com/%s/%s/%s" % (UPSTREAM, head, path))
        if rerr:
            return None, rerr
        if raw is None:
            return None, "big"
        text = raw.decode("utf-8", "replace")
        with open(fn, "w", encoding="utf-8", newline=NL) as f:
            f.write(text)
        return text.split(NL), None
    if state:
        return None, state
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    except Exception:
        return None, "net"
    with open(fn, "w", encoding="utf-8", newline=NL) as f:
        f.write(text)
    return text.split(NL), None


def where_symbol(lines, token):
    return [i for i, ln in enumerate(lines, 1) if token in ln][:6]


def window_nums(seg):
    out = []
    for pat in NUM_PATTERNS:
        for m in pat.finditer(seg):
            for piece in re.split(r"[/、,]", m.group(1)):
                piece = piece.strip()
                if piece.isdigit() and 1 <= int(piece) <= 40000:
                    out.append(int(piece))
    return sorted(set(out))


def _route_literals(seg):
    """把反引号里的路由字面量去掉，只留下真正的代码符号。

    `GET /plugin/metrics*` 这种写法里 IDENT_RE 会抓到 metrics，而它不是代码符号，是路由名。
    2026-11-17 实测：一行里连写四个 `xxx.py:NN` 时，这种假符号会被安到前一个路径头上，
    探针报「记录行上没有本行任何标识符」的假漂移。带 / 的那段整个摘掉；不带 / 的
    `health()`、`PluginPageMutationGuardedRoute` 这些照旧留下。
    """
    return re.sub(r"`[^`]*?/[^`]*?`", " ", seg)


def collect(text):
    """每个路径只看它自己之后、下一个路径之前的那段文字。

    段内没有标识符时不要退回整行。整行里有路径本身，`manifest.ts:129` 会抽出
    `manifest`，而上游正文里几乎到处都写着 manifest。那种锚点自己命中自己，
    代码搬走了也报「对得上」。这类锚点标 weak，交给 check 单独报「核不了」。
    """
    anchors = []
    for no, line in enumerate(text.split(NL), 1):
        found = list(PATH_RE.finditer(line))
        if not found:
            continue
        for i, m in enumerate(found):
            stop = found[i + 1].start() if i + 1 < len(found) else len(line)
            seg = line[m.end():stop]
            nums = window_nums(seg)
            if not nums:
                continue
            toks = [x for x in IDENT_RE.findall(_route_literals(seg))
                    if x.lower() not in STOPWORDS and not re.fullmatch(r"[0-9a-fA-F]{7,}", x)]
            anchors.append({"line": no, "path": m.group(1), "nums": nums[:10],
                            "tokens": list(dict.fromkeys(toks))[:6],
                            "weak": not toks,
                            "text": line.strip()[:120]})
    seen, uniq = set(), []
    for an in anchors:
        k = (an["path"], tuple(an["nums"]), an["weak"])
        if k not in seen:
            seen.add(k)
            uniq.append(an)
    return uniq


def enclosing(lines, token, n):
    """记录行是否落在该标识符的定义域内。装饰器行不含函数名、区间落在函数体内，都属正常。

    第一版只认 Python 的 def/class 与 Field/Literal 赋值。TS 与 JS 文件里的锚点
    因此永远进不了定义体判定：函数搬走、改名，探针仍说对得上。这里补 TS/JS 的
    顶层定义形状，同时保留 Python 那两条。
    """
    starts = [i for i, ln in enumerate(lines, 1) if token in ln]
    if not starts:
        return False
    top = re.compile(r"^(?:async\s+)?(?:def|class)\s+\w|^\w.*=\s*(?:Field|Literal)\b"
                     r"|^(?:export\s+)?(?:default\s+)?(?:async\s+)?"
                     r"(?:function|class|interface|type|enum|namespace|module|abstract\s+class)\s+\w"
                     r"|^(?:export\s+)?(?:const|let|var)\s+\w"
                     r"|^\w[\w$]*\s*\(.*\)\s*\{\s*$"
                     r"|^\w[\w$]*\s*(?:\([^)]*\))?\s*[:=]\s*(?:async\s*)?\(")
    for s in starts:
        if s > n:
            continue
        blocked = False
        for k in range(s + 1, n + 1):
            if k <= len(lines) and top.match(lines[k - 1]) and token not in lines[k - 1]:
                blocked = True
                break
        if not blocked:
            return True
    return False


def _all_blank(lines, present):
    """记录的那些行号是不是全是空行。全空就不能说「这行写着那个标识符」。"""
    return bool(present) and all(not lines[n - 1].strip() for n in present)


def check(anchors, head, get=None, get_raw=None, cache_dir=None):
    """逐条核对锚点。状态五种：ok 对得上，drift 行号漂了，gone 上游 404（真结论），
    unreachable 取不到（限流或断网，不是结论），weak 这句话没有符号可比对（探针管不了，
    也不算核过）。把 gone 与 unreachable 混在一起，一次限流就会被读成「上游把文件删了」。
    把 weak 算成 ok，就回到旧版的假绿：锚点只写了路径与行号时，谁都没在核它。

    get / get_raw / cache_dir 透传给 upstream_lines，自检因此能离线跑满 check 的判据本身
    （2026-11-17：以前自检只测 _all_blank 这个纯函数，接线断了也照样绿）。
    """
    file_cache, rows = {}, []
    for an in anchors:
        path = an["path"]
        if path not in file_cache:
            file_cache[path] = upstream_lines(path, head, get=get, get_raw=get_raw,
                                              cache_dir=cache_dir)
        lines, err = file_cache[path]
        if err == "missing":
            rows.append(dict(an, status="gone", detail="上游 404：该文件已改名或已删除"))
            continue
        if lines is None:
            why = {"limited": "限流（403/429），带 GITHUB_TOKEN 再跑",
                   "big": "文件超过 contents API 的正文上限（1MB），需改用 git blob API 才能核"}.get(err, "传输失败（超时或断网）")
            rows.append(dict(an, status="unreachable", detail=why))
            continue
        if an.get("weak"):
            rows.append(dict(an, status="weak",
                             detail="这句话只有路径与行号，没有可比对的符号。上游怎么改都验不出来，"
                                    "请在 profile 里补一个符号名（函数、字段或类）"))
            continue
        drift, present = [], []
        for n in an["nums"]:
            if n > len(lines):
                drift.append("%d 超出该文件行数(%d)" % (n, len(lines)))
            else:
                present.append(n)
        # 命中判据放宽两档：记录行本身，或紧邻的下一行（装饰器在 def 之前）
        here = " ".join(lines[n - 1] for n in present)
        here += " " + " ".join(lines[n] for n in present if n < len(lines))
        # 2026-11-17 补一档收紧：记录行全落在空行上时，旧版会因为「空行在某个定义体内」判成对得上。
        # 上游行号往上挪几位就能撞上空行，这种锚点其实已经漂了。本轮实测抓到三处这种假绿。
        blank_hit = bool(an["tokens"]) and _all_blank(lines, present)
        miss = bool(an["tokens"]) and present and all(t not in here for t in an["tokens"])
        if blank_hit or miss:
            moved = []
            for t in an["tokens"][:3]:
                w = where_symbol(lines, t)
                moved.append("%s→%s" % (t, "/".join(map(str, w)) if w else "未找到"))
            if blank_hit:
                drift.append("记录行在上游全是空行，锚点没落在写代码的那一行；" + ", ".join(moved))
            else:
                # 再退一档：记录行是否落在某个标识符的定义体内（区间锚、装饰器锚都属这种）
                inside = [t for t in an["tokens"] if any(enclosing(lines, t, n) for n in present)]
                if not inside:
                    drift.append("记录行上没有本行任何标识符，也不在其定义体内；" + ", ".join(moved))
        rows.append(dict(an, status="drift" if drift else "ok", detail="; ".join(drift)))
    return rows


def self_test():
    """钉住两处真出过错的形状：状态分类，以及从一句话里抓行号。

    2026-11-15 又钉两处假绿：段内没有符号时退回整行拿路径片段当 token，
    以及 enclosing 只认 Python 定义形状。这两种都会让「代码已搬走」读成「仍对得上」。
    """
    def anchor(text):
        got = collect(text)
        return got[0] if got else {}

    cases = [
        (classify(404), "missing"), (classify(403), "limited"), (classify(429), "limited"),
        (classify(500), "net"), (classify(301), "net"),
        # 200 但无正文 = 文件过大，不是上游 404
        (body_state(None), "missing"), (body_state({"content": ""}), "big"),
        (body_state({"content": "x"}), None),
        # issue 号带 #、章节号带 §，都不该被当成行号；「NNN 行」「:NNN」「（NNN」三种形状要抓到
        (window_nums("读于 :35 与 1050 行"), [35, 1050]),
        (window_nums("issue #3683 §4.2 自查 5、6、8"), []),
        (window_nums("（id 212、name 213）"), [212, 213]),
        (window_nums("覆盖率 95%"), []),
        # 只有路径与行号的一句话：标 weak，token 必须为空（不许拿路径片段凑）
        (anchor("见 plugin/app/x.ts:129 那一处。").get("weak"), True),
        (anchor("见 plugin/app/x.ts:129 那一处。").get("tokens"), []),
        # 句子里有符号就不 weak
        (anchor("plugin/app/x.py:12 里是 handler 函数").get("weak"), False),
        (anchor("plugin/app/x.py:12 里是 handler 函数").get("tokens"), ["handler"]),
        # enclosing 认 TS/JS 顶层定义：下一个定义把作用域关掉时，记录行不算在域内。
        # 这一条是真的对照：只认 Python 形状时，第 4 行的 export const 不算边界，会错判 True。
        (enclosing(["function handler() {", "  return 1", "}",
                    "export const WORLD = {", "  k: 1"], "handler", 5), False),
        # Python 的 def 边界照旧算边界
        (enclosing(["def handler():", "    return 1", "def other():", "    pass"],
                   "handler", 4), False),
        # 同一个函数体内部的行算在域内（装饰器与区间锚属这种）
        (enclosing(["def handler():", "    x = 1", "    return x"], "handler", 3), True),
        (enclosing(["export class Panel extends Base {", "  onMount() {", "    const x = 1"],
                   "Panel", 3), True),
        # 2026-11-17 收紧：行号撞上空行时必须算漂，哪怕它落在某个定义体内
        (_all_blank(["def handler():", "", "    pass"], [2]), True),
        (_all_blank(["def handler():", "    x = 1"], [2]), False),
        (_all_blank(["def handler():", "    x = 1"], []), False),
        (_all_blank(["a", "b", "c"], [2, 3]), False),
    ]
    # 2026-11-17 补：1MB 以上的文件必须走 raw 端点拿正文。Contents API 对这种文件返回 200
    # 但不带 content，旧版把它算成「取不到」，探针会天天退 2 却不给出任何结论。
    import shutil
    import tempfile
    cache = tempfile.mkdtemp(prefix="drift-self-")
    seen = []
    try:
        def contents_big(url):
            seen.append(("contents", url))
            return {"content": None, "size": 1100000}, None       # 200 无正文 = 文件过大

        def raw_ok(url):
            seen.append(("raw", url))
            return ("handler: int = 1" + NL + "value = 2").encode("utf-8"), None

        want_lines = ["handler: int = 1", "value = 2"]
        lines, err = upstream_lines("plugin/settings.py", "deadbeefdeadbeef",
                                    get=contents_big, get_raw=raw_ok, cache_dir=cache)
        cases.append(((err, lines), (None, want_lines)))
        cases.append((seen[1][1],
                      "https://raw.githubusercontent.com/%s/deadbeefdeadbeef/plugin/settings.py"
                      % UPSTREAM))
        # 第二次应当命中缓存：再取就一个网络调用都不发
        calls = []
        lines2, err2 = upstream_lines("plugin/settings.py", "deadbeefdeadbeef",
                                      get=lambda u: calls.append(u) or ({}, None),
                                      get_raw=lambda u: calls.append(u) or (None, None),
                                      cache_dir=cache)
        cases.append(((len(calls), err2, lines2), (0, None, want_lines)))
        # raw 自己失败时按故障类型分账：断网不算「上游删了文件」，404 才算
        lines3, err3 = upstream_lines("plugin/big.py", "cafe1234cafe1234", get=contents_big,
                                      get_raw=lambda u: (None, "net"), cache_dir=cache)
        cases.append(((err3, lines3), ("net", None)))
        lines4, err4 = upstream_lines("plugin/gone.py", "cafe1234cafe1234", get=contents_big,
                                      get_raw=lambda u: (None, "missing"), cache_dir=cache)
        cases.append(((err4, lines4), ("missing", None)))
        # check() 层的判据。光测 _all_blank 这个纯函数不够：接线断了照样绿。
        fake_src = ("def handler():" + NL + "    x = 1" + NL + NL + "    return x" + NL)
        b64 = base64.b64encode(fake_src.encode("utf-8")).decode("ascii")

        def get_inline(url):
            return {"content": b64}, None

        def an(n):
            return {"path": "plugin/a.py", "line": 1, "nums": [n],
                    "tokens": ["handler"], "weak": False}

        cache2 = tempfile.mkdtemp(prefix="drift-check-")
        try:
            for n, want_status in ((3, "drift"), (1, "ok"), (2, "ok")):
                got_status = check([an(n)], "f00df00df00d",
                                   get=get_inline, cache_dir=cache2)[0]["status"]
                cases.append((got_status, want_status))
            # 大文件也要能过 check：Contents 不给正文时由 raw 补上，判据照旧
            def get_big_no_body(url):
                return {"content": None}, None

            got_big = check([an(1)], "beefbeefbeef", get=get_big_no_body,
                            get_raw=lambda u: (fake_src.encode("utf-8"), None),
                            cache_dir=tempfile.mkdtemp(prefix="drift-big-"))[0]["status"]
            cases.append((got_big, "ok"))
        finally:
            shutil.rmtree(cache2, ignore_errors=True)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    bad = [(i, got, want) for i, (got, want) in enumerate(cases) if got != want]
    for i, got, want in bad:
        print("  自检失败 #%d: 得到 %r 期望 %r" % (i, got, want))
    print("探针自检 %d 例：%s" % (len(cases), "全部通过" if not bad else "%d 例失败" % len(bad)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="profile 锚点漂移探针")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="有漂移或有 weak 锚点就退出 1（默认只报告）")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存重拉")
    ap.add_argument("--self-test", action="store_true", help="只跑自检，不碰网络")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not os.path.isfile(a.profile):
        print("用法错误: 找不到 profile:", a.profile)
        return 2
    if a.fresh and os.path.isdir(CACHE_DIR):
        for fn in os.listdir(CACHE_DIR):
            os.remove(os.path.join(CACHE_DIR, fn))

    text = open(a.profile, encoding="utf-8-sig").read().replace("\r\n", NL)
    anchors = collect(text)

    head, from_cache = resolve_head(use_cache=not a.fresh)
    if not head:
        print("探针取不到上游 %s 的当前 sha（匿名限额、离线或凭据问题），本次不作判定。" % UPSTREAM)
        return 2

    rows = check(anchors, head)
    unreachable = [r for r in rows if r["status"] == "unreachable"]
    weak = [r for r in rows if r["status"] == "weak"]
    drift = [r for r in rows if r["status"] in ("drift", "gone")]
    ok = len(rows) - len(drift) - len(unreachable) - len(weak)
    if a.json:
        print(json.dumps({"upstream": UPSTREAM, "head": head, "head_from_cache": from_cache,
                          "anchors": len(rows), "ok": ok, "weak": len(weak),
                          "unreachable": len(unreachable), "drift": drift,
                          "weak_rows": [{"line": r["line"], "path": r["path"],
                                         "nums": r["nums"]} for r in weak]},
                         ensure_ascii=False, indent=1))
    else:
        print("上游 %s，当前 main = %s%s" % (UPSTREAM, head[:9],
              "（取自 %d 小时内的缓存，--fresh 可强制重取）" % (HEAD_TTL // 3600) if from_cache else ""))
        print("锚点核对 %d 条：对得上 %d，漂移 %d，核不了 %d，取不到 %d"
              % (len(rows), ok, len(drift), len(weak), len(unreachable)))
        for r in (drift + unreachable + weak)[:30]:
            print("  [%s] profile 第 %d 行  %s  记录号 %s\n        %s"
                  % (r["status"], r["line"], r["path"], "/".join(map(str, r["nums"][:6])), r["detail"]))
        if len(drift) + len(unreachable) + len(weak) > 30:
            print("  ...其余 %d 条用 --json 查看" % (len(drift) + len(unreachable) + len(weak) - 30))
        if unreachable:
            print("结论：%d 条锚点本次无法判定（取不到上游，不算漂移）。" % len(unreachable))
        elif weak:
            print("结论：%d 条锚点没有可比对的符号，只有路径与行号，这几条不算核过。"
                  "把符号名补进 profile 那句里，探针才管得住它。" % len(weak))
        else:
            print("结论：" + ("全部锚点在当前 main 上仍对得上。" if not drift else
                            "行号已漂。按 detail 更新行号，或把锚点降成「只写标识符 + 读于 <sha>」。"))
    if unreachable:
        return 2
    # 2026-11-17：CI 这一步已从「只报」改成阻塞。weak 也要算失败——它的意思是我们自己的锚点没写符号，
    # 上游怎么变都验不出来。让它静默通过等于在门禁里留一格假绿。
    return 1 if (a.strict and (drift or weak)) else 0


if __name__ == "__main__":
    sys.exit(main())
