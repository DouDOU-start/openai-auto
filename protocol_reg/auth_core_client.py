from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


class AuthCoreClient:
    """薄封装：延迟加载当前仓库的 utils.auth_core。"""

    def __init__(self, project_root: Path, license_file: Path | None = None):
        project_root = project_root.resolve()
        self._validate_project_root(project_root)
        root = str(project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        self._install_license(license_file)
        try:
            from utils.auth_core import generate_payload, init_auth
        except ModuleNotFoundError as exc:
            # Most commonly this happens when the bundled native extension was
            # built for a different OS/arch (e.g. manylinux x86_64 on macOS).
            ext_hint = None
            try:
                candidates = sorted((project_root / "utils").glob("auth_core*") )
                ext_hint = ", ".join(p.name for p in candidates) if candidates else None
            except Exception:
                ext_hint = None
            msg = f"未能加载内置 utils.auth_core: {project_root}"
            if ext_hint:
                msg += f" (发现文件: {ext_hint})"
            msg += "。常见原因: 扩展文件与当前 Python/系统架构不匹配。"
            raise RuntimeError(msg) from exc
        except ImportError as exc:
            # ImportError (not ModuleNotFoundError) is typical for ABI/ELF mismatch.
            msg = f"未能导入内置 utils.auth_core: {project_root}。"
            msg += "常见原因: 扩展文件与当前系统/架构不匹配(例如 Linux x86_64 .so 在 macOS/arm64 上)。"
            raise RuntimeError(msg) from exc

        self._generate_payload = generate_payload
        self._init_auth = init_auth

    @staticmethod
    def _validate_project_root(project_root: Path) -> None:
        utils_dir = project_root / "utils"
        if not utils_dir.is_dir():
            raise RuntimeError(f"未找到内置依赖目录: {utils_dir}")
        if not (utils_dir / "db_manager.py").exists():
            raise RuntimeError(f"未找到内置依赖文件: {utils_dir / 'db_manager.py'}")
        if not any(utils_dir.glob("auth_core*")):
            raise RuntimeError(f"未找到 auth_core 扩展文件: {utils_dir}")

    def _install_license(self, license_file: Path | None) -> None:
        if license_file is None:
            return
        if not license_file.exists():
            raise RuntimeError(f"授权文件不存在: {license_file}")
        content = license_file.read_text(encoding="utf-8").strip()
        if not content:
            raise RuntimeError(f"授权文件内容为空: {license_file}")

        try:
            from utils import db_manager

            with db_manager.get_db_conn(is_write=True) as conn:
                cursor = db_manager.get_cursor(conn)
                db_manager.execute_sql(
                    cursor,
                    """
                    CREATE TABLE IF NOT EXISTS system_kv (
                        `key` TEXT PRIMARY KEY,
                        value TEXT
                    )
                    """,
                )
            db_manager.set_sys_kv("auth_license_file", content)
        except Exception as exc:
            raise RuntimeError(f"写入 auth_core 授权文件失败: {exc}") from exc

    def init_auth(self, *, session: Any, email: str, masked_email: str, proxies: Any, verify: bool) -> tuple[str, str]:
        return self._init_auth(
            session=session,
            email=email,
            masked_email=masked_email,
            proxies=proxies,
            verify=verify,
        )

    def payload(
        self,
        *,
        did: str,
        flow: str,
        proxy: str,
        user_agent: str,
        ctx: dict[str, Any],
    ) -> str:
        return self._generate_payload(
            did=did,
            flow=flow,
            proxy=proxy,
            user_agent=user_agent,
            impersonate="chrome110",
            ctx=ctx,
        ) or ""
