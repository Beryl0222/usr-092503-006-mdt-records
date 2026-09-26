"""会议纪要与决定沿革的可读导出（Markdown）。

导出内容只呈现：材料版本、各专业意见的确认状态、利益冲突披露、
缺席例外补审状态、候选/签发/失效沿革与同意状态。
不包含、也不生成任何自动诊断结论或处方内容。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

DISCIPLINE_CN = {
    "surgery": "外科",
    "pathology": "病理",
    "radiology": "影像",
    "fertility_counseling": "生育咨询",
}
KIND_CN = {
    "pathology_report": "病理报告",
    "imaging_report": "影像报告",
    "preference_record": "生育意愿记录",
    "risk_review": "手术风险评估",
}
STATE_CN = {
    "draft": "草稿",
    "confirmed": "已确认",
    "invalidated": "已失效",
}
PLAN_STATE_CN = {
    "candidate": "候选",
    "invalidated": "已失效",
    "issued": "已签发",
}
ISSUED_STATE_CN = {
    "active": "有效",
    "consent_withdrawn": "同意已撤回",
    "superseded": "已被新材料取代",
}
CONSENT_STATE_CN = {
    "granted": "已同意",
    "withdrawn": "已撤回",
    "superseded": "因材料改版失效",
}
EXCEPTION_STATE_CN = {
    "pending": "待补审",
    "approved": "补审通过",
    "rejected": "补审驳回",
    "overdue": "逾期未补审",
}


def _ts(value: float | None) -> str:
    if value is None:
        return "—"
    return _dt.datetime.fromtimestamp(value, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _d(value: str, mapping: dict) -> str:
    return f"{mapping.get(value, value)}（{value}）"


def _refs(refs: list[dict]) -> str:
    return "、".join(
        f"{KIND_CN.get(r['kind'], r['kind'])} v{r['version']}" for r in refs
    ) or "—"


def render_markdown(data: dict[str, Any]) -> str:
    lines: list[str] = []
    case = data["case"]
    lines.append(f"# 多学科讨论会议纪要与决定沿革 — {case['case_key']}")
    lines.append("")
    lines.append(f"- 建档时间：{_ts(case['created_at'])}")
    lines.append(f"- 导出时间：{_ts(data['generated_at'])}")
    cur_ev = data.get("evidence_versions", {})
    if cur_ev:
        lines.append("- 当前材料版本：")
        for kind in sorted(cur_ev):
            lines.append(f"    - {KIND_CN.get(kind, kind)}：v{cur_ev[kind]}")
    lines.append("")
    lines.append("> 本文件仅为院内决策留痕，不构成自动诊断或处方；患者信息已去标识化。")
    lines.append("")

    for block in data["meetings"]:
        m = block["meeting"]
        title = f"## 会议 #{m['meeting_id']}" + ("（紧急会议）" if m["emergency"] else "")
        lines.append(title)
        lines.append("")
        lines.append(f"- 状态：{m['status']}；计划时间：{_ts(m['scheduled_at'])}；关闭时间：{_ts(m['closed_at'])}")
        required = "、".join(_d(d, DISCIPLINE_CN) for d in block["required"])
        lines.append(f"- 必需专业：{required}")
        r = block["readiness"]
        lines.append(
            "- 齐备性：{0}".format(
                "✅ 可形成候选方案" if r["ready"] else "❌ 未齐备"
            )
        )
        lines.append(
            f"    - 已确认：{'、'.join(_d(d, DISCIPLINE_CN) for d in r['confirmed']) or '无'}"
        )
        lines.append(
            f"    - 缺席例外覆盖：{'、'.join(_d(d, DISCIPLINE_CN) for d in r['excused']) or '无'}"
        )
        if r["uncovered"]:
            lines.append(
                f"    - 尚未覆盖：{'、'.join(_d(d, DISCIPLINE_CN) for d in r['uncovered'])}"
            )
        if r["undisclosed"]:
            lines.append(
                f"    - 利益冲突未披露：{'、'.join(_d(d, DISCIPLINE_CN) for d in r['undisclosed'])}"
            )
        if r["overdue_pending_exceptions"]:
            lines.append("    - 存在已逾补审期限仍未补审的缺席例外")
        if r["late_reviews"]:
            lines.append(
                "    - 存在超时补审（已标记 late，不计为齐备）："
                + "、".join(
                    f"例外#{x['exception_id']}/{DISCIPLINE_CN.get(x['discipline'], x['discipline'])}"
                    for x in r["late_reviews"]
                )
            )
        lines.append("")

        lines.append("### 专业意见与确认状态")
        if not block["opinions"]:
            lines.append("（无）")
        for o in block["opinions"]:
            author = o["author_id"]
            if o.get("on_behalf_of"):
                author += f"（受 {o['on_behalf_of']} 委托提交）"
            lines.append(
                f"- [{_d(o['state'], STATE_CN)}] {_d(o['discipline'], DISCIPLINE_CN)}"
                f" — {author}；引用证据：{_refs(o['evidence_refs'])}"
            )
            lines.append(f"    - 利益冲突披露：{'已披露' if o['coi_disclosed'] else '未披露'}")
            if o.get("invalidate_reason"):
                lines.append(f"    - 失效原因：{o['invalidate_reason']}（{_ts(o['invalidated_at'])}）")
            lines.append(f"    - 意见摘录：{o['body']}")
        lines.append("")

        lines.append("### 紧急缺席例外")
        if not block["exceptions"]:
            lines.append("（无）")
        for ex in block["exceptions"]:
            late_tag = "；⚠️ 超时补审" if ex["late"] else ""
            lines.append(
                f"- 例外#{ex['exception_id']} {_d(ex['discipline'], DISCIPLINE_CN)}"
                f" — {_d(ex['state'], EXCEPTION_STATE_CN)}{late_tag}"
            )
            lines.append(
                f"    - 登记：{_ts(ex['created_at'])}；补审期限：{_ts(ex['review_deadline'])}；"
                f"补审时间：{_ts(ex['reviewed_at'])}；补审人：{ex.get('reviewer_id') or '—'}"
            )
            lines.append(f"    - 缺席原因：{ex['reason']}")
        lines.append("")

        lines.append("### 候选方案沿革（含证据版本快照）")
        plans = block.get("candidate_plans", [])
        if not plans:
            lines.append("（尚未形成候选方案）")
        for p in plans:
            snap = p["snapshot"]
            ev = "、".join(
                f"{KIND_CN.get(k, k)} v{v}" for k, v in sorted(snap["evidence_versions"].items())
            )
            lines.append(
                f"- 候选#{p['candidate_id']} — {_d(p['status'], PLAN_STATE_CN)}"
                f"；形成于 {_ts(p['formed_at'])}（{p['formed_by']}）；证据版本：{ev or '无'}"
            )
            stale = []
            for ob in snap["opinions"]:
                for s in ob.get("stale_refs", []):
                    stale.append(f"{ob['discipline']}:{s}")
            if stale:
                lines.append(f"    - 已过时引用：{'、'.join(stale)}")
            if p.get("invalidate_reason"):
                lines.append(
                    f"    - 失效原因：{p['invalidate_reason']}（{_ts(p['invalidated_at'])}）"
                )
        issued = block.get("issued_plan")
        if issued:
            lines.append(
                f"- 签发 #{issued['plan_id']}（候选#{issued['candidate_id']}）"
                f" — 签发人：{issued['signer_id']}"
                + (f"（受 {issued['on_behalf_of']} 委托）" if issued.get("on_behalf_of") else "")
                + f"；时间：{_ts(issued['signed_at'])}；状态：{_d(issued['status'], ISSUED_STATE_CN)}"
            )
            consent = issued.get("consent")
            if consent:
                lines.append(
                    f"    - 患者同意：{_d(consent['status'], CONSENT_STATE_CN)}"
                    f"；对应生育意愿版本 v{consent['preference_version']}"
                    f"；同意时间 {_ts(consent['granted_at'])}"
                    f"；撤回时间 {_ts(consent['withdrawn_at'])}"
                    f"；失效时间 {_ts(consent['superseded_at'])}"
                )
        lines.append("")

    lines.append("## 决定沿革时间线")
    for ev in data.get("timeline", []):
        d = ev["data"]
        t = _ts(ev["at"])
        if ev["type"] == "meeting":
            lines.append(f"- {t} 会议#{d['meeting_id']} 建立（紧急={bool(d['emergency'])}）")
        elif ev["type"] == "evidence":
            flag = "（旧版停用）" if not d["active"] else ""
            lines.append(
                f"- {t} 材料：{KIND_CN.get(d['kind'], d['kind'])} v{d['version']} 上传{flag}"
            )
        elif ev["type"] == "opinion":
            lines.append(
                f"- {t} {DISCIPLINE_CN.get(d['discipline'], d['discipline'])}意见#{d['opinion_id']}"
                f" 状态→{STATE_CN.get(d['state'], d['state'])}"
                + (f"（{d['invalidate_reason']}）" if d.get("invalidate_reason") else "")
            )
        elif ev["type"] == "absence_exception":
            lines.append(
                f"- {t} 缺席例外#{d['exception_id']}（{DISCIPLINE_CN.get(d['discipline'], d['discipline'])}）"
                f" 状态→{EXCEPTION_STATE_CN.get(d['state'], d['state'])}"
                + ("（超时）" if d["late"] else "")
            )
        elif ev["type"] == "candidate_plan":
            lines.append(
                f"- {t} 候选方案#{d['candidate_id']} 形成，状态→{PLAN_STATE_CN.get(d['status'], d['status'])}"
                + (f"（{d['invalidate_reason']}）" if d.get("invalidate_reason") else "")
            )
        elif ev["type"] == "issue":
            lines.append(
                f"- {t} 方案签发#{d['plan_id']}，签发人 {d['signer_id']}，状态→{ISSUED_STATE_CN.get(d['plan_status'], d['plan_status'])}"
            )
        elif ev["type"] == "consent":
            lines.append(
                f"- {t} 同意#{d['consent_id']}（方案#{d['plan_id']}，意愿v{d['preference_version']}）"
                f" 状态→{CONSENT_STATE_CN.get(d['status'], d['status'])}"
            )
    lines.append("")
    lines.append("> 提示：本纪要不输出任何自动诊断或处方；诊疗结论以授权医生签发内容为准。")
    lines.append("")
    return "\n".join(lines)
