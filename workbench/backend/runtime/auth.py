"""Local-first authentication for the Workbench HTTP and WebSocket surfaces."""

from __future__ import annotations

import ipaddress
import secrets


class WorkbenchAuth:
    """Verify a pre-shared operator token without storing it in the browser."""

    cookie_name = "workbench_session"

    def __init__(self, token: str | None) -> None:
        normalized = (token or "").strip()
        self.token = normalized or None

    @property
    def enabled(self) -> bool:
        return self.token is not None

    def verify(self, authorization: str | None, cookie: str | None) -> bool:
        if self.token is None:
            return True
        candidate = None
        if authorization is not None:
            scheme, separator, value = authorization.partition(" ")
            if scheme.lower() == "bearer" and separator and value and " " not in value:
                candidate = value
        if candidate is None and cookie:
            candidate = cookie
        return candidate is not None and secrets.compare_digest(candidate, self.token)

    def is_request_allowed(self, authorization: str | None, remote_host: str | None) -> bool:
        if self.enabled:
            return self.verify(authorization, None)
        if not remote_host:
            return True
        try:
            address = ipaddress.ip_address(remote_host)
        except ValueError:
            return remote_host in {"localhost", "testclient"}
        return address.is_loopback
