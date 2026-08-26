"""Wrangler Python Worker entry. Re-exports `Default` from `sleeper_manager.cloudflare.worker`."""

from sleeper_manager.cloudflare.worker import Default

__all__ = ["Default"]
