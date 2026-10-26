"""异常格式化：让日志带上消息，而不只是类型名。

存在理由 / Why
--------------
仓库里曾大量出现这种写法::

    log.warning("按日拉取赛程失败：%s", type(exc).__name__)

日志里只留下 ``AttributeError``，**没有消息**。

后果很具体：本次排查中，``/oddsprobe`` 报的是
``AttributeError: 'str' object has no attribute 'isoformat'``，
而这行日志只会显示 ``AttributeError``——看不出是哪个对象、哪个属性。
一个本该一眼定位的问题，因此多花了十几轮往返。

**只打类型名 = 把最有用的那半句话丢掉。**

本模块零依赖（不 import telegram、不 import 任何数据源），
以便 api_client / football_data / service 等底层模块都能安全使用。

用法::

    from errfmt import fmt_exc
    log.warning("拉取失败：%s", fmt_exc(exc))
"""
from __future__ import annotations

__all__ = ["fmt_exc"]


def fmt_exc(exc: BaseException, limit: int = 300) -> str:
    """``"TypeName: message"``，异常消息为空时退化为纯类型名。

    Args:
        exc: 任意异常
        limit: 截断长度。日志不是小说——超长消息（如整页 HTML 塞进
            HTTPError）会淹没上下文，截断保留前 300 字符足够定位。

    Returns:
        格式化后的字符串。``str(exc)`` 为空时只返回类型名，
        避免出现 ``"ValueError: "`` 这种悬空冒号。
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    if not msg:
        return name
    return f"{name}: {msg}"[:limit]
