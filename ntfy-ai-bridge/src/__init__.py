# Optional: Package metadata & top-level convenience exports
__version__ = "1.0.0"

from .crypto import SecurityEngine
from .backend_adapter import BackendAdapter
from .ntfy_client import NtfyClient

__all__ = ["SecurityEngine", "BackendAdapter", "NtfyClient"]