from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


class AuthCoreClient:
    """薄封装：延迟加载当前仓库的 utils.auth_core。"""

    def __init__(self, project_root: Path, license_file: Path | None = None):
        root = str(project_root.resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        self._install_license(license_file)
        from utils.auth_core import generate_payload, init_auth

        self._generate_payload = generate_payload
        self._init_auth = init_auth

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
