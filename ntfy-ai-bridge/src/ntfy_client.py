# ==============================================================================
# Project: GAIA Messenger
# File:    ntfy_client.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.0.0 / 10/11/2026
#
# Copyright (c) 2026 Kevin Cazabon
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT
# ==============================================================================

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

httpx = None  # Lazy imported or standard dependency
try:
    import httpx
except ImportError:
    pass

from .crypto import SecurityEngine
from .backend_adapter import BackendAdapter

logger = logging.getLogger(__name__)


class NtfyClient:
    """Async engine for ntfy stream listening, payload verification, and outbound posting."""

    def __init__(self, config: Dict[str, Any], security: SecurityEngine, backend: BackendAdapter):
        self.config = config
        self.security = security
        self.backend = backend

        self.server_url = config["ntfy"]["server_url"].rstrip("/")
        self.topic = config["ntfy"]["topic"]
        self.auth_token = config["ntfy"].get("auth_token", "").strip()
        self.instance_id = config["bridge"]["instance_id"]
        self.display_name = config["bridge"].get("display_name", "Assistant")
        self.enforce_signatures = config["security"].get("enforce_signatures", True)

        self._running = False
        self._processed_msg_ids = set()
        self._last_event_id = "1m"

    async def start(self) -> None:
        """Starts the persistent inbound SSE listener loop."""
        self._running = True
        logger.info(f"Starting ntfy listener stream for topic '{self.topic}' at {self.server_url}...")

        retry_delay = 2
        max_delay = 30

        while self._running:
            stream_url = f"{self.server_url}/{self.topic}/json?since={self._last_event_id}"
            headers = {}
            if self.auth_token:
                headers["Authorization"] = (
                    self.auth_token
                    if self.auth_token.lower().startswith(("bearer ", "basic "))
                    else f"Bearer {self.auth_token}"
                )

            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", stream_url, headers=headers) as response:
                        response.raise_for_status()
                        retry_delay = 2
                        logger.info(f"Connected and listening on ntfy topic '{self.topic}'.")

                        async for line in response.aiter_lines():
                            if not self._running:
                                break
                            if not line.strip():
                                continue

                            try:
                                payload = json.loads(line)
                                if payload.get("event") == "message":
                                    await self._handle_inbound_frame(payload)
                            except json.JSONDecodeError:
                                continue
                            except Exception as ex:
                                logger.error(f"Error processing ntfy line frame: {ex}", exc_info=True)

            except Exception as err:
                if self._running:
                    logger.warning(f"ntfy connection lost ({err}). Reconnecting in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_delay)

    def stop(self) -> None:
        """Stops the inbound streaming loop."""
        self._running = False

    async def _handle_inbound_frame(self, frame: Dict[str, Any]) -> None:
        msg_id = frame.get("id")
        if msg_id:
            self._last_event_id = msg_id
            if msg_id in self._processed_msg_ids:
                return
            self._processed_msg_ids.add(msg_id)
            if len(self._processed_msg_ids) > 1000:
                self._processed_msg_ids.clear()

        title = frame.get("title", "").strip()
        raw_message = frame.get("message", "").strip()

        # Echo Suppression: Skip messages posted by this bridge instance
        if title.startswith(f"[{self.display_name}]") or title.startswith(f"[{self.instance_id}]"):
            return

        if not raw_message:
            return

        # 1. Decrypt E2EE Layer
        try:
            decrypted_text = self.security.decrypt_text(raw_message)
        except Exception:
            # Drop unencrypted or invalid messages
            return

        # 2. Extract Session & Prompt
        prompt_text = decrypted_text
        session_id = f"ntfy_{self.instance_id}"

        # Unpack JSON envelope even if enforce_signatures is False
        try:
            if decrypted_text.strip().startswith("{"):
                env = json.loads(decrypted_text)
                if isinstance(env, dict) and "payload" in env:
                    p = env["payload"]
                    if isinstance(p, dict):
                        prompt_text = p.get("text", prompt_text)
                        session_id = p.get("session_id", session_id)
        except Exception:
            pass

        # 3. Verify Signatures if enabled
        if self.enforce_signatures:
            is_valid, sender_or_err, envelope_payload = self.security.verify_and_extract(
                decrypted_text, expected_target=self.instance_id
            )
            if not is_valid:
                logger.debug(f"Dropped envelope: {sender_or_err}")
                return

            prompt_text = envelope_payload.get("text", "")
            session_id = envelope_payload.get("session_id", session_id)

        if not prompt_text.strip():
            return

        logger.info(f"Verified prompt received for '{self.instance_id}' (Session: {session_id}): '{prompt_text[:60]}...'")

        # 4. Query Backend & Send Reply
        try:
            response_text = await self.backend.query(prompt_text, session_id=session_id)
            await self.send_reply(response_text, session_id=session_id)
        except Exception as err:
            logger.error(f"Error processing query for prompt: {err}")
            await self.send_notification(
                title=f"[{self.display_name}] System Error",
                message=f"Failed to process request: {str(err)}",
                session_id=session_id
            )

    async def send_notification(
        self,
        title: str,
        message: str,
        priority: str = "default",
        session_id: Optional[str] = None,
        attachment_bytes: Optional[bytes] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Encrypts and dispatches a notification or attachment to the outbound ntfy topic."""
        post_url = f"{self.server_url}/{self.topic}"
        headers = {
            "Title": title,
            "Priority": priority,
        }

        if self.auth_token:
            headers["Authorization"] = (
                self.auth_token
                if self.auth_token.lower().startswith(("bearer ", "basic "))
                else f"Bearer {self.auth_token}"
            )

        if session_id:
            headers["Tags"] = f"sid:{session_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # Binary Attachment Path (PUT)
                if attachment_bytes:
                    enc_bytes = self.security.encrypt_bytes(attachment_bytes)
                    headers["Filename"] = f"enc_{filename or 'file.bin'}"

                    if message:
                        headers["Message"] = self.security.encrypt_text(message)

                    resp = await client.put(post_url, content=enc_bytes, headers=headers)
                    return resp.status_code in (200, 201, 202)

                # Standard Encrypted Text Notification (POST)
                enc_message = self.security.encrypt_text(message)
                resp = await client.post(post_url, content=enc_message.encode("utf-8"), headers=headers)
                return resp.status_code in (200, 201, 202)

            except Exception as err:
                logger.error(f"Outbound ntfy publish error: {err}")
                return False

    async def send_reply(self, message: str, session_id: Optional[str] = None) -> bool:
        """Sends an encrypted reply back to the user."""
        title = f"[{self.display_name}]"
        return await self.send_notification(title=title, message=message, session_id=session_id)