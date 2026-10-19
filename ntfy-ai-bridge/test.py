from src.crypto import SecurityEngine
import yaml

with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

engine = SecurityEngine(
    e2ee_passphrase=cfg["security"]["e2ee_passphrase"],
    hmac_secret=cfg["security"]["hmac_secret"]
)

# Test roundtrip
enc = engine.encrypt_text("test message")
dec = engine.decrypt_text(enc)
print("Roundtrip test:", "PASS" if dec == "test message" else "FAIL")