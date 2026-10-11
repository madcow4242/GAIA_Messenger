# ==============================================================================
# Project: GAIA Messenger
# File:    backend_adapter.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.0.0 / 10/11/2026
#
# Copyright (c) 2026 Kevin Cazabon
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT
# ==============================================================================


import uuid
import json
import base64
import logging
from typing import Dict, Any, Optional, List, Union
import httpx

logger = logging.getLogger(__name__)


class BackendAdapter:
    """Universal adapter supporting both GAIA Agent APIs and standard OpenAI-compatible local LLM servers (Lemonade, vLLM, Ollama, etc.)."""

    def __init__(self, config: Dict[str, Any]):
        self.backend_type = config.get("type", "gaia").lower()
        self.api_url = config.get("api_url", "http://127.0.0.1:4200").replace("localhost", "127.0.0.1").rstrip("/")
        
        # In GAIA mode this is the agent_id (e.g. 'kevins-pa'); in OpenAI/Lemonade mode this is the model name/alias
        self.target_id = config.get("agent_id") or config.get("model_id") or "kevins-pa"
        self.api_key = config.get("api_key", "lemonade")
        self.system_prompt = config.get("system_prompt", "You are a helpful personal assistant.")
        self.timeout = float(config.get("timeout_seconds", 300))
        
        # Cache for active GAIA session IDs
        self._gaia_session_cache: Dict[str, str] = {}

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ntfy-ai-bridge/1.0",
        }
        if self.backend_type == "gaia":
            headers["X-GAIA-UI"] = "1"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def query(
        self, 
        prompt: str, 
        agent_or_model: Optional[str] = None, 
        session_id: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        image_mime: str = "image/jpeg"
    ) -> str:
        """Main entry point. Routes queries to GAIA or standard OpenAI Chat Completions API."""
        target = agent_or_model or self.target_id

        if self.backend_type == "gaia":
            return await self._query_gaia(prompt, target_agent=target, session_id=session_id)
        else:
            return await self._query_openai(prompt, target_model=target, image_bytes=image_bytes, image_mime=image_mime)

    async def list_available_targets(self) -> List[str]:
        """Queries available agents (GAIA) or downloaded models (OpenAI/Lemonade)."""
        if self.backend_type == "gaia":
            url = f"{self.api_url}/api/agents"
        else:
            # Handles base URLs ending with or without /v1
            endpoint = "/models" if self.api_url.endswith(("/v1", "/api/v1")) else "/v1/models"
            url = f"{self.api_url}{endpoint}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    
                    # Extract list from OpenAI structure {"data": [{"id": "model_1"}, ...]}
                    if "data" in data and isinstance(data["data"], list):
                        return [item["id"] for item in data["data"] if "id" in item]
                    
                    # Extract list from GAIA structure {"agents": [...]}
                    raw_agents = data.get("agents", data) if isinstance(data, dict) else data
                    if isinstance(raw_agents, list):
                        return [str(item.get("id", item)) if isinstance(item, dict) else str(item) for item in raw_agents]

            except Exception as err:
                logger.warning(f"Failed to query available targets from {url}: {err}")
                
        return [self.target_id]

    # -------------------------------------------------------------------------
    # GAIA API Implementation
    # -------------------------------------------------------------------------
    async def _query_gaia(self, prompt: str, target_agent: str, session_id: Optional[str]) -> str:
        """Dispatches query to GAIA's POST /api/chat/send with automatic session recovery."""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            active_session_id = session_id or await self._get_or_create_gaia_session(target_agent, client)
            url = f"{self.api_url}/api/chat/send"

            payload = {
                "session_id": active_session_id,
                "message": prompt,
                "agent_type": target_agent,
                "stream": False
            }

            resp = await client.post(url, json=payload, headers=self._get_headers())

            # Handle session expiration or invalid session ID gracefully
            if resp.status_code in (404, 422) and "session" in resp.text.lower():
                logger.warning(f"Session '{active_session_id[:8]}' rejected by GAIA ({resp.status_code}). Refreshing session...")
                self._gaia_session_cache.pop(target_agent, None)
                active_session_id = await self._get_or_create_gaia_session(target_agent, client)
                payload["session_id"] = active_session_id
                resp = await client.post(url, json=payload, headers=self._get_headers())

            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                return self._extract_text_from_json(resp.json())
            return resp.text

    async def _get_or_create_gaia_session(self, agent_type: str, client: httpx.AsyncClient) -> str:
        """Creates a registered session on GAIA via POST /api/sessions."""
        if agent_type in self._gaia_session_cache:
            return self._gaia_session_cache[agent_type]

        url = f"{self.api_url}/api/sessions"
        headers = self._get_headers()

        payload = {
            "agent_type": agent_type,
            "title": f"ntfy Session [{agent_type}]"
        }

        try:
            logger.info(f"Creating new session on GAIA for agent_type='{agent_type}'...")
            resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
            
            if resp.status_code in (200, 201):
                data = resp.json()
                # SessionResponse contains 'id'
                sid = data.get("id") or data.get("session_id")
                if sid:
                    logger.info(f"Successfully initialized GAIA session '{sid}' for agent '{agent_type}'.")
                    self._gaia_session_cache[agent_type] = sid
                    return sid
            
            logger.error(f"GAIA /api/sessions error HTTP {resp.status_code}: {resp.text}")
        except Exception as err:
            logger.error(f"Failed connecting to GAIA /api/sessions: {err}")

        raise RuntimeError(f"Could not create active session on GAIA for agent_type '{agent_type}'.")


    # -------------------------------------------------------------------------
    # Standard OpenAI / Lemonade API Implementation
    # -------------------------------------------------------------------------
    async def _query_openai(
        self, 
        prompt: str, 
        target_model: str, 
        image_bytes: Optional[bytes] = None, 
        image_mime: str = "image/jpeg"
    ) -> str:
        endpoint = "/chat/completions" if self.api_url.endswith(("/v1", "/api/v1")) else "/v1/chat/completions"
        url = f"{self.api_url}{endpoint}"

        # Construct message content (supporting text + optional vision payload)
        if image_bytes:
            b64_img = base64.b64encode(image_bytes).decode("utf-8")
            user_content: Union[str, List[Dict[str, Any]]] = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{b64_img}"}}
            ]
        else:
            user_content = prompt

        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            logger.info(f"Posting OpenAI request to model '{target_model}' at {url}...")
            resp = await client.post(url, json=payload, headers=self._get_headers())
            resp.raise_for_status()
            
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                choice = data["choices"][0]
                if "message" in choice and "content" in choice["message"]:
                    return str(choice["message"]["content"])
            
            return self._extract_text_from_json(data)

    @staticmethod
    def _extract_text_from_json(result: Any) -> str:
        if isinstance(result, dict):
            for key in ("response", "content", "output", "result"):
                if key in result:
                    return str(result[key])
            if "message" in result:
                msg = result["message"]
                return str(msg.get("content", msg)) if isinstance(msg, dict) else str(msg)
        return str(result)