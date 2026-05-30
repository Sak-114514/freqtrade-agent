from __future__ import annotations

from enum import Enum


class PermissionLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class PermissionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolPermissionError(RuntimeError):
    """Raised when a tool request is not allowed by the permission policy."""


def is_high_risk_text(text: str) -> bool:
    lowered = text.lower()
    risky_terms = [
        "forceenter",
        "force enter",
        "forcebuy",
        "force buy",
        "强制买",
        "强买",
        "forceexit",
        "force exit",
        "forcesell",
        "force sell",
        "强制卖",
        "强平",
        "delete_trade",
        "delete trade",
        "cancel_order",
        "cancel open order",
        "关闭 dry_run",
        "关闭 dry-run",
        "disable dry_run",
        "disable dry-run",
        "live trading",
        "开启实盘",
        "切换实盘",
        "改成实盘",
        "实盘交易",
        "真实交易",
        "真实下单",
        "改配置",
        "修改配置",
        "config.json",
        "改策略",
        "修改策略",
        "shell",
        "docker",
        "stake_amount",
        "leverage",
        "杠杆",
        "api key",
        "api secret",
    ]
    return any(term in lowered for term in risky_terms)


def is_l1_control_text(text: str) -> str | None:
    lowered = text.lower()
    if any(term in lowered for term in ["pause", "暂停", "停止开仓", "stopentry", "stopbuy"]):
        return "ft_pause"
    if any(
        term in lowered
        for term in ["start", "启动机器人", "开始运行", "恢复运行"]
    ):
        return "ft_start"
    if any(term in lowered for term in ["stop", "停止机器人", "停掉机器人"]):
        return "ft_stop"
    if any(term in lowered for term in ["reload_config", "reload config", "重载配置"]):
        return "ft_reload_config"
    return None
