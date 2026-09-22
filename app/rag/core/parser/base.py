from abc import ABC, abstractmethod
from pathlib import Path


class IFileParser(ABC):
    """文件解析器协议。

    协议入参为 ``Path | None`` 而非 ``bytes`` —— 配合 ``ParseSourceIO.download_to_path``
    的流式下载，避免源文件以 bytes 形态全量驻留内存。生产解析仅接受本地路径。
    """

    @abstractmethod
    def parse(self, source: Path | None) -> str:
        """读取本地文件路径并返回 Markdown 字符串。

        ``source`` 为 ``None`` 时解析器必须拒绝。
        """


class BaseParser(IFileParser):
    """解析器基类，集中存放跨 provider 复用的校验与元数据逻辑。"""

    metadata: dict

    def __init__(self):
        self.metadata = {}

    def validate_source(self, source: Path | None) -> bool:
        """校验本地源文件存在且非空。

        路径必须真实存在且文件非空，保留原 ``validate_stream`` 的"非空"业务语义。
        """
        if source is None:
            raise ValueError("文件解析需要本地源文件路径")
        path = Path(source)
        if not path.exists() or not path.is_file():
            raise ValueError("源文件不存在")
        if path.stat().st_size == 0:
            raise ValueError("文件流不可为空")
        return True

    def extract_metadata(self) -> dict:
        return self.metadata
