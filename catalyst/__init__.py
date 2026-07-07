"""Catalyst — multi-source news ingestion (Bluesky + RSS/Atom) into SQLite."""

from .models import Author, Metrics, Post

__all__ = ["Author", "Metrics", "Post"]
__version__ = "0.1.0"
