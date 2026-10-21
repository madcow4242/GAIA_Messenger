# ==============================================================================
# Project: GAIA Messenger
# File:    ntfy_client.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.2.0 / 10/21/2026
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
from typing import Dict, Any, Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None

from .crypto import SecurityEngine
from .backend_adapter import BackendAdapter

logger = logging.getLogger(__name__)


class NtfyClient:
    """Async engine for ntfy stream listening, payload verification, and outbound posting

    using deterministic thread slots and routing envelope headers.
    """

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

    def _parse_routing(self, frame: Dict[str, Any], decrypted_text: str) -> Tuple[Dict[str, str], str]:
        """Extracts envelope routing metadata from HTTP headers, tags, or JSON envelopes,

        providing resilient fallbacks for legacy clients.
        """
        headers = frame.get("headers") or {}
        tags = frame.get("tags") or []

        def get_header(key: str, default: str = "") -> str:
            for k, v in headers.items():
                if k.lower() == key.lower():
                    return str(v).strip()
            return default

        # 1. Extract primary routing from headers
        routing = {
            "sender_type": get_header("X-Sender-Type", "client"),
            "sender_id": get_header("X-Sender-ID", "unknown_client"),
            "target_server": get_header("X-Target-Server", "*"),
            "target_client": get_header("X-Target-Client", "*"),
            "agent_id": get_header("X-Agent-ID", self.backend.target_id or "kevins-pa"),
            "thread_id": get_header("X-Thread-ID", "main"),
            "msg_type": get_header("X-Message-Type", "chat"),
        }

        # 2. Inspect tags for compatibility overrides
        for tag in tags:
            tag_str = str(tag).strip()
            if tag_str.startswith("tid:"):
                routing["thread_id"] = tag_str.split(":", 1)[1].strip()
            elif tag_str.startswith("sid:"):
                routing["thread_id"] = tag_str.split(":", 1)[1].strip()
            elif tag_str.startswith("agent:"):
                routing["agent_id"] = tag_str.split(":", 1)[1].strip()
            elif tag_str.startswith("client:"):
                routing["sender_id"] = tag_str.split(":", 1)[1].strip()
            elif tag_str.startswith("server:"):
                routing["target_server"] = tag_str.split(":", 1)[1].strip()

        clean_prompt = decrypted_text

        # 3. Inspect JSON body envelope (signed or unsigned)
        if clean_prompt.strip().startswith("{"):
            try:
                env = json.loads(clean_prompt)
                if isinstance(env, dict):
                    # Envelope with dedicated routing dictionary
                    if "routing" in env and isinstance(env["routing"], dict):
                        r = env["routing"]
                        for k in routing.keys():
                            if k in r and r[k]:
                                routing[k] = str(r[k]).strip()
                        clean_prompt = env.get("content") or env.get("text") or clean_prompt

                    # Standard payload dictionary
                    elif "payload" in env and isinstance(env["payload"], dict):
                        p = env["payload"]
                        clean_prompt = p.get("text", clean_prompt)
                        if p.get("agent_type") or p.get("agent_id"):
                            routing["agent_id"] = p.get("agent_type") or p.get("agent_id")
                        if p.get("session_id"):
                            routing["thread_id"] = p.get("session_id")
                        if p.get("sender_id"):
                            routing["sender_id"] = p.get("sender_id")

                    # Direct fields at top level
                    else:
                        if "text" in env:
                            clean_prompt = env["text"]
                        if "agent_id" in env:
                            routing["agent_id"] = env["agent_id"]
                        if "thread_id" in env:
                            routing["thread_id"] = env["thread_id"]
            except Exception:
                pass

        # 4. Handle inline agent mentions (e.g. "@coder write a script")
        if clean_prompt.startswith("@"):
            match = re.match(r"^@([a-zA-Z0-9_\-]+)\s*(.*)", clean_prompt, re.DOTALL)
            if match:
                routing["agent_id"] = match.group(1)
                clean_prompt = match.group(2).strip()

        return routing, clean_prompt

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

        logger.info(f"[Inbound Frame] id={msg_id} title='{title}' tags={frame.get('tags')}")

        # 1. Echo Suppression: Drop notifications emitted by server bridges
        if (
            title.startswith(f"[{self.display_name}]")
            or title.startswith(f"[{self.instance_id}]")
            or title.startswith("GAIA Assistant")
            or title.startswith("GAIA (@")
        ):
            return

        if not raw_message and not attachment:
            return

        # 2. Decrypt Payload
        decrypted_text = ""
        if raw_message:
            try:
                decrypted_text = self.security.decrypt_text(raw_message)

                if self.enforce_signatures:
                    is_valid, sender_or_err, envelope_payload = self.security.verify_and_extract(
                        decrypted_text, expected_target=self.instance_id
                    )
                    if not is_valid:
                        logger.warning(f"[REJECTED] Signature/Target verification failed: {sender_or_err}")
                        return
                    if isinstance(envelope_payload, dict):
                        decrypted_text = json.dumps(envelope_payload)
            except Exception as dec_err:
                logger.warning(f"Could not decrypt inbound text payload: {dec_err}")
                if not attachment:
                    return

        # 3. Extract Routing Envelope
        routing, prompt_text = self._parse_routing(frame, decrypted_text)

        # 4. Enforce Server Guardrails
        # A: Drop all server-originating messages (bridges do not chat with bridges)
        if routing["sender_type"] == "server":
            return

        # B: Drop messages intended for a different bridge instance
        target_srv = routing["target_server"]
        if target_srv != "*" and target_srv != self.instance_id:
            logger.warning(f"[REJECTED] Server mismatch: target is '{target_srv}', but I am '{self.instance_id}'")
            return

        sender_id = routing["sender_id"]
        agent_id = routing["agent_id"]
        thread_id = routing["thread_id"]

        # Deterministic Session Key maps 1:1 with GAIA internal session context
        compound_session_key = f"{sender_id}::{agent_id}::{thread_id}"

        # --- Checkpoint 1: Inbound parsed & verified ---
        await self.send_ack(
            ref_msg_id=msg_id,
            status="received",
            target_client=sender_id,
            thread_id=thread_id,
            agent_id=agent_id
        )

        # 5. Handle Inbound Attachment
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

        # 6. Compose Agent Prompt with Attachment Path
        if local_attachment_path:
            caption_str = f" User caption: '{prompt_text}'." if prompt_text else ""
            prompt_text = (
                f"The user sent an attached file saved locally at: '{local_attachment_path}'.{caption_str} "
                f"Please analyze this file/image and respond to the user."
            )

        # 7. Command Intercept
        if prompt_text.strip() == "!agents":
            logger.info(f"Intercepted !agents command from client '{sender_id}'.")
            targets = await self.backend.list_available_targets()
            agents_json = json.dumps(targets)
            await self.send_reply(
                message=f"[AGENTS_LIST]:{agents_json}",
                target_client=sender_id,
                agent_id="system",
                thread_id=thread_id,
            )
            return

        if not prompt_text.strip():
            return

        logger.info(
            f"Dispatching prompt to agent '{agent_id}' "
            f"[Client: {sender_id}, Thread: {thread_id}]: '{prompt_text[:60]}...'"
        )

        await self.send_ack(
            ref_msg_id=msg_id,
            status="processing",
            target_client=sender_id,
            thread_id=thread_id,
            agent_id=agent_id
        )

        # 8. Query GAIA Backend & Reply
        try:
            response_text = await self.backend.query(
                prompt_text,
                agent_or_model=agent_id,
                session_id=compound_session_key,
            )
            await self.send_reply(
                message=response_text,
                target_client=sender_id,
                agent_id=agent_id,
                thread_id=thread_id,
            )
        except Exception as err:
            logger.error(f"Error querying backend for prompt: {err}")
            await self.send_notification(
                title=f"[{self.display_name}] System Error",
                message=f"Failed to process request: {str(err)}",
                target_client=sender_id,
                agent_id="system",
                thread_id=thread_id,
                msg_type="status",
            )

    async def send_notification(
        self,
        title: str,
        message: str,
        priority: str = "default",
        thread_id: str = "alerts",
        target_client: str = "*",
        agent_id: str = "kevins-pa",
        msg_type: str = "notification",
        attachment_bytes: Optional[bytes] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Sends an encrypted notification envelope to ntfy matching client routing rules."""
        priority_map = {"min": "1", "low": "2", "default": "3", "high": "4", "urgent": "5"}
        prio_val = priority_map.get(str(priority).lower(), "3")

        # 1. Ensure title contains [GAIA] or [Assistant] so clients classify it as server-originated
        formatted_title = title if ("[" in title or "GAIA" in title or "Assistant" in title) else f"[GAIA] {title}"

        # 2. Package routing tags
        tag_list = [f"tid:{thread_id}", f"agent:{agent_id}", "robot"]
        tags_header = ",".join(tag_list)

        # 3. Encrypt payload if E2EE passphrase is configured
        outbound_message = message
        if self.config.get("security", {}).get("e2ee_passphrase") and outbound_message:
            outbound_message = self.security.encrypt_text(outbound_message)

        headers = {
            "Title": formatted_title,
            "Priority": prio_val,
            "Tags": tags_header,
            "X-Sender-Type": "server",
            "X-Sender-ID": self.instance_id,
            "X-Target-Client": target_client,
            "X-Thread-ID": thread_id,
            "X-Message-Type": msg_type,
        }

        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        cleaned_server = self.server_url.rstrip("/")
        publish_url = f"{cleaned_server}/{self.topic}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                if attachment_bytes:
                    headers["Filename"] = filename or "enc_file.bin"
                    if outbound_message:
                        headers["Message"] = outbound_message.replace("\r", " ").replace("\n", " ")
                    resp = await client.put(publish_url, content=attachment_bytes, headers=headers)
                else:
                    resp = await client.post(publish_url, content=outbound_message.encode("utf-8"), headers=headers)
                return resp.status_code in (200, 201)
            except Exception as err:
                logger.error(f"Failed to post notification to ntfy: {err}")
                return False

    async def send_reply(
        self,
        message: str,
        target_client: str = "*",
        agent_id: Optional[str] = None,
        thread_id: str = "main",
    ) -> bool:
        """Sends an encrypted reply back to the originating client and deterministic thread slot,

        auto-detecting and uploading any generated local files.
        """
        resolved_agent = agent_id or self.backend.target_id or "kevins-pa"
        title = f"GAIA (@{resolved_agent})"

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

        # Standard agent chat responses use priority "4" (High) to trigger mobile push wakeups
        return await self.send_notification(
            title=title,
            message=message,
            priority="4",
            thread_id=thread_id,
            target_client=target_client,
            agent_id=resolved_agent,
            msg_type="chat",
            attachment_bytes=outbound_bytes,
            filename=filename,
        )

    async def send_ack(
        self,
        ref_msg_id: str,
        status: str,  # "received" | "processing"
        target_client: str,
        thread_id: str,
        agent_id: str
    ) -> None:
        """Sends a silent, lightweight ACK packet to inform clients of pipeline state."""
        ack_payload = json.dumps({
            "routing": {
                "sender_type": "server",
                "sender_id": self.instance_id,
                "target_client": target_client,
                "thread_id": thread_id,
                "agent_id": agent_id,
                "msg_type": "ack"
            },
            "ack": {
                "ref_id": ref_msg_id,
                "status": status,
                "timestamp": time.time()
            }
        })

        if self.config.get("security", {}).get("e2ee_passphrase"):
            ack_payload = self.security.encrypt_text(ack_payload)

        headers = {
            "Priority": "1",  # Silent: no phone buzz/ring
            "X-Sender-Type": "server",
            "X-Sender-ID": self.instance_id,
            "X-Target-Client": target_client,
            "X-Thread-ID": thread_id,
            "X-Message-Type": "ack",
            "Tags": f"tid:{thread_id},ack:{status}"
        }

        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(f"{self.server_url}/{self.topic}", content=ack_payload.encode("utf-8"), headers=headers)
            except Exception as err:
                logger.debug(f"Failed to dispatch ACK '{status}': {err}")