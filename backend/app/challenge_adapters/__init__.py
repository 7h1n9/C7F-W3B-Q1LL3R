"""Challenge-specific, metadata-only execution adapters."""

from app.challenge_adapters.asset_warranty import AssetWarrantyAdapter, adapter_for
from app.challenge_adapters.dasctf_arena import DasctfArenaAdapter

__all__ = ["AssetWarrantyAdapter", "DasctfArenaAdapter", "adapter_for"]
