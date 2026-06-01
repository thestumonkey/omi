"""
Minimal stub for firebase_admin.messaging.

Drop-in replacement used when firebase-admin is not installed.
All send operations are no-ops that print a log line.
"""

from dataclasses import dataclass, field
from typing import List, Optional

# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class Notification:
    title: str = ''
    body: str = ''


@dataclass
class AndroidNotification:
    tag: str = ''


@dataclass
class AndroidConfig:
    collapse_key: str = ''
    priority: str = 'normal'
    notification: Optional[AndroidNotification] = None


@dataclass
class Aps:
    content_available: bool = False


@dataclass
class APNSPayload:
    aps: Optional[Aps] = None


@dataclass
class APNSConfig:
    headers: dict = field(default_factory=dict)
    payload: Optional[APNSPayload] = None


@dataclass
class WebpushNotification:
    title: Optional[str] = None
    body: Optional[str] = None
    icon: Optional[str] = None


@dataclass
class WebpushFCMOptions:
    link: Optional[str] = None


@dataclass
class WebpushConfig:
    headers: dict = field(default_factory=dict)
    notification: Optional[WebpushNotification] = None
    fcm_options: Optional[WebpushFCMOptions] = None


@dataclass
class Message:
    token: str = ''
    notification: Optional[Notification] = None
    data: Optional[dict] = None
    android: Optional[AndroidConfig] = None
    apns: Optional[APNSConfig] = None
    webpush: Optional[WebpushConfig] = None


# ── Stub send responses ───────────────────────────────────────────────────────


@dataclass
class SendResponse:
    success: bool = True
    message_id: str = 'stub-message-id'
    exception: Optional[Exception] = None


@dataclass
class BatchResponse:
    responses: List[SendResponse] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0


# ── Stub send functions ───────────────────────────────────────────────────────


def send(message: Message) -> str:
    print(f'[FCM stub] send: token={message.token!r} title={getattr(message.notification, "title", None)!r}')
    return 'stub-message-id'


def send_each(messages: List[Message]) -> BatchResponse:
    print(f'[FCM stub] send_each: {len(messages)} message(s)')
    responses = [SendResponse(success=True) for _ in messages]
    return BatchResponse(responses=responses, success_count=len(messages), failure_count=0)
