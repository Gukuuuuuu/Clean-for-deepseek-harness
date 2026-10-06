#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

PROG = "dsh-clean"
APP_BUNDLE_ID = "@deepseek-ai/dsh-desktop"
SAFE_CHAR_RE = re.compile(r"^[A-Za-z0-9._-]$")
SPILL_DIR_RE = re.compile(r"^dsh-spill-[A-Za-z0-9]{6}$")

VERBOSE = False
HOME_LABEL = "~/.dsh"


# --------------------------------------------------------------------------- 输出
def info(msg: str = "") -> None:
    print(msg)


def vinfo(msg: str) -> None:
    if VERBOSE:
        print("  · " + msg)


def warn(msg: str) -> None:
    print("警告: " + msg)


def step(msg: str) -> None:
    print("→ " + msg)


def ok(msg: str) -> None:
    print("  ✔ " + msg)


def fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    sys.stdout.flush()
    print("错误: " + msg, file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- 工具
def human(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return ("%d %s" if u == "B" else "%.1f %s") % (f, u)
        f /= 1024
    return "%d B" % n


def abbr(path: str, home: str) -> str:
    ap, ah = os.path.abspath(path), os.path.abspath(home)
    if ap == ah:
        return HOME_LABEL
    if ap.startswith(ah + os.sep):
        return HOME_LABEL + "/" + os.path.relpath(ap, ah)
    return path


def is_under(path: str, root: str) -> bool:
    try:
        p, r = os.path.abspath(path), os.path.abspath(root)
        return os.path.commonpath([p, r]) == r
    except ValueError:
        return False


def require_under(path: str, root: str) -> None:
    if not is_under(path, root):
        fail("拒绝删除允许范围之外的路径: %s（允许范围: %s）" % (path, root))


def du(path: str):
    """返回 (字节数, 文件数)，不跟随符号链接。"""
    if os.path.islink(path):
        return 0, 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path), 1
        except OSError:
            return 0, 0
    total = count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
                count += 1
            except OSError:
                pass
    return total, count


def shred_file(path: str) -> None:
    """尽力而为地用随机字节覆盖一次（APFS/SSD 不保证物理擦除）。"""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return
        with open(path, "r+b", buffering=0) as fh:
            done = 0
            while done < size:
                chunk = min(1 << 16, size - done)
                fh.write(os.urandom(chunk))
                done += chunk
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def shred_tree(path: str) -> None:
    for root, dirs, files in os.walk(path):
        for fn in files:
            shred_file(os.path.join(root, fn))


def remove_path(path: str, root: str, shred: bool = False) -> int:
    """删除文件/目录（符号链接只删链接本身）。返回删除的字节数。"""
    require_under(path, root)
    if os.path.islink(path):
        os.unlink(path)
        return 0
    if os.path.isfile(path):
        size = os.path.getsize(path)
        if shred:
            shred_file(path)
        os.unlink(path)
        return size
    if os.path.isdir(path):
        size, _ = du(path)
        if shred:
            shred_tree(path)
        shutil.rmtree(path)
        return size
    return 0


def write_json_atomic(path: str, doc) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    data = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ------------------------------------------------- DSH 路径编码（与 DSH 源码一致）
def encode_segment(raw: str) -> str:
    """dsh-session-persistence-jsonl 的单射路径段编码。"""
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for ch in raw:
        if ch != "~" and SAFE_CHAR_RE.match(ch):
            out.append(ch)
        else:
            out.append("~%04X" % ord(ch))
    return "".join(out)


def decode_segment(seg: str) -> str:
    out = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "~" and i + 5 <= len(seg):
            try:
                out.append(chr(int(seg[i + 1:i + 5], 16)))
                i += 5
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def project_key(cwd: str) -> str:
    """项目目录名编码（分隔符折叠为 '-'，有损、可读）。"""
    readable = []
    sep = False
    for ch in cwd:
        if ch in "/\\:":
            if not sep:
                readable.append("-")
            sep = True
        elif ch != "~" and SAFE_CHAR_RE.match(ch):
            readable.append(ch)
            sep = False
        else:
            readable.append("~%04X" % ord(ch))
            sep = False
    s = re.sub(r"^-+", "", "".join(readable)) or "root"
    return "--%s--" % s[:251]


def project_dir_name(cwd) -> str:
    return "_no-cwd" if cwd is None else project_key(cwd)


def canonical(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


# --------------------------------------------------------------------------- 扫描
class Conversation:
    __slots__ = ("id", "title", "cwd", "dirs", "projcache", "workspaces", "nbytes", "nfiles")

    def __init__(self, sid: str):
        self.id = sid
        self.title = None
        self.cwd = None
        self.dirs = []
        self.projcache = None
        self.workspaces = []       # [(title, path, workspaceId)]
        self.nbytes = 0
        self.nfiles = 0

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        if self.workspaces:
            return self.workspaces[0][0] or "(未命名)"
        return "(未命名)"


def load_workspace_doc(home: str):
    p = os.path.join(home, "storages", "workspace.json")
    if not os.path.isfile(p):
        return None, p
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh), p
    except Exception as exc:  # noqa: BLE001
        warn("无法解析 %s: %s" % (p, exc))
        return None, p


def scan(home: str):
    convs = {}

    def get(sid: str) -> Conversation:
        c = convs.get(sid)
        if c is None:
            c = Conversation(sid)
            convs[sid] = c
        return c

    sessions_root = os.path.join(home, "sessions")
    if os.path.isdir(sessions_root):
        for pname in sorted(os.listdir(sessions_root)):
            pdir = os.path.join(sessions_root, pname)
            if os.path.islink(pdir) or not os.path.isdir(pdir):
                continue
            for sname in sorted(os.listdir(pdir)):
                sdir = os.path.join(pdir, sname)
                if os.path.islink(sdir) or not os.path.isdir(sdir):
                    continue
                c = get(decode_segment(sname))
                c.dirs.append(sdir)
                b, f = du(sdir)
                c.nbytes += b
                c.nfiles += f

    projdir = os.path.join(home, "storages", "session_projcache", "sessions")
    if os.path.isdir(projdir):
        for fn in sorted(os.listdir(projdir)):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(projdir, fn)
            if os.path.islink(fp) or not os.path.isfile(fp):
                continue
            c = get(decode_segment(fn[:-5]))
            c.projcache = fp
            b, f = du(fp)
            c.nbytes += b
            c.nfiles += f
            try:
                with open(fp, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except Exception:  # noqa: BLE001
                doc = None
            if isinstance(doc, dict):
                rec = doc.get("record") if isinstance(doc.get("record"), dict) else {}
                ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
                if isinstance(ident.get("cwd"), str):
                    c.cwd = ident["cwd"]
                rows = rec.get("rows") if isinstance(rec.get("rows"), dict) else {}
                trow = rows.get("title")
                if isinstance(trow, dict) and isinstance(trow.get("val"), str) and trow["val"].strip():
                    c.title = trow["val"].strip()

    wsdoc, wspath = load_workspace_doc(home)
    if isinstance(wsdoc, dict):
        tables = wsdoc.get("tables") if isinstance(wsdoc.get("tables"), dict) else {}
        workspaces = tables.get("workspaces") if isinstance(tables.get("workspaces"), dict) else {}
        for wid, rec in workspaces.items():
            if not isinstance(rec, dict):
                continue
            wpath = rec.get("path") if isinstance(rec.get("path"), str) else ""
            wtitle = rec.get("title") if isinstance(rec.get("title"), str) else ""
            for sid in rec.get("sessionIds") or []:
                if not isinstance(sid, str):
                    continue
                c = get(sid)
                c.workspaces.append((wtitle, wpath, wid))
                if not c.cwd and wpath:
                    c.cwd = wpath
        g = wsdoc.get("global") if isinstance(wsdoc.get("global"), dict) else {}
        for key in ("archivedSessionIds", "pinnedSessionIds"):
            for sid in g.get(key) or []:
                if isinstance(sid, str):
                    get(sid)

    return convs, wsdoc, wspath


# --------------------------------------------------------------------------- 选择
def select(convs, args):
    targeted = bool(args.session or args.workspace or args.match)
    if not targeted:
        chosen = dict(convs)
    else:
        chosen = {}

        def add(c: Conversation):
            chosen[c.id] = c

        for raw in args.session:
            forms = {raw, "session-" + raw}
            hit = [c for sid, c in convs.items() if sid in forms]
            if not hit:
                warn("未找到对话: %s" % raw)
            for c in hit:
                add(c)

        for wp in args.workspace:
            target = canonical(wp)
            key = project_dir_name(target)
            hit = []
            for c in convs.values():
                if c.cwd and canonical(c.cwd) == target:
                    hit.append(c)
                    continue
                if any(p and canonical(p) == target for _, p, _ in c.workspaces):
                    hit.append(c)
                    continue
                if c.cwd is None and any(os.path.basename(os.path.dirname(d)) == key for d in c.dirs):
                    hit.append(c)
            if not hit:
                warn("未找到属于工作区 %s 的对话" % wp)
            for c in hit:
                add(c)

        for pat in args.match:
            try:
                rx = re.compile(pat)
            except re.error as exc:
                fail("无效正则 %r: %s" % (pat, exc))
            for c in convs.values():
                if rx.search(c.id) or (c.title and rx.search(c.title)) or (c.cwd and rx.search(c.cwd)):
                    add(c)

    for raw in args.keep_session:
        forms = {raw, "session-" + raw}
        for sid in list(chosen):
            if sid in forms:
                del chosen[sid]

    return {sid: chosen[sid] for sid in sorted(chosen)}


# --------------------------------------------------------------------------- 清理规则
def scrub_json(node, ids, count=None):
    """递归移除对已删会话的引用。返回 (新节点, 是否整体丢弃, 命中数)。"""
    if count is None:
        count = [0]
    if isinstance(node, str):
        if node in ids:
            count[0] += 1
            return None, True, count[0]
        return node, False, count[0]
    if isinstance(node, dict):
        for key in ("sessionId", "session_id", "id"):
            val = node.get(key)
            if isinstance(val, str) and val in ids:
                count[0] += 1
                return None, True, count[0]
        out = {}
        for k, v in node.items():
            if k in ids:
                count[0] += 1
                continue
            nv, dropped, _ = scrub_json(v, ids, count)
            if dropped:
                continue
            out[k] = nv
        return out, False, count[0]
    if isinstance(node, list):
        out = []
        for v in node:
            nv, dropped, _ = scrub_json(v, ids, count)
            if dropped:
                continue
            out.append(nv)
        return out, False, count[0]
    return node, False, count[0]


def scrub_workspace(doc, ids, drop_empty: bool, reset: bool):
    d = json.loads(json.dumps(doc))
    g = d.get("global") if isinstance(d.get("global"), dict) else {}
    d["global"] = g
    refs = 0
    for key in ("archivedSessionIds", "pinnedSessionIds"):
        arr = g.get(key)
        if isinstance(arr, list):
            keep = [x for x in arr if not (isinstance(x, str) and x in ids)]
            refs += len(arr) - len(keep)
            g[key] = keep

    tables = d.get("tables") if isinstance(d.get("tables"), dict) else {}
    d["tables"] = tables
    workspaces = tables.get("workspaces") if isinstance(tables.get("workspaces"), dict) else {}
    tables["workspaces"] = workspaces

    emptied = []
    for wid, rec in workspaces.items():
        if not isinstance(rec, dict):
            continue
        arr = rec.get("sessionIds")
        if isinstance(arr, list):
            keep = [x for x in arr if not (isinstance(x, str) and x in ids)]
            refs += len(arr) - len(keep)
            rec["sessionIds"] = keep
        if not rec.get("sessionIds"):
            emptied.append(wid)

    notes = []
    if reset:
        d["global"] = {
            "initialized": False,
            "workspaceIds": [],
            "archivedSessionIds": [],
            "pinnedSessionIds": [],
        }
        tables["workspaces"] = {}
        notes.append("工作区注册表已重置为首次启动状态（下次启动会重新创建默认工作区）")
    elif drop_empty and emptied:
        for wid in emptied:
            workspaces.pop(wid, None)
        g["workspaceIds"] = [w for w in (g.get("workspaceIds") or []) if w not in emptied]
        if g.get("defaultWorkspaceId") in emptied:
            g.pop("defaultWorkspaceId", None)
            notes.append("默认工作区记录已删除，defaultWorkspaceId 已清除")
        pm = g.get("pendingMutation")
        if isinstance(pm, dict) and pm.get("workspaceId") in emptied:
            g.pop("pendingMutation", None)
        notes.append("已删除 %d 个不再拥有任何对话的工作区记录" % len(emptied))
    return d, refs, notes


def scan_storage_units(storages_root, ids, skip_paths):
    edits = []
    if not os.path.isdir(storages_root):
        return edits
    for root, _dirs, files in os.walk(storages_root):
        for fn in files:
            fp = os.path.join(root, fn)
            if fp in skip_paths or os.path.islink(fp):
                continue
            # 以会话 ID 命名的逐记录文件（含 <id>.json / <id>.json.tmp / .<id>.tmp-… 等形式）
            head = fn.lstrip(".").split(".")[0]
            if head and (head in ids or decode_segment(head) in ids):
                edits.append({"path": fp, "kind": "delete", "hits": 1})
                continue
            if not fn.endswith(".json"):
                continue
            try:
                with open(fp, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
            new, _dropped, hits = scrub_json(doc, ids)
            if hits:
                edits.append({"path": fp, "kind": "rewrite", "doc": new, "hits": hits})
    return edits


# --------------------------------------------------------------------------- 环境
def resolve_home(explicit):
    if explicit:
        p = explicit
    else:
        env = os.environ.get("DSH_HOME", "")
        p = env if env.strip() else os.path.join(os.path.expanduser("~"), ".dsh")
    return canonical(p)


def looks_like_dsh_home(home: str) -> bool:
    markers = ("sessions", "storages", "profiles", ".credentials.yaml", "attachments", "cache")
    return any(os.path.exists(os.path.join(home, m)) for m in markers)


def default_app_state_dir():
    user = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(user, "Library", "Application Support", APP_BUNDLE_ID)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(user, "AppData", "Roaming")
        return os.path.join(base, APP_BUNDLE_ID)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(user, ".config")
    return os.path.join(base, APP_BUNDLE_ID)


APP_STATE_SUBDIRS = (
    "Local Storage",
    "Session Storage",
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "blob_storage",
    "Shared Dictionary",
    # Electron 运行期残留（退出后是无主残留文件/链接，删掉无害，会被重建）
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
)

# 明确保留、不删的：Cookies / Local State / Preferences / DIPS / Trust Tokens /
# Network Persistent State —— 它们只承载登录态与浏览器运行状态，不含对话内容，
# 保留可避免重新登录账号。


def default_log_dirs():
    if sys.platform == "darwin":
        p = os.path.join(os.path.expanduser("~"), "Library", "Logs", "DeepSeek Harness")
        return [p] if os.path.isdir(p) else []
    return []


def macos_bundle_id():
    """从应用包 Info.plist 读取 Bundle ID（macOS 偏好域），失败则退回已知值。"""
    if sys.platform != "darwin":
        return ""
    candidates = []
    env_bundle = os.environ.get("DSH_APP_BUNDLE", "").strip()
    if env_bundle:
        candidates.append(os.path.join(env_bundle, "Contents", "Info.plist"))
    for base in ("/Applications", os.path.join(os.path.expanduser("~"), "Applications")):
        candidates.append(os.path.join(base, "DeepSeek Harness.app", "Contents", "Info.plist"))
    info = next((p for p in candidates if os.path.isfile(p)), "")
    if info:
        try:
            out = subprocess.run(
                ["/usr/bin/plutil", "-extract", "CFBundleIdentifier", "raw", info],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
    return "com.deepseek.dsh"


def macos_crash_reports(bundle_id: str):
    """本应用的崩溃报告（可能含路径/内存片段），存在才纳入清理。"""
    if sys.platform != "darwin":
        return []
    d = os.path.join(os.path.expanduser("~"), "Library", "Logs", "DiagnosticReports")
    if not os.path.isdir(d):
        return []
    hits = []
    for fn in os.listdir(d):
        low = fn.lower()
        if ("deepseek" in low or "dsh" in low) and fn.endswith((".ips", ".crash", ".diag")):
            hits.append(os.path.join(d, fn))
    return sorted(hits)


def macos_pref_path_traces(bundle_id: str):
    """macOS 偏好里的“最近目录”痕迹（不是对话内容，但是使用痕迹）。

    返回 [(plist 路径, 需要删除的 key)]；删除走 /usr/bin/defaults，
    因为 cfprefsd 会缓存偏好，直接删文件可能被回写。
    """
    if sys.platform != "darwin" or not bundle_id:
        return []
    plist = os.path.join(os.path.expanduser("~"), "Library", "Preferences", bundle_id + ".plist")
    if not os.path.isfile(plist):
        return []
    keys = []
    try:
        out = subprocess.run(
            ["/usr/bin/plutil", "-p", plist], capture_output=True, text=True, timeout=10
        )
        if "NSOSPLastRootDirectory" in out.stdout:
            keys.append("NSOSPLastRootDirectory")
    except Exception:  # noqa: BLE001
        pass
    return [(plist, k) for k in keys]


def macos_forensic_notes():
    """macOS 上“是否真的物理不可恢复”的实话：FileVault 与本地快照。"""
    if sys.platform != "darwin":
        return []
    notes = []
    try:
        out = subprocess.run(
            ["/usr/bin/fdesetup", "status"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        if "FileVault is On" in out:
            notes.append("FileVault 已开启：磁盘上的已删块受加密保护。")
        else:
            notes.append(
                "FileVault 未开启（%s）：删除只是解除文件链接，SSD 上仍可能被取证工具恢复；"
                "若要物理级不可恢复，请开启 FileVault，或整盘抹除后再处置设备。" % (out or "未知状态")
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["/usr/bin/tmutil", "listlocalsnapshots", "/"], capture_output=True, text=True, timeout=20
        ).stdout.strip()
        snaps = [l.strip() for l in out.splitlines() if l.strip().startswith("com.apple.")]
        user_snaps = [s for s in snaps if not s.startswith("com.apple.os.update")]
        if user_snaps:
            notes.append(
                "检测到 %d 个本地 APFS 快照，可能仍保留删除前的文件副本；"
                "确认不需要后用：sudo tmutil deletelocalsnapshots /" % len(user_snaps)
            )
        else:
            notes.append("没有用户级 APFS 本地快照（只有系统更新快照，系统会自动回收）。")
    except Exception:  # noqa: BLE001
        pass
    return notes


def temp_roots():
    roots = []
    for cand in (os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp"):
        if cand and os.path.isdir(cand):
            c = canonical(cand)
            if c not in roots:
                roots.append(c)
    return roots


def find_spill_dirs():
    """临时目录里的 spill 根目录（含 Electron 每次启动的 scoped_dir* 内层）。"""
    found = []

    def scan(root):
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            p = os.path.join(root, name)
            if SPILL_DIR_RE.match(name):
                if os.path.isdir(p) and not os.path.islink(p):
                    found.append(p)
            elif name.startswith("scoped_dir") and os.path.isdir(p) and not os.path.islink(p):
                scan(p)

    for root in temp_roots():
        scan(root)
    return sorted(set(found))


def find_dsh_processes():
    """查找 DSH 相关进程。返回 (进程列表, 检查是否成功执行)。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20
            ).stdout
        except Exception:  # noqa: BLE001
            return [], False
        if not out.strip():
            return [], False
        hits = [
            (0, line.strip())
            for line in out.splitlines()
            if "deepseek" in line.lower() or "dsh" in line.lower()
        ]
        return hits, True

    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=20
        )
    except Exception:  # noqa: BLE001
        return [], False
    if proc.returncode != 0 or not proc.stdout.strip():
        return [], False

    me = os.getpid()
    pat = re.compile(
        r"DeepSeek Harness\.app|dsh-desktop|dsh-headless|@deepseek-ai/dsh-[a-z-]+/lib"
        r"|(?:^|/)dsh\s+(?:web|serve|headless)\b"
    )
    hits = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, cmd = parts
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == me or PROG in cmd or "dsh-wipe-conversations" in cmd:
            continue
        if pat.search(cmd):
            hits.append((pid, cmd[:160]))
    return hits, True


def app_singleton_lock(appdir: str):
    """桌面端运行时会留下 SingletonLock（崩溃后可能是残留，仅作提示）。"""
    p = os.path.join(appdir, "SingletonLock")
    return p if os.path.lexists(p) else None


def locked_session_locks(home):
    """返回当前被其它进程持有 flock 的 session.lock（能发现“正在写入的会话”）。"""
    if os.name == "nt":
        return []
    try:
        import fcntl
    except ImportError:
        return []
    held = []
    root = os.path.join(home, "sessions")
    if not os.path.isdir(root):
        return held
    for dirpath, _dirs, files in os.walk(root):
        if "session.lock" not in files:
            continue
        fp = os.path.join(dirpath, "session.lock")
        if os.path.islink(fp):
            continue
        try:
            fd = os.open(fp, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                held.append(fp)
        finally:
            os.close(fd)
    return sorted(held)


# --------------------------------------------------------------------------- 计划
def build_plan(args, home, convs, selected, wsdoc, wspath):
    ids = set(selected)
    plan = {
        "ids": ids,
        "selected": list(selected.values()),
        "session_dirs": [],
        "projcache_files": [],
        "workspace_json": None,
        "workspace_backups": [],
        "storage_edits": [],
        "attachments": [],
        "attachments_kept": [],
        "caches": [],
        "spill": [],
        "app_state": [],
        "app_state_kept": False,
        "logs": [],
        "crash_reports": [],
        "pref_traces": [],
        "purged_app_dir": "",
    }

    for c in selected.values():
        plan["session_dirs"].extend(c.dirs)
        if c.projcache:
            plan["projcache_files"].append(c.projcache)

    # workspace.json + 备份
    if wsdoc is not None and wspath:
        newdoc, refs, notes = scrub_workspace(
            wsdoc, ids, args.drop_empty_workspaces, args.reset_workspaces
        )
        changed = json.dumps(newdoc, sort_keys=True) != json.dumps(wsdoc, sort_keys=True)
        plan["workspace_json"] = {
            "path": wspath,
            "doc": newdoc,
            "refs": refs,
            "notes": notes,
            "changed": changed,
        }
        sdir = os.path.dirname(wspath)
        base = os.path.basename(wspath)
        if os.path.isdir(sdir):
            for fn in sorted(os.listdir(sdir)):
                if fn.startswith(base + ".bak-"):
                    fp = os.path.join(sdir, fn)
                    if os.path.isfile(fp) and not os.path.islink(fp):
                        try:
                            with open(fp, encoding="utf-8") as fh:
                                bdoc = json.load(fh)
                        except Exception:  # noqa: BLE001
                            continue
                        bnew, brefs, bnotes = scrub_workspace(
                            bdoc, ids, args.drop_empty_workspaces, args.reset_workspaces
                        )
                        if brefs or bnotes:
                            plan["workspace_backups"].append(
                                {"path": fp, "doc": bnew, "refs": brefs, "notes": bnotes}
                            )

    skip = set(plan["projcache_files"])
    if wspath:
        skip.add(wspath)
    for item in plan["workspace_backups"]:
        skip.add(item["path"])
    if args.scrub:
        plan["storage_edits"] = scan_storage_units(
            os.path.join(home, "storages"), ids, skip
        )

    att_dirs = [
        p
        for p in (os.path.join(home, "attachments"), os.path.join(home, "cache", "attachments"))
        if os.path.exists(p)
    ]
    if args.attachments:
        plan["attachments"] = att_dirs
        if os.path.isdir(os.path.join(home, "cache")):
            plan["caches"].append(os.path.join(home, "cache"))
    else:
        plan["attachments_kept"] = att_dirs

    if args.spill:
        plan["spill"] = find_spill_dirs()

    appdir = canonical(args.app_state_dir) if args.app_state_dir else default_app_state_dir()
    if args.app_state:
        if os.path.isdir(appdir):
            if args.purge_app_dir:
                plan["app_state"].append(appdir)
                plan["purged_app_dir"] = appdir
            else:
                for sub in APP_STATE_SUBDIRS:
                    p = os.path.join(appdir, sub)
                    if os.path.exists(p):
                        plan["app_state"].append(p)
            plan["app_state_root"] = appdir
        else:
            warn("未找到桌面端状态目录: %s（跳过 --app-state）" % appdir)
        plan["logs"] = default_log_dirs()
        bundle_id = getattr(args, "pref_domain", None) or macos_bundle_id()
        plan["crash_reports"] = macos_crash_reports(bundle_id)
        plan["pref_traces"] = macos_pref_path_traces(bundle_id)
    else:
        plan["app_state_kept"] = os.path.isdir(appdir)

    return plan


def print_plan(args, home, convs, selected, plan):
    info("=" * 72)
    info("计划：彻底删除 DSH 对话数据")
    info("=" * 72)
    info("DSH home      : %s" % abbr(home, home))
    info("发现的对话数  : %d" % len(convs))
    info("将删除的对话  : %d%s" % (len(selected), "" if plan["session_dirs"] else "（无本地文件，仅清理索引）"))
    info("")

    for i, c in enumerate(plan["selected"], 1):
        info("[%d] %s" % (i, c.id))
        info("    标题      : %s" % c.label)
        info("    工作目录  : %s" % (c.cwd or "(未知)"))
        if c.dirs:
            for d in c.dirs:
                b, f = du(d)
                info("    会话目录  : %s  (%s / %d 个文件)" % (abbr(d, home), human(b), f))
        else:
            info("    会话目录  : （磁盘上不存在）")
        if c.projcache:
            b, f = du(c.projcache)
            info("    投影缓存  : %s  (%s)" % (abbr(c.projcache, home), human(b)))
    info("")

    info("其它清理项：")
    wj = plan["workspace_json"]
    if wj and wj["changed"]:
        info("  · workspace.json: 移除 %d 处会话引用" % wj["refs"])
        for n in wj["notes"]:
            info("      - %s" % n)
    elif wj:
        info("  · workspace.json: 无需改动")
    for b in plan["workspace_backups"]:
        info("  · 备份 %s: 同步移除 %d 处引用" % (abbr(b["path"], home), b["refs"]))
    if plan["storage_edits"]:
        for e in plan["storage_edits"]:
            if e["kind"] == "delete":
                info("  · 删除记录文件 %s" % abbr(e["path"], home))
            else:
                info("  · 改写 %s（清除 %d 处引用）" % (abbr(e["path"], home), e["hits"]))
    elif args.scrub:
        info("  · 其它存储单元: 未发现会话引用")
    for p in plan["attachments"]:
        b, f = du(p)
        info("  · %s  (%s / %d 个文件)" % (abbr(p, home), human(b), f))
    for p in plan["caches"]:
        b, f = du(p)
        info("  · %s  (%s)" % (abbr(p, home), human(b)))
    if plan["app_state"]:
        if plan["purged_app_dir"]:
            b, f = du(plan["purged_app_dir"])
            info("  · 整目录删除桌面端数据 %s（%s / %d 个文件）" % (plan["purged_app_dir"], human(b), f))
            info("      - 会一并清除 Cookies / 登录态，下次需要重新登录账号")
        else:
            info("  · 桌面端 GUI 状态与缓存: %d 项（%s）" % (len(plan["app_state"]), plan["app_state_root"]))
            for p in plan["app_state"]:
                info("      - %s" % os.path.basename(p))
            info("      - 保留 Cookies / Local State（登录态不丢）")
    if plan["crash_reports"]:
        info("  · 应用崩溃报告: %d 个" % len(plan["crash_reports"]))
        for p in plan["crash_reports"]:
            info("      - %s" % p)
    if plan["pref_traces"]:
        for plist, key in plan["pref_traces"]:
            info("  · macOS 偏好痕迹: 从 %s 删除键 %s（最近使用目录，非对话内容）" % (plist, key))
    if plan["logs"]:
        info("  · 应用日志: %s" % ", ".join(plan["logs"]))
    if plan["spill"]:
        info("  · 临时 spill 目录: %d 个" % len(plan["spill"]))
        for p in plan["spill"]:
            info("      - %s" % p)
    for p in plan["attachments_kept"]:
        b, f = du(p)
        info("  · 保留（未开启 --attachments）: %s  (%s)" % (abbr(p, home), human(b)))
    if plan["app_state_kept"]:
        info("  · 保留（未开启 --app-state）: 桌面端 GUI 本地状态（草稿/布局可能仍引用已删会话）")
    info("")

    if args.backup:
        info("备份目录: %s" % args.backup)
    info("shred 覆盖: %s" % ("开（尽力而为）" if args.shred else "关"))
    info("=" * 72)


# --------------------------------------------------------------------------- 备份
def do_backup(plan, home, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = os.path.join(backup_dir, "dsh-wipe-" + stamp)
    items = list(plan["session_dirs"]) + list(plan["projcache_files"])
    for extra in plan["attachments"]:
        items.append(extra)
    if plan["workspace_json"] and plan["workspace_json"]["changed"]:
        items.append(plan["workspace_json"]["path"])
    for b in plan["workspace_backups"]:
        items.append(b["path"])
    for e in plan["storage_edits"]:
        items.append(e["path"])
    n = 0
    for p in items:
        if not is_under(p, home) or not os.path.exists(p):
            continue
        dest = os.path.join(root, os.path.relpath(p, home))
        os.makedirs(os.path.dirname(dest) or root, exist_ok=True)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.copytree(p, dest, symlinks=True)
            else:
                shutil.copy2(p, dest, follow_symlinks=False)
            n += 1
        except OSError as exc:
            warn("备份失败 %s: %s" % (p, exc))
    ok("已备份 %d 项到 %s" % (n, root))
    return root


# --------------------------------------------------------------------------- 执行
def execute(args, home, plan):
    removed_bytes = 0
    removed_items = 0

    if plan["session_dirs"]:
        step("删除会话正文目录")
        for p in plan["session_dirs"]:
            b = remove_path(p, home, args.shred)
            removed_bytes += b
            removed_items += 1
            ok("%s" % abbr(p, home))
        sessions_root = os.path.join(home, "sessions")
        if os.path.isdir(sessions_root):
            for name in os.listdir(sessions_root):
                p = os.path.join(sessions_root, name)
                if os.path.isdir(p) and not os.path.islink(p) and not os.listdir(p):
                    try:
                        os.rmdir(p)
                        vinfo("移除空项目目录 %s" % abbr(p, home))
                    except OSError:
                        pass

    if plan["projcache_files"]:
        step("删除投影缓存记录")
        for p in plan["projcache_files"]:
            removed_bytes += remove_path(p, home, args.shred)
            removed_items += 1
            ok("%s" % abbr(p, home))

    wj = plan["workspace_json"]
    if wj and wj["changed"]:
        step("更新工作区注册表")
        write_json_atomic(wj["path"], wj["doc"])
        ok("%s（移除 %d 处会话引用）" % (abbr(wj["path"], home), wj["refs"]))
        for n in wj["notes"]:
            ok(n)
    for b in plan["workspace_backups"]:
        write_json_atomic(b["path"], b["doc"])
        ok("备份 %s 同步更新" % abbr(b["path"], home))

    if args.scrub and plan["storage_edits"]:
        step("清理 storages/ 下其它单元中的会话引用")
        for e in plan["storage_edits"]:
            if e["kind"] == "delete":
                removed_bytes += remove_path(e["path"], home, args.shred)
                removed_items += 1
                ok("删除 %s" % abbr(e["path"], home))
            else:
                write_json_atomic(e["path"], e["doc"])
                ok("改写 %s（%d 处）" % (abbr(e["path"], home), e["hits"]))

    if plan["attachments"]:
        step("删除附件对象与图片缓存")
        for p in plan["attachments"]:
            removed_bytes += remove_path(p, home, args.shred)
            removed_items += 1
            ok("%s" % abbr(p, home))

    if plan["caches"]:
        step("删除派生缓存")
        for p in plan["caches"]:
            removed_bytes += remove_path(p, home, args.shred)
            removed_items += 1
            ok("%s" % abbr(p, home))

    if plan["spill"]:
        step("删除临时 spill 目录")
        for p in plan["spill"]:
            roots = temp_roots()
            root = next((r for r in roots if is_under(p, r)), os.path.dirname(p))
            removed_bytes += remove_path(p, root, args.shred)
            removed_items += 1
            ok("%s" % p)

    if plan["app_state"]:
        step("清空桌面端 GUI 状态与缓存")
        root = plan.get("app_state_root") or os.path.dirname(plan["app_state"][0])
        for p in plan["app_state"]:
            removed_bytes += remove_path(p, root, args.shred)
            removed_items += 1
            ok("%s" % p)

    if plan["logs"]:
        step("清空应用日志")
        for d in plan["logs"]:
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.islink(p) or os.path.isfile(p):
                    removed_bytes += remove_path(p, d)
                elif os.path.isdir(p):
                    removed_bytes += remove_path(p, d, args.shred)
            ok(d)

    if plan["crash_reports"]:
        step("删除应用崩溃报告")
        root = os.path.dirname(plan["crash_reports"][0])
        for p in plan["crash_reports"]:
            removed_bytes += remove_path(p, root, args.shred)
            removed_items += 1
            ok(p)

    if plan["pref_traces"]:
        step("清理 macOS 偏好里的使用痕迹")
        for plist, key in plan["pref_traces"]:
            domain = os.path.splitext(os.path.basename(plist))[0]
            try:
                res = subprocess.run(
                    ["/usr/bin/defaults", "delete", domain, key],
                    capture_output=True, text=True, timeout=15,
                )
                if res.returncode == 0:
                    ok("defaults delete %s %s" % (domain, key))
                else:
                    vinfo("defaults delete %s %s 跳过（%s）" % (domain, key, res.stderr.strip()[:80]))
            except Exception as exc:  # noqa: BLE001
                warn("清理偏好键失败: %s" % exc)

    # 收尾：移除 sessions/ 与 storages/ 下已空的目录
    for base in (os.path.join(home, "storages"), os.path.join(home, "sessions")):
        if not os.path.isdir(base):
            continue
        for root, _dirs, _files in os.walk(base, topdown=False):
            if root == base:
                continue
            try:
                if not os.listdir(root):
                    os.rmdir(root)
                    vinfo("移除空目录 %s" % abbr(root, home))
            except OSError:
                pass

    return removed_items, removed_bytes


# --------------------------------------------------------------------------- 验证
def verify(home, ids, plan, args):
    if not ids:
        return []
    needles = [i.encode("utf-8") for i in ids]
    leftovers = []
    skip_dirs = {"node_modules", ".git", "__pycache__"}
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                if os.path.islink(fp) or os.path.getsize(fp) > 8 * 1024 * 1024:
                    continue
                with open(fp, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if any(n in data for n in needles):
                leftovers.append(fp)
    if not args.app_state:
        appdir = canonical(args.app_state_dir) if args.app_state_dir else default_app_state_dir()
        if os.path.isdir(appdir):
            for root, _dirs, files in os.walk(appdir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fp) > 8 * 1024 * 1024:
                            continue
                        with open(fp, "rb") as fh:
                            data = fh.read()
                    except OSError:
                        continue
                    if any(n in data for n in needles):
                        leftovers.append(fp)
                        break
                else:
                    continue
                break
    return leftovers


# ------------------------------------------- 外部副本检查（macOS 常见导出位置）
EXPORT_NAME_RE = re.compile(r"(^session-|^session\.v?\d*\.jsonl|\.jsonl(\.zstd)?$|^sessions?-\d)")
EXPORT_SCAN_SUBDIRS = ("Downloads", "Desktop", "Documents")


def find_external_exports(ids):
    """~/Downloads、~/Desktop、~/Documents 里像“会话导出”的文件（只报告不删）。"""
    home_dir = os.path.expanduser("~")
    roots = [os.path.join(home_dir, d) for d in EXPORT_SCAN_SUBDIRS]
    needles = [i.encode("utf-8") for i in ids]
    hits = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= 2:
                dirs[:] = []
            for fn in files:
                if fn.startswith("."):
                    continue
                fp = os.path.join(dirpath, fn)
                if any(i in fn for i in ids) or EXPORT_NAME_RE.search(fn):
                    hits.append(fp)
                    continue
                try:
                    if os.path.getsize(fp) > 4 * 1024 * 1024:
                        continue
                    with open(fp, "rb") as fh:
                        head = fh.read(512 * 1024)
                except OSError:
                    continue
                if any(n in head for n in needles):
                    hits.append(fp)
    return sorted(set(hits))


# --------------------------------------------------------------------------- 主流程
def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="彻底删除 DeepSeek Harness (DSH) 的本地对话数据（默认仅预览）。",
        epilog=(
            "示例:\n"
            "  %(prog)s                                    预览：全清会做什么\n"
            "  %(prog)s --all --yes                        全清（先退出 DSH！）\n"
            "  %(prog)s --session session-1a2b3c4d-... --yes\n"
            "  %(prog)s --workspace ~/Projects/my-app --yes\n"
            "  %(prog)s --match '密码|token' --yes\n"
        ),
    )
    sel = p.add_argument_group("选择条件（都不给=全部对话）")
    sel.add_argument("--all", action="store_true", help="选中所有对话")
    sel.add_argument("--session", action="append", default=[], metavar="ID",
                     help="按会话 ID 选择（可重复；带或不带 session- 前缀都行）")
    sel.add_argument("--workspace", action="append", default=[], metavar="PATH",
                     help="选择某个工作区目录下的全部对话（可重复）")
    sel.add_argument("--match", action="append", default=[], metavar="REGEX",
                     help="按正则匹配 标题 / ID / 工作目录（可重复）")
    sel.add_argument("--keep-session", action="append", default=[], metavar="ID",
                     help="排除指定会话（可重复）")

    act = p.add_argument_group("动作")
    act.add_argument("--yes", action="store_true", help="真正执行删除（不加则只预览）")
    act.add_argument("--attachments", dest="attachments", action="store_true", default=None,
                     help="删除附件对象与图片缓存（整体清空时默认开启）")
    act.add_argument("--no-attachments", dest="attachments", action="store_false",
                     help="保留附件与图片缓存")
    act.add_argument("--app-state", dest="app_state", action="store_true", default=None,
                     help="清空桌面端 GUI 本地状态与缓存（整体清空时默认开启）")
    act.add_argument("--no-app-state", dest="app_state", action="store_false",
                     help="保留 GUI 本地状态")
    act.add_argument("--purge-app-dir", action="store_true",
                     help="把整个桌面端数据目录一起删掉（含 Cookies/登录态，需重新登录）")
    act.add_argument("--spill", dest="spill", action="store_true", default=None,
                     help="删除临时目录中的 dsh-spill-* 文件（整体清空时默认开启）")
    act.add_argument("--no-spill", dest="spill", action="store_false",
                     help="保留 spill 文件")
    act.add_argument("--scrub-storages", dest="scrub", action="store_true", default=True,
                     help="清理 storages/ 下其它单元里的会话引用（默认开）")
    act.add_argument("--no-scrub-storages", dest="scrub", action="store_false",
                     help="不扫描其它存储单元")
    act.add_argument("--drop-empty-workspaces", action="store_true",
                     help="同时删除 sessionIds 清空后的工作区记录")
    act.add_argument("--reset-workspaces", action="store_true",
                     help="把工作区注册表重置为首次启动状态")
    act.add_argument("--shred", action="store_true",
                     help="删除前用随机字节覆盖（尽力而为，SSD/APFS 不保证）")
    act.add_argument("--backup", metavar="DIR", help="删除前把将删内容备份到 DIR")

    safe = p.add_argument_group("安全 / 环境")
    safe.add_argument("--home", metavar="PATH", help="DSH home（默认 $DSH_HOME 或 ~/.dsh）")
    safe.add_argument("--app-state-dir", metavar="PATH", help="桌面端 userData 目录")
    safe.add_argument("--pref-domain", metavar="BUNDLE_ID",
                      help="覆盖 macOS 偏好域（默认从应用 Info.plist 读取 Bundle ID）")
    safe.add_argument("--force", action="store_true", help="跳过“DSH 正在运行”检查")
    safe.add_argument("-v", "--verbose", action="store_true", help="更详细的输出")
    return p


def main(argv=None) -> int:
    global VERBOSE, HOME_LABEL
    args = build_parser().parse_args(argv)
    VERBOSE = args.verbose

    home = resolve_home(args.home)
    default_home = canonical(os.path.join(os.path.expanduser("~"), ".dsh"))
    HOME_LABEL = "~/.dsh" if home == default_home else home
    if not os.path.isdir(home):
        fail("DSH home 不存在: %s" % home)
    if not looks_like_dsh_home(home) and not args.force:
        fail("目录不像 DSH home（未找到 sessions/storages/profiles 等）: %s\n"
             "     确认 --home 是否正确；确实要用请加 --force。" % home)

    select_all = not (args.session or args.workspace or args.match)
    if args.attachments is None:
        args.attachments = select_all
    if args.app_state is None:
        args.app_state = select_all
    if args.spill is None:
        args.spill = select_all
    if args.reset_workspaces:
        args.drop_empty_workspaces = True

    convs, wsdoc, wspath = scan(home)
    selected = select(convs, args)

    if not selected:
        info("没有匹配的对话，无需删除。")
        return 0

    plan = build_plan(args, home, convs, selected, wsdoc, wspath)
    print_plan(args, home, convs, selected, plan)

    if not args.yes:
        info("以上为预览，未删除任何内容。确认后请加 --yes 执行。")
        return 0

    procs, proc_check_ok = find_dsh_processes()
    held_locks = locked_session_locks(home)
    appdir = canonical(args.app_state_dir) if args.app_state_dir else default_app_state_dir()
    singleton = app_singleton_lock(appdir)
    if singleton:
        warn("桌面端状态目录仍存在 SingletonLock，DSH 可能还在运行（也可能是崩溃残留）：%s" % singleton)
    if not args.force:
        if procs:
            warn("检测到 DSH 相关进程正在运行：")
            for pid, cmd in procs:
                print("    pid %s  %s" % (pid, cmd))
        if held_locks:
            warn("以下会话正被其它进程写入（session.lock 已被持有）：")
            for p in held_locks[:10]:
                print("    " + abbr(p, home))
        if procs or held_locks:
            fail("请先完全退出 DSH（桌面端 / dsh web / headless），再运行本脚本。\n"
                 "     运行中的进程会持有会话文件并以 5 秒节流写回投影缓存，导致删除不彻底。\n"
                 "     确实要在运行中删除请加 --force（不推荐）。")
        if not proc_check_ok:
            fail("无法确认 DSH 是否正在运行（ps/tasklist 不可用，常见于沙箱或受限环境）。\n"
                 "     为避免删到正在写入的会话，本脚本拒绝继续。\n"
                 "     请在普通终端里重试；若确认 DSH 已退出，可加 --force。")
    else:
        if procs:
            warn("忽略运行中的 DSH 进程（--force）：删除可能不彻底，索引可能被重新写回。")
        if held_locks:
            warn("忽略被持有的会话写锁（--force）：正在写入的会话可能删除不完整。")

    if args.backup:
        do_backup(plan, home, canonical(args.backup))

    info("")
    info("开始删除 …")
    items, nbytes = execute(args, home, plan)

    info("")
    info("=" * 72)
    info("完成：处理 %d 项，释放约 %s" % (items, human(nbytes)))
    leftovers = verify(home, plan["ids"], plan, args)
    if leftovers:
        warn("以下文件仍包含已删会话 ID 的明文引用（请检查）：")
        for p in leftovers[:40]:
            print("    " + abbr(p, home))
        if len(leftovers) > 40:
            print("    … 共 %d 个" % len(leftovers))
    else:
        info("验证：DSH home%s 内已无任何已删会话 ID 的明文引用。"
             % ("与 GUI 状态" if args.app_state else ""))

    exports = find_external_exports(plan["ids"])
    if exports:
        warn("在 ~/Downloads、~/Desktop、~/Documents 发现 %d 个疑似会话导出文件" % len(exports))
        warn("（例如用“下载 Session Log”导出过），这些副本不会被本脚本删除，请自行确认：")
        for p in exports[:20]:
            print("    " + p)
        if len(exports) > 20:
            print("    … 共 %d 个" % len(exports))
    else:
        info("外部副本检查：~/Downloads、~/Desktop、~/Documents 未发现会话导出文件。")

    notes = macos_forensic_notes()
    if notes:
        info("-" * 72)
        info("macOS 物理层面提示：")
        for n in notes:
            info("  · " + n)
    info("=" * 72)
    info("提醒：若开启过“在使用官方模型 API 时上传 Session Log”，")
    info("      服务端已接收的副本无法由本脚本删除（设置 → 通用 可关闭该开关）。")
    info("      删除不可恢复%s。" % ("（本次已做 --backup 备份）" if args.backup else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        raise SystemExit(130)
