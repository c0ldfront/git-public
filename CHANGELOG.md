# Changelog

- 2026-10-20 (1.0.0): Created. `init`/`config`/`preview`/`push` built on bundled
  git-filter-repo 2.47.0; standalone PyInstaller artifact; sandboxed artifact
  smoke test passes.
- 2026-10-20 (1.1.0): Added the enforced workflow:
  - a `commit-msg` hook requiring split public/private messages
  - a `pre-push` hook that publishes before every private push of the source
    branch and refuses direct pushes to the public remote
  - a publish-time audit of every outgoing commit, with grandfathering via
    `public.enforceAfter`
  - `check` and `hooks` commands and the `--private-remote` option
  - `init` now stores the source branch and installs the hooks. Earlier
    commands and flags still work; with `--no-hooks`, `init` behaves as in
    1.0.0, except that a publish can now be refused for rule violations in
    post-init commits.
- 2026-10-20 (1.2.0): Settings and the public/private split moved to tracked
  `PUBLIC.md` and `PRIVATE.md`:
  - `init` creates both files (create-only); the default PRIVATE.md is the
    complement of the public paths.
  - Publishing is refused when the export's history contains a PRIVATE.md
    path or glob, or when a public path lies inside a private entry.
  - `preview` and `config` report tracked files listed in neither file.
  - The remote is optional: without one, the commit rule still applies and
    `git push` stays private.
  - Repositories configured by 1.1.0 git config keep working and migrate on
    `init`.
  - CLI flags, exit codes and hooks are unchanged.
- 2026-10-20 (1.3.0): Optional start point. `git public start -m MESSAGE
  [--at REV]` (shorthand `git public -m MESSAGE`) and `start --clear` edit only
  the `start-at`/`start-message` bullets in PUBLIC.md.
  - The export folds everything up to the start point into one commit with
    that message and re-creates later commits on top, deterministically.
  - The private history is untouched.
  - Without a start point, behavior is identical to 1.2.0.
- 2026-10-20 (1.3.1): First public release. Neutral example in the
  commit-rule help, MIT license and third-party notices, and a configurable
  build Python (`make PYTHON=...`).

