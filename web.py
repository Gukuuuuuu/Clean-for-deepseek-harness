#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_PATH = os.path.join(HERE, "core.py")


# --------------------------------------------------------------------- 载入核心模块
def load_core():
    """包内运行时用相对导入；直接执行本文件时回退为按路径加载同目录 core.py。"""
    if __package__:
        from . import core as mod  # noqa: PLC0415
        return mod
    if not os.path.isfile(CORE_PATH):
        sys.stderr.write(
            "错误: 找不到 %s\n"
            "      本页面复用它的扫描/删除逻辑，请确保同一目录下有 core.py。\n" % CORE_PATH
        )
        raise SystemExit(1)
    spec = importlib.util.spec_from_file_location("dshclean_core", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = load_core()


class ApiError(Exception):
    def __init__(self, message, status=400, detail=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail or {}


# --------------------------------------------------------------------- 状态收集
def set_label(home: str) -> None:
    default = W.canonical(os.path.join(os.path.expanduser("~"), ".dsh"))
    W.HOME_LABEL = "~/.dsh" if home == default else home


def safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def tree_mtime(path: str) -> float:
    newest = safe_mtime(path)
    for root, _dirs, files in os.walk(path):
        newest = max(newest, safe_mtime(root))
        for fn in files:
            newest = max(newest, safe_mtime(os.path.join(root, fn)))
    return newest


def collect_state(cli) -> dict:
    home = W.resolve_home(cli.home)
    set_label(home)
    if not os.path.isdir(home):
        raise ApiError("DSH home 不存在: %s" % home, 400)

    convs, wsdoc, _wspath = W.scan(home)
    archived, pinned = set(), set()
    if isinstance(wsdoc, dict):
        g = wsdoc.get("global") if isinstance(wsdoc.get("global"), dict) else {}
        archived = {x for x in (g.get("archivedSessionIds") or []) if isinstance(x, str)}
        pinned = {x for x in (g.get("pinnedSessionIds") or []) if isinstance(x, str)}

    rows = []
    for sid, c in convs.items():
        mtime = 0.0
        for d in c.dirs:
            mtime = max(mtime, tree_mtime(d))
        if c.projcache:
            mtime = max(mtime, safe_mtime(c.projcache))
        ws_title = c.workspaces[0][0] if c.workspaces else ""
        ws_path = c.workspaces[0][1] if c.workspaces else ""
        rows.append(
            {
                "id": sid,
                "title": c.title or "",
                "cwd": c.cwd or "",
                "workspace": ws_title,
                "workspacePath": ws_path,
                "grouped": bool(c.workspaces),
                "size": c.nbytes,
                "files": c.nfiles,
                "mtime": int(mtime * 1000),
                "hasDisk": bool(c.dirs),
                "hasCache": bool(c.projcache),
                "archived": sid in archived,
                "pinned": sid in pinned,
            }
        )
    rows.sort(key=lambda r: r["mtime"], reverse=True)

    appdir = W.canonical(cli.app_state_dir) if cli.app_state_dir else W.default_app_state_dir()
    procs, proc_ok = W.find_dsh_processes()
    held = W.locked_session_locks(home)
    singleton = W.app_singleton_lock(appdir)

    return {
        "home": home,
        "homeLabel": W.HOME_LABEL,
        "appStateDir": appdir if os.path.isdir(appdir) else "",
        "conversations": rows,
        "totals": {
            "count": len(rows),
            "bytes": sum(r["size"] for r in rows),
            "archived": sum(1 for r in rows if r["archived"]),
            "pinned": sum(1 for r in rows if r["pinned"]),
        },
        "running": {
            "procs": [{"pid": p, "cmd": c} for p, c in procs],
            "heldLocks": [W.abbr(p, home) for p in held],
            "singleton": singleton or "",
            "procCheckOk": proc_ok,
            "blocked": bool(procs or held) or (not proc_ok),
        },
        "spillDirs": [p for p in W.find_spill_dirs()],
        "forensicNotes": W.macos_forensic_notes(),
        "attachments": [
            p
            for p in (
                os.path.join(home, "attachments"),
                os.path.join(home, "cache", "attachments"),
            )
            if os.path.exists(p)
        ],
    }


# --------------------------------------------------------------------- 计划 / 摘要
def payload_argv(payload: dict, cli, do_delete: bool):
    ids = [str(x) for x in (payload.get("ids") or [])]
    opts = payload.get("options") or {}
    argv = []
    if cli.home:
        argv.append("--home=" + cli.home)
    if cli.app_state_dir:
        argv.append("--app-state-dir=" + cli.app_state_dir)
    if getattr(cli, "pref_domain", None):
        argv.append("--pref-domain=" + cli.pref_domain)
    if ids:
        for sid in ids:
            argv.append("--session=" + sid)
    else:
        argv.append("--all")
    argv.append("--attachments" if opts.get("attachments") else "--no-attachments")
    argv.append("--app-state" if opts.get("app_state") else "--no-app-state")
    argv.append("--spill" if opts.get("spill") else "--no-spill")
    argv.append("--scrub-storages" if opts.get("scrub", True) else "--no-scrub-storages")
    if opts.get("reset_workspaces"):
        argv.append("--reset-workspaces")
    elif opts.get("drop_empty_workspaces"):
        argv.append("--drop-empty-workspaces")
    if opts.get("shred"):
        argv.append("--shred")
    if opts.get("purge_app_dir"):
        argv.append("--purge-app-dir")
    backup = str(opts.get("backup") or "").strip()
    if backup:
        if not os.path.isabs(os.path.expanduser(backup)):
            raise ApiError("备份目录必须是绝对路径", 400)
        argv.append("--backup=" + os.path.expanduser(backup))
    if opts.get("force"):
        argv.append("--force")
    if do_delete:
        argv.append("--yes")
    return argv


def normalized_selection(payload: dict):
    ids = sorted({str(x) for x in (payload.get("ids") or [])})
    opts = payload.get("options") or {}
    norm = {
        "ids": ids,
        "attachments": bool(opts.get("attachments")),
        "app_state": bool(opts.get("app_state")),
        "spill": bool(opts.get("spill")),
        "scrub": bool(opts.get("scrub", True)),
        "drop_empty_workspaces": bool(opts.get("drop_empty_workspaces")),
        "reset_workspaces": bool(opts.get("reset_workspaces")),
        "shred": bool(opts.get("shred")),
        "purge_app_dir": bool(opts.get("purge_app_dir")),
        "backup": str(opts.get("backup") or "").strip(),
        "force": bool(opts.get("force")),
    }
    return norm


def plan_token(payload: dict) -> str:
    norm = normalized_selection(payload)
    blob = json.dumps(norm, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def plan_for(payload: dict, cli, do_delete: bool):
    argv = payload_argv(payload, cli, do_delete)
    args = W.build_parser().parse_args(argv)
    home = W.resolve_home(args.home)
    set_label(home)
    if not os.path.isdir(home):
        raise ApiError("DSH home 不存在: %s" % home, 400)
    convs, wsdoc, wspath = W.scan(home)
    with contextlib.redirect_stdout(io.StringIO()):
        selected = W.select(convs, args)
    if not selected:
        raise ApiError("没有匹配的对话（可能已被删除，请刷新）", 400)
    plan = W.build_plan(args, home, convs, selected, wsdoc, wspath)
    return args, home, convs, selected, plan


def _mk_action(label, items, size=None):
    return {"label": label, "items": items, "size": size}


def summarize(home: str, plan: dict, args, total_count: int) -> dict:
    actions = []
    warnings = []

    session_dirs = plan["session_dirs"]
    if session_dirs:
        size = sum(W.du(p)[0] for p in session_dirs)
        actions.append(
            _mk_action(
                "删除 %d 个会话目录（正文、session.lock、历史 generation）"
                % len(session_dirs),
                [W.abbr(p, home) for p in session_dirs],
                size,
            )
        )
    if plan["projcache_files"]:
        actions.append(
            _mk_action(
                "删除 %d 个投影缓存记录（标题、待办、目标、用量）"
                % len(plan["projcache_files"]),
                [W.abbr(p, home) for p in plan["projcache_files"]],
                sum(W.du(p)[0] for p in plan["projcache_files"]),
            )
        )
    wj = plan["workspace_json"]
    if wj and wj["changed"]:
        actions.append(
            _mk_action(
                "更新 workspace.json：移除 %d 处会话引用" % wj["refs"],
                [W.abbr(wj["path"], home)] + ["· " + n for n in wj["notes"]],
            )
        )
    for b in plan["workspace_backups"]:
        actions.append(
            _mk_action(
                "同步清理备份 %s（%d 处引用）" % (W.abbr(b["path"], home), b["refs"]), []
            )
        )
    if plan["storage_edits"]:
        items = []
        for e in plan["storage_edits"]:
            items.append(
                ("删除 " if e["kind"] == "delete" else "改写 ")
                + W.abbr(e["path"], home)
                + ("" if e["kind"] == "delete" else "（%d 处引用）" % e["hits"])
            )
        actions.append(_mk_action("清理其它存储单元（%d 个文件）" % len(items), items))
    if plan["attachments"]:
        actions.append(
            _mk_action(
                "删除附件对象与图片缓存",
                [W.abbr(p, home) for p in plan["attachments"]],
                sum(W.du(p)[0] for p in plan["attachments"]),
            )
        )
    if plan["caches"]:
        actions.append(
            _mk_action(
                "删除派生缓存目录",
                [W.abbr(p, home) for p in plan["caches"]],
                sum(W.du(p)[0] for p in plan["caches"]),
            )
        )
    if plan["spill"]:
        actions.append(
            _mk_action(
                "删除临时 spill 目录（%d 个）" % len(plan["spill"]),
                list(plan["spill"]),
                sum(W.du(p)[0] for p in plan["spill"]),
            )
        )
    if plan["app_state"]:
        if plan.get("purged_app_dir"):
            actions.append(
                _mk_action(
                    "整目录删除桌面端数据（含 Cookies，需重新登录）",
                    [plan["purged_app_dir"]],
                    W.du(plan["purged_app_dir"])[0],
                )
            )
            warnings.append("整目录删除会一并清除登录态，下次打开 DSH 需要重新登录账号")
        else:
            actions.append(
                _mk_action(
                    "清空桌面端 GUI 状态与缓存（%d 项，保留登录态）" % len(plan["app_state"]),
                    [os.path.basename(p) for p in plan["app_state"]],
                )
            )
    if plan.get("crash_reports"):
        actions.append(
            _mk_action("删除应用崩溃报告（%d 个）" % len(plan["crash_reports"]), list(plan["crash_reports"]))
        )
    if plan.get("pref_traces"):
        actions.append(
            _mk_action(
                "清理 macOS 偏好使用痕迹（%d 个键）" % len(plan["pref_traces"]),
                ["defaults delete %s %s" % (os.path.splitext(os.path.basename(pl)), k) for pl, k in plan["pref_traces"]],
            )
        )
    if plan["logs"]:
        actions.append(_mk_action("清空应用日志", list(plan["logs"])))
    if args.backup:
        actions.append(_mk_action("先备份到 %s" % args.backup, []))

    if plan["attachments_kept"]:
        warnings.append(
            "仍保留附件对象（未勾选“附件与图片缓存”）："
            + "、".join(W.abbr(p, home) for p in plan["attachments_kept"])
        )
    if plan["app_state_kept"]:
        warnings.append(
            "仍保留桌面端 GUI 本地状态：草稿、布局里可能继续留有已删会话的痕迹"
        )
    if plan["attachments"] and len(plan["selected"]) < total_count:
        warnings.append(
            "附件按内容寻址、无法按会话区分：本次会把「全部」附件与图片缓存一起删除"
        )
    if args.shred:
        warnings.append(
            "已启用 --shred：删除前会用随机字节覆盖一遍，但 APFS/SSD 无法保证物理不可恢复"
        )

    return {
        "count": len(plan["selected"]),
        "bytes": sum(c.nbytes for c in plan["selected"]),
        "conversations": [
            {
                "id": c.id,
                "title": c.title or "",
                "cwd": c.cwd or "",
                "size": c.nbytes,
                "dirs": [W.abbr(d, home) for d in c.dirs],
                "projcache": W.abbr(c.projcache, home) if c.projcache else "",
            }
            for c in plan["selected"]
        ],
        "actions": actions,
        "warnings": warnings,
    }


# --------------------------------------------------------------------- 删除执行
DELETE_LOCK = threading.Lock()


def run_delete(payload: dict, cli) -> dict:
    norm = normalized_selection(payload)
    token = plan_token(payload)
    if payload.get("plan_token") != token:
        raise ApiError("选择或选项在预览之后发生了变化，请重新预览", 409)
    if payload.get("confirm") != "删除":
        raise ApiError("确认文本不正确", 400)

    args, home, _convs, _selected, plan = plan_for(payload, cli, do_delete=True)

    procs, proc_ok = W.find_dsh_processes()
    held = W.locked_session_locks(home)
    blocked = bool(procs or held) or (not proc_ok)
    if blocked and not norm["force"]:
        detail = {
            "procs": [{"pid": p, "cmd": c} for p, c in procs],
            "heldLocks": [W.abbr(p, home) for p in held],
            "procCheckOk": proc_ok,
        }
        raise ApiError(
            "DSH 正在运行（或无法确认进程状态），已拒绝删除。"
            "请完全退出 DSH 桌面端 / dsh web / headless 后重试；"
            "确实要强制删除请在页面上勾选“强制”。",
            409,
            detail,
        )

    with DELETE_LOCK:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if norm["backup"]:
                W.do_backup(plan, home, W.canonical(norm["backup"]))
            items, nbytes = W.execute(args, home, plan)
            leftovers = W.verify(home, plan["ids"], plan, args)
        log = [ln for ln in buf.getvalue().splitlines() if ln.strip()]

    return {
        "ok": True,
        "items": items,
        "bytes": nbytes,
        "deleted": norm["ids"],
        "log": log,
        "leftovers": [W.abbr(p, home) for p in leftovers],
        "forced": blocked and norm["force"],
    }


# --------------------------------------------------------------------- 页面
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DSH 对话清理</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161a22; --panel2:#1c212b; --line:#262c39;
    --fg:#e6e9f0; --muted:#8d96a8; --accent:#5b8cff; --accent2:#3f6fe0;
    --danger:#ff5f6d; --danger2:#c9353f; --ok:#37d399; --warn:#ffb454;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif}
  code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{display:flex;align-items:center;gap:14px;padding:14px 20px;
         border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
  header .brand{font-size:16px;font-weight:600}
  header .meta{color:var(--muted);font-size:12.5px;display:flex;gap:14px;flex-wrap:wrap}
  header .meta b{color:var(--fg);font-weight:600}
  header .grow{flex:1}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:8px;padding:7px 13px;font-size:13px;cursor:pointer}
  button:hover{border-color:#38405480}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.primary:hover{background:var(--accent2)}
  button.danger{background:var(--danger);border-color:var(--danger);color:#fff;font-weight:600}
  button.danger:hover{background:var(--danger2)}
  button.big{padding:11px 22px;font-size:15px;border-radius:10px}
  button:disabled{opacity:.45;cursor:not-allowed}
  .banner{margin:14px 20px 0;padding:12px 15px;border-radius:10px;font-size:13px;line-height:1.6}
  .banner.danger{background:#3a1720;border:1px solid #7a2530;color:#ffd7db}
  .banner.warn{background:#3a2c15;border:1px solid #7a5a25;color:#ffe6c2}
  .banner.ok{background:#123024;border:1px solid #1f6b4c;color:#c7f5e2}
  .banner ul{margin:6px 0 0 18px;padding:0}
  main{padding:16px 20px 110px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .toolbar{display:flex;gap:9px;padding:12px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap;align-items:center}
  input[type=text],input[type=search],select{background:var(--bg);border:1px solid var(--line);color:var(--fg);
       border-radius:8px;padding:7px 10px;font-size:13px;outline:none}
  input[type=search]{min-width:240px}
  input[type=text]:focus,input[type=search]:focus,select:focus{border-color:var(--accent)}
  .grow{flex:1}
  .clearnote{padding:11px 15px;border-bottom:1px solid var(--line);color:var(--muted);font-size:12.5px;line-height:1.7}
  .clearnote b{color:var(--fg);font-weight:600}
  .clearnote .tag{display:inline-block;background:var(--panel2);border:1px solid var(--line);
       border-radius:20px;padding:1px 9px;margin:2px 4px 2px 0;font-size:12px;color:#c3ccdd}
  .tablewrap{max-height:56vh;overflow:auto}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
  th{position:sticky;top:0;background:var(--panel2);font-size:12px;color:var(--muted);font-weight:600;z-index:1}
  tr.row:hover{background:#1a1f29}
  tr.sel{background:#18233a}
  td.title{max-width:640px}
  td.title .t{font-weight:600;word-break:break-word}
  td.title .sub{color:var(--muted);font-size:12px;margin-top:3px;word-break:break-all}
  .badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;margin-left:6px;
         border:1px solid var(--line);color:var(--muted);vertical-align:middle}
  .badge.arch{color:#ffd08a;border-color:#7a5a25}
  .badge.pin{color:#9ad0ff;border-color:#2b5d86}
  .badge.nodisk{color:#ff9aa4;border-color:#7a2530}
  .badge.ws{color:#b9c4d8}
  .nowrap{white-space:nowrap;color:var(--muted);font-size:12.5px}
  details.adv{border-top:1px solid var(--line);padding:10px 15px;background:#141821}
  details.adv summary{color:var(--muted);font-size:12.5px;cursor:pointer}
  details.adv .opt{padding:9px 0 0}
  details.adv label{display:flex;gap:9px;align-items:flex-start;cursor:pointer}
  details.adv input[type=checkbox]{margin-top:3px;accent-color:var(--accent)}
  details.adv .name{font-size:13px}
  details.adv .hint{color:var(--muted);font-size:12px;margin:2px 0 0 24px}
  details.adv input[type=text]{width:100%;margin-top:6px}
  footer{position:fixed;left:0;right:0;bottom:0;display:flex;align-items:center;gap:12px;
         padding:12px 20px;background:var(--panel);border-top:1px solid var(--line);z-index:6}
  footer .sel{font-size:13.5px;color:var(--muted)}
  footer .sel b{color:var(--fg)}
  .modal{position:fixed;inset:0;background:#000000a8;display:flex;align-items:center;justify-content:center;
         padding:24px;z-index:20}
  .modal .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
               width:min(860px,100%);max-height:88vh;display:flex;flex-direction:column}
  .modal .hd{padding:15px 18px;border-bottom:1px solid var(--line);font-weight:600;font-size:15px}
  .modal .bd{padding:16px 18px;overflow:auto}
  .modal .ft{padding:14px 18px;border-top:1px solid var(--line);display:flex;gap:10px;justify-content:flex-end;align-items:center}
  .modal .ft .grow{flex:1}
  .act{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:9px;background:var(--panel2)}
  .act .lbl{font-size:13px}
  .act details{margin-top:7px}
  .act summary{color:var(--muted);font-size:12px;cursor:pointer}
  .act ul{margin:7px 0 0 0;padding-left:18px;color:var(--muted);font-size:12px}
  .act li{word-break:break-all}
  pre.log{background:#0b0e13;border:1px solid var(--line);border-radius:10px;padding:12px;
          max-height:38vh;overflow:auto;font-size:12px;color:#c9d2e3;white-space:pre-wrap;word-break:break-all}
  .hidden{display:none!important}
  .kv{color:var(--muted);font-size:13px}
  .kv b{color:var(--fg)}
  .warnbox{background:#3a2c15;border:1px solid #7a5a25;color:#ffe6c2;border-radius:10px;padding:10px 12px;font-size:12.5px;margin-top:10px}
  .confirm{margin-top:14px;padding:12px;border:1px dashed var(--danger);border-radius:10px}
  .confirm input{margin-top:8px;width:180px}
  .center{text-align:center;color:var(--muted);padding:26px}
</style>
</head>
<body>
<header>
  <div class="brand">🧹 DSH 对话清理</div>
  <div class="meta" id="meta"></div>
  <div class="grow"></div>
  <button id="btn-refresh">刷新</button>
  <button id="btn-shutdown">退出服务</button>
</header>
<div id="banner"></div>
<main>
  <section class="panel">
    <div class="toolbar">
      <input type="search" id="q" placeholder="搜索标题 / ID / 路径…">
      <select id="ws"><option value="">全部工作区</option></select>
      <select id="sort">
        <option value="mtime">按最后活动</option>
        <option value="size">按大小</option>
        <option value="title">按标题</option>
        <option value="workspace">按工作区</option>
      </select>
      <label class="kv"><input type="checkbox" id="onlyArchived"> 仅归档</label>
      <div class="grow"></div>
      <button id="btn-all">全选</button>
      <button id="btn-invert">反选</button>
      <button id="btn-none">清空选择</button>
    </div>
    <div class="clearnote">
      <b>「彻底清空」会删掉：</b>
      <span class="tag">会话正文</span><span class="tag">会话文件夹</span><span class="tag">投影缓存（标题/待办/用量）</span>
      <span class="tag">工作区列表</span><span class="tag">GUI 草稿与“当前会话”指针</span><span class="tag">附件与图片缓存</span>
      <span class="tag">临时 spill 文件</span><span class="tag">macOS 偏好痕迹</span>
      <br>磁盘上的项目目录（如 <code>~/Projects/my-app</code>）<b>不会被删除</b>，账号登录状态默认也保留。
    </div>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th style="width:34px"></th><th>对话</th><th style="width:150px">工作区</th>
          <th style="width:96px">大小</th><th style="width:140px">最后活动</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <details class="adv">
      <summary>高级选项（一般不用改）</summary>
      <div class="opt">
        <label><input type="checkbox" id="adv_shred">
          <span><span class="name">删除前覆盖文件</span>
          <div class="hint">尽力而为；APFS/SSD 上无法保证物理不可恢复。</div></span></label>
      </div>
      <div class="opt">
        <label><input type="checkbox" id="adv_purge">
          <span><span class="name">连账号登录状态一起清除</span>
          <div class="hint">把整个桌面端数据目录删掉（含 Cookies），下次打开 DSH 需要重新登录。</div></span></label>
      </div>
      <div class="opt">
        <label><input type="checkbox" id="adv_backup">
          <span><span class="name">先备份一份再删</span>
          <div class="hint">按下方的绝对路径保存副本，删完还能人工找回。</div></span></label>
        <input type="text" id="adv_backup_dir" placeholder="~/dsh-backup">
      </div>
      <div class="opt hidden" id="force_opt">
        <label><input type="checkbox" id="adv_force">
          <span><span class="name" style="color:var(--danger)">强制删除（不推荐）</span>
          <div class="hint">DSH 仍在运行时删除会不彻底：索引可能被重新写回，正在写入的会话可能残留。</div></span></label>
      </div>
    </details>
  </section>
</main>

<footer>
  <div class="sel" id="selinfo"></div>
  <div class="grow"></div>
  <button id="btn-del-sel" class="hidden">删除选中的对话</button>
  <button id="btn-nuke" class="danger big">彻底清空全部对话</button>
</footer>

<div id="modal" class="modal hidden"><div class="card">
  <div class="hd" id="m_hd"></div>
  <div class="bd" id="m_bd"></div>
  <div class="ft" id="m_ft"></div>
</div></div>

<script>
const TOKEN = "__TOKEN__";
let STATE = null;
let SELECTED = new Set();
const modal = document.getElementById('modal');

function h(tag, props, ...kids){
  const e = document.createElement(tag);
  if (props) for (const k in props){
    if (k === 'class') e.className = props[k];
    else if (k === 'text') e.textContent = props[k];
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), props[k]);
    else if (props[k] !== undefined && props[k] !== null) e.setAttribute(k, props[k]);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) e.append(kid);
  return e;
}
function human(n){
  const u = ['B','KiB','MiB','GiB','TiB']; let f = Number(n)||0, i = 0;
  while (f >= 1024 && i < u.length-1){ f /= 1024; i++; }
  return (i === 0 ? f.toFixed(0) : f.toFixed(f < 10 ? 1 : 0)) + ' ' + u[i];
}
function when(ms){
  if (!ms) return '—';
  const d = new Date(ms), diff = (Date.now()-ms)/1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff/60)+' 分钟前';
  if (diff < 86400) return Math.floor(diff/3600)+' 小时前';
  if (diff < 86400*30) return Math.floor(diff/86400)+' 天前';
  return d.toLocaleDateString();
}
async function api(path, body){
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'X-DSH-Token': TOKEN, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({error: '返回内容不是 JSON'}));
  if (!res.ok) { const err = new Error(data.error || ('HTTP ' + res.status)); err.detail = data; throw err; }
  return data;
}
function openModal(title, bodyNode, buttons){
  document.getElementById('m_hd').textContent = title;
  document.getElementById('m_bd').replaceChildren(bodyNode);
  const ft = document.getElementById('m_ft'); ft.replaceChildren();
  (buttons||[]).forEach(b => {
    const btn = h('button', {class: b.primary ? 'primary' : (b.danger ? 'danger' : ''), text: b.label, onclick: b.onClick});
    if (b.disabled) btn.disabled = true;
    if (b.ref) b.ref(btn);
    ft.append(btn);
  });
  modal.classList.remove('hidden');
}
function closeModal(){ modal.classList.add('hidden'); }

function filtered(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const ws = document.getElementById('ws').value;
  const onlyArch = document.getElementById('onlyArchived').checked;
  const sort = document.getElementById('sort').value;
  let rows = (STATE.conversations || []).filter(r => {
    if (ws && (r.workspacePath || '') !== ws && (r.workspace || '') !== ws) return false;
    if (onlyArch && !r.archived) return false;
    if (!q) return true;
    return (r.title||'').toLowerCase().includes(q) || (r.id||'').toLowerCase().includes(q)
        || (r.cwd||'').toLowerCase().includes(q) || (r.workspace||'').toLowerCase().includes(q);
  });
  const cmp = {
    mtime: (a,b) => b.mtime - a.mtime,
    size: (a,b) => b.size - a.size,
    title: (a,b) => (a.title||a.id).localeCompare(b.title||b.id),
    workspace: (a,b) => (a.workspace||'').localeCompare(b.workspace||'') || (b.mtime-a.mtime)
  }[sort];
  return rows.sort(cmp);
}
function render(){
  const m = document.getElementById('meta'); m.replaceChildren();
  const t = STATE.totals;
  m.append(h('span', {text: '位置 '}), h('b', {class:'mono', text: STATE.homeLabel}));
  m.append(h('b', {text: t.count + ' 个对话'}), h('b', {text: human(t.bytes)}));
  if (t.archived) m.append(h('b', {text: t.archived + ' 个已归档'}));
  if (t.pinned) m.append(h('b', {text: t.pinned + ' 个已置顶'}));

  const banner = document.getElementById('banner'); banner.replaceChildren();
  const run = STATE.running;
  if (run.blocked){
    const lines = [];
    if (run.procs.length) lines.push('检测到 DSH 进程：' + run.procs.map(p => 'pid ' + p.pid).join('、'));
    if (run.heldLocks.length) lines.push('会话写锁被持有：' + run.heldLocks.slice(0,5).join('、'));
    if (!run.procCheckOk) lines.push('无法确认进程状态（ps/tasklist 不可用，常见于沙箱）');
    const box = h('div', {class:'banner danger'});
    box.append(h('b', {text: '⛔ 现在不能安全删除：'}));
    box.append(document.createTextNode('请完全退出 DSH 桌面端 / dsh web / headless 后刷新本页。'));
    const ul = h('ul'); lines.forEach(l => ul.append(h('li', {text:l}))); box.append(ul);
    box.append(h('div', {class:'kv', text:'若坚持现在删除，可在“高级选项”里勾选“强制删除”——不推荐，可能删不干净。'}));
    banner.append(box);
    document.getElementById('force_opt').classList.remove('hidden');
  } else {
    banner.append(h('div', {class:'banner ok', text:'✅ 未检测到 DSH 进程，可以安全执行删除。'}));
    document.getElementById('force_opt').classList.add('hidden');
    document.getElementById('adv_force').checked = false;
  }
  if ((STATE.forensicNotes||[]).length){
    const fb = h('div', {class:'banner warn'});
    fb.append(h('b', {text:'macOS 物理层提示：'}));
    const ul = h('ul'); STATE.forensicNotes.forEach(n => ul.append(h('li', {text:n}))); fb.append(ul);
    banner.append(fb);
  }

  const wsSel = document.getElementById('ws');
  const cur = wsSel.value;
  const seen = new Map();
  (STATE.conversations||[]).forEach(r => { if (r.workspacePath && !seen.has(r.workspacePath)) seen.set(r.workspacePath, r.workspace || r.workspacePath); });
  wsSel.replaceChildren(h('option', {value:'', text:'全部工作区'}));
  [...seen.entries()].sort().forEach(([p, name]) => wsSel.append(h('option', {value:p, text:name || p})));
  wsSel.value = cur;

  const tbody = document.getElementById('rows'); tbody.replaceChildren();
  const rows = filtered();
  if (!rows.length) tbody.append(h('tr', {}, h('td', {colspan:5, class:'center', text:'没有匹配的对话'})));
  rows.forEach(r => {
    const cb = h('input', {type:'checkbox'});
    cb.checked = SELECTED.has(r.id);
    cb.addEventListener('change', () => {
      if (cb.checked) SELECTED.add(r.id); else SELECTED.delete(r.id);
      render();
    });
    const title = h('div', {class:'t', text: r.title || '(未命名)'});
    if (r.archived) title.append(h('span', {class:'badge arch', text:'归档'}));
    if (r.pinned) title.append(h('span', {class:'badge pin', text:'置顶'}));
    if (!r.hasDisk) title.append(h('span', {class:'badge nodisk', text:'仅索引'}));
    if (!r.grouped) title.append(h('span', {class:'badge ws', text:'Ungrouped'}));
    const tr = h('tr', {class: 'row' + (cb.checked ? ' sel' : '')},
      h('td', {}, cb),
      h('td', {class:'title'}, title, h('div', {class:'sub mono', text: r.id + (r.cwd ? '  ·  ' + r.cwd : '')})),
      h('td', {}, h('div', {text: r.workspace || '—'}), h('div', {class:'sub', text: r.hasDisk ? (r.files + ' 个文件') : '无正文文件'})),
      h('td', {class:'nowrap', text: human(r.size)}),
      h('td', {class:'nowrap', text: when(r.mtime)})
    );
    tr.addEventListener('click', ev => { if (ev.target !== cb){ cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); } });
    tbody.append(tr);
  });

  const selBytes = (STATE.conversations||[]).filter(r => SELECTED.has(r.id)).reduce((a,r) => a + r.size, 0);
  const sel = document.getElementById('selinfo');
  if (SELECTED.size) {
    sel.replaceChildren(h('span', {text:'已选 '}), h('b', {text:String(SELECTED.size)}), h('span', {text:' 个 · '}), h('b', {text: human(selBytes)}));
  } else {
    sel.replaceChildren(h('span', {text:'共 '}), h('b', {text:String(STATE.totals.count)}), h('span', {text:' 个对话 · '}), h('b', {text: human(STATE.totals.bytes)}));
  }
  const delSel = document.getElementById('btn-del-sel');
  delSel.classList.toggle('hidden', SELECTED.size === 0 || SELECTED.size === STATE.totals.count);
  delSel.textContent = '删除选中的 ' + SELECTED.size + ' 个';
  const nuke = document.getElementById('btn-nuke');
  nuke.textContent = '彻底清空全部对话（' + STATE.totals.count + ' 个）';
  nuke.disabled = STATE.totals.count === 0;
}
function optionsFor(mode){
  const full = mode === 'all';
  return {
    attachments: full,
    app_state: full,
    spill: full,
    scrub: true,
    reset_workspaces: full,
    drop_empty_workspaces: full,
    shred: document.getElementById('adv_shred').checked,
    purge_app_dir: document.getElementById('adv_purge').checked,
    backup: document.getElementById('adv_backup').checked ? document.getElementById('adv_backup_dir').value : '',
    force: document.getElementById('adv_force').checked
  };
}
async function loadState(){
  document.getElementById('banner').replaceChildren(h('div', {class:'banner', text:'正在读取…'}));
  STATE = await api('/api/state');
  SELECTED = new Set([...SELECTED].filter(id => STATE.conversations.some(r => r.id === id)));
  render();
}
function previewBody(sum, full){
  const box = h('div');
  box.append(h('div', {class:'kv'},
    h('b', {text: (full ? '彻底清空 ' : '删除 ') + sum.count + ' 个对话'}),
    document.createTextNode('，合计约 ' + human(sum.bytes))));
  sum.actions.forEach(a => {
    const act = h('div', {class:'act'});
    act.append(h('div', {class:'lbl', text:'· ' + a.label + (a.size ? '（' + human(a.size) + '）' : '')}));
    if (a.items && a.items.length){
      const det = h('details');
      const shown = a.items.slice(0, 200);
      det.append(h('summary', {text:'查看 ' + a.items.length + ' 项明细'}));
      const ul = h('ul'); shown.forEach(i => ul.append(h('li', {class:'mono', text:i})));
      if (a.items.length > shown.length) ul.append(h('li', {text:'… 还有 ' + (a.items.length - shown.length) + ' 项'}));
      det.append(ul); act.append(det);
    }
    box.append(act);
  });
  sum.warnings.forEach(w => box.append(h('div', {class:'warnbox', text:'⚠ ' + w})));
  const conf = h('div', {class:'confirm'});
  conf.append(h('div', {class:'kv', text:'此操作不可撤销。请输入 “删除” 以启用执行按钮：'}));
  const inp = h('input', {type:'text', placeholder:'删除', autocomplete:'off'});
  conf.append(inp); box.append(conf);
  return {box, inp};
}
async function doPreview(mode){
  const full = mode === 'all';
  const ids = full ? (STATE.conversations||[]).map(r => r.id) : [...SELECTED];
  if (!ids.length) return;
  const payload = {ids: ids, options: optionsFor(mode)};
  document.getElementById('btn-nuke').disabled = true;
  document.getElementById('btn-del-sel').disabled = true;
  try {
    const sum = await api('/api/preview', payload);
    const {box, inp} = previewBody(sum, full);
    let execBtn;
    const check = () => { if (execBtn) execBtn.disabled = inp.value.trim() !== '删除'; };
    inp.addEventListener('input', check);
    openModal(full ? '确认：彻底清空全部对话' : '确认：删除选中的对话', box, [
      {label:'取消', onClick: closeModal},
      {label: full ? '彻底清空' : '删除', danger:true, disabled:true,
       ref: (b) => { execBtn = b; },
       onClick: async () => {
         execBtn.disabled = true; execBtn.textContent = '删除中…';
         try {
           const res = await api('/api/delete', Object.assign({}, payload, {plan_token: sum.plan_token, confirm: '删除'}));
           showResult(res, full);
         } catch (e) { showError(e); }
       }}
    ]);
  } catch (e) { showError(e); }
  finally {
    document.getElementById('btn-nuke').disabled = STATE.totals.count === 0;
    document.getElementById('btn-del-sel').disabled = false;
  }
}
function showError(e){
  const box = h('div');
  box.append(h('div', {class:'warnbox', text:'⚠ ' + e.message}));
  if (e.detail && e.detail.detail){
    const d = e.detail.detail;
    if ((d.procs||[]).length) box.append(h('pre', {class:'log', text: d.procs.map(p => 'pid ' + p.pid + '  ' + p.cmd).join('\n')}));
    if ((d.heldLocks||[]).length) box.append(h('pre', {class:'log', text: '被持有的写锁：\n' + d.heldLocks.join('\n')}));
  }
  openModal('未能执行', box, [{label:'知道了', primary:true, onClick: closeModal}]);
}
function showResult(res, full){
  const box = h('div');
  box.append(h('div', {class:'kv'}, h('b', {text:'完成：处理 ' + res.items + ' 项'}), document.createTextNode('，释放约 ' + human(res.bytes))));
  if (res.forced) box.append(h('div', {class:'warnbox', text:'⚠ 本次是强制删除（DSH 当时可能在运行），可能不彻底，建议刷新确认。'}));
  if (res.leftovers && res.leftovers.length){
    const ul = h('ul'); res.leftovers.forEach(p => ul.append(h('li', {class:'mono', text:p})));
    box.append(h('div', {class:'warnbox', text:'⚠ 仍有文件包含已删会话 ID 的明文引用：'}), ul);
  } else {
    box.append(h('div', {class:'banner ok', text:'✅ 已回扫验证：DSH home 内不再有已删会话 ID 的明文引用。'}));
  }
  box.append(h('div', {class:'kv', text:'执行日志：'}));
  box.append(h('pre', {class:'log', text: (res.log||[]).join('\n')}));
  openModal(full ? '彻底清空完成' : '删除完成', box, [
    {label:'关闭', onClick: closeModal},
    {label:'刷新列表', primary:true, onClick: async () => { closeModal(); SELECTED.clear(); await loadState(); }}
  ]);
}
document.getElementById('btn-all').addEventListener('click', () => { filtered().forEach(r => SELECTED.add(r.id)); render(); });
document.getElementById('btn-none').addEventListener('click', () => { SELECTED.clear(); render(); });
document.getElementById('btn-invert').addEventListener('click', () => {
  filtered().forEach(r => { if (SELECTED.has(r.id)) SELECTED.delete(r.id); else SELECTED.add(r.id); });
  render();
});
document.getElementById('btn-refresh').addEventListener('click', loadState);
document.getElementById('btn-nuke').addEventListener('click', () => doPreview('all'));
document.getElementById('btn-del-sel').addEventListener('click', () => doPreview('selected'));
document.getElementById('btn-shutdown').addEventListener('click', async () => {
  try { await api('/api/shutdown', {}); } catch (e) {}
  openModal('服务已退出', h('div', {class:'kv', text:'本地服务已退出，可以关闭本页面了。'}),
    [{label:'关闭', primary:true, onClick: closeModal}]);
});
['q','ws','sort','onlyArchived'].forEach(id => document.getElementById(id).addEventListener('input', render));
document.getElementById('ws').addEventListener('change', render);
document.getElementById('sort').addEventListener('change', render);
document.getElementById('onlyArchived').addEventListener('change', render);
modal.addEventListener('click', ev => { if (ev.target === modal) closeModal(); });
loadState().catch(e => { document.getElementById('banner').replaceChildren(h('div', {class:'banner danger', text:'读取失败：' + e.message})); });
</script>
</body>
</html>"""


# --------------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "DSH-Cleanup-Web"
    protocol_version = "HTTP/1.1"
    cli = None
    token = ""
    verbose = False

    # ---- 基础设施
    def log_message(self, fmt, *a):
        if self.verbose:
            sys.stderr.write("[web] " + (fmt % a) + "\n")

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        if self.headers.get("X-DSH-Token") == self.token:
            return True
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(self.path).query)
        return (qs.get("token") or [""])[0] == self.token

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise ApiError("请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象", 400)
        return data

    # ---- 路由
    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not self._authorized():
                self._send(403, "无效或缺失的 token。请使用启动时打印的完整地址。", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX_HTML.replace("__TOKEN__", self.token), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            if not self._authorized():
                self._send(403, {"error": "无效 token"})
                return
            try:
                self._send(200, collect_state(self.cli))
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "读取状态失败: %s" % exc})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if not self._authorized():
            self._send(403, {"error": "无效 token"})
            return
        if path not in ("/api/preview", "/api/delete", "/api/shutdown"):
            self._send(404, {"error": "not found"})
            return
        try:
            payload = self._body()
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
            return

        if path == "/api/preview":
            try:
                args, home, convs, _s, plan = plan_for(payload, self.cli, do_delete=False)
                summary = summarize(home, plan, args, len(convs))
                summary["plan_token"] = plan_token(payload)
                self._send(200, summary)
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "生成预览失败: %s" % exc})
            return

        if path == "/api/delete":
            try:
                self._send(200, run_delete(payload, self.cli))
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "删除失败: %s" % exc})
            return

        # /api/shutdown
        self._send(200, {"ok": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


# --------------------------------------------------------------------- 入口
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dsh-clean-web",
        description="DSH 对话清理的本地可视化网页（只监听 127.0.0.1）。",
    )
    parser.add_argument("--home", metavar="PATH", help="DSH home（默认 $DSH_HOME 或 ~/.dsh）")
    parser.add_argument("--app-state-dir", metavar="PATH", help="桌面端 userData 目录")
    parser.add_argument("--pref-domain", metavar="BUNDLE_ID", help="覆盖 macOS 偏好域")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765，被占用时自动 +1）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--token", metavar="TOKEN", help="指定访问 token（默认随机生成）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 HTTP 访问日志")
    cli = parser.parse_args(argv)

    if cli.port < 1 or cli.port > 65535:
        sys.stderr.write("错误: 端口不合法\n")
        return 1

    token = cli.token or secrets.token_urlsafe(18)
    Handler.cli = cli
    Handler.token = token
    Handler.verbose = cli.verbose

    home = W.resolve_home(cli.home)
    if not os.path.isdir(home):
        sys.stderr.write("错误: DSH home 不存在: %s\n" % home)
        return 1

    httpd = None
    port = cli.port
    for _ in range(12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        sys.stderr.write("错误: 找不到可用端口\n")
        return 1
    httpd.daemon_threads = True

    url = "http://127.0.0.1:%d/?token=%s" % (port, token)
    procs, proc_ok = W.find_dsh_processes()
    print("=" * 68)
    print("DSH 对话清理（本地网页）")
    print("  地址      : %s" % url)
    print("  DSH home  : %s" % home)
    print("  备份/覆盖 : 可在页面上选择；删除不可恢复")
    if procs:
        print("  ⚠ 检测到 DSH 正在运行（pid %s）：" % "、".join(str(p) for p, _ in procs))
        print("    页面会拒绝删除。请完全退出 DSH 后重新打开本页面再执行。")
    elif not proc_ok:
        print("  ⚠ 当前环境无法检查进程（ps/tasklist 不可用）：页面默认拒绝删除。")
    else:
        print("  ✅ 未检测到 DSH 进程，可以安全执行删除。")
    print("  停止服务  : 页面右上角“退出服务”，或按 Ctrl+C")
    print("=" * 68)
    sys.stdout.flush()

    if not cli.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
