# git public

Publish part of a private Git repository as a public one, keeping real
history for the published files, and nothing else.

You keep a single private repository with everything in it: notes,
experiments, data, the code. `git public` keeps a public repository in step
with it automatically. That public repository contains only the paths you
declare public, and only the public half of each commit message.

```
private repo (everything)                 public repo (code only)
  notes/  data/  code/                      src/  README.md ...
  "Fix parser crash                         "Fix parser crash"
   Private: ticket 4711, logs"     ──▶
```

- **One push updates both.** `git push` to your private remote first
  publishes the filtered history to the public remote.
- **Split commit messages.** Everything from a `Private:` line down stays
  private. A hook rejects commits that touch public files without a split
  message.
- **Hard limits.** `PRIVATE.md` lists paths and globs that may never be
  published. A push is refused if one appears anywhere in the public history.
- **Stable history.** Exports are deterministic, so public commit IDs never
  change and every push is a fast-forward.
- **Optional fresh start.** The public history can begin as a single commit
  with a message you choose, while the private history stays complete.

It is a Git extension: once `git-public` is on your `PATH`, `git public ...`
works. It is a single executable with no Python installation needed. Its
history filtering is done by a bundled copy of
[git-filter-repo](https://github.com/newren/git-filter-repo).

## Install

Download `git-public` and `SHA256SUMS` from the
[latest release](https://github.com/c0ldfront/git-public/releases/latest),
then:

```sh
sha256sum -c SHA256SUMS
install -m755 git-public ~/.local/bin/
git public -h
```

To build it from source, see [Build](#build). To uninstall, run
`git public hooks remove` in each repository, then delete the executable.

## Quick start

```sh
cd my-private-repo
git public init --remote git@github.com:you/project.git --subdirectory code
#   creates PUBLIC.md and PRIVATE.md and installs two hooks
git add PUBLIC.md PRIVATE.md && git commit -m "Declare public paths"

git public preview      # exactly what would be published; pushes nothing
git push                # publishes to the public remote, then pushes privately
```

`--subdirectory code` makes `code/` the root of the public repository. Use
`--path src --path README.md` instead to publish paths in place.

## Commit messages

A commit that touches public files needs a public part, then a line starting
with `Private:` and a different private part:

```
Fix crash when the config file is empty

Treat an empty file as the defaults.

Private:
Reported by a customer in ticket 4711; see the support notes.
```

The public repository gets the first two paragraphs. The `commit-msg` hook
rejects such a commit if:

- the `Private:` section is missing or empty
- the subject line itself is private
- both parts say the same thing

Commits that touch only private files need no split.

The hook is the first check. A second check runs on every publish: each
outgoing public commit is traced back to its private original and audited.
That catches `--no-verify`, amends, rebases and commits made in clones
without the hooks.

## PUBLIC.md and PRIVATE.md

Both files live at the repository root and are committed, so every clone
shares the same split. `init` creates them. After that you edit them by hand.

```markdown
## Settings

- remote: git@github.com:you/project.git
- branch: main
- source-branch: main
- private-remote: origin
- marker: Private:
- enforce-after: <commit>
- start-at: <commit>                     (optional, see below)
- start-message: Initial public release

## Paths

- code/ -> /
```

**PUBLIC.md**

- **Paths:**
  - `- dir/` publishes the path in place.
  - `- dir/ -> /` makes it the public root. It must then be the only path.
- **Settings:**
  - `remote`, `branch`: where and to which branch to publish.
  - `source-branch`: the private branch to publish.
  - `private-remote`: the remote whose pushes trigger publishing.
  - `marker`: the private-section marker.
  - `mailmap`: a mailmap file for public author and committer names.
  - `empty-message`: the replacement for older messages that were entirely
    private.
  - `enforce-after`: commits up to and including this one are exempt from the
    message rule, so existing history can be published.
- **Parsing:** only bullets under the `## Settings` and `## Paths` headings
  are read. Unknown keys are an error.

**PRIVATE.md**

```markdown
## Paths

- notes/
- data/
- *.key
```

- **Entries:** directories, files or globs. A glob without `/` matches names
  anywhere.
- **Enforcement:** `git public` refuses to publish if any entry appears in
  the public **history**; deleting the file in a later commit is not enough.
  It also refuses a public path that lies inside a private entry.
- **Defaults:** `init` starts the file with everything tracked that is not
  public.
- **Unclassified files:** `preview` and `config` report tracked files listed
  in neither file. Those stay private.

## Starting the public history fresh

By default, the public repository receives the filtered history of every
commit that touched the public paths. To begin it at one commit instead:

```sh
git public -m "Initial public release"            # start at the current tip
git public start --at <rev> -m "Initial import"   # start at an earlier commit
git public start --clear                          # publish the full history again
```

- **The first commit:** everything up to the start point becomes one public
  commit with your message. Later commits follow on top, unchanged.
- **Private history:** never rewritten.
- **Before the start point:** commits are not published, so their messages
  and any files deleted before it never reach the public repository.
- **Moving or clearing:** changing the start point rewrites the public history,
  so the next push needs `--force`.

## Commands

| Command | Purpose |
| --- | --- |
| `git public init [options]` | Create PUBLIC.md/PRIVATE.md (only if missing) and install the hooks |
| `git public preview` | Build the export and print every outgoing commit and file; never pushes |
| `git public push [--yes] [--force]` | Publish now, without a private push. `--force` overwrites a diverged public branch with a lease |
| `git public start -m MSG [--at REV] [--clear]` | Set or clear the start point (`git public -m MSG` is shorthand) |
| `git public check [FILE]` | Test a commit message (file or stdin) against the rule |
| `git public config` | Show the effective settings, private entries, unclassified files and hook status |
| `git public hooks [install\|remove\|status]` | Manage the two hooks |

- **Overrides:** `preview` and `push` accept `--remote`, `--branch`,
  `--subdirectory`, `--path`, `--mailmap` and the other `init` options, to
  override PUBLIC.md for one run.
- **Help:** `git public <command> -h` shows a command's options. Plain
  `git public --help` makes Git look for a man page instead.
- **Exit codes:**
  - 0: success
  - 1: refused or failed (rule violation, private path, divergence,
    unreachable remote)
  - 2: usage or configuration error

## How it works

Every export is built in a fresh temporary clone, never in your repository:

1. `git clone --no-local` the source branch.
2. Filter it with the bundled git-filter-repo: keep the public paths, drop
   commits that touch nothing public, cut each message at the marker and
   apply the mailmap.
3. Optionally fold the history up to the start point into one commit.
4. Audit the outgoing commits and the PRIVATE.md entries, then push. Pushing
   uses your normal Git credentials.

**Hooks:**

- `pre-push`: pushing the source branch to the private remote first runs
  `git public push --yes`. If publishing fails, the private push is aborted
  too, so the two repositories stay in step. Pushing to the public remote
  directly is refused.
- `commit-msg`: enforces the split message rule above.

**What git public writes:** `init` writes the two files (only if missing) and
the hooks; `start` edits two bullets in PUBLIC.md. Nothing else in your
repository is ever written. Existing hooks that git public didn't install are
never overwritten; you get the line to add to them instead.

## Requirements and platforms

- **Platform:** Linux x86_64 with glibc 2.44 or newer. Older glibc, macOS and
  Windows are untested.
- **Runtime:** `git`, `/bin/sh` for the hooks, and credentials for both
  remotes.

## Limitations

- **The rule can't judge content.** A secret written above the `Private:`
  line is published, and a forgotten marker publishes the whole message, so
  read `git public preview`.
- **Old file versions are published.** Every earlier version of a public file
  goes out too, unless it predates the start point.
- **Hooks are per clone.** PUBLIC.md and PRIVATE.md travel with the
  repository, but hooks don't, so run `git public hooks install` in each new
  clone.
- **`git push --no-verify` skips publishing.** It pushes privately only; the
  next normal push publishes everything outstanding.
- **Push order.** The public push happens before the private one. If the
  private push is then rejected, the public repository is briefly ahead.
- **Changing settings changes public commit IDs.** Changing the selection,
  marker, mailmap or start point means the next push needs `--force`.
- **Scope:** one source branch per repository. Tags, notes, LFS objects and
  submodule contents are not published.

## Build

Build requirements: [uv](https://github.com/astral-sh/uv), Python 3 and
network access for the pinned packages in `requirements-build.lock`
(PyInstaller 6.19.0, git-filter-repo 2.47.0).

```sh
make build          # -> dist/git-public (one self-contained executable)
make test           # build, then run the smoke test against the executable
```

`tests/smoke.py` builds synthetic private and public repositories and runs the
real executable in a [bubblewrap](https://github.com/containers/bubblewrap)
sandbox with every Python interpreter hidden. It runs from a path containing
spaces and exercises every command and hook, including the failure cases.

## License

MIT; see [LICENSE](LICENSE). The release executable bundles git-filter-repo,
the Python runtime and the PyInstaller bootloader; their licenses are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `licenses/`.
See [CHANGELOG.md](CHANGELOG.md) for history.
