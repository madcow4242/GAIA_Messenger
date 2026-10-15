# ==============================================================================
# Project: GAIA Messenger
# File:    ntfy_client.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.1.0 / 10/13/2026
#
# Copyright (c) 2026 Kevin Cazabon
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT
# ==============================================================================

import asyncio
import json
import logging
import re
import time
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

    def _extract_file_path(self, text: str) -> Optional[Path]:
        """Scans agent response text for Markdown image links, raw paths, or filenames,
        and locates the file on disk.
        """
        raw_dir = self.config.get("bridge", {}).get("attachments_dir", "~/BusinessData")
        search_dir = Path(raw_dir).expanduser().resolve()

        # 1. Extract markdown links: ![alt](target) or [alt](target)
        candidates = re.findall(r"(?:!\[.*?\]|\[.*?\])\(([^)]+)\)", text)

        # 2. Extract raw file paths or filenames ending in common image/document extensions
        ext_pattern = r"(?:file://)?(/[^\s\)\"']+\.(?:png|jpg|jpeg|webp|gif|pdf))"
        candidates.extend(re.findall(ext_pattern, text, re.IGNORECASE))
        
        rel_pattern = r"\b([^\s\)\"']+\.(?:png|jpg|jpeg|webp|gif|pdf))\b"
        candidates.extend(re.findall(rel_pattern, text, re.IGNORECASE))

        # 3. Clean and verify candidate paths
        seen = set()
        for cand in candidates:
            cand_clean = cand.strip().strip("'\"").split()[0]
            if cand_clean.startswith("file://"):
                cand_clean = cand_clean[7:]

            if not cand_clean or cand_clean in seen:
                continue
            seen.add(cand_clean)

            p = Path(cand_clean)

            if p.is_file():
                return p
            if (search_dir / cand_clean).is_file():
                return search_dir / cand_clean
            if (search_dir / p.name).is_file():
                return search_dir / p.name

            tmp_candidate = Path("/tmp/gaia_attachments") / p.name
            if tmp_candidate.is_file():
                return tmp_candidate

        return None

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
        attachment = frame.get("attachment") or {}
        tags = frame.get("tags") or []

        # Echo suppression: Skip messages generated by this bridge instance
        if title.startswith(f"[{self.display_name}]") or title.startswith(f"[{self.instance_id}]") or title.startswith("GAIA"):
            return

        # Must have either text or an attachment
        if not raw_message and not attachment:
            return

        # 1. Base Variables
        prompt_text = ""
        session_id = f"ntfy_{self.instance_id}"
        target_agent = None

        # Fallback 1: Extract agent and session ID from ntfy tags
        for tag in tags:
            if tag.startswith("sid:"):
                session_id = tag.split(":", 1)[1].strip() # session_id = tag.replace("sid:", "")
            elif tag.startswith("agent:"):
                session_id = tag.split(":", 1)[1].strip() #target_agent = tag.replace("agent:", "")

        if raw_message:
            try:
                decrypted_text = self.security.decrypt_text(raw_message)
                prompt_text = decrypted_text

                # Fallback 2: Unpack JSON envelope (Unsigned)
                if decrypted_text.strip().startswith("{"):
                    env = json.loads(decrypted_text)
                    if isinstance(env, dict) and "payload" in env:
                        p = env["payload"]
                        if isinstance(p, dict):
                            prompt_text = p.get("text", prompt_text)
                            session_id = p.get("session_id", session_id)
                            # Extract agent routing here
                            target_agent = p.get("agent_type") or p.get("agent_id") or target_agent

                # Fallback 3: Unpack Payload Envelope (Signed)
                if self.enforce_signatures:
                    is_valid, sender_or_err, envelope_payload = self.security.verify_and_extract(
                        decrypted_text, expected_target=self.instance_id
                    )
                    if not is_valid:
                        logger.debug(f"Dropped envelope: {sender_or_err}")
                        return
                    prompt_text = envelope_payload.get("text", "")
                    session_id = envelope_payload.get("session_id", session_id)
                    target_agent = envelope_payload.get("agent_type") or envelope_payload.get("agent_id") or target_agent
            except Exception as dec_err:
                logger.warning(f"Could not decrypt text payload ({dec_err})")
                if not attachment:
                    return

        # Fallback extraction from tags or prompt
        if not target_agent:
            for tag in tags:
                if tag.startswith("agent:"):
                    target_agent = tag.split(":", 1)[1]
                    break
        if not target_agent and prompt_text.startswith("@"):
            match = re.match(r"^@([a-zA-Z0-9_\-]+)", prompt_text)
            if match:
                target_agent = match.group(1)

        # 2. Inbound Attachment Download & Decryption
        local_attachment_path = None
        attachment_url = attachment.get("url")
        attachment_name = attachment.get("name", "attachment.jpg")

        if attachment_url:
            try:
                auth_headers = {}
                if self.auth_token:
                    auth_headers["Authorization"] = (
                        self.auth_token
                        if self.auth_token.lower().startswith(("bearer ", "basic "))
                        else f"Bearer {self.auth_token}"
                    )

                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(attachment_url, headers=auth_headers)
                    if resp.status_code == 200:
                        raw_bytes = resp.content
                        try:
                            decrypted_bytes = self.security.decrypt_bytes(raw_bytes)
                            logger.info("[ntfy attachment] Inbound file decrypted successfully.")
                        except Exception as dec_err:
                            logger.warning(f"[ntfy attachment] Decrypt failed ({dec_err}); using raw bytes.")
                            decrypted_bytes = raw_bytes

                        raw_dir = self.config.get("bridge", {}).get("attachments_dir", "~/BusinessData")
                        save_dir = Path(raw_dir).expanduser().resolve()
                        save_dir.mkdir(parents=True, exist_ok=True)

                        clean_name = Path(attachment_name).name
                        if clean_name.startswith("enc_"):
                            clean_name = clean_name[4:]
                        if clean_name.endswith(".bin"):
                            clean_name = clean_name[:-4] + ".jpg"

                        local_attachment_path = save_dir / f"inbound_{int(time.time())}_{clean_name}"
                        local_attachment_path.write_bytes(decrypted_bytes)
                        
                        try:
                            local_attachment_path.chmod(0o666)
                        except Exception:
                            pass

                        logger.info(f"[ntfy attachment] Saved inbound attachment to {local_attachment_path}")

            except Exception as err:
                logger.error(f"Failed downloading inbound attachment: {err}")

        # 3. Build agent prompt incorporating attachment path
        if local_attachment_path:
            caption_str = f" User caption: '{prompt_text}'." if prompt_text else ""
            prompt_text = (
                f"The user sent an attached image saved locally at: '{local_attachment_path}'.{caption_str} "
                f"Please analyze this image using your `analyze_image` tool and respond to the user."
            )

        # 4. Command Intercept
        if prompt_text.strip() == "!agents":
            logger.info("Intercepted !agents command. Returning list of valid models/agents.")
            targets = await self.backend.list_available_targets()
            agents_json = json.dumps(targets)
            await self.send_reply(f"[AGENTS_LIST]:{agents_json}", session_id=session_id, agent_id="system")
            return

        if not prompt_text.strip():
            return

        logger.info(f"Verified prompt received for '{target_agent or 'default'}' (Session: {session_id[:8]}): '{prompt_text[:60]}...'")

        # 5. Query backend passing extracted agent_or_model
        try:
            response_text = await self.backend.query(prompt_text, agent_or_model=target_agent, session_id=session_id)
            await self.send_reply(response_text, session_id=session_id, agent_id=target_agent)
        except Exception as err:
            logger.error(f"Error processing query for prompt: {err}")
            await self.send_notification(
                title=f"[{self.display_name}] System Error",
                message=f"Failed to process request: {str(err)}",
                session_id=session_id,
            )

    async def send_notification(
        self,
        title: str,
        message: str,
        priority: str = "default",
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        attachment_bytes: Optional[bytes] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Encrypts and dispatches a notification or attachment to the outbound ntfy topic."""
        post_url = f"{self.server_url}/{self.topic}"
        
        # Build strict tags for iOS processing
        tags_list = []
        if session_id: tags_list.append(f"sid:{session_id}")
        if agent_id: tags_list.append(f"agent:{agent_id}")
        
        headers = {
            "Title": title,
            "Priority": priority,
        }
        if tags_list:
            headers["Tags"] = ",".join(tags_list)

        if self.auth_token:
            headers["Authorization"] = (
                self.auth_token
                if self.auth_token.lower().startswith(("bearer ", "basic "))
                else f"Bearer {self.auth_token}"
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # Binary Attachment Path (PUT)
                if attachment_bytes:
                    enc_bytes = self.security.encrypt_bytes(attachment_bytes)
                    headers["Filename"] = f"enc_{filename or 'file.bin'}"

                    if message:
                        enc_msg = self.security.encrypt_text(message)
                        headers["Message"] = enc_msg.replace("\r\n", " ").replace("\n", " ")

                    resp = await client.put(post_url, content=enc_bytes, headers=headers)
                    return resp.status_code in (200, 201, 202)

                # Standard Encrypted Text Notification (POST)
                enc_message = self.security.encrypt_text(message)
                resp = await client.post(post_url, content=enc_message.encode("utf-8"), headers=headers)
                return resp.status_code in (200, 201, 202)

            except Exception as err:
                logger.error(f"Outbound ntfy publish error: {err}")
                return False

    async def send_reply(self, message: str, session_id: Optional[str] = None, agent_id: Optional[str] = None) -> bool:
        """Sends an encrypted reply, auto-detecting and uploading generated images/files."""
        # Use a title that bypasses internal echo suppression but flags for clients
        resolved_agent = agent_id or self.backend.target_id
        title = f"GAIA Assistant (@{resolved_agent})"

        outbound_bytes = None
        filename = None

        candidate_path = self._extract_file_path(message)
        if candidate_path:
            try:
                outbound_bytes = candidate_path.read_bytes()
                filename = candidate_path.name
                logger.info(f"[ntfy outbound] Found generated file: {candidate_path} ({len(outbound_bytes)} bytes)")
            except Exception as err:
                logger.warning(f"[ntfy outbound] Failed reading file at {candidate_path}: {err}")

        # Force priority="4" (High) for standard GAIA agent replies so iOS wakes up
        return await self.send_notification(
            title=title,
            message=message,
            priority="4",
            session_id=session_id,
            agent_id=resolved_agent,
            attachment_bytes=outbound_bytes,
            filename=filename,
        )