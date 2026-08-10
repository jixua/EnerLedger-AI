from app.rag.config import settings
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.minio_storage import MinioStorage
from app.rag.services.storage.oss_storage import OssStorage


class StorageFactory:
    """对象存储工厂。"""

    @staticmethod
    def get_storage() -> BaseObjectStorage:
        provider = settings.STORAGE_TYPE.lower()
        if provider == "minio":
            return MinioStorage()
        if provider == "oss":
            return OssStorage()
        raise ValueError(f"不支持的存储提供方: {provider}")
