# Third-party notices

The source in this repository is MIT licensed (see `LICENSE`). The release
executable is built with PyInstaller and bundles the following; their license
texts are in `licenses/`.

| Component | Version | License | Text |
| --- | --- | --- | --- |
| [git-filter-repo](https://github.com/newren/git-filter-repo) | 2.47.0 | MIT (see its COPYING for the optional alternatives) | `licenses/git-filter-repo-COPYING.txt`, `licenses/git-filter-repo-COPYING.mit.txt` |
| [Python](https://www.python.org/) runtime and standard library | 3.14 | Python Software Foundation License | `licenses/Python-LICENSE.txt` |
| [PyInstaller](https://pyinstaller.org/) bootloader | 6.19.0 | GPL 2.0 or later with the Bootloader Exception, which permits distributing the bundled executable under any license | `licenses/PyInstaller-COPYING.txt` |

PyInstaller and its hooks are build-time tools; only the bootloader is part of
the executable.
