#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="ntfy-ai-bridge"

echo "🚀 Installing ntfy-ai-bridge in $PROJECT_DIR..."

# 1. Ensure version-matched python3-venv and pip are installed
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "🔍 Detected Python version: $PY_VERSION"

if ! python3 -m venv --help > /dev/null 2>&1 || ! python3 -c "import ensurepip" > /dev/null 2>&1; then
    echo "📦 Installing python${PY_VERSION}-venv dependency..."
    sudo apt-get update && sudo apt-get install -y "python${PY_VERSION}-venv" python3-venv python3-pip
fi

# 2. Clean stale/failed venv directory if present
if [ -d "$PROJECT_DIR/venv" ] && [ ! -f "$PROJECT_DIR/venv/bin/activate" ]; then
    echo "🧹 Removing incomplete virtual environment..."
    rm -rf "$PROJECT_DIR/venv"
fi

# 3. Create isolated virtual environment
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "🔨 Creating Python virtual environment..."
    python3 -m venv "$PROJECT_DIR/venv"
fi

# 4. Install dependencies
echo "📥 Installing dependencies from requirements.txt..."
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip > /dev/null
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# 5. Generate systemd service
echo "⚙️ Configuring systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Standalone ntfy-AI Bridge Daemon
After=network.target gaia-desktop.daemon.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/uvicorn src.main:app --host 127.0.0.1 --port 8085
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 6. Enable and start daemon
sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE_NAME}.service

echo ""
echo "✅ Installation complete!"
echo "📡 Service status: sudo systemctl status ${SERVICE_NAME}.service"
echo "🪵 View logs:      sudo journalctl -u ${SERVICE_NAME}.service -f"