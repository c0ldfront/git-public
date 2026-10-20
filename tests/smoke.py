"""Relocated git-public artifact tests in a sandbox without Python, $HOME or the checkout."""
import glob, os, shutil, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {"PATH": "/usr/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_AUTHOR_NAME": "Private Person", "GIT_AUTHOR_EMAIL": "me@private.example",
       "GIT_COMMITTER_NAME": "Private Person", "GIT_COMMITTER_EMAIL": "me@private.example"}


def host(*args, cwd, date="2026-01-01T00:00:00Z"):
    """Synthetic fixture setup with the host git (outside the sandbox, no hooks yet)."""
    env = dict(ENV, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


with tempfile.TemporaryDirectory(prefix="git public artifact ") as tmp:
    work = Path(tmp)
    bindir = work / "bin dir"; bindir.mkdir()
    shutil.copy2(ROOT / "dist/git-public", bindir / "git-public")
    repo = work / "private repo"; repo.mkdir()
    public, private = work / "public.git", work / "private.git"
    for bare in (public, private):
        host("init", "--quiet", "--bare", "--initial-branch=main", str(bare), cwd=work)
    host("init", "--quiet", "--initial-branch=main", cwd=repo)
    host("remote", "add", "origin", "/w/private.git", cwd=repo)
    (repo / "research").mkdir(); (repo / "src").mkdir()
    (repo / "research/notes.md").write_text("secret offsets\n")
    host("add", ".", cwd=repo); host("commit", "-qm", "Research notes only", cwd=repo, date="2026-01-01T00:00:00Z")
    (repo / "src/main.c").write_text("int main(void){return 0;}\n")
    (repo / "README.md").write_text("# demo\n")
    host("add", ".", cwd=repo); host("commit", "-qm", "Legacy commit before the rule", cwd=repo, date="2026-01-02T00:00:00Z")
    (repo / "src/main.c").write_text("int main(void){return 1;}\n")
    host("add", ".", cwd=repo)
    host("commit", "-qm", "Add entry point\n\nInitial skeleton.\n\nPrivate:\nFound at 0x4A3F20, see research/notes.md\n", cwd=repo, date="2026-01-03T00:00:00Z")
    (repo / "src/main.c").write_text("int main(void){return 2;}\n")
    host("add", ".", cwd=repo); host("commit", "-qm", "Private: whole message is private", cwd=repo, date="2026-01-04T00:00:00Z")
    (repo / ".mailmap").write_text("Public Name <public@example.org> Private Person <me@private.example>\n")
    host("add", ".", cwd=repo); host("commit", "-qm", "Add mailmap", cwd=repo, date="2026-01-05T00:00:00Z")

    # Sandbox: read-only /usr with every Python interpreter and libpython masked.
    cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--clearenv", "--ro-bind", "/usr", "/usr",
           "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64", "--symlink", "usr/bin", "/bin",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for d in glob.glob("/usr/lib/python3*"):
        cmd += ["--tmpfs", d]
    for f in set(map(os.path.realpath, glob.glob("/usr/bin/python*") + glob.glob("/usr/lib/libpython*"))):
        cmd += ["--ro-bind", "/dev/null", f]
    cmd += ["--bind", str(work), "/w", "--chdir", "/w/private repo"]
    for k, v in dict(ENV, PATH="/w/bin dir:/usr/bin").items():
        cmd += ["--setenv", k, v]

    def at(directory):
        """The sandbox command with a different working directory."""
        c = list(cmd); c[c.index("--chdir") + 1] = directory
        return c

    def run(*args, ok=True, code=None, stdin=None):
        p = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=120,
                           input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        if code is not None:
            assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
        else:
            assert (p.returncode == 0) == ok, (args, p.returncode, p.stdout, p.stderr)
        return p

    def change(path, text):
        (repo / path).write_text(text)
        run("git", "add", path)

    hooks = repo / ".git/hooks"
    snapshot = lambda: (host("for-each-ref", cwd=repo), host("config", "--local", "--list", cwd=repo),
                        sorted(str(p) for p in repo.rglob("*") if ".git/" not in str(p) or "/hooks/" in str(p)))
    refs = lambda bare: host("for-each-ref", cwd=bare)
    head = lambda: host("rev-parse", "HEAD", cwd=repo).strip()
    log = lambda ref="main": host("log", "--format=%H %an <%ae>%n%B", ref, cwd=public)

    run("python3", "-c", "print(1)", ok=False)  # the runtime really lacks Python
    before = snapshot()
    assert "Commands:" in run("git-public", "--help").stdout
    assert "preview" in run("git", "public", "-h").stdout  # dispatched as a git extension
    assert snapshot() == before

    # --- setup -------------------------------------------------------------
    run("git", "public", "preview", code=2)  # nothing configured yet
    run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "missing", code=2)
    run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "../x", code=2)
    run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "src", "--check")
    assert snapshot() == before
    (hooks / "pre-push").write_text("#!/bin/sh\necho mine\n")  # a hook git-public does not own
    p = run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "src", code=1)
    assert "not managed by git-public" in p.stderr
    assert (hooks / "pre-push").read_text() == "#!/bin/sh\necho mine\n" and "public." not in host("config", "--local", "--list", cwd=repo)
    assert not (repo / "PUBLIC.md").exists() and not (repo / "PRIVATE.md").exists()  # refused before writing
    (hooks / "pre-push").unlink()
    run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "src")
    files = lambda: {n: (repo / n).read_bytes() for n in ("PUBLIC.md", "PRIVATE.md")}
    made = files()
    pub_md, priv_md = made["PUBLIC.md"].decode(), made["PRIVATE.md"].decode()
    assert "- remote: /w/public.git" in pub_md and "- src/ -> /" in pub_md and "- enforce-after: " in pub_md, pub_md
    for entry in ("- research/", "- README.md", "- .mailmap", "- PUBLIC.md", "- PRIVATE.md"):
        assert entry + "\n" in priv_md, (entry, priv_md)
    assert "- src" not in priv_md and "public." not in host("config", "--local", "--list", cwd=repo)
    hook_bytes = {n: (hooks / n).read_bytes() for n in ("commit-msg", "pre-push")}
    run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "src")  # same options: converges
    run("git", "public", "init")  # no options: uses the existing files
    p = run("git", "public", "init", "--remote", "/w/public.git", "--subdirectory", "src", "--branch", "other", code=1)
    assert "already exists" in p.stderr
    assert files() == made and {n: (hooks / n).read_bytes() for n in hook_bytes} == hook_bytes
    p = run("git", "public", "config")
    assert "hook pre-push: installed" in p.stdout and "settings from: PUBLIC.md" in p.stdout and "private = " in p.stdout

    before = snapshot()
    p = run("git", "public", "preview")
    assert "Outgoing commits (3)" in p.stdout and "0x4A3F20" not in p.stdout and "Research notes" not in p.stdout, p.stdout
    assert "Update public source" in p.stdout and "nothing pushed" in p.stdout  # grandfathered commits pass
    assert snapshot() == before and refs(public) == ""  # preview pushes and changes nothing

    # --- commit rule (commit-msg hook) ------------------------------------
    change("src/main.c", "int main(void){return 3;}\n")
    tip = head()
    p = run("git", "commit", "-qm", "Tweak return value", code=1)
    assert "commit rejected" in p.stderr and "no 'Private:' section" in p.stderr and head() == tip, p.stderr
    p = run("git", "commit", "-qm", "Tweak return\n\nPrivate:\nTweak   return", code=1)
    assert "identical" in p.stderr and head() == tip
    run("git", "commit", "-qm", "Private: subject cannot be private", code=1)
    run("git", "commit", "-qm", "Return 3\n\nPrivate: bisected via notes")
    change("research/notes.md", "research only\n")
    run("git", "commit", "-qm", "Research-only commits need no split")
    run("git", "public", "check", stdin="A\n\nPrivate: b\n")
    run("git", "public", "check", stdin="same\n", code=1)

    # --- git push publishes (pre-push hook) -------------------------------
    p = run("git", "push", "/w/public.git", "main", code=1)  # never push the private branch straight to public
    assert "public remote" in p.stderr and refs(public) == ""
    run("git", "push", "-q", "origin", "main")
    assert host("rev-parse", "main", cwd=private).strip() == head()
    assert host("ls-tree", "-r", "--name-only", "main", cwd=public).split() == ["main.c"]
    text = host("log", "--format=%B", "main", cwd=public)  # messages only; the fixture author is "Private Person"
    assert "Add entry point" in text and "Initial skeleton." in text and "Return 3" in text
    for secret in ("0x4A3F20", "bisected", "Private", "Research", "research"):
        assert secret not in text, (secret, text)

    change("src/main.c", "int main(void){return 4;}\n")
    run("git", "commit", "-qm", "Return 4\n\nPrivate: matches capture")
    old = host("rev-parse", "main", cwd=public).strip()
    run("git", "push", "-q", "origin", "main")
    assert host("rev-parse", "main~1", cwd=public).strip() == old  # deterministic fast-forward

    # A commit that bypassed the hook blocks both pushes.
    change("src/main.c", "int main(void){return 5;}\n")
    run("git", "commit", "-q", "--no-verify", "-m", "Leaky message")
    pub_before, priv_before = refs(public), refs(private)
    p = run("git", "push", "-q", "origin", "main", code=1)
    assert "split-message rule" in p.stderr and "private push aborted" in p.stderr, p.stderr
    assert refs(public) == pub_before and refs(private) == priv_before
    assert "Leaky message" in run("git", "public", "preview", code=1).stdout
    run("git", "commit", "-q", "--amend", "-m", "Return 5\n\nPrivate: fixed the message")
    run("git", "push", "-q", "origin", "main")
    assert host("rev-parse", "main", cwd=private).strip() == head() and "Leaky" not in log()

    # Pushing another branch to the private remote does not publish.
    run("git", "switch", "-qc", "feature")
    change("src/main.c", "int main(void){return 6;}\n")
    run("git", "commit", "-qm", "Return 6\n\nPrivate: experiment")
    pub_before = refs(public)
    run("git", "push", "-q", "origin", "feature")
    assert refs(public) == pub_before and "feature" in refs(private)
    run("git", "switch", "-q", "main")

    # Someone else pushed to the public branch: git push refuses; --force with lease resolves it.
    clone = work / "other"; host("clone", "-q", str(public), str(clone), cwd=work)
    (clone / "x.txt").write_text("x\n"); host("add", ".", cwd=clone); host("commit", "-qm", "outside", cwd=clone); host("push", "-q", cwd=clone)
    change("src/main.c", "int main(void){return 7;}\n")
    run("git", "commit", "-qm", "Return 7\n\nPrivate: final")
    priv_before = refs(private)
    p = run("git", "push", "-q", "origin", "main", code=1)
    assert "non-fast-forward" in p.stderr and refs(private) == priv_before
    run("git", "public", "push", "--yes", "--force")
    run("git", "push", "-q", "origin", "main")
    assert "outside" not in host("log", "--format=%s", "main", cwd=public)

    # PRIVATE.md is a hard line: private paths in the export's history block publishing.
    with open(repo / "PRIVATE.md", "a") as f:
        f.write("- src/keys/\n- *.pem\n")
    (repo / "src/keys").mkdir()
    change("src/keys/signing.txt", "secret\n")
    run("git", "commit", "-qm", "Add key\n\nPrivate: should never ship")
    pub_before, priv_before = refs(public), refs(private)
    p = run("git", "push", "-q", "origin", "main", code=1)
    assert "PRIVATE.md" in p.stderr and "src/keys/signing.txt" in p.stderr and refs(public) == pub_before and refs(private) == priv_before, p.stderr
    run("git", "rm", "-q", "src/keys/signing.txt"); run("git", "commit", "-qm", "Drop key\n\nPrivate: still in history")
    assert "src/keys/signing.txt" in run("git", "public", "preview", code=1).stdout  # history, not just the tip
    run("git", "reset", "-q", "--hard", "HEAD~2")
    change("src/cert.pem", "pem\n")
    run("git", "commit", "-qm", "Add cert\n\nPrivate: glob test")
    assert "*.pem" in run("git", "public", "preview", code=1).stdout
    run("git", "reset", "-q", "--hard", "HEAD~1")
    change("notes.txt", "loose\n")
    run("git", "commit", "-qm", "Unlisted top-level file")
    assert "neither PUBLIC.md nor PRIVATE.md" in run("git", "public", "preview").stdout

    # The files travel with the repository: commit them, clone, install hooks there.
    run("git", "add", "PUBLIC.md", "PRIVATE.md"); run("git", "commit", "-qm", "Declare public and private paths")
    run("git", "push", "-q", "origin", "main")
    run("git", "clone", "-q", "/w/private.git", "/w/second clone")
    p = subprocess.run(at("/w/second clone") + ["git", "public", "config"], capture_output=True, text=True)
    assert p.returncode == 0 and "settings from: PUBLIC.md" in p.stdout and "src" in p.stdout, (p.stdout, p.stderr)
    second = at("/w/second clone")
    assert subprocess.run(second + ["git", "public", "hooks", "install"], capture_output=True).returncode == 0
    (work / "second clone/src/main.c").write_text("int main(void){return 8;}\n")
    p = subprocess.run(second + ["git", "commit", "-qam", "No split here"], capture_output=True, text=True)
    assert p.returncode == 1 and "commit rejected" in p.stderr, (p.returncode, p.stderr)

    # A public path inside a private entry is refused; path mode with a mailmap to a second branch.
    p = run("git", "public", "preview", "--path", "src", "--path", "README.md", code=2)
    assert "inside private entry 'README.md'" in p.stderr, p.stderr
    (repo / "PRIVATE.md").write_text((repo / "PRIVATE.md").read_text().replace("- README.md\n", ""))
    run("git", "public", "push", "--yes", "--path", "src", "--path", "README.md", "--mailmap", ".mailmap", "--branch", "paths")
    assert host("ls-tree", "-r", "--name-only", "paths", cwd=public).split() == ["README.md", "src/main.c"]
    assert "private.example" not in log("paths") and "public@example.org" in log("paths")
    run("git", "public", "push", code=0)  # up to date: no confirmation needed, nothing to do
    run("git", "public", "preview", "--remote", "/w/nope.git", code=1)
    assert "not inside a git working tree" in run("git", "-C", "/w", "public", "preview", code=1).stderr

    # A repository configured by 1.1.0 (git config keys) still works and migrates on init.
    legacy = work / "legacy"; legacy.mkdir()
    host("init", "-q", "-b", "main", cwd=legacy); (legacy / "lib").mkdir(); (legacy / "lib/a.c").write_text("a\n")
    host("add", ".", cwd=legacy); host("commit", "-qm", "Legacy", cwd=legacy)
    host("config", "public.remote", "/w/public.git", cwd=legacy); host("config", "public.subdirectory", "lib", cwd=legacy)
    old = at("/w/legacy")
    p = subprocess.run(old + ["git", "public", "config"], capture_output=True, text=True)
    assert p.returncode == 0 and "settings from: git config" in p.stdout, p.stdout
    assert subprocess.run(old + ["git", "public", "init", "--no-hooks"], capture_output=True).returncode == 0
    assert "- lib/ -> /" in (legacy / "PUBLIC.md").read_text() and "- remote: /w/public.git" in (legacy / "PUBLIC.md").read_text()

    # Start point: the public history begins at one commit with a chosen message;
    # the private history (with a leaky message and a deleted private file) is untouched.
    fresh, fresh_pub = work / "fresh", work / "fresh-public.git"
    host("init", "-q", "--bare", "-b", "main", str(fresh_pub), cwd=work)
    fresh.mkdir(); host("init", "-q", "-b", "main", cwd=fresh); (fresh / "src").mkdir()
    (fresh / "src/a.c").write_text("a\n"); (fresh / "src/dump.pem").write_text("pem\n")
    host("add", ".", cwd=fresh); host("commit", "-qm", "Dump found at 0xDEAD in research", cwd=fresh, date="2026-02-01T00:00:00Z")
    host("rm", "-q", "src/dump.pem", cwd=fresh); host("commit", "-qm", "Drop dump", cwd=fresh, date="2026-02-02T00:00:00Z")
    (fresh / "src/a.c").write_text("a2\n"); host("commit", "-qam", "Tidy", cwd=fresh, date="2026-02-03T00:00:00Z")
    private_log = host("log", "--format=%H %s", cwd=fresh)
    f = at("/w/fresh")
    frun = lambda *a, code=0: (lambda p: (p.returncode == code or (_ for _ in ()).throw(AssertionError((a, p.returncode, p.stdout, p.stderr)))) and p)(
        subprocess.run(f + list(a), capture_output=True, text=True, stdin=subprocess.DEVNULL))
    frun("git", "public", "init", "--remote", "/w/fresh-public.git", "--subdirectory", "src", "--no-hooks")
    with open(fresh / "PRIVATE.md", "a") as fh:
        fh.write("- *.pem\n")
    assert "dump.pem" in frun("git", "public", "preview", code=1).stdout  # full history would leak it
    frun("git", "public", "start", code=2)  # a message is required
    frun("git", "public", "start", "-m", "x", "--at", "nope", code=2)
    frun("git", "public", "-m", "Initial public release")
    tip = host("rev-parse", "HEAD", cwd=fresh).strip()
    pub_md = (fresh / "PUBLIC.md").read_text()
    assert f"- start-at: {tip}\n" in pub_md and "- start-message: Initial public release\n" in pub_md, pub_md
    assert "already says that" in frun("git", "public", "-m", "Initial public release").stdout
    assert (fresh / "PUBLIC.md").read_text() == pub_md
    p = frun("git", "public", "preview")
    assert "Start:" in p.stdout and "0xDEAD" not in p.stdout and "dump.pem" not in p.stdout, p.stdout
    frun("git", "public", "push", "--yes")
    assert host("log", "--format=%s", "main", cwd=fresh_pub).split("\n")[0] == "Initial public release"
    assert host("rev-list", "--count", "main", cwd=fresh_pub).strip() == "1"
    assert host("rev-parse", "main^{tree}", cwd=fresh_pub).strip() == host("rev-parse", "HEAD:src", cwd=fresh).strip()
    assert "dump.pem" not in host("log", "--format=", "--name-only", "main", cwd=fresh_pub)
    assert host("log", "--format=%H %s", cwd=fresh) == private_log  # private history untouched
    (fresh / "src/b.c").write_text("b\n"); host("add", ".", cwd=fresh)
    host("commit", "-qm", "Add b\n\nPrivate: from notes", cwd=fresh, date="2026-02-04T00:00:00Z")
    root_commit = host("rev-parse", "main", cwd=fresh_pub).strip()
    frun("git", "public", "push", "--yes")
    assert host("log", "--format=%s", "main", cwd=fresh_pub).split() == ["Add", "b", "Initial", "public", "release"]
    assert host("rev-parse", "main~1", cwd=fresh_pub).strip() == root_commit  # fast-forward on the start commit
    assert "Nothing to publish" in frun("git", "public", "push", "--yes").stdout  # deterministic rerun
    frun("git", "public", "start", "--at", "HEAD~1", "-m", "Other start")
    frun("git", "public", "push", "--yes", code=1)  # moving the start point rewrites public history
    frun("git", "public", "push", "--yes", "--force")
    assert host("log", "--format=%s", "main", cwd=fresh_pub).splitlines() == ["Add b", "Other start"]
    frun("git", "public", "start", "--clear")
    assert "start-at" not in (fresh / "PUBLIC.md").read_text()
    frun("git", "public", "preview", code=1)  # full history again: the private file is back in it

    run("git", "public", "hooks", "remove")
    assert not (hooks / "commit-msg").exists() and not (hooks / "pre-push").exists()
    run("git", "public", "hooks", "remove")  # converges

print("git-public artifact smoke: OK")
