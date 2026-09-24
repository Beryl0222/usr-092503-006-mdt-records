"""院内讨论模块之间交换的稳定字段。"""

from enum import StrEnum
import re


class Discipline(StrEnum):
    """参与多学科讨论的专业。"""

    SURGERY = "surgery"
    PATHOLOGY = "pathology"
    RADIOLOGY = "radiology"
    FERTILITY_COUNSELING = "fertility_counseling"


class EvidenceKind(StrEnum):
    """讨论记录引用的材料类别。"""

    PATHOLOGY_REPORT = "pathology_report"
    IMAGING_REPORT = "imaging_report"
    PREFERENCE_RECORD = "preference_record"
    RISK_REVIEW = "risk_review"


class ConfirmationState(StrEnum):
    """专业意见的确认状态。"""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    INVALIDATED = "invalidated"


def validate_case_key(value: str) -> str:
    """验证不含直接身份信息的病例键。"""

    if not re.fullmatch(r"CASE-[A-F0-9]{16}", value):
        raise ValueError("病例键格式不正确")
    return value
