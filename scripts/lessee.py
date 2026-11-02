"""Read the lessee (arrendatario) from config rows, accepting the legacy ``client`` keys.

Ballbox's glossary (ballbox-db ``company/glossary.md``, 2026-11-02): the lessee
(arrendatario) rents machines from Ballbox and pays the rent, e.g. Adidas. These
configs used to call it ``client``. The local config files are gitignored and also
ship in the ``MONTHLY_REPORT_CONFIG_TGZ_B64`` secret, so readers take the new key
first and fall back to the old one.
"""
from typing import Any, Mapping

LESSEE_KEY = "lessee"
LESSEE_LABEL_KEY = "lessee_label"
LEGACY_LESSEE_KEY = "client"
LEGACY_LESSEE_LABEL_KEY = "client_label"


def _get(row: Mapping[str, Any] | None, key: str, legacy_key: str) -> Any:
    if not row:
        return None
    value = row.get(key)
    if value is None:
        value = row.get(legacy_key)
    return value


def get_lessee(row: Mapping[str, Any] | None) -> Any:
    """``row["lessee"]``, else the legacy ``row["client"]``, else None."""
    return _get(row, LESSEE_KEY, LEGACY_LESSEE_KEY)


def get_lessee_label(row: Mapping[str, Any] | None) -> Any:
    """``row["lessee_label"]``, else the legacy ``row["client_label"]``, else None."""
    return _get(row, LESSEE_LABEL_KEY, LEGACY_LESSEE_LABEL_KEY)
