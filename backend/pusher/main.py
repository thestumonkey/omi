import asyncio
import json
import logging
import os

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI

from routers import pusher, metrics
from utils.http_client import close_all_clients
from utils.executors import drain_background_tasks, log_executor_health

# Firebase init removed — storage is S3/MinIO, FCM is stubbed, auth is Casdoor/OIDC.

app = FastAPI()
app.include_router(pusher.router)
app.include_router(metrics.router)

paths = ['_temp', '_samples', '_segments', '_speech_profiles']
for path in paths:
    if not os.path.exists(path):
        os.makedirs(path)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(log_executor_health())


@app.on_event("shutdown")
async def shutdown_event():
    await drain_background_tasks(timeout=10.0)
    await close_all_clients()


@app.get('/health')
async def health_check():
    return {"status": "healthy"}
