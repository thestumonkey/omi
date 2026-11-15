"""Self-hosted: form memories only from conversations the owner took part in. [fork-only]

A wearable hears television, radio and other people's talk. Upstream still
writes "third party" and "unknown" memories from those conversations; it only
refuses to say they are about the owner. This gate drops the whole memory pass
unless the transcript proves the owner spoke:

  * exactly one speaker cluster is marked as the owner (``is_user``), the same
    evidence upstream's ``may_attribute_to_owner`` trusts; and
  * that cluster said at least SELF_HOSTED_MEMORY_OWNER_MIN_WORDS words (default
    20), so one mis-matched TV line does not open the gate.

The same rule marks such a conversation discarded (restorable with "Show
discarded") before any summary model call; a conversation the user kept is
left alone.

The "Only my conversations" switch (selfhosted_settings/capture.owner_only,
set from the apps through /v1/selfhosted/capture) turns the gate on or off.
Until someone sets it, SELF_HOSTED_MEMORY_OWNER_GATE=on|off decides, and
without that the gate is on when SELF_HOSTED is set, off on upstream deploys.
Wired in by the tail blocks of utils/conversations/process_conversation.py.
"""

import os
from dataclasses import dataclass
from typing import Any, Sequence

from utils.conversations.owner_attribution import OwnerAttributionEvidence, speaker_cluster_key
from utils import selfhosted_config

DEFAULT_MIN_OWNER_WORDS = 20


def gate_enabled() -> bool:
    stored = selfhosted_config.owner_only_setting()
    if stored is not None:
        return stored
    return default_enabled()


def default_enabled() -> bool:
    """The value before anyone sets the switch."""
    raw = os.environ.get('SELF_HOSTED_MEMORY_OWNER_GATE', '').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return False
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    return selfhosted_config.is_self_hosted()


def can_identify_owner(uid: str) -> bool:
    """Speaker ID marks the owner only with a Voice Profile. Without one, every
    recording would look ownerless, so the gate stays open. A storage error
    also keeps it open: showing a TV recording is better than hiding the owner's."""
    try:
        from utils.other.storage import get_user_has_speech_profile

        return bool(get_user_has_speech_profile(uid))
    except Exception:
        return False


def applies_to(uid: str) -> bool:
    return gate_enabled() and can_identify_owner(uid)


def min_owner_words() -> int:
    try:
        return max(0, int(os.environ.get('SELF_HOSTED_MEMORY_OWNER_MIN_WORDS', DEFAULT_MIN_OWNER_WORDS)))
    except ValueError:
        return DEFAULT_MIN_OWNER_WORDS


@dataclass(frozen=True)
class OwnerPresence:
    trust: str
    owner_words: int
    admitted: bool
    reason: str


def owner_presence(segments: Sequence[Any], *, min_words: int) -> OwnerPresence:
    evidence = OwnerAttributionEvidence.from_segments(segments or [])
    if evidence.trust != 'unique_owner':
        return OwnerPresence(evidence.trust, 0, False, f'owner_{evidence.trust}')
    owner_words = sum(
        len(str(getattr(segment, 'text', '') or '').split())
        for segment in segments
        if getattr(segment, 'is_user', False) and speaker_cluster_key(segment) == evidence.owner_cluster_id
    )
    if owner_words < min_words:
        return OwnerPresence(evidence.trust, owner_words, False, 'owner_too_quiet')
    return OwnerPresence(evidence.trust, owner_words, True, 'owner_present')
