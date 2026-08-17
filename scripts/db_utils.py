"""Shared utility functions for wikidata2 dataset scripts."""

import re

_WIKIDATA_URI_PREFIX = "http://www.wikidata.org/entity/"


def slugify(name: str) -> str:
    """Lowercase, replace non-alphanumeric runs with underscores, strip edges."""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return s.strip("_")


def strip_uri(value: object) -> object:
    """Strip Wikidata entity URI prefix, keeping only the Q/P number."""
    if isinstance(value, str) and value.startswith(_WIKIDATA_URI_PREFIX):
        return value[len(_WIKIDATA_URI_PREFIX) :]
    return value
