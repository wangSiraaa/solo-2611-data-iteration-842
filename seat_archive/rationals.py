"""精确有理数工具。

可见度的全部长度与端点都以 :class:`fractions.Fraction` 保存与比较，
档案里以紧凑字符串落盘（``"5/8"``、``"-13/4"``、``"3"``），不使用浮点。
"""

from __future__ import annotations

from fractions import Fraction


def parse_q(value) -> Fraction:
    """把档案中的有理数记载解析为 ``Fraction``。

    接受 ``Fraction``/``int`` 本身，以及 ``"p/q"``、``"p"``、``"-p/q"``
    形式的字符串；拒绝空值与非法记法。
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("空的有理数记载")
        return Fraction(text)
    raise TypeError(f"无法解释为有理数: {value!r}")


def fmt_q(value: Fraction) -> str:
    """格式化为档案/报告所用的紧凑字符串。"""
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def q(value) -> Fraction:
    """``parse_q`` 的短名，便于构造示例数据。"""
    return parse_q(value)
