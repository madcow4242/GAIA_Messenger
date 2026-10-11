# GAIA Messenger

GAIA Messenger is a lightweight, stand-alone daemon that acts as a secure push-notification bridge between your local AI agents (or LLM server) and remote clients. 

It allows you to communicate with your local models from anywhere in the world using a seamless text-chat interface, complete with bi-directional support for images and attachments. Instead of opening firewall ports or managing complex reverse proxies, GAIA Messenger routes traffic through the [ntfy.sh](https://ntfy.sh) push notification service. This can be used via their free public tier, paid private channels, or hosted entirely for free on your own infrastructure.

Security is a core priority: all payloads are secured with **AES-256 End-to-End Encryption (E2EE)** and authenticated using **HMAC per-message signing**. This guarantees payload privacy and prevents bad actors from spoofing or injecting fake prompts into your AI server—even when operating over public notification channels.

## Features
* **Universal LLM Support:** Natively supports AMD's GAIA, Lemonade Server, or any OpenAI-API compatible backend.
* **E2E Encryption & Signing:** AES-256 encryption guarantees payload privacy, while HMAC signature verification blocks unauthorized or tampered messages.
* **Multi-modal:** Bi-directional support for text, images, and file attachments.
* **Proactive Agent Tooling:** Includes a drop-in tool (`gaia_notify.py`) allowing your AI agents to send outbound push alerts directly to your mobile devices or web UI.
* **Multiple Clients Included:** Comes with a zero-install HTML web client and source code for an iOS app.
* **Automated Service Management:** Included installation script sets up the virtual environment and registers the bridge as a system service.

## File Layout

```text
GAIA_Messenger/
├── Android/                     # Future Android client source
├── html/
│   └── GAIA_Messenger.html      # Stand-alone web client (Zero-install)
├── iPhone/
│   ├── ContentView.swift        # iOS UI and Logic
│   └── GAIA_MessengerApp.swift  # iOS App Entry Point
├── ntfy-ai-bridge/              # The backend Python daemon
│   ├── src/
│   │   ├── __init__.py
│   │   ├── backend_adapter.py   # Handles GAIA/OpenAI API formatting
│   │   ├── crypto.py            # AES-256 E2EE and HMAC signing logic
│   │   ├── main.py              # Daemon entry point
│   │   └── ntfy_client.py       # ntfy.sh pub/sub logic
│   ├── config.yaml              # Local active configuration (Git-ignored)
│   ├── config.yaml.example      # Template for system configuration
│   ├── install.sh               # Environment setup & systemd installer
│   └── requirements.txt         # Python dependencies
└── tools/                       # Standalone agent tools for re-use
    ├── __init__.py
    ├── gaia_notify.py           # Proactive push notification tool for AI agents
    └── README.md                # Tool integration guide
```

## Prerequisites
* **Linux system** with Python 3.10+ installed.
* A running **GAIA Desktop / Lemonade Server** instance, or any **OpenAI-API compatible backend** (e.g., vLLM, LM Studio, Ollama).
* A unique topic string for `ntfy.sh` (or your own self-hosted ntfy server).

---

## Installation & Setup

### Step 1: Clone the Repository
```bash
git clone https://github.com/madcow4242/GAIA_Messenger.git
cd GAIA_Messenger/ntfy-ai-bridge
```

### Step 2: Configure the Bridge (`config.yaml`)
Create your configuration file **before** running the installation script:

```bash
cp config.yaml.example config.yaml
nano config.yaml  # Or use your preferred editor
```

Edit the parameters in `config.yaml`:
* **Backend URL:** The address of your AI server (e.g., `http://localhost:4200/v1` for GAIA or Lemonade).
* **ntfy Topic:** Your unique push notification channel string.
* **Encryption Key & HMAC Secret:** Set secure secret keys for AES-256 encryption and HMAC per-message signing.  
  *(Note: You will enter these exact same keys into your HTML or iOS client settings!)*

### Step 3: Run the Installer
Once `config.yaml` is configured, run `install.sh`:

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer will:
1. Create the Python virtual environment (`venv`).
2. Install all required dependencies from `requirements.txt`.
3. Register and enable the daemon as a system service (`ntfy-ai-bridge.service`) so it runs automatically on boot and stays active in the background.

---

## Agent Tool Integration

To allow your local AI agent (GAIA, LangChain, etc.) to proactively dispatch notifications to your device via the bridge, import the included `notify` tool from the `tools/` directory:

```python
from tools.gaia_notify import notify

# Add `notify` to your agent's available toolset
agent = MyAgent(tools=[notify])
```

By default, the tool connects to `[http://127.0.0.1:8085/v1/publish](http://127.0.0.1:8085/v1/publish)`. You can override this endpoint by setting the `NTFY_BRIDGE_URL` environment variable.

---

## Client Usage

### Standalone HTML Web Client
You do not need to build the mobile app to start using GAIA Messenger. The included HTML page runs locally in any modern desktop, tablet, or mobile browser without installation or web server setup.

1. Navigate to the `html/` directory in this repository.
2. Open `GAIA_Messenger.html` directly in your browser.
3. Open the **Settings** menu (gear icon).
4. Enter the **ntfy topic**, **AES-256 encryption key**, and **HMAC signing key** matching your daemon's `config.yaml`.
5. Send a message! Prompts are encrypted and signed in your browser, sent over ntfy to your daemon, and responses are decrypted automatically as they arrive.

---

## License

This project is released under the [MIT License](LICENSE).

*(Note: Instructions for compiling the iOS client in Xcode are intentionally excluded from this document. Ensure you have appropriate Apple Developer provisioning profiles if compiling `iPhone/GAIA_MessengerApp.swift`).*