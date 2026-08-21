#!/usr/bin/env python3
"""Prune orphaned Open WebUI uploads (files whose chat/knowledge reference is gone).

Open WebUI (as of v0.11.0) never garbage-collects uploaded images: deleting a
chat leaves the file on disk (uploads/) AND its row in webui.db's `file` table.
Over time these orphans accumulate. This script finds `file` rows that are no
longer referenced by any chat, knowledge base, or note, and (with --apply)
deletes both the DB row and the on-disk file.

Designed to run INSIDE the open-webui container (paths in the `file.path`
column are container paths like /app/backend/data/uploads/...). The companion
wrapper scripts/openwebui-prune-uploads.sh execs this via `docker exec`.

SAFE BY DEFAULT: dry-run unless --apply is given. A grace period
(--min-age-days, default 7) protects freshly-uploaded files that may not yet be
attached to a saved chat.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

DEFAULT_DB = os.environ.get("OPENWEBUI_DB", "/app/backend/data/webui.db")
DEFAULT_UPLOADS = os.environ.get("OPENWEBUI_UPLOADS", "/app/backend/data/uploads")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def referenced_ids(cur: sqlite3.Cursor) -> set[str]:
    """All file ids still referenced by a LIVE chat, knowledge base, or note.

    IMPORTANT: deleting a chat in Open WebUI does NOT cascade-delete its
    `chat_file` rows, so the join tables are full of dangling references to
    already-deleted chats. We therefore only count a join-table reference when
    its parent row (chat / knowledge) still exists — otherwise a deleted chat's
    attachment would look "referenced" forever and never get pruned.
    """
    refs: set[str] = set()

    # Authoritative join tables, but only rows whose parent still exists.
    for table, parent, parent_key in (
        ("chat_file", "chat", "chat_id"),
        ("knowledge_file", "knowledge", "knowledge_id"),
    ):
        try:
            q = (f"SELECT jt.file_id FROM {table} jt "
                 f"JOIN {parent} p ON p.id = jt.{parent_key} "
                 f"WHERE jt.file_id IS NOT NULL")
            for (fid,) in cur.execute(q):
                if fid:
                    refs.add(fid)
        except sqlite3.OperationalError:
            pass  # table/column may differ on other schema versions

    # Belt-and-suspenders: some versions embed the file id inline in chat JSON
    # or note bodies rather than (or in addition to) the join tables. Scan the
    # concatenated blobs of LIVE rows once and test membership per candidate id.
    blob_parts: list[str] = []
    for table, col in (("chat", "chat"), ("note", "data"), ("message", "content")):
        try:
            for (val,) in cur.execute(f"SELECT {col} FROM {table}"):
                if val:
                    blob_parts.append(val if isinstance(val, str) else str(val))
        except sqlite3.OperationalError:
            pass
    referenced_ids._blob = "\n".join(blob_parts)  # type: ignore[attr-defined]
    return refs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--min-age-days", type=float, default=7.0,
                    help="only prune files older than this many days (default: 7)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--uploads", default=DEFAULT_UPLOADS)
    ap.add_argument("--quiet", action="store_true", help="only print the summary line")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: webui.db not found at {args.db}", file=sys.stderr)
        return 2

    cutoff = time.time() - args.min_age_days * 86400
    conn = sqlite3.connect(args.db, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    cur = conn.cursor()

    refs = referenced_ids(cur)
    blob = getattr(referenced_ids, "_blob", "")

    rows = cur.execute("SELECT id, filename, path, meta, created_at FROM file").fetchall()
    total = len(rows)
    orphans = []
    for fid, filename, path, meta, created_at in rows:
        if fid in refs or (fid and fid in blob):
            continue
        if created_at and created_at > cutoff:
            continue  # within grace period
        # size: prefer on-disk, fall back to meta.size
        size = 0
        if path and os.path.exists(path):
            size = os.path.getsize(path)
        else:
            try:
                import json
                size = int(json.loads(meta or "{}").get("size", 0))
            except Exception:
                size = 0
        orphans.append((fid, filename, path, size))

    freed = sum(o[3] for o in orphans)
    action = "DELETING" if args.apply else "would delete"

    if not args.quiet:
        for fid, filename, path, size in orphans:
            print(f"  {action}: {filename}  ({human(size)})  id={fid}")

    deleted = 0
    if args.apply and orphans:
        for fid, filename, path, size in orphans:
            # Remove disk file (path column, plus any id-prefixed leftovers).
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                elif os.path.isdir(args.uploads):
                    for f in os.listdir(args.uploads):
                        if f.startswith(fid):
                            os.remove(os.path.join(args.uploads, f))
            except OSError as e:
                print(f"  WARN: could not delete file for {fid}: {e}", file=sys.stderr)
            # Remove DB rows (file + any dangling join rows).
            cur.execute("DELETE FROM file WHERE id = ?", (fid,))
            for table in ("chat_file", "knowledge_file"):
                try:
                    cur.execute(f"DELETE FROM {table} WHERE file_id = ?", (fid,))
                except sqlite3.OperationalError:
                    pass
            deleted += 1
        conn.commit()

    conn.close()

    verb = "pruned" if args.apply else "orphaned (dry-run)"
    print(f"openwebui-prune-uploads: {total} files scanned, {len(orphans)} {verb}, "
          f"{human(freed)} {'freed' if args.apply else 'reclaimable'} "
          f"(grace {args.min_age_days:g}d)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
