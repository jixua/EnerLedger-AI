"""兼容入口：始终启动当前项目自己的独立 FastAPI 应用。"""

from app.main import app

__all__ = ["app"]
