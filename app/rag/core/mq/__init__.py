"""
MQ 消息中台模块

提供多厂商 MQ 抽象层，支持 Kafka / RabbitMQ 通过配置切换。
架构对标 SKILL.md 中的 QingLuoPay MQ Pattern，Python 化实现。

Pipeline: MessageModel → VendorAdapter.send() → Broker → VendorListener → BusinessReceiver
"""

from app.rag.core.mq.interfaces import (
    IMQSender,
    IMQReceiver,
    MQVendorType,
)
from app.rag.core.mq.message import AbstractMessage, MessagePayload
from app.rag.core.mq.exceptions import (
    MQException,
    MQConnectionError,
    MQSendError,
    MQConsumeError,
    MQConfigError,
    MQSerializationError,
)
from app.rag.core.mq.factory import MQFactory

__all__ = [
    "IMQSender",
    "IMQReceiver",
    "MQVendorType",
    "AbstractMessage",
    "MessagePayload",
    "MQException",
    "MQConnectionError",
    "MQSendError",
    "MQConsumeError",
    "MQConfigError",
    "MQSerializationError",
    "MQFactory",
]
