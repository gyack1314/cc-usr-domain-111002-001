"""虚拟化设备台账的行覆盖采集模块。"""
from .core import CollectError, Collector, Config, collect

__all__ = ["CollectError", "Collector", "Config", "collect"]
