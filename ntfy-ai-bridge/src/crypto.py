
# ==============================================================================
# Project: GAIA Messenger
# File:    crypto.py
# Author:  Kevin Cazabon (kevin@cazabon.com)
# Version: 1.0.0 / 10/11/2026
#
# Copyright (c) 2026 Kevin Cazabon
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT
# ==============================================================================

import os
import json
import time
import base64
import hmac
import hashlib
from typing import Dict, Any, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class SecurityEngine:
    """Handles AES-256-GCM End-to-End Encryption and HMAC Anti-Spoofing Signatures."""

    def __init__(self, e2ee_passphrase: str, hmac_secret: str):
        self.passphrase = e2ee_passphrase.strip()
        self.hmac_secret = hmac_secret.strip().encode('utf-8')

    # ---------------------------------------------------------
    # 1. AES-256-GCM Encryption (Text & Binary)
    # ---------------------------------------------------------
    
    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000
        )
        return kdf.derive(passphrase.encode('utf-8'))

    def encrypt_bytes(self, raw_data: bytes) -> bytes:
        """Encrypts binary data (e.g., image attachments) with dynamic salt and nonce."""
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = self._derive_key(self.passphrase, salt)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, raw_data, None)
        return salt + nonce + ciphertext

    def decrypt_bytes(self, enc_data: bytes) -> bytes:
        """Decrypts binary data, supporting both 28-byte dynamic and 12-byte static JS Web Crypto headers."""
        if not enc_data or len(enc_data) < 12:
            raise ValueError("Encrypted data payload too short")

        # Strategy 1: Dynamic 28-byte header (16-byte Salt + 12-byte Nonce + Ciphertext)
        if len(enc_data) >= 28:
            try:
                salt = enc_data[:16]
                nonce = enc_data[16:28]
                ciphertext = enc_data[28:]
                key = self._derive_key(self.passphrase, salt)
                return AESGCM(key).decrypt(nonce, ciphertext, None)
            except Exception:
                pass

        # Strategy 2: Static 12-byte header (legacy fallback)
        try:
            salt = b"kevins-pa-salt-v1"
            nonce = enc_data[:12]
            ciphertext = enc_data[12:]
            key = self._derive_key(self.passphrase, salt)
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except Exception as e:
            print(f"Tried to decrypt: {repr(enc_data)}")
            raise ValueError(f"Failed to decrypt binary payload: {e}")

    def encrypt_text(self, text: str) -> str:
        """Encrypts text into a Base64-encoded string."""
        raw_bytes = text.encode('utf-8')
        enc_bytes = self.encrypt_bytes(raw_bytes)
        return base64.b64encode(enc_bytes).decode('utf-8')

    def decrypt_text(self, b64_text: str) -> str:
        """Decrypts a Base64-encoded encrypted text string."""
        try:
            enc_bytes = base64.b64decode(b64_text)
            dec_bytes = self.decrypt_bytes(enc_bytes)
            return dec_bytes.decode('utf-8')
        except Exception as e:
            raise ValueError(f"Text decryption failed: {e}")


    # ---------------------------------------------------------
    # 2. HMAC Signatures & Identity Envelope (Anti-Spoofing)
    # ---------------------------------------------------------

    def sign_envelope(self, target_agent: str, sender_id: str, payload: Dict[str, Any]) -> str:
        """Packages a payload into a verifiable envelope and returns it as a JSON string."""
        nonce = os.urandom(8).hex()
        timestamp = int(time.time())
        
        envelope = {
            "target": target_agent,
            "sender": sender_id,
            "nonce": nonce,
            "ts": timestamp,
            "payload": payload
        }
        
        # Create signature over the deterministic components
        sig_base = f"{nonce}|{timestamp}|{json.dumps(payload, separators=(',', ':'), sort_keys=True)}"
        signature = hmac.new(self.hmac_secret, sig_base.encode('utf-8'), hashlib.sha256).hexdigest()
        
        envelope["sig"] = signature
        return json.dumps(envelope)

    def verify_and_extract(self, envelope_json: str, expected_target: str, max_age_seconds: int = 60) -> Tuple[bool, str, Dict[str, Any]]:
        """Verifies HMAC signature, timestamp, and target before extracting the payload.
        Returns: (is_valid, error_msg_or_sender, payload_dict)
        """
        try:
            env = json.loads(envelope_json)
            
            # Check target routing
            if env.get("target") != expected_target:
                return False, f"Mismatched target (got {env.get('target')})", {}
            
            # Prevent replay attacks
            msg_age = int(time.time()) - env.get("ts", 0)
            if msg_age > max_age_seconds or msg_age < -5:
                return False, f"Message expired or time-skewed (age: {msg_age}s)", {}

            # Verify Signature
            payload = env.get("payload", {})
            sig_base = f"{env.get('nonce')}|{env.get('ts')}|{json.dumps(payload, separators=(',', ':'), sort_keys=True)}"
            expected_sig = hmac.new(self.hmac_secret, sig_base.encode('utf-8'), hashlib.sha256).hexdigest()
            
            if not hmac.compare_digest(env.get("sig", ""), expected_sig):
                return False, "Invalid HMAC signature", {}
                
            return True, env.get("sender", "unknown"), payload
            
        except json.JSONDecodeError:
            return False, "Invalid JSON envelope format", {}
        except Exception as e:
            return False, f"Envelope verification error: {str(e)}", {}