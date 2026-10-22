"""SmartEdu platform adapter; importing it performs no network access."""

from .resolver import SmartEduProvider, normalize_input

__all__ = ["SmartEduProvider", "normalize_input"]
