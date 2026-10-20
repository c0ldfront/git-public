# Builds one self-contained Linux x86_64 executable; git is a runtime prerequisite.
PYTHON ?= python3
.PHONY: build test clean
build:
	uv venv --allow-existing .build/venv --python $(PYTHON)
	uv pip install --python .build/venv/bin/python -r requirements-build.lock
	.build/venv/bin/python -m PyInstaller --clean --noconfirm --onefile --name git-public --distpath dist --workpath .build/work --specpath .build git_public.py

test: build
	$(PYTHON) -I tests/smoke.py
clean:
	$(RM) -r dist .build/work
