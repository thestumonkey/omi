"""``firebase_admin.messaging`` stand-in: push is off. [fork-only]

Self-hosted has no FCM project, so sends are logged and reported as delivered.
Message types accept any keyword arguments: upstream adds fields to them often,
and a fixed field list here would turn each such change into a crash.
"""

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class _Payload:
    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        # Unset optional fields read as None, like the SDK's defaults.
        if name.startswith("__"):
            raise AttributeError(name)
        return None

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in vars(self).items() if k != "args")
        return f"{type(self).__name__}({fields})"


class Notification(_Payload): ...


class AndroidNotification(_Payload): ...


class AndroidConfig(_Payload): ...


class ApsAlert(_Payload): ...


class Aps(_Payload): ...


class APNSPayload(_Payload): ...


class APNSConfig(_Payload): ...


class WebpushNotification(_Payload): ...


class WebpushFCMOptions(_Payload): ...


class WebpushConfig(_Payload): ...


class Message(_Payload): ...


class MulticastMessage(_Payload): ...


class SendResponse:
    def __init__(self, message_id: Optional[str] = "selfhosted-noop", exception: Optional[BaseException] = None):
        self.message_id = message_id
        self.exception = exception

    @property
    def success(self) -> bool:
        return self.exception is None


class BatchResponse:
    def __init__(self, responses: List[SendResponse]):
        self.responses = responses
        self.success_count = sum(1 for r in responses if r.success)
        self.failure_count = len(responses) - self.success_count


def send(message: Message, dry_run: bool = False, app: Any = None) -> str:
    logger.info("push disabled (self-hosted): dropped 1 message")
    return "selfhosted-noop"


def send_each(messages: List[Message], dry_run: bool = False, app: Any = None) -> BatchResponse:
    logger.info("push disabled (self-hosted): dropped %d message(s)", len(messages))
    return BatchResponse([SendResponse() for _ in messages])


def send_each_for_multicast(
    multicast_message: MulticastMessage, dry_run: bool = False, app: Any = None
) -> BatchResponse:
    tokens = getattr(multicast_message, "tokens", None) or []
    return send_each([Message() for _ in tokens], dry_run, app)
