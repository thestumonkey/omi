"""``firebase_admin.credentials`` stand-in. [fork-only]

Self-hosted deployments have no Google service account. Upstream still builds
a credential object before ``initialize_app``; these classes accept whatever it
passes and hold it, so boot code runs unchanged.
"""

from typing import Any


class Base:
    def get_credential(self) -> Any:
        return None


class Certificate(Base):
    def __init__(self, cert: Any = None):
        self.cert = cert
        self.project_id = cert.get("project_id") if isinstance(cert, dict) else None


class ApplicationDefault(Base):
    pass


class RefreshToken(Base):
    def __init__(self, refresh_token: Any = None):
        self.refresh_token = refresh_token
