"""入库内容守卫：去标识化与"非自动诊断/处方"约束。

本系统只记录院内人工形成的决策留痕。任何试图写入自动诊断、
自动处方标记，或含有直接身份标识的内容都会被拒绝。
"""

from __future__ import annotations

import re
from typing import Any

from .errors import bad_request

#: 所有面向调用方的响应统一附带的合规声明。
NOTICE = "本系统仅记录院内人工决策与确认状态，不提供自动诊断或处方建议。"

#: 出现即拒绝的自动诊断/处方标记（大小写不敏感）。
_AUTO_MARKERS = (
    "自动诊断",
    "自动处方",
    "自动生成诊断",
    "auto-diagnosis",
    "auto diagnosis",
    "auto-prescription",
    "auto prescription",
    "ai诊断",
    "ai处方",
)

#: 材料/意见内容中禁止出现的身份标识字段名。
_IDENTITY_KEYS = {
    "name",
    "patient_name",
    "real_name",
    "id_number",
    "id_card",
    "idcard",
    "passport",
    "phone",
    "mobile",
    "telephone",
    "address",
    "mrn",
    "birth_date",
    "birthday",
    "email",
}

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")


def assert_human_authored(text: str, field: str = "content") -> None:
    """拒绝带有自动诊断/处方标记的文本，保证留痕均为人工形成。"""

    lowered = text.lower()
    for marker in _AUTO_MARKERS:
        if marker in lowered:
            raise bad_request(
                "auto_content_rejected",
                f"字段 {field} 含有自动诊断/处方标记，本系统只记录人工形成的意见",
            )


def assert_deidentified(obj: Any, path: str = "content") -> None:
    """递归检查 JSON 结构，拒绝疑似直接身份标识。"""

    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in _IDENTITY_KEYS:
                raise bad_request(
                    "deidentification_violation",
                    f"字段 {path}.{key} 疑似直接身份标识，入院前须完成去标识化",
                )
            assert_deidentified(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            assert_deidentified(value, f"{path}[{index}]")
    elif isinstance(obj, str):
        if _PHONE_RE.search(obj) or _IDCARD_RE.search(obj):
            raise bad_request(
                "deidentification_violation",
                f"字段 {path} 含有疑似手机号或证件号，入院前须完成去标识化",
            )
