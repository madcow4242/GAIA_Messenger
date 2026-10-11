import os
import requests

# Try importing @tool from popular frameworks, or fall back to a dummy decorator
try:
    from langchain.tools import tool
except ImportError:
    try:
        from gaia.agent import tool  # Or your specific GAIA agent tool decorator
    except ImportError:
        def tool(func):
            return func

@tool
def notify(message: str, priority: str = "default", title: str = "GAIA Alert", session_id: str = "") -> str:
    """Dispatches a proactive push notification to the user's mobile device and Web UI via the local ntfy-ai-bridge daemon.

    Args:
        message: The plaintext notification body to deliver to the user.
        priority: Delivery urgency level ('min', 'low', 'default', 'high', 'urgent'). Defaults to 'default'.
        title: Notification header title shown on the lock screen and web UI. Defaults to 'GAIA Alert'.
        session_id: Optional session ID to attach the notification to a specific chat thread.

    Returns:
        Status message indicating whether the delivery succeeded.
    """
    bridge_url = os.getenv("NTFY_BRIDGE_URL", "http://127.0.0.1:8085/v1/publish")
    
    payload = {
        "title": title,
        "message": message,
        "priority": priority,
    }
    if session_id:
        payload["session_id"] = session_id

    try:
        response = requests.post(bridge_url, json=payload, timeout=10)
        if response.status_code == 200:
            return "Notification dispatched successfully via ntfy-ai-bridge."
        return f"Bridge returned HTTP {response.status_code}: {response.text}"
        
    except requests.exceptions.ConnectionError:
        return f"Error: Could not reach ntfy-ai-bridge at {bridge_url}. Verify `sudo systemctl status ntfy-ai-bridge` is active."
    except Exception as err:
        return f"Failed to dispatch notification via ntfy-ai-bridge: {str(err)}"