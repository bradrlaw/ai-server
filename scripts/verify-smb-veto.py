#!/usr/bin/env python3
"""Verify the SMB share hides the credential stores (ADR-0022, 2026-10-16 addendum).

Read-only, stdlib only, no sudo and no SMB login. It never opens a hidden file:
it only reads the two smb.conf copies (through testparm) and file metadata.

Checks:
  1. The deployed /etc/samba/smb.conf has the same [srv-ai] `veto files` as the
     repo's config/smb.conf (setup-samba.sh re-run since the last edit), with
     `delete veto files = no` and `wide links = no`.
  2. Every credential store in STORES that exists has a vetoed name in its
     path, so an edit to the veto line can't silently un-hide one.
  3. Nothing visible exposes a hidden item under another name. Samba checks
     names, not targets, so it would serve:
       - a visible symlink pointing at or into a hidden item (Samba follows a
         link in the last path component without veto-checking its target);
       - a visible hardlink of a hidden file;
       - a visible bind mount of a hidden item.
     Links that leave the share are refused by `wide links = no`.

Copies of a secret saved in some other visible file are NOT detected.

Usage: scripts/verify-smb-veto.py [--repo-only] [-v]
  --repo-only  check config/smb.conf only (before running setup-samba.sh)
  -v           list every hidden item
  exit 0 = all checks passed, 1 = a check failed, 2 = usage/environment error.
"""
import glob
import os
import re
import shutil
import subprocess
import sys

REPO_CONF = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "config", "smb.conf")
LIVE_CONF = "/etc/samba/smb.conf"
SHARE = "srv-ai"

# Credential stores that must never be visible over SMB (globs, relative to
# the share path). Keep in sync with the veto comment in config/smb.conf.
STORES = [
    "docker/.env",
    "scripts/server-status.env",
    "var/status-service/auth.json",
    "comfyui*/login/PASSWORD",
    "comfyui*/custom_nodes*/ComfyUI-Login/.secret-key.txt",
    "ai-toolkit/aitk_db.db*",
    "openclaw/auth-secrets",
    "openclaw/state",
    "openclaw.bak-*",
    "hermes",
    "storage-bulk/backups",
    "comfyui*/user/comfyui_*.log",
]
# Dependency/cache trees: their hidden `state` dirs etc. are only counted.
NOISY = re.compile(r"(^|/)(node_modules|site-packages|dist-packages|venvs?|\.venv|\.cache|\.npm|__pycache__)(/|$)")

PASS = FAIL = 0


def report(ok, msg):
    global PASS, FAIL
    if ok is None:
        print(f"  INFO  {msg}")
        return
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def param(conf, name):
    p = subprocess.run(["testparm", "-s", f"--section-name={SHARE}", f"--parameter-name={name}", conf],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"testparm failed on {conf}: {p.stderr.strip().splitlines()[-1:]}")
    return p.stdout.strip()


def compile_veto(value):
    names = [n for n in value.split("/") if n]
    rx = [re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in n),
                     re.IGNORECASE | re.DOTALL) for n in names]
    return names, rx


def main():
    args = set(sys.argv[1:])
    if args & {"-h", "--help"}:
        print(__doc__.strip())
        return 0
    if args - {"--repo-only", "-v"}:
        print(__doc__.split("Usage:")[1].strip().split("\n")[0])
        return 2
    if not shutil.which("testparm"):
        print("testparm not found (is samba installed?)")
        return 2
    repo_only, verbose = "--repo-only" in args, "-v" in args
    repo_conf = os.path.normpath(REPO_CONF)

    print(f"== [{SHARE}] config")
    repo_veto = param(repo_conf, "veto files")
    report(bool(repo_veto), f"{repo_conf} sets veto files ({len(compile_veto(repo_veto)[0])} names)")
    confs = [repo_conf]
    if repo_only:
        conf = repo_conf
        report(None, f"--repo-only: {LIVE_CONF} not compared")
    else:
        conf = LIVE_CONF
        confs.append(LIVE_CONF)
        live_veto = param(LIVE_CONF, "veto files")
        report(live_veto == repo_veto,
               f"{LIVE_CONF} veto files {'matches the repo' if live_veto == repo_veto else 'DIFFERS from the repo'}"
               + ("" if live_veto == repo_veto else " -> run: sudo /srv/ai/scripts/setup-samba.sh"))
    for c in confs:
        report(param(c, "delete veto files") == "No", f"{c}: delete veto files = No")
        report(param(c, "wide links") == "No", f"{c}: wide links = No (links leaving the share are refused)")
    root = param(conf, "path").rstrip("/") or "/"
    names, rx = compile_veto(param(conf, "veto files"))

    def vetoed(name):
        return any(r.fullmatch(name) for r in rx)

    def hidden_by(rel, regexes=None):
        regexes = rx if regexes is None else regexes
        return next((c for c in rel.split("/") if any(r.fullmatch(c) for r in regexes)), None)

    repo_rx = compile_veto(repo_veto)[1]
    print(f"\n== credential stores hidden by {conf} ({root})")
    for pat in STORES:
        hits = sorted(glob.glob(os.path.join(root, pat)))
        if not hits:
            report(None, f"{pat}: not present")
        for h in hits:
            rel = os.path.relpath(h, root)
            by = hidden_by(rel)
            if by:
                report(True, f"{rel} (by `{by}`)")
            elif conf != repo_conf and hidden_by(rel, repo_rx):
                report(False, f"{rel} is VISIBLE: the repo conf hides it, deploy it (see above)")
            else:
                report(False, f"{rel} is VISIBLE: add its name to veto files in config/smb.conf")

    # Walk the visible tree without following links; prune vetoed names.
    hidden, links, multi, vis_dirs = [], [], {}, set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            rel = os.path.relpath(e.path, root)
            if vetoed(e.name):
                hidden.append(rel)
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if e.is_symlink():
                links.append(rel)
            elif e.is_dir(follow_symlinks=False):
                vis_dirs.add((st.st_dev, st.st_ino))
                stack.append(e.path)
            elif st.st_nlink > 1:
                multi.setdefault((st.st_dev, st.st_ino), rel)

    # Identities of everything inside the hidden items (metadata only).
    hidden_ids = {}
    for rel in hidden:
        top = os.path.join(root, rel)
        try:
            st = os.lstat(top)
        except OSError:
            continue
        hidden_ids[(st.st_dev, st.st_ino)] = rel
        if os.path.isdir(top) and not os.path.islink(top):
            for dp, dns, fns in os.walk(top, onerror=lambda _e: None):
                for n in dns + fns:
                    try:
                        s = os.lstat(os.path.join(dp, n))
                    except OSError:
                        continue
                    hidden_ids[(s.st_dev, s.st_ino)] = os.path.relpath(os.path.join(dp, n), root)

    print("\n== nothing visible exposes a hidden item")
    leaks, outside, root_real = [], 0, os.path.realpath(root)
    for rel in links:
        real = os.path.realpath(os.path.join(root, rel))
        if os.path.commonpath([real, root_real]) != root_real:
            outside += 1
            continue
        by = hidden_by(os.path.relpath(real, root_real))
        if by:
            leaks.append((rel, os.path.relpath(real, root_real), by))
    for rel, target, by in leaks:
        report(False, f"symlink {rel} -> {target} exposes a hidden item (`{by}`): remove it or rename it to a vetoed name")
    report(not leaks if links else None,
           f"{len(links)} visible symlinks: {len(leaks)} point at or into a hidden item, "
           f"{outside} leave the share (refused)")
    shared = [(multi[k], hidden_ids[k]) for k in multi if k in hidden_ids]
    for vis, hid in shared:
        report(False, f"hardlink {vis} is the same file as hidden {hid}")
    report(not shared, f"{len(multi)} multiply-linked visible files: {len(shared)} share an inode with a hidden file")
    mounts = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split()[4])
            if mp != root and mp.startswith(root + "/"):
                mounts.append(os.path.relpath(mp, root))
    vis_mounts = [m for m in mounts if not hidden_by(m)]
    bad = []
    for m in vis_mounts:
        try:
            st = os.stat(os.path.join(root, m))
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in hidden_ids:
            bad.append((m, hidden_ids[(st.st_dev, st.st_ino)]))
    for m, hid in bad:
        report(False, f"mount {m} is a bind mount of hidden {hid}")
    report(not bad, f"visible mounts ({', '.join(vis_mounts) or 'none'}): {len(bad)} expose a hidden item")
    bind_dirs = [hidden_ids[k] for k in vis_dirs if k in hidden_ids]
    report(not bind_dirs, f"{len(vis_dirs)} visible folders: {len(bind_dirs)} are also a hidden folder"
           + (f" ({', '.join(bind_dirs[:5])})" if bind_dirs else ""))

    print(f"\n== hidden over SMB ({len(names)} names)")
    main_hidden = sorted(h for h in hidden if not NOISY.search(h))
    shown = main_hidden if verbose else main_hidden[:25]
    for h in shown:
        print(f"        {h}{'/' if os.path.isdir(os.path.join(root, h)) and not os.path.islink(os.path.join(root, h)) else ''}")
    if len(main_hidden) > len(shown):
        print(f"        ... {len(main_hidden) - len(shown)} more (-v lists all)")
    print(f"        + {len(hidden) - len(main_hidden)} inside dependency/cache trees")

    print(f"\nResult: {PASS} passed, {FAIL} failed.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
