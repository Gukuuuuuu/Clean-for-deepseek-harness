<div align="center">

# dshClean

**Rip DeepSeek Harness conversations out by the roots**

Message logs · session folders · projection cache · workspace list · GUI drafts · attachments · temp spill files · OS traces

Web UI · one-command CLI · preview by default · post-delete verification

[中文](README.md) · [English](README.en.md)

![Python](https://img.shields.io/badge/Python-3.8%2B-3776ab?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-macOS%20verified-lightgrey)

</div>

---

> [!NOTE]
> This is a third-party tool and is not affiliated with DeepSeek.

## Why this exists

DeepSeek Harness (DSH) **has no delete feature** — "archive" merely hides a conversation from the sidebar while the log stays on disk. The upstream source says it plainly:

> "Session files are not deleted — logs accumulate under `root` until removed externally; the seam has no delete interface."
>
> "Session deletion and folder removal are independent features that are not yet provided."

Worse, one conversation is spread across **seven locations**, so deleting `sessions/` alone leaves titles, drafts, indexes and references behind:

```
$DSH_HOME/                                (defaults to ~/.dsh)
├── sessions/<project>/<session>/          ← transcript: session.vN.jsonl.zstd + session.lock
├── storages/
│   ├── session_projcache/sessions/*.json  ← title / todos / goal / token usage projections
│   ├── workspace.json (+ .bak-*)          ← session refs in workspaces, archive & pin sets
│   └── <other units>.json                 ← e.g. schedule references
├── attachments/ , cache/                  ← attachment objects and image cache
└── $TMPDIR/dsh-spill-*                    ← spilled oversized tool output

~/Library/Application Support/@deepseek-ai/dsh-desktop/     (macOS)
├── Local Storage/leveldb                  ← drafts (dsh.conversation.<id>) and the "current session" pointer
└── Cache / Code Cache / GPUCache / …      ← browser caches and runtime leftovers
```

dshClean handles all of it and then re-scans to verify that no deleted session ID is still present in plaintext.
## What exactly gets deleted

| Location | Content | Default |
|---|---|---|
| `$DSH_HOME/sessions/<project>/<session>/` | Transcript `session.vN.jsonl.zstd` (all generations) and `session.lock` | ✅ always |
| `$DSH_HOME/storages/session_projcache/sessions/<id>.json` | Title, todos, goal, token usage, sandbox mode | ✅ always |
| `$DSH_HOME/storages/workspace.json` and `.bak-*` | `sessionIds` / `archivedSessionIds` / `pinnedSessionIds` (atomic write, JSON stays valid) | ✅ always |
| Other units under `$DSH_HOME/storages/` | References and per-record files (`<id>.json.bak`, dot-prefixed temp files) | ✅ always |
| `$DSH_HOME/attachments/`, `$DSH_HOME/cache/` | Attachment objects and image request cache | full wipe |
| 12 entries in the desktop data dir | `Local Storage` (drafts, current-session pointer), `Session Storage`, caches, `blob_storage`, `Shared Dictionary`, `Singleton*` | full wipe |
| `$TMPDIR/dsh-spill-XXXXXX` (also inside `scoped_dir*/`) | Spilled tool output | full wipe |
| `~/Library/Preferences/<bundle-id>.plist` | Only `NSOSPLastRootDirectory` (an "recent directory" trace, not conversation content), via `defaults`/`cfprefsd` | full wipe |
| App logs and this app's crash reports | `~/Library/Logs/DeepSeek Harness/`, `DiagnosticReports` | full wipe |

**Never touched**: your real project folders (e.g. `~/Projects/my-app`), account login state (Cookies / Local State, unless `--purge-app-dir`), DSH configuration and credentials (`profiles/`, `.credentials.yaml`).

## Safety design

- **Dry-run by default** — without `--yes` it only prints the plan (paths, sizes, reference counts)
- **Refuses to run while DSH is alive** — checks the process table (`ps` / `tasklist`) *and* probes `session.lock` with a non-blocking `flock`; if neither check can run, it **fails closed** instead of proceeding
- **Path fencing** — every deletion target must live under an allowed root (DSH home / desktop data dir / temp dirs); symlinks are unlinked, never followed
- **Two-step confirmation (web)** — the preview returns a `plan_token`; the delete request must present it, and any change of selection or options invalidates it. A typed confirmation word is required too
- **Atomic writes** — structured files such as `workspace.json` are replaced via temp file + fsync + rename
- **Post-delete verification** — re-scans the DSH home and GUI state for deleted session IDs, and reports suspected exported copies in `~/Downloads`, `~/Desktop`, `~/Documents`
- **Optional backup and overwrite** — `--backup DIR`, `--shred`

## What it cannot delete 

- **Server-side copies** — if "upload Session Log when using the official model API" was ever enabled, the increments already sent to DeepSeek cannot be removed locally; turn the switch off in *Settings → General*.
- **Physical recovery** — `rm` only unlinks. On SSD + APFS, `--shred` cannot guarantee unrecoverability. The tool reports FileVault and APFS local-snapshot status after deletion; for real anti-forensics, enable FileVault or erase the whole disk before disposing of the machine.
- **Exported copies** — suspected "Download Session Log" files are **listed** for you but never deleted automatically.
- **Telemetry already sent** — OpenTelemetry feedback uploads are a separate setting and out of scope.

## How it works

```
scan → build_plan → preview/confirm → execute → verify
```
