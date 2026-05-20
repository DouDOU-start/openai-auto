from __future__ import annotations

from pathlib import Path

from .web import WebRuntime, _default_license_file, _repo_root, create_app


repo_root = _repo_root()
runtime = WebRuntime(
    repo_root=repo_root,
    accounts_path=(repo_root / "data" / "accounts.txt").resolve(),
    sessions_path=(repo_root / "data" / "sessions.json").resolve(),
    token_path=(repo_root / "data" / "tokens.jsonl").resolve(),
    checkout_path=(repo_root / "data" / "checkout_urls.jsonl").resolve(),
    config_path=(repo_root / "config" / "protocol-reg.yaml").resolve(),
    license_file=_default_license_file(repo_root),
)
app = create_app(runtime=runtime)
