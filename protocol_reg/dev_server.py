from __future__ import annotations

from .web import WebRuntime, _default_license_file, _repo_root, create_app


repo_root = _repo_root()
runtime = WebRuntime(
    repo_root=repo_root,
    config_path=(repo_root / "config" / "protocol-reg.yaml").resolve(),
    license_file=_default_license_file(repo_root),
)
app = create_app(runtime=runtime)
