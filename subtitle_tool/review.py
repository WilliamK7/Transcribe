from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .common import ToolError

MT_LANGUAGES = {"auto", "Chinese", "English", "Japanese", "German", "Korean", "Russian", "French",
                "Portuguese", "Arabic", "Italian", "Spanish", "Hindi", "Indonesian", "Thai", "Turkish",
                "Ukrainian", "Vietnamese", "Czech", "Danish", "Filipino", "Finnish", "Icelandic", "Malay",
                "Norwegian", "Polish", "Swedish"}
MANUAL_REASON = "用户在终端确认"


def decision_origin(record) -> Literal["automatic", "manual"]:
    """The only compatibility reader for the old display-text marker."""
    if "decision_origin" not in record:
        return "manual" if record.get("reason") == MANUAL_REASON else "automatic"
    value = record["decision_origin"]
    if value not in ("automatic", "manual"):
        raise ToolError("人工决定来源字段无效。", "cache_mismatch")
    return value


@dataclass(frozen=True)
class ReviewDecision:
    decision: Literal["original", "translate"]
    source_lang: str = "auto"

    def __post_init__(self):
        if (self.decision not in ("original", "translate") or self.source_lang not in MT_LANGUAGES or
                self.decision == "original" and self.source_lang != "auto"):
            raise ToolError("复核决定或 MT 源语言无效。", "review")

    def values(self):
        return dict(decision=self.decision, source_lang=self.source_lang,
                    decision_origin="manual", reason=MANUAL_REASON)
