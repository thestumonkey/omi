import json
import os

import asyncio

from utils.other.jobs import start_job
import logging

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

# Firebase init removed — storage is S3/MinIO, FCM is stubbed, auth is Casdoor/OIDC.

logger.info('Starting job...')
asyncio.run(start_job())
