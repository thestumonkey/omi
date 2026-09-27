"""``firebase_admin.exceptions`` stand-in. [fork-only]

Same class tree as the real SDK, so upstream ``except`` clauses keep matching.
"""

from typing import Any, Optional


class FirebaseError(Exception):
    def __init__(self, code: str, message: str, cause: Optional[BaseException] = None, http_response: Any = None):
        super().__init__(message)
        self.code = code
        self.cause = cause
        self.http_response = http_response


class InvalidArgumentError(FirebaseError):
    def __init__(self, message: str, cause: Optional[BaseException] = None, http_response: Any = None):
        super().__init__("INVALID_ARGUMENT", message, cause, http_response)


class NotFoundError(FirebaseError):
    def __init__(self, message: str, cause: Optional[BaseException] = None, http_response: Any = None):
        super().__init__("NOT_FOUND", message, cause, http_response)


class UnavailableError(FirebaseError):
    def __init__(self, message: str, cause: Optional[BaseException] = None, http_response: Any = None):
        super().__init__("UNAVAILABLE", message, cause, http_response)


class UnknownError(FirebaseError):
    def __init__(self, message: str, cause: Optional[BaseException] = None, http_response: Any = None):
        super().__init__("UNKNOWN", message, cause, http_response)
