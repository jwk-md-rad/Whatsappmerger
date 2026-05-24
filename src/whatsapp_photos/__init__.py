from .ingest import IngestReport, ingest
from .search import MessageHit, SearchFilters, search_messages

# Back-compat aliases for the old public API.
PhotoHit = MessageHit
search_photos = search_messages

__all__ = [
    "ingest", "IngestReport",
    "search_messages", "search_photos",
    "SearchFilters", "MessageHit", "PhotoHit",
]
__version__ = "0.1.0"
