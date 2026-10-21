# ==============================================================================
# Project: GAIA Messenger
# File:    main.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.0.0 / 10/11/2026
#
# Copyright (c) 2026 Kevin Cazabon
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT
# ==============================================================================

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import yaml
from fastapi import FastAPI, HTTPException, Body

from .crypto import SecurityEngine
from .backend_adapter import BackendAdapter
from .ntfy_client import NtfyClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ntfy-ai-bridge")

# Load configuration
config_path = Path(__file__).resolve().parent.parent / "config.yaml"
if not config_path.exists():
    raise FileNotFoundError(f"Configuration file not found at '{config_path}'")

with open(config_path, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# Initialize engines
crypto_engine = SecurityEngine(
    e2ee_passphrase=config["security"]["e2ee_passphrase"],
    hmac_secret=config["security"]["hmac_secret"],
)
backend_adapter = BackendAdapter(config["backend"])
ntfy_client = NtfyClient(config, crypto_engine, backend_adapter)

listener_task: asyncio.Task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global listener_task
    logger.info("Initializing ntfy-ai-bridge daemon...")
    listener_task = asyncio.create_task(ntfy_client.start())
    yield
    logger.info("Shutting down ntfy-ai-bridge daemon...")
    ntfy_client.stop()
    if listener_task:
        listener_task.cancel()


app = FastAPI(title="ntfy-ai-bridge", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "ok", "instance_id": config["bridge"]["instance_id"]}


@app.post("/v1/publish")
async def publish_notification(payload: dict = Body(...)):
    """Local endpoint used by GAIA's `notify` tool or cron scripts to send notifications."""
    title = payload.get("title", f"[{config['bridge'].get('display_name', 'Assistant')}]")
    message = payload.get("message") or payload.get("body", "")
    priority = payload.get("priority", "default")

    # Routing envelope fields
    thread_id = payload.get("thread_id") or payload.get("session_id") or "main"
    target_client = payload.get("target_client", "*")
    agent_id = payload.get("agent_id") or config.get("backend", {}).get("default_model", "kevins-pa")
    msg_type = payload.get("msg_type", "notification")
    attachment_path = payload.get("attachment_path")

    if not message and not attachment_path:
        raise HTTPException(status_code=400, detail="Message or attachment_path is required.")

    attachment_bytes = None
    filename = None
    if attachment_path:
        path_obj = Path(attachment_path).expanduser().resolve()
        if not path_obj.is_file():
            raise HTTPException(status_code=404, detail=f"Attachment file not found: {attachment_path}")
        attachment_bytes = path_obj.read_bytes()
        filename = path_obj.name

    success = await ntfy_client.send_notification(
        title=title,
        message=message,
        priority=priority,
        thread_id=thread_id,
        target_client=target_client,
        agent_id=agent_id,
        msg_type=msg_type,
        attachment_bytes=attachment_bytes,
        filename=filename,
    )

    if success:
        return {"status": "success", "detail": "Notification published successfully"}
    raise HTTPException(status_code=500, detail="Failed to publish notification to ntfy server")