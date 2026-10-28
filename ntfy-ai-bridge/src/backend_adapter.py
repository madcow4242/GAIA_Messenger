# ==============================================================================
# Project: GAIA Messenger
# File:    backend_adapter.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.2.0 / 10/21/2026
#
# Copyright (c) 2026 Kevin Cazabon
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT
# ==============================================================================

import asyncio
import re
import uuid
import json
import base64
import logging
from typing import Dict, Any, Optional, List, Union, Tuple
import httpx

# Graceful import of VectorMemory
try:
    from vector_memory import VectorMemory
    VECTOR_MEMORY_AVAILABLE = True
except ImportError:
    VECTOR_MEMORY_AVAILABLE = False

logger = logging.getLogger(__name__)

#---------------------------------------------------------------------------
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
        
        # Vector Memory configuration
        self.enable_memory = config.get("enable_vector_memory", True)
        self.vector_store_dir = config.get("vector_store_dir", "~/.gaia/vector_store")
        self._vm: Optional[Any] = None

        if self.enable_memory and VECTOR_MEMORY_AVAILABLE:
            try:
                self._vm = VectorMemory.get_instance(self.vector_store_dir)
                logger.info(f"VectorMemory initialized for BackendAdapter at '{self.vector_store_dir}'.")
            except Exception as e:
                logger.warning(f"Could not initialize VectorMemory: {e}")

        # Maps (client_session_id, agent_type) -> gaia_internal_session_id
        self._gaia_session_cache: Dict[Tuple[str, str], str] = {}

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ntfy-ai-bridge/1.2",
        }
        if self.backend_type == "gaia":
            headers["X-GAIA-UI"] = "1"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _extract_agent_from_prompt(self, prompt: str) -> Tuple[Optional[str], str]:
        """Detects and strips leading '@agent_name' tag if present (e.g., '@coder write code' -> ('coder', 'write code'))."""
        match = re.match(r"^\s*@([a-zA-Z0-9_\-]+)\s*(.*)$", prompt, re.DOTALL)
        if match:
            return match.group(1).lower(), match.group(2).strip()
        return None, prompt

    def _get_semantic_context(self, prompt: str) -> str:
        """Retrieves semantic vector memories and matching emails from ChromaDB only.
        Core user context, axioms, and graph entities are handled natively by the agent.
        """
        if not self._vm or not self.enable_memory:
            return ""

        clean = prompt.strip().lower()
        if len(clean) < 8 or clean in {"hi", "hello", "status", "ping", "test", "help", "clear"}:
            return ""

        try:
            results = self._vm.search_all(prompt, n_results=3)
            snippets = []

            if results.get("memories"):
                for m in results["memories"]:
                    snippets.append(f"• {m}")

            if results.get("emails"):
                for e in results["emails"][:2]:
                    clean_email = e.replace("\n\n", " ").strip()
                    snippets.append(f"• [Email] {clean_email[:250]}...")

            if snippets:
                return "[SEMANTIC MEMORY RECALL]\n" + "\n".join(snippets)
        except Exception as e:
            logger.warning(f"Semantic memory pre-fetch failed: {e}")

        return ""

    async def query(
        self, 
        prompt: str, 
        agent_or_model: Optional[str] = None, 
        session_id: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        image_mime: str = "image/jpeg"
    ) -> str:
        """Main entry point. Routes queries to GAIA or standard OpenAI Chat Completions API."""
        logger.info(f"[DIAG] query() prompt='{prompt[:40]}' | agent_or_model='{agent_or_model}' | session_id='{session_id}'")

        extracted_agent, cleaned_prompt = self._extract_agent_from_prompt(prompt)
        
        # Priority order: explicitly passed agent -> extracted @mention -> default fallback
        target = (agent_or_model or extracted_agent or self.target_id).strip()
        final_prompt = cleaned_prompt if cleaned_prompt else prompt

        # Retrieve relevant long-term memory & email history before dispatching
        memory_context = await asyncio.to_thread(self._get_semantic_context, final_prompt)

        logger.info(f"[{self.backend_type.upper()}] Routing query to target='{target}', session_id='{session_id}' (Memory context: {'Found' if memory_context else 'None'})")

        if self.backend_type == "gaia":
            return await self._query_gaia(
                final_prompt, 
                target_agent=target, 
                client_session_id=session_id,
                memory_context=memory_context
            )
        else:
            return await self._query_openai(
                final_prompt, 
                target_model=target, 
                image_bytes=image_bytes, 
                image_mime=image_mime,
                memory_context=memory_context
            )

    async def list_available_targets(self) -> List[str]:
        """Queries available agents (GAIA) or downloaded models (OpenAI/Lemonade)."""
        if self.backend_type == "gaia":
            url = f"{self.api_url}/api/agents"
        else:
            endpoint = "/models" if self.api_url.endswith(("/v1", "/api/v1")) else "/v1/models"
            url = f"{self.api_url}{endpoint}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    
                    if "data" in data and isinstance(data["data"], list):
                        return [item["id"] for item in data["data"] if "id" in item]
                    
                    raw_agents = data.get("agents", data) if isinstance(data, dict) else data
                    if isinstance(raw_agents, list):
                        return [str(item.get("id", item)) if isinstance(item, dict) else str(item) for item in raw_agents]

            except Exception as err:
                logger.warning(f"Failed to query available targets from {url}: {err}")
                
        return [self.target_id]

    # -------------------------------------------------------------------------
    # GAIA API Implementation
    # -------------------------------------------------------------------------
    async def _query_gaia(
        self, 
        prompt: str, 
        target_agent: str, 
        client_session_id: Optional[str],
        memory_context: str = ""
    ) -> str:
        """Dispatches query to GAIA's POST /api/chat/send with session mapping and error recovery."""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            client_sid = client_session_id or f"default_{target_agent}"
            active_gaia_sid = await self._resolve_gaia_session(client_sid, target_agent, client)

            # For GAIA, prepend context lightly so the agent has direct background without needing a tool call
            payload_message = prompt
            if memory_context:
                payload_message = f"{memory_context}\n\n[USER DIRECTIVE]\n{prompt}"
            
            url = f"{self.api_url}/api/chat/send"
            payload = {
                "session_id": active_gaia_sid,
                "message": payload_message,
                "agent_type": target_agent,
                "agent_id": target_agent,
                "stream": False
            }

            resp = await client.post(url, json=payload, headers=self._get_headers())

            if resp.status_code in (404, 422) and "session" in resp.text.lower():
                logger.warning(f"Session '{active_gaia_sid[:8]}' rejected by GAIA ({resp.status_code}). Refreshing session...")
                self._gaia_session_cache.pop((client_sid, target_agent), None)
                active_gaia_sid = await self._create_gaia_session(target_agent, client_sid, client)
                payload["session_id"] = active_gaia_sid
                resp = await client.post(url, json=payload, headers=self._get_headers())

            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                return self._extract_text_from_json(resp.json())
            return resp.text

    def _parse_client_sid(self, client_sid: str) -> tuple[str, str, str]:
        """Parses 'sender_id::agent_id::thread_id' into individual components."""
        parts = client_sid.split("::")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            return parts[0], self.target_id, parts[1]
        return client_sid, self.target_id, "main"

    async def _resolve_gaia_session(self, client_sid: str, agent_type: str, client: httpx.AsyncClient) -> str:
        """Resolves or maps a client compound key to an existing or new GAIA server session ID."""
        cache_key = (client_sid, agent_type)
        if cache_key in self._gaia_session_cache:
            return self._gaia_session_cache[cache_key]

        sender_id, _, thread_id = self._parse_client_sid(client_sid)
        title_marker = f"ntfy [{thread_id}] {sender_id}"

        # Check GAIA's existing sessions to reconnect after bridge reboots
        try:
            sessions_url = f"{self.api_url}/api/sessions"
            resp = await client.get(sessions_url, headers=self._get_headers(), timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("sessions") or data.get("data", [])
                for session in items:
                    if title_marker in session.get("title", ""):
                        existing_sid = session.get("id") or session.get("session_id")
                        if existing_sid:
                            logger.info(f"Reconnected to existing GAIA session '{existing_sid}' for '{title_marker}'.")
                            self._gaia_session_cache[cache_key] = existing_sid
                            return existing_sid
        except Exception as err:
            logger.warning(f"Could not query GAIA existing sessions: {err}")

        return await self._create_gaia_session(agent_type, client_sid, client)

    async def _create_gaia_session(self, agent_type: str, client_sid: str, client: httpx.AsyncClient) -> str:
        """Creates a registered session on GAIA via POST /api/sessions with a clear title slot."""
        url = f"{self.api_url}/api/sessions"
        headers = self._get_headers()

        sender_id, _, thread_id = self._parse_client_sid(client_sid)
        session_title = f"ntfy [{thread_id}] {sender_id} (@{agent_type})"

        payload = {
            "agent_type": agent_type,
            "title": session_title
        }

        try:
            logger.info(f"Creating new session on GAIA: '{session_title}'...")
            resp = await client.post(url, json=payload, headers=headers, timeout=15.0)

            if resp.status_code in (200, 201):
                data = resp.json()
                sid = data.get("id") or data.get("session_id")
                if sid:
                    logger.info(f"Successfully initialized GAIA session '{sid}' for agent '{agent_type}'.")
                    self._gaia_session_cache[(client_sid, agent_type)] = sid
                    return sid

            logger.error(f"GAIA /api/sessions error HTTP {resp.status_code}: {resp.text}")
        except Exception as err:
            logger.error(f"Failed connecting to GAIA /api/sessions: {err}")

        return client_sid

    # -------------------------------------------------------------------------
    # Standard OpenAI / Lemonade API Implementation
    # -------------------------------------------------------------------------
    async def _query_openai(
        self, 
        prompt: str, 
        target_model: str, 
        image_bytes: Optional[bytes] = None, 
        image_mime: str = "image/jpeg",
        memory_context: str = ""
    ) -> str:
        endpoint = "/chat/completions" if self.api_url.endswith(("/v1", "/api/v1")) else "/v1/chat/completions"
        url = f"{self.api_url}{endpoint}"

        if image_bytes:
            b64_img = base64.b64encode(image_bytes).decode("utf-8")
            user_content: Union[str, List[Dict[str, Any]]] = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{b64_img}"}}
            ]
        else:
            user_content = prompt

        # Dynamically enrich the system prompt with retrieved memory for this turn
        active_system_prompt = self.system_prompt
        if memory_context:
            active_system_prompt += (
                f"\n\n[RELEVANT LONG-TERM MEMORY & EMAIL CONTEXT]\n"
                f"{memory_context}\n"
                f"Use this verified context naturally if relevant to the user's message."
            )

        messages = []
        if active_system_prompt:
            messages.append({"role": "system", "content": active_system_prompt})
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