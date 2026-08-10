"""向量召回与索引底座的最小公共入口。"""

from .exceptions import (
    VectorRetrievalBackendError,
    VectorRetrievalConfigurationError,
    VectorRetrievalEncodingError,
    VectorRetrievalError,
    VectorStorageConfigurationError,
    VectorStorageError,
)
from .facade import VectorStorageFacade
from .factory import compose_vector_storage_facade, create_vector_storage_facade
from .models import VectorSearchHit, VectorSearchResult

__all__ = [
    "VectorRetrievalBackendError",
    "VectorRetrievalConfigurationError",
    "VectorRetrievalEncodingError",
    "VectorRetrievalError",
    "VectorSearchHit",
    "VectorSearchResult",
    "VectorStorageConfigurationError",
    "VectorStorageError",
    "VectorStorageFacade",
    "compose_vector_storage_facade",
    "create_vector_storage_facade",
]
