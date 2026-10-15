"""Self-hosted server settings the apps can read and change. [fork-only]

    GET /v1/selfhosted/llm   the chat model in use, the default, and the models
                             the self-hosted LLM server offers
    POST /v1/selfhosted/llm  {"model": "<id>"} picks one; null goes back to the
                             SELF_HOSTED_LLM_MODEL default
    GET  /v1/selfhosted/voice   live voice mode: "off" or "cloud" (OpenAI/Gemini)
    POST /v1/selfhosted/voice   {"mode": "off" | "cloud"}
    GET  /v1/selfhosted/capture {"owner_only": bool}: process only conversations
                                the owner spoke in (owner_memory_gate.py)
    POST /v1/selfhosted/capture {"owner_only": bool}

The choice is server-wide (one household server, one model), so it reaches the
pusher and desktop backend too. The llm routes 404 when SELF_HOSTED_LLM_URL
is not set.
"""

import logging
from typing import List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from utils import selfhosted_config
from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/v1/selfhosted', tags=['selfhosted'])


class LlmSettings(BaseModel):
    model: str
    default_model: str
    models: List[str]
    models_error: Optional[str] = None


class LlmSettingsUpdate(BaseModel):
    model: Optional[str] = None


def _selfhosted():
    from utils.llm import selfhosted

    if not selfhosted.SELF_HOSTED_LLM_URL:
        raise HTTPException(status_code=404, detail='No self-hosted LLM configured')
    return selfhosted


def _settings(selfhosted) -> LlmSettings:
    try:
        models, error = selfhosted.list_models(), None
    except (httpx.HTTPError, ValueError) as e:
        logger.warning('self-hosted LLM: cannot list models: %s', e)
        models, error = [], 'The LLM server did not return its model list.'
    return LlmSettings(
        model=selfhosted.current_model(),
        default_model=selfhosted.default_model(),
        models=models,
        models_error=error,
    )


@router.get('/llm', response_model=LlmSettings)
def get_llm_settings(uid: str = Depends(auth.get_current_user_uid)):
    return _settings(_selfhosted())


@router.post('/llm', response_model=LlmSettings)
def update_llm_settings(body: LlmSettingsUpdate, uid: str = Depends(auth.get_current_user_uid)):
    selfhosted = _selfhosted()
    model = (body.model or '').strip() or None
    if model is not None:
        try:
            offered = selfhosted.list_models()
        except (httpx.HTTPError, ValueError):
            offered = None  # server unreachable: trust the caller rather than block the change
        if offered is not None and model not in offered:
            raise HTTPException(status_code=400, detail=f'Model not offered by the LLM server: {model}')
    selfhosted.save_model(model)
    logger.info('self-hosted LLM: model set to %s by %s', model or '(default)', uid)
    return _settings(selfhosted)


class VoiceSettings(BaseModel):
    mode: Literal['off', 'cloud']


@router.get('/voice', response_model=VoiceSettings)
def get_voice_settings(uid: str = Depends(auth.get_current_user_uid)):
    return VoiceSettings(mode=selfhosted_config.voice_mode())


@router.post('/voice', response_model=VoiceSettings)
def update_voice_settings(body: VoiceSettings, uid: str = Depends(auth.get_current_user_uid)):
    selfhosted_config.write('voice', {'mode': body.mode})
    logger.info('self-hosted voice: mode set to %s by %s', body.mode, uid)
    return VoiceSettings(mode=selfhosted_config.voice_mode())


class CaptureSettings(BaseModel):
    owner_only: bool


@router.get('/capture', response_model=CaptureSettings)
def get_capture_settings(uid: str = Depends(auth.get_current_user_uid)):
    from utils.conversations import owner_memory_gate

    return CaptureSettings(owner_only=owner_memory_gate.gate_enabled())


@router.post('/capture', response_model=CaptureSettings)
def update_capture_settings(body: CaptureSettings, uid: str = Depends(auth.get_current_user_uid)):
    from utils.conversations import owner_memory_gate

    selfhosted_config.write('capture', {'owner_only': body.owner_only})
    logger.info('self-hosted capture: owner_only set to %s by %s', body.owner_only, uid)
    return CaptureSettings(owner_only=owner_memory_gate.gate_enabled())
