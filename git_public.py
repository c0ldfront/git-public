"""git public: publish a filtered, history-preserving public export of a private repository."""
import argparse
import contextlib
import fnmatch
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import git_filter_repo as fr

VERSION = "1.3.1"
PUBLIC_FILE = "PUBLIC.md"
PRIVATE_FILE = "PRIVATE.md"
# PUBLIC.md "## Settings" keys -> internal setting names.
FILE_KEYS = {"remote": "remote", "branch": "branch", "source-branch": "sourceBranch",
             "private-remote": "privateRemote", "marker": "marker", "mailmap": "mailmap",
             "empty-message": "emptyMessage", "enforce-after": "enforceAfter",
             "start-at": "startAt", "start-message": "startMessage"}
FILTER_REPO_VERSION = "2.47.0"  # pinned in requirements-build.lock
DEFAULT_MARKER = "Private:"
DEFAULT_EMPTY = "Update public source"
DEFAULT_PRIVATE_REMOTE = "origin"
STAGING_REF = "refs/git-public/remote"
KEYS = ("remote", "branch", "sourceBranch", "privateRemote", "subdirectory", "path", "marker", "mailmap",
        "emptyMessage", "enforceAfter", "startAt", "startMessage")
# Variables a hook inherits that would point export-side git commands at the private repository.
REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_QUARANTINE_PATH", "GIT_NAMESPACE")
HOOK_TAG = "# managed by git-public"
HOOKS = {name: f'#!/bin/sh\n{HOOK_TAG}\nexec git public hook {name} "$@"\n' for name in ("commit-msg", "pre-push")}

HELP = """\
Publish part of a private repository to a public remote, keeping history for
the published files only and stripping private commit-message sections.

Workflow after `git public init`:
  git commit   any commit touching published paths must split its message:
               a public part, then a line starting with the marker
               (default "Private:") and a different private part
  git push     pushing the source branch to the private remote first
               publishes the filtered export to the public remote; if that
               fails, nothing is pushed anywhere

What is public and what is private is declared in two tracked files at the
repository root, so every clone shares them:
  PUBLIC.md    settings and the paths that are published
  PRIVATE.md   paths and globs that must never be published; an export whose
               history contains one is refused, as is a public path inside one
`init` creates both (PRIVATE.md starts as everything tracked that is not
public). Edit them to change the split, commit them, then run `preview`.

Each export is rebuilt from a fresh temporary clone, so commit IDs are stable
and public pushes stay fast-forwards. The private repository is never modified
except by `init` (creates PUBLIC.md/PRIVATE.md if missing) and `hooks`.

Commands:
  init      create PUBLIC.md/PRIVATE.md and install the commit-msg and pre-push hooks
  config    show the effective settings and hook status
  check     validate a commit message file (or stdin) against the split rule
  preview   build the export and show exactly what would be published
  push      build the export and publish it now (normally done by git push)
  start     begin the public history at one commit with your message
            (`git public -m MESSAGE` is shorthand); the private history is kept
  hooks     install, remove or show the managed hooks

Run `git public <command> -h` for options. `git public --help` is intercepted
by git (it looks for a man page); use `git public -h` or `git-public --help`.
"""


class Failure(Exception):
    """A diagnosable failure that exits non-zero."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def git(*args, cwd=None, check=True, input=None):
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=input)
    except FileNotFoundError:
        raise Failure("git executable not found on PATH")
    if check and proc.returncode != 0:
        raise Failure(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def repo_root():
    proc = git("rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise Failure("not inside a git working tree; run this from your private repository")
    return Path(proc.stdout.strip())


def isolate_from_hook_env():
    for name in REPO_ENV:
        os.environ.pop(name, None)


def md_sections(text):
    """Return {heading: [bullet text]} for `## Heading` sections; fenced code is ignored."""
    sections, current, fenced = {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if bullet and current is not None:
            sections[current].append(bullet.group(1))
    return sections


def unquote(value):
    return value.strip().strip("`").strip()


def read_files(root):
    """Settings from PUBLIC.md and the private list from PRIVATE.md."""
    sections = md_sections((root / PUBLIC_FILE).read_text(errors="replace"))
    cfg = {"source": PUBLIC_FILE}
    for item in sections.get("settings", []):
        key, sep, value = item.partition(":")
        key = unquote(key).lower()
        if not sep or key not in FILE_KEYS:
            raise Failure(f"{PUBLIC_FILE}: unknown setting {item!r}; known: {', '.join(FILE_KEYS)}", 2)
        if unquote(value):
            cfg[FILE_KEYS[key]] = unquote(value)
    paths, root_dir = [], None
    for item in sections.get("paths", []):
        src, arrow, dst = item.partition("->")
        src = unquote(src).rstrip("/")
        if not arrow:
            paths.append(src)
            continue
        if unquote(dst) != "/":
            raise Failure(f"{PUBLIC_FILE}: only `-> /` (publish as the repository root) is supported: {item!r}", 2)
        if root_dir:
            raise Failure(f"{PUBLIC_FILE}: more than one path is marked `-> /`", 2)
        root_dir = src
    if root_dir and paths:
        raise Failure(f"{PUBLIC_FILE}: a `-> /` root cannot be combined with other paths", 2)
    if root_dir:
        cfg["subdirectory"] = root_dir
    if paths:
        cfg["path"] = paths
    private = root / PRIVATE_FILE
    cfg["private"] = [unquote(i) for i in md_sections(private.read_text(errors="replace")).get("paths", [])] if private.exists() else []
    return cfg


def read_config(root):
    """PUBLIC.md/PRIVATE.md when present; otherwise the 1.1.0 `public.*` git config keys."""
    if (root / PUBLIC_FILE).exists():
        return read_files(root)
    settings = {"source": "git config"}
    for key in KEYS:
        proc = git("config", "--local", "--get-all", f"public.{key}", cwd=root, check=False)
        values = [v for v in proc.stdout.splitlines() if v != ""]
        if values:
            settings[key] = values if key == "path" else values[-1]
    return settings


def private_match(path, patterns):
    """Return the PRIVATE.md entry that covers a repository path, or None."""
    for pattern in patterns:
        pat = pattern.rstrip("/")
        if any(ch in pat for ch in "*?["):
            parts = path.split("/")
            prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
            if any(fnmatch.fnmatchcase(p, pat) for p in prefixes):
                return pattern
            if "/" not in pat and any(fnmatch.fnmatchcase(part, pat) for part in parts):
                return pattern
        elif path == pat or path.startswith(pat + "/"):
            return pattern
    return None


def settings_from(args, root):
    cfg = read_config(root)
    if getattr(args, "subdirectory", None) or getattr(args, "path", None):
        # A selection on the command line replaces the stored one entirely.
        cfg.pop("subdirectory", None)
        cfg.pop("path", None)
    pick = lambda name, key: getattr(args, name, None) if getattr(args, name, None) not in (None, []) else cfg.get(key)
    s = {
        "remote": pick("remote", "remote"),
        "branch": pick("branch", "branch") or "main",
        "sourceBranch": pick("source_branch", "sourceBranch"),
        "privateRemote": pick("private_remote", "privateRemote") or DEFAULT_PRIVATE_REMOTE,
        "subdirectory": pick("subdirectory", "subdirectory"),
        "path": pick("path", "path") or [],
        "marker": pick("marker", "marker") or DEFAULT_MARKER,
        "mailmap": pick("mailmap", "mailmap"),
        "emptyMessage": pick("empty_message", "emptyMessage") or DEFAULT_EMPTY,
        "enforceAfter": getattr(args, "enforce_after", None) or cfg.get("enforceAfter"),
        "startAt": cfg.get("startAt"),
        "startMessage": (cfg.get("startMessage") or "").replace("\\n", "\n"),
        "private": cfg.get("private", []),
        "source": cfg.get("source"),
    }
    if not s["sourceBranch"]:
        head = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=root, check=False)
        if head.returncode != 0:
            raise Failure("HEAD is detached; pass --source-branch")
        s["sourceBranch"] = head.stdout.strip()
    return s


def branch_exists(root, branch):
    return git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", cwd=root, check=False).returncode == 0


def validate(s, root, need_branch=True, need_remote=True):
    if need_remote and not s["remote"]:
        raise Failure(f"no public remote configured; add `- remote: URL` under Settings in {PUBLIC_FILE} or pass --remote", 2)
    if s["subdirectory"] and s["path"]:
        raise Failure("use either --subdirectory or --path, not both", 2)
    if not s["subdirectory"] and not s["path"]:
        raise Failure("nothing selected for publication; pass --subdirectory DIR or one or more --path P", 2)
    for value in [s["subdirectory"], *s["path"]]:
        if value and (value.startswith("/") or ".." in Path(value).parts or value in (".", "")):
            raise Failure(f"unsafe or empty path selector: {value!r} (use repository-relative paths)", 2)
        hit = private_match(value, s["private"]) if value else None
        if hit:
            raise Failure(f"public path {value!r} is inside private entry {hit!r} in {PRIVATE_FILE}; "
                          f"fix {PUBLIC_FILE} or {PRIVATE_FILE}", 2)
    if not s["marker"].strip():
        raise Failure("marker must not be blank", 2)
    exists = branch_exists(root, s["sourceBranch"])
    if need_branch and not exists:
        raise Failure(f"source branch {s['sourceBranch']!r} does not exist", 2)
    if exists and s["subdirectory"] and git("cat-file", "-e", f"{s['sourceBranch']}:{s['subdirectory'].rstrip('/')}", cwd=root, check=False).returncode != 0:
        raise Failure(f"{s['subdirectory']!r} does not exist on branch {s['sourceBranch']}", 2)
    if s["mailmap"] and not (root / s["mailmap"]).resolve().is_file():
        raise Failure(f"mailmap file not found: {(root / s['mailmap']).resolve()}", 2)


def remote_url(remote, root):
    proc = git("remote", "get-url", "--push", remote, cwd=root, check=False)
    if proc.returncode == 0:
        remote = proc.stdout.strip()
    local = root / remote
    if "://" not in remote and ":" not in remote.split("/")[0] and local.exists():
        return str(local.resolve())
    return remote


# --- commit-message rule -----------------------------------------------------

def split_message(text, marker):
    """Return (public, private); private is None when the marker is absent."""
    match = re.search(r"^[ \t]*" + re.escape(marker), text, re.MULTILINE)
    if not match:
        return text.strip(), None
    return text[: match.start()].strip(), text[match.end():].strip()


def split_problem(text, marker):
    public, private = split_message(text, marker)
    if private is None:
        return f"no {marker!r} section"
    if not public:
        return "the public part is empty (the subject line must be public)"
    if not private:
        return f"the {marker!r} section is empty"
    norm = lambda t: " ".join(t.split()).casefold()
    if norm(public) == norm(private):
        return "the public and private parts are identical"
    return None


def rule_help(marker):
    return (f"Commits that touch published paths need a public part and a different private part:\n\n"
            f"    Fix crash when the config file is empty\n\n    Treat an empty file as the defaults.\n\n"
            f"    {marker}\n    Reported by a customer in ticket 4711; see the support notes.\n")


def strip_message(message, marker, empty):
    """Return the public part of a commit message (bytes in, bytes out)."""
    public, _ = split_message(message.decode("utf-8", "surrogateescape"), marker)
    return ((public or empty) + "\n").encode("utf-8", "surrogateescape")


def clean_message_file(text, root):
    """Drop comment lines and everything below the scissors line, as git does."""
    char = git("config", "core.commentChar", cwd=root, check=False).stdout.strip() or "#"
    lines = []
    for line in text.splitlines():
        if line.startswith(char) and ">8" in line and "-" * 8 in line:
            break
        if char != "auto" and line.startswith(char):
            continue
        lines.append(line)
    return "\n".join(lines)


def selection(s):
    return [s["subdirectory"].rstrip("/")] if s["subdirectory"] else [p.rstrip("/") for p in s["path"]]


def touches(names, s):
    sel = selection(s)
    return [n for n in names if any(n == p or n.startswith(p + "/") for p in sel)]


# --- export -----------------------------------------------------------------

def build_export(s, root, work):
    """Clone the source branch into work/export and filter it. Returns the export path."""
    export = work / "export"
    git("clone", "--quiet", "--no-local", "--no-tags", "--single-branch", "--branch", s["sourceBranch"], str(root), str(export))
    argv = ["--quiet"]
    if s["subdirectory"]:
        argv += ["--subdirectory-filter", s["subdirectory"].rstrip("/")]
    else:
        for p in s["path"]:
            argv += ["--path", p]
    if s["mailmap"]:
        argv += ["--mailmap", str((root / s["mailmap"]).resolve())]
    marker, empty = s["marker"], s["emptyMessage"]
    previous = Path.cwd()
    chatter = io.StringIO()  # filter-repo progress notes; shown only on failure
    os.chdir(export)
    try:
        with contextlib.redirect_stdout(chatter), contextlib.redirect_stderr(chatter):
            fr.setup_gettext()
            options = fr.FilteringOptions.parse_args(argv)
            fr.RepoFilter(options, message_callback=lambda m: strip_message(m, marker, empty)).run()
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise Failure(f"git filter-repo failed: {exc.code}\n{chatter.getvalue()}")
    finally:
        os.chdir(previous)
    if git("rev-parse", "--verify", "--quiet", "HEAD", cwd=export, check=False).returncode != 0:
        raise Failure("the export is empty: no commit touched the selected paths")
    return export


def load_origin(export):
    """filter-repo's commit-map: exported commit -> private original."""
    origin = {}
    for line in (export / ".git/filter-repo/commit-map").read_text().splitlines()[1:]:
        old, new = line.split()
        origin[new] = old
    return origin


def raw_commit(export, sha):
    """Split a commit object into (header lines, message bytes)."""
    data = subprocess.run(["git", "cat-file", "commit", sha], cwd=export, capture_output=True, check=True).stdout
    head, _, message = data.partition(b"\n\n")
    return head.split(b"\n"), message


def write_commit(export, tree, parents, identity, message):
    """Create a commit object from explicit parts; identical inputs give the identical ID."""
    lines = [b"tree " + tree] + [b"parent " + p for p in parents] + identity
    data = b"\n".join(lines) + b"\n\n" + message
    return subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=export, input=data,
                          capture_output=True, check=True).stdout.strip()


def apply_start(s, root, export, origin):
    """Fold the exported history up to start-at into one commit carrying start-message.

    Later commits are re-created on top with their trees, authors, dates and messages
    unchanged. Only the export is rewritten; the private repository is never touched.
    Returns None without a start point, else details for the report.
    """
    if not s["startAt"]:
        return None
    if not s["startMessage"].strip():
        raise Failure(f"{PUBLIC_FILE} sets start-at but no start-message", 2)
    start = git("rev-parse", "--verify", "--quiet", f"{s['startAt']}^{{commit}}", cwd=root, check=False).stdout.strip()
    if not start or git("merge-base", "--is-ancestor", start, s["sourceBranch"], cwd=root, check=False).returncode != 0:
        raise Failure(f"start-at {s['startAt']} is not a commit on {s['sourceBranch']}", 2)
    ancestors = set(git("rev-list", start, cwd=root).stdout.split())
    order = [line.split() for line in git("rev-list", "--topo-order", "--reverse", "--parents", "HEAD", cwd=export).stdout.splitlines()]
    before = {c[0] for c in order if origin.get(c[0]) in ancestors}
    if not before:
        return {"folded": 0, "root": None, "start": start}
    base = [c[0] for c in order if c[0] in before][-1]
    head, _ = raw_commit(export, base)
    identity = [h for h in head if h.startswith((b"author ", b"committer "))]
    tree = git("rev-parse", f"{base}^{{tree}}", cwd=export).stdout.strip().encode()
    new_root = write_commit(export, tree, [], identity, s["startMessage"].strip().encode() + b"\n").decode()
    mapped, new_origin = {}, {new_root: origin[base]}
    for sha, *parents in order:
        if sha in before:
            continue
        head, message = raw_commit(export, sha)
        new_parents = []
        for p in parents:
            target = new_root if p in before else mapped[p]
            if target not in new_parents:
                new_parents.append(target)
        keep = [h for h in head if h.startswith((b"author ", b"committer ", b"encoding "))]
        tree = next(h for h in head if h.startswith(b"tree "))[5:]
        mapped[sha] = write_commit(export, tree, [p.encode() for p in new_parents], keep, message).decode()
        new_origin[mapped[sha]] = origin[sha]
    tip = mapped.get(git("rev-parse", "HEAD", cwd=export).stdout.strip(), new_root)
    branch = git("symbolic-ref", "HEAD", cwd=export).stdout.strip()
    git("update-ref", branch, tip, cwd=export)
    origin.clear()
    origin.update(new_origin)
    return {"folded": len(before), "root": new_root, "start": start}


def inspect(s, root, export, url, origin, start=None):
    """Fetch the public branch, describe the outgoing commits and audit their source messages."""
    ls = git("ls-remote", url, f"refs/heads/{s['branch']}", cwd=export, check=False)
    if ls.returncode != 0:
        raise Failure(f"cannot reach public remote {url}: {ls.stderr.strip()}")
    remote_sha = ls.stdout.split()[0] if ls.stdout.strip() else None
    state = {"remote_sha": remote_sha, "head": git("rev-parse", "HEAD", cwd=export).stdout.strip(), "diverged": False}
    rev = ["HEAD"]
    if remote_sha:
        git("fetch", "--quiet", "--no-tags", url, f"+refs/heads/{s['branch']}:{STAGING_REF}", cwd=export)
        rev.append(f"^{STAGING_REF}")
        state["diverged"] = git("merge-base", "--is-ancestor", STAGING_REF, "HEAD", cwd=export, check=False).returncode != 0
    state["outgoing"] = [c for c in git("rev-list", "--reverse", *rev, cwd=export).stdout.split() if c]
    state["files"] = git("ls-tree", "-r", "--name-only", "HEAD", cwd=export).stdout.splitlines()
    state["start"] = start
    state["violations"] = []
    for sha in state["outgoing"]:
        if start and sha == start["root"]:
            continue  # the start commit carries the chosen public message
        old = origin.get(sha)
        if old is None:
            raise Failure(f"cannot map exported commit {sha[:12]} back to its private commit")
        if s["enforceAfter"] and git("merge-base", "--is-ancestor", old, s["enforceAfter"], cwd=root, check=False).returncode == 0:
            continue  # predates `git public init`; grandfathered
        problem = split_problem(git("show", "-s", "--format=%B", old, cwd=root).stdout, s["marker"])
        if problem:
            subject = git("show", "-s", "--format=%s", old, cwd=root).stdout.strip()
            state["violations"].append(f"{old[:12]} {subject!r}: {problem}")
    # PRIVATE.md is checked against every path in the export's history, not just the tip.
    history = {n for n in git("log", "--format=", "--name-only", "HEAD", cwd=export).stdout.splitlines() if n}
    state["private_hits"] = sorted(f"{source_path(s, n)} (PRIVATE.md: {hit})" for n in history
                                   if (hit := private_match(source_path(s, n), s["private"])))
    state["unclassified"] = unclassified(s, root)
    return state


def source_path(s, exported):
    """Map a path in the export back to its path in the private repository."""
    return f"{s['subdirectory'].rstrip('/')}/{exported}" if s["subdirectory"] else exported


def unclassified(s, root):
    """Tracked files on the source branch that neither PUBLIC.md nor PRIVATE.md covers."""
    if s["source"] != PUBLIC_FILE:
        return []
    names = git("ls-tree", "-r", "--name-only", s["sourceBranch"], cwd=root).stdout.splitlines()
    return [n for n in names if not touches([n], s) and not private_match(n, s["private"])]


def group_paths(names, limit=10):
    groups = sorted({n.split("/")[0] + ("/" if "/" in n else "") for n in names})
    return ", ".join(groups[:limit]) + (" ..." if len(groups) > limit else "")


def report(s, root, url, export, state, out):
    src = git("rev-parse", "--short", s["sourceBranch"], cwd=root).stdout.strip()
    sel = f"subdirectory {s['subdirectory']}" if s["subdirectory"] else "paths " + ", ".join(s["path"])
    print(f"Source:   {root} ({s['sourceBranch']} @ {src}), {sel}", file=out)
    where = state["remote_sha"][:12] if state["remote_sha"] else "branch does not exist yet"
    print(f"Public:   {url} ({s['branch']}: {where})", file=out)
    print(f"Export:   {state['head'][:12]}, {len(state['files'])} files", file=out)
    start = state.get("start")
    if start and start["root"]:
        print(f"Start:    public history begins at private {start['start'][:12]} as one commit "
              f"({start['folded']} earlier public commit(s) folded in): {s['startMessage'].splitlines()[0]!r}", file=out)
    elif start:
        print(f"Start:    start-at {start['start'][:12]} precedes every public commit; nothing folded", file=out)
    warnings = []
    if state["diverged"]:
        warnings.append("public branch has commits that are not in this export (non-fast-forward); push needs --force")
    leak = re.compile(re.escape(s["marker"].rstrip(":").strip()), re.IGNORECASE)
    if not state["outgoing"]:
        print("\nNothing to publish: the public branch is up to date.", file=out)
    else:
        print(f"\nOutgoing commits ({len(state['outgoing'])}), exactly as they will appear publicly:", file=out)
    for sha in state["outgoing"]:
        info = git("show", "-s", "--format=%h  %an <%ae>  %ad%n%B", "--date=short", sha, cwd=export).stdout.rstrip()
        head, _, body = info.partition("\n")
        print(f"\n  {head}", file=out)
        for line in body.splitlines():
            print(f"    | {line}", file=out)
        if body.strip() == s["emptyMessage"]:
            warnings.append(f"{sha[:12]}: message was entirely private; replaced with {s['emptyMessage']!r}")
        elif leak.search(body):
            warnings.append(f"{sha[:12]}: public message still mentions {leak.pattern!r}; check it")
    dirty = git("status", "--porcelain", "--", *selection(s), cwd=root).stdout.strip()
    if dirty:
        warnings.append("uncommitted changes under the published paths are not included")
    print("\nPublished files:", file=out)
    for name in state["files"][:200]:
        print(f"  {name}", file=out)
    if len(state["files"]) > 200:
        print(f"  ... and {len(state['files']) - 200} more", file=out)
    if state["unclassified"]:
        warnings.append(f"{len(state['unclassified'])} tracked file(s) are in neither {PUBLIC_FILE} nor {PRIVATE_FILE} "
                        f"(they stay private): {group_paths(state['unclassified'])}")
    for w in warnings:
        print(f"\nwarning: {w}", file=out)
    if state["private_hits"]:
        print(f"\nerror: the export's history contains paths listed in {PRIVATE_FILE}:", file=out)
        for hit in state["private_hits"][:50]:
            print(f"  {hit}", file=out)
        print("\nMove them out of the published paths, or remove them from history "
              "(e.g. git filter-repo --invert-paths --path P) before publishing.", file=out)
    if state["violations"]:
        print("\nerror: these commits touch published paths without a split message:", file=out)
        for v in state["violations"]:
            print(f"  {v}", file=out)
        print(f"\nReword them (e.g. git rebase -i, `reword`) before publishing.\n{rule_help(s['marker'])}", file=out)


def run_export(args, push, out=sys.stdout):
    root = repo_root()
    isolate_from_hook_env()
    s = settings_from(args, root)
    validate(s, root)
    url = remote_url(s["remote"], root)
    with tempfile.TemporaryDirectory(prefix="git-public-") as tmp:
        export = build_export(s, root, Path(tmp))
        origin = load_origin(export)
        start = apply_start(s, root, export, origin)
        state = inspect(s, root, export, url, origin, start)
        report(s, root, url, export, state, out)
        if state["private_hits"]:
            raise Failure(f"{len(state['private_hits'])} private path(s) would be published; nothing published")
        if state["violations"]:
            raise Failure(f"{len(state['violations'])} outgoing commit(s) break the split-message rule; nothing published")
        if not push:
            print("\n(preview: nothing pushed)", file=out)
            return 0
        if not state["outgoing"] and not state["diverged"]:
            return 0
        if state["diverged"] and not args.force:
            raise Failure("refusing non-fast-forward push; inspect the public branch, then run `git public push --force`")
        if not args.yes:
            if not sys.stdin.isatty():
                raise Failure("confirmation required: rerun with --yes", 2)
            answer = input(f"\nPush {len(state['outgoing'])} commit(s) to {url} {s['branch']}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Aborted; nothing pushed.", file=out)
                return 1
        cmd = ["push", "--no-verify"]
        if state["diverged"]:
            cmd.append(f"--force-with-lease=refs/heads/{s['branch']}:{state['remote_sha']}")
        cmd += [url, f"HEAD:refs/heads/{s['branch']}"]
        proc = git(*cmd, cwd=export, check=False)
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            raise Failure(f"push failed ({proc.returncode})")
        print(f"\nPublished {state['head'][:12]} to {url} {s['branch']}.", file=out)
    return 0


# --- hooks ------------------------------------------------------------------

def hooks_dir(root):
    return (root / git("rev-parse", "--git-path", "hooks", cwd=root).stdout.strip()).resolve()


def hook_state(root):
    """Map hook name -> 'installed' | 'missing' | 'foreign' (an unmanaged hook is present)."""
    states = {}
    for name in HOOKS:
        path = hooks_dir(root) / name
        if not path.exists():
            states[name] = "missing"
        elif HOOK_TAG in path.read_text(errors="replace"):
            states[name] = "installed"
        else:
            states[name] = "foreign"
    return states


def install_hooks(root, check, out=sys.stdout):
    states = hook_state(root)
    foreign = [n for n, st in states.items() if st == "foreign"]
    if foreign:
        lines = "\n".join(f"  {hooks_dir(root) / n}: add the line  git public hook {n} \"$@\" || exit 1" for n in foreign)
        raise Failure(f"existing hooks are not managed by git-public; nothing was changed. Merge them manually:\n{lines}")
    for name, body in HOOKS.items():
        path = hooks_dir(root) / name
        if path.exists() and path.read_text() == body and os.access(path, os.X_OK):
            continue
        print(f"{'would install' if check else 'installing'} hook {path}", file=out)
        if check:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.git-public.tmp")
        tmp.write_text(body)
        tmp.chmod(0o755)
        os.replace(tmp, path)


def cmd_hooks(args):
    root = repo_root()
    if args.action == "install":
        install_hooks(root, args.check)
    elif args.action == "remove":
        for name, st in hook_state(root).items():
            if st == "installed":
                print(f"{'would remove' if args.check else 'removing'} hook {hooks_dir(root) / name}")
                if not args.check:
                    (hooks_dir(root) / name).unlink()
    for name, st in hook_state(root).items():
        print(f"{name}: {st}")
    return 0


def cmd_hook(args):
    """Entry points called by the managed hook scripts."""
    root = repo_root()
    cfg = read_config(root)
    if not (cfg.get("subdirectory") or cfg.get("path")):
        return 0  # not configured in this repository
    s = settings_from(argparse.Namespace(), root)
    if args.name == "commit-msg":
        if not args.args:
            raise Failure("commit-msg hook needs the message file", 2)
        names = git("diff", "--cached", "--name-only", "-z", cwd=root).stdout.split("\0")
        public = touches([n for n in names if n], s)
        if not public:
            return 0
        text = clean_message_file(Path(args.args[0]).read_text(errors="surrogateescape"), root)
        problem = split_problem(text, s["marker"])
        if problem:
            shown = ", ".join(public[:5]) + (" ..." if len(public) > 5 else "")
            raise Failure(f"commit rejected: it touches published paths ({shown}) but {problem}.\n\n"
                          f"{rule_help(s['marker'])}\nYour message was kept in {args.args[0]}; "
                          f"retry with: git commit -e -F {args.args[0]}")
        return 0
    if args.name == "pre-push":
        remote, url = (args.args + ["", ""])[:2]
        if not s["remote"]:
            print(f"git public: no public remote in {PUBLIC_FILE}; pushing privately only", file=sys.stderr)
            return 0
        public_url = remote_url(s["remote"], root)
        target = remote_url(url, root) if url else ""
        if remote == s["remote"] or target == public_url:
            raise Failure("push refused: that is the public remote, which only receives filtered exports. "
                          "Push to the private remote instead (git push), or publish with `git public push`.")
        if remote != s["privateRemote"]:
            return 0
        source = f"refs/heads/{s['sourceBranch']}"
        pushing = False
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == source and set(parts[1]) != {"0"}:
                pushing = True
        if not pushing:
            return 0
        print(f"git public: publishing the filtered export of {s['sourceBranch']} before the private push", file=sys.stderr)
        try:
            return run_export(argparse.Namespace(yes=True, force=False), push=True, out=sys.stderr)
        except Failure as exc:
            raise Failure(f"{exc}\ngit public: private push aborted too, so the two repositories stay in step.", exc.code)
    raise Failure(f"unknown hook {args.name!r}", 2)


# --- commands ---------------------------------------------------------------

PUBLIC_INTRO = """\
# Public

Everything under **Paths** is published to the public repository by
`git public`; everything else in this repository stays private. Commit this
file: every clone reads it. To change what is public, edit it and run
`git public preview`. `PRIVATE.md` lists what must never be published.

Paths: `- dir/` publishes that path in place; `- dir/ -> /` makes `dir/` the
root of the public repository (then it must be the only path).
"""

PRIVATE_INTRO = """\
# Private

Nothing here is ever published. `git public` refuses to publish an export
whose history contains one of these paths, and refuses a `PUBLIC.md` path
that lies inside one. Entries are `dir/`, a file path, or a glob such as
`*.EXE` (a glob without `/` matches file and directory names anywhere).
Tracked files listed in neither file stay private but are reported.
"""


def render_public(s):
    lines = [PUBLIC_INTRO, "## Settings", ""]
    for key, name in FILE_KEYS.items():
        if s.get(name):
            lines.append(f"- {key}: {s[name]}")
    lines += ["", "## Paths", ""]
    if s["subdirectory"]:
        lines.append(f"- {s['subdirectory'].rstrip('/')}/ -> /")
    lines += [f"- {p}" for p in s["path"]]
    return "\n".join(lines) + "\n"


def private_defaults(s, root):
    """Everything tracked that is not public, at the shallowest level that contains no public path."""
    public = selection(s)
    names = set(git("ls-files", cwd=root).stdout.splitlines()) | {PUBLIC_FILE, PRIVATE_FILE}
    entries = set()
    for name in names:
        if touches([name], s):
            continue
        parts = name.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if not any(p == prefix or p.startswith(prefix + "/") for p in public):
                entries.add(prefix + ("/" if i < len(parts) else ""))
                break
    return sorted(entries)


def render_private(entries):
    return "\n".join([PRIVATE_INTRO, "## Paths", "", *[f"- {e}" for e in entries]]) + "\n"


def create_only(path, text):
    """Write a new file atomically; never replaces an existing one."""
    tmp = path.with_name(f".{path.name}.git-public.tmp")
    tmp.write_text(text)
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()


def cmd_init(args):
    root = repo_root()
    public_path, private_path = root / PUBLIC_FILE, root / PRIVATE_FILE
    given = any(getattr(args, k, None) for k in ("remote", "branch", "source_branch", "private_remote", "subdirectory",
                                                 "path", "marker", "mailmap", "empty_message", "enforce_after"))
    s = settings_from(args, root)
    validate(s, root, need_branch=False, need_remote=False)
    writes = {}
    if not public_path.exists():
        # Commits up to this point are grandfathered; later ones must follow the split rule.
        enforce = s["enforceAfter"] or (s["sourceBranch"] if branch_exists(root, s["sourceBranch"]) else None)
        if enforce:
            resolved = git("rev-parse", "--verify", "--quiet", f"{enforce}^{{commit}}", cwd=root, check=False)
            if resolved.returncode != 0:
                raise Failure(f"--enforce-after {enforce!r} is not a commit", 2)
            s["enforceAfter"] = resolved.stdout.strip()
        writes[public_path] = render_public(s)
    elif given and render_public(s) != public_path.read_text(errors="replace"):
        raise Failure(f"{PUBLIC_FILE} already exists and differs from these options; edit it directly "
                      f"(or delete it to regenerate). Nothing was changed.", 1)
    if not private_path.exists():
        writes[private_path] = render_private(private_defaults(s, root))
    if not args.no_hooks:
        install_hooks(root, check=True, out=io.StringIO())  # refuse before writing anything
    for path, text in writes.items():
        print(f"{'would create' if args.check else 'creating'} {path.name}:\n")
        print("    " + text.rstrip().replace("\n", "\n    ") + "\n")
    if not writes:
        print(f"Using existing {PUBLIC_FILE} and {PRIVATE_FILE}.")
    if args.check:
        if not args.no_hooks:
            install_hooks(root, check=True)
        print("(check: nothing written)")
        return 0
    for path, text in writes.items():
        create_only(path, text)
    if not args.no_hooks:
        install_hooks(root, check=False)
    print(f"Next: review {PRIVATE_FILE}, commit both files, then run: git public preview")
    return 0


def cmd_config(args):
    root = repo_root()
    s = settings_from(args, root)
    print(f"settings from: {s['source'] if s['source'] == PUBLIC_FILE else 'git config (1.1.0 layout; run `git public init` to create PUBLIC.md)'}")
    for key in KEYS:
        value = s.get(key)
        if isinstance(value, list):
            value = ", ".join(value) or None
        print(f"public.{key} = {value if value else '(unset)'}")
    print(f"private = {', '.join(s['private']) or '(none)'}")
    if branch_exists(root, s["sourceBranch"]) and (s["subdirectory"] or s["path"]):
        loose = unclassified(s, root)
        if loose:
            print(f"unclassified: {len(loose)} tracked file(s) in neither file: {group_paths(loose)}")
    for name, st in hook_state(root).items():
        print(f"hook {name}: {st}")
    return 0


def set_settings(path, updates):
    """Replace or remove `- key: value` bullets under `## Settings`; other text is kept as is."""
    lines = path.read_text().splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(r"^##\s+settings\s*$", l.strip(), re.I)), None)
    if start is None:
        raise Failure(f"{path.name} has no '## Settings' section", 2)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    keyed = lambda l: unquote(re.sub(r"^\s*[-*]\s+", "", l).partition(":")[0]).lower()
    body = [l for l in lines[start + 1:end] if not (re.match(r"^\s*[-*]\s+", l) and keyed(l) in updates)]
    last = max((i for i, l in enumerate(body) if re.match(r"^\s*[-*]\s+", l)), default=-1)
    added = [f"- {k}: {v}\n" for k, v in updates.items() if v is not None]
    if last < 0:
        body = ["\n", *added, *body] if added else body
    else:
        body[last + 1:last + 1] = added
    return "".join(lines[:start + 1] + body + lines[end:])


def cmd_start(args):
    root = repo_root()
    path = root / PUBLIC_FILE
    if not path.exists():
        raise Failure(f"no {PUBLIC_FILE}; run `git public init` first", 2)
    if args.clear:
        updates = {"start-at": None, "start-message": None}
    else:
        if not args.message or not args.message.strip():
            raise Failure("give the first public commit's message with -m MESSAGE (or use --clear)", 2)
        s = settings_from(argparse.Namespace(), root)
        rev = args.at or s["sourceBranch"]
        sha = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", cwd=root, check=False).stdout.strip()
        if not sha:
            raise Failure(f"--at {rev!r} is not a commit", 2)
        if git("merge-base", "--is-ancestor", sha, s["sourceBranch"], cwd=root, check=False).returncode != 0:
            raise Failure(f"--at {rev!r} is not on {s['sourceBranch']}", 2)
        updates = {"start-at": sha, "start-message": args.message.strip().replace("\n", "\\n")}
    text = set_settings(path, updates)
    if text == path.read_text():
        print(f"{PUBLIC_FILE} already says that; nothing changed.")
        return 0
    for key, value in updates.items():
        print(f"{'would ' if args.check else ''}{'remove' if value is None else 'set'} {key}{'' if value is None else ': ' + value}")
    if args.check:
        print("(check: nothing written)")
        return 0
    tmp = path.with_name(f".{path.name}.git-public.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    print(f"Updated {PUBLIC_FILE}. Commit it, then run `git public preview`. The private history is unchanged.")
    return 0


def cmd_check(args):
    root = repo_root()
    marker = settings_from(args, root)["marker"]
    text = Path(args.file).read_text(errors="surrogateescape") if args.file != "-" else sys.stdin.read()
    problem = split_problem(clean_message_file(text, root), marker)
    if problem:
        raise Failure(f"{problem}\n\n{rule_help(marker)}")
    public, private = split_message(clean_message_file(text, root), marker)
    print(f"ok: public part {len(public.splitlines())} line(s), private part {len(private.splitlines())} line(s)")
    return 0


def add_selection(p):
    p.add_argument("--remote", help="public remote: a remote name in this repo, URL, or local path")
    p.add_argument("--branch", help="branch to publish to on the public remote (default: main)")
    p.add_argument("--source-branch", help="private branch to export (default: current branch)")
    p.add_argument("--private-remote", help=f"remote whose pushes trigger publishing (default: {DEFAULT_PRIVATE_REMOTE})")
    p.add_argument("--subdirectory", help="publish this directory as the public repository root")
    p.add_argument("--path", action="append", default=[], help="publish this path at its current location (repeatable)")
    p.add_argument("--marker", help=f"private-section marker line prefix (default: {DEFAULT_MARKER!r})")
    p.add_argument("--mailmap", help="mailmap file applied to public authors/committers (repo-relative)")
    p.add_argument("--empty-message", help=f"message for grandfathered commits whose message was entirely private (default: {DEFAULT_EMPTY!r})")


def parser():
    top = argparse.ArgumentParser(prog="git public", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument("--version", action="version", version=f"git-public {VERSION} (git-filter-repo {FILTER_REPO_VERSION})")
    top.add_argument("-m", "--message", dest="top_message", metavar="MESSAGE",
                     help="shorthand for `git public start -m MESSAGE`")
    sub = top.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser("init", help="create PUBLIC.md/PRIVATE.md and install hooks")
    add_selection(p)
    p.add_argument("--enforce-after", metavar="REV", help="grandfather commits up to REV (default: the source branch tip at first init)")
    p.add_argument("--no-hooks", action="store_true", help="create the files only; do not install hooks")
    p.add_argument("--check", action="store_true", help="show what would be written without writing")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("config", help="show effective settings and hook status")
    add_selection(p)
    p.set_defaults(func=cmd_config)
    p = sub.add_parser("check", help="validate a commit message against the split rule")
    p.add_argument("file", nargs="?", default="-", help="message file (default: stdin)")
    p.add_argument("--marker", help=f"private-section marker (default: configured or {DEFAULT_MARKER!r})")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("preview", help="show what would be published; never pushes")
    add_selection(p)
    p.set_defaults(func=lambda a: run_export(a, push=False))
    p = sub.add_parser("push", help="publish the export now")
    add_selection(p)
    p.add_argument("--yes", action="store_true", help="push without the interactive confirmation")
    p.add_argument("--force", action="store_true", help="allow a non-fast-forward push (with lease)")
    p.set_defaults(func=lambda a: run_export(a, push=True))
    p = sub.add_parser("start", help="make the public history begin at one commit with your message")
    p.add_argument("-m", "--message", help="message of the first public commit")
    p.add_argument("--at", metavar="REV", help="private commit to start from (default: the source branch tip)")
    p.add_argument("--clear", action="store_true", help="remove the start point; publish the full filtered history")
    p.add_argument("--check", action="store_true", help="show the change without writing PUBLIC.md")
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("hooks", help="install, remove or show the managed hooks")
    p.add_argument("action", nargs="?", default="status", choices=("install", "remove", "status"))
    p.add_argument("--check", action="store_true", help="show changes without making them")
    p.set_defaults(func=cmd_hooks)
    p = sub.add_parser("hook")  # internal: called by the managed hook scripts; unlisted
    p.add_argument("name", choices=sorted(HOOKS))
    p.add_argument("args", nargs="*")
    p.set_defaults(func=cmd_hook)
    return top


def main(argv=None):
    top = parser()
    args = top.parse_args(argv)
    if not args.command and args.top_message:
        args = argparse.Namespace(func=cmd_start, message=args.top_message, at=None, clear=False, check=False)
    elif not args.command:
        top.print_help()
        return 0
    try:
        return args.func(args)
    except Failure as exc:
        print(f"git public: error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("git public: interrupted; nothing pushed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
