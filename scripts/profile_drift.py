#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profile 锚点漂移探针（零第三方依赖，Python 3.8+；网络只走 gh 的只读 API）

用途
  profile-neko.md 的锚点写法不统一：`(1050 行，读于 a222e50)`、`（id 212、name 213）`、
  `plugins.py:28`、`runs.py:35` 都有。行号会随上游漂移，而这份 profile 是团队的契约来源。
  本脚本把「路径 + 紧跟该路径的行号」抽出来，对**当前 main**逐条核对：记录的那一行上是否还
  出现该行附近的标识符。不在了，就给出该标识符现在在哪。

用法
  python scripts/profile_drift.py [--profile 路径] [--json] [--strict] [--limit N] [--fresh]

  默认只报告：不改文件、不因漂移失败（退出码 0）。--strict 时有漂移退 1，留给以后要 blocking
  的场合；CI 现在按非阻塞接。--fresh 忽略缓存重拉。

缓存
  拉到的上游文件放在 scripts/.drift-cache/（已加进 .gitignore），避免一次跑打上百次 API。

退出码：0 报告完成；--strict 且有漂移时 1；取不到上游时 2。
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
UPSTREAM = "Project-N-E-K-O/N.E.K.O"
NL = chr(10)

PATH_RE = re.compile(r"(?<![\w/.\-])((?:plugin|app|config|docs|nekoclaw|memory|launcher_core|"
                     r"main_logic|\.agent)/[\w./\-]*\.(?:py|md|toml|json|js|ts))")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
# 只认这三种形状：NNN 行、:NNN、（NNN 或（NNN/NNN。issue 号带 #、章节号带 §，都不会被吃进来。
NUM_PATTERNS = (re.compile(r"(?<![\w.\-])(\d{1,5})(?=\s*行)"),
                re.compile(r":(\d{1,5})(?![\d%])"),
                re.compile(r"[（(]\s*(\d{1,5}(?:\s*[/、,]\s*\d{1,5})*)"))
STOPWORDS = {"plugin", "read", "line", "sha", "fact", "def", "main", "code", "true", "false",
             "none", "str", "int", "bool", "list", "dict", "self", "return", "import", "from",
             "https", "http", "www", "md", "py", "toml", "json", "get", "post", "put", "delete",
             "api", "ctx", "sdk", "app", "pinned", "readme", "guide", "this", "that", "with"}


def api(url):
    """直连 GitHub REST，不依赖 gh：CI 里没有交互式登录也能跑。有 GITHUB_TOKEN 就带上。"""
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
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, OSError):
        return None


def resolve_head(use_cache=True):
    mark = os.path.join(CACHE_DIR, "head.txt")
    if use_cache and os.path.isfile(mark):
        with open(mark, encoding="utf-8") as f:
            sha = f.read().strip()
        if sha:
            return sha
    meta = api("https://api.github.com/repos/" + UPSTREAM)
    branch = (meta or {}).get("default_branch") or "main"
    data = api("https://api.github.com/repos/%s/commits/%s" % (UPSTREAM, branch))
    sha = (data or {}).get("sha", "")
    if sha:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(mark, "w", encoding="utf-8") as f:
            f.write(sha)
    return sha


def upstream_lines(path, head):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fn = os.path.join(CACHE_DIR, "%s.%s.txt" % (head[:8], path.replace("/", "__")))
    if os.path.isfile(fn):
        with open(fn, encoding="utf-8", errors="replace") as f:
            return f.read().split(NL)
    data = api("https://api.github.com/repos/%s/contents/%s?ref=%s" % (UPSTREAM, path, head))
    if not data or not data.get("content"):
        return None
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    except Exception:
        return None
    with open(fn, "w", encoding="utf-8", newline=NL) as f:
        f.write(text)
    return text.split(NL)


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


def collect(text):
    """每个路径只看它自己之后、下一个路径之前的那段文字。"""
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
            toks = [x for x in IDENT_RE.findall(seg)
                    if x.lower() not in STOPWORDS and not re.fullmatch(r"[0-9a-fA-F]{7,}", x)]
            if not toks:
                toks = [x for x in IDENT_RE.findall(line)
                        if x.lower() not in STOPWORDS and not re.fullmatch(r"[0-9a-fA-F]{7,}", x)]
            anchors.append({"line": no, "path": m.group(1), "nums": nums[:10],
                            "tokens": list(dict.fromkeys(toks))[:6],
                            "text": line.strip()[:120]})
    seen, uniq = set(), []
    for an in anchors:
        k = (an["path"], tuple(an["nums"]))
        if k not in seen:
            seen.add(k)
            uniq.append(an)
    return uniq


def enclosing(lines, token, n):
    """记录行是否落在该标识符的定义域内。装饰器行不含函数名、区间落在函数体内，都属正常。"""
    starts = [i for i, ln in enumerate(lines, 1) if token in ln]
    if not starts:
        return False
    top = re.compile(r"^(?:async\s+)?(?:def|class)\s+\w|^\w.*=\s*(?:Field|Literal)\b")
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


def check(anchors, head):
    file_cache, rows = {}, []
    for an in anchors:
        path = an["path"]
        if path not in file_cache:
            file_cache[path] = upstream_lines(path, head)
        lines = file_cache[path]
        if lines is None:
            rows.append(dict(an, status="gone", detail="上游取不到该文件（可能已改名或删除）"))
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
        if an["tokens"] and present and all(t not in here for t in an["tokens"]):
            # 再退一档：记录行是否落在某个标识符的定义体内（区间锚、装饰器锚都属这种）
            inside = [t for t in an["tokens"] if any(enclosing(lines, t, n) for n in present)]
            if not inside:
                moved = []
                for t in an["tokens"][:3]:
                    w = where_symbol(lines, t)
                    moved.append("%s→%s" % (t, "/".join(map(str, w)) if w else "未找到"))
                drift.append("记录行上没有本行任何标识符，也不在其定义体内；" + ", ".join(moved))
        rows.append(dict(an, status="drift" if drift else "ok", detail="; ".join(drift)))
    return rows


def main():
    ap = argparse.ArgumentParser(description="profile 锚点漂移探针")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有漂移就退出 1（默认只报告）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fresh", action="store_true", help="忽略缓存重拉")
    a = ap.parse_args()

    if not os.path.isfile(a.profile):
        print("用法错误: 找不到 profile:", a.profile)
        return 2
    if a.fresh and os.path.isdir(CACHE_DIR):
        for fn in os.listdir(CACHE_DIR):
            os.remove(os.path.join(CACHE_DIR, fn))

    text = open(a.profile, encoding="utf-8-sig").read().replace("\r\n", NL)
    anchors = collect(text)
    if a.limit:
        anchors = anchors[:a.limit]

    head = resolve_head(use_cache=not a.fresh)
    if not head:
        print("探针取不到上游 %s 的当前 sha（gh 未授权或离线），本次不作判定。" % UPSTREAM)
        return 2

    rows = check(anchors, head)
    drift = [r for r in rows if r["status"] != "ok"]
    if a.json:
        print(json.dumps({"upstream": UPSTREAM, "head": head, "anchors": len(rows),
                          "ok": len(rows) - len(drift), "drift": drift}, ensure_ascii=False, indent=1))
    else:
        print("上游 %s，当前 main = %s" % (UPSTREAM, head[:9]))
        print("锚点核对 %d 条：对得上 %d，有问题 %d" % (len(rows), len(rows) - len(drift), len(drift)))
        for r in drift[:30]:
            print("  [%s] profile 第 %d 行  %s  记录号 %s\n        %s"
                  % (r["status"], r["line"], r["path"], "/".join(map(str, r["nums"][:6])), r["detail"]))
        if len(drift) > 30:
            print("  ...其余 %d 条用 --json 查看" % (len(drift) - 30))
        print("结论：" + ("全部锚点在当前 main 上仍对得上。" if not drift else
                        "行号已漂。按 detail 更新行号，或把锚点降成「只写标识符 + 读于 <sha>」。"))
    return 1 if (a.strict and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
