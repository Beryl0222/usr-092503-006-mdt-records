"""多学科决策留痕的核心领域服务。

事务边界：每个公共方法以 ``store.tx()`` 开启立即写事务，在事务内
完成“读取当前状态 → 判定 → 条件写入”，因此并发请求只有一个能通过
条件更新，其余得到 409。

本模块只记录医生提交的专业意见与院内流程结论，**不产生、也不输出**
任何自动诊断或处方建议。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from .contracts import ConfirmationState, Discipline, EvidenceKind, validate_case_key
from .errors import ConflictError, NotFoundError, PayloadError, PermissionDeniedError
from .store import Store

ALL_DISCIPLINES = [d.value for d in Discipline]

# 材料类别 → 受其更新影响、必须重新确认的专业。
EVIDENCE_IMPACT: dict[str, list[str]] = {
    EvidenceKind.PATHOLOGY_REPORT: [Discipline.PATHOLOGY, Discipline.SURGERY],
    EvidenceKind.IMAGING_REPORT: [Discipline.RADIOLOGY, Discipline.SURGERY],
    EvidenceKind.PREFERENCE_RECORD: [
        Discipline.SURGERY,
        Discipline.FERTILITY_COUNSELING,
    ],
    EvidenceKind.RISK_REVIEW: [Discipline.SURGERY],
}

OPINION_REQUIRED_FIELDS = ("body",)


def _now() -> float:
    return time.time()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence_ref_key(ref: dict) -> tuple[str, int]:
    try:
        return str(ref["kind"]), int(ref["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadError("材料引用必须包含 kind 与整数 version") from exc


class MdtService:
    def __init__(self, store: Store, clock: Callable[[], float] = _now):
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------------ 用户

    def get_user(self, user_id: str) -> dict | None:
        return self.store.one("SELECT * FROM users WHERE user_id=?", (user_id,))

    def user_for_token(self, token: str) -> dict | None:
        return self.store.one(
            """SELECT u.* FROM tokens t JOIN users u ON u.user_id=t.user_id
               WHERE t.token=? AND u.active=1""",
            (token,),
        )

    def user_disciplines(self, user_id: str) -> list[str]:
        rows = self.store.all(
            "SELECT discipline FROM user_disciplines WHERE user_id=? ORDER BY discipline",
            (user_id,),
        )
        return [r["discipline"] for r in rows]

    # ------------------------------------------------------------------ 审计

    def audit(
        self,
        actor_id: str,
        action: str,
        *,
        resource: str | None = None,
        case_key: str | None = None,
        result: str = "allowed",
        sensitive: bool = False,
        detail: dict | None = None,
    ) -> None:
        with self.store.tx() as c:
            c.execute(
                """INSERT INTO audit_log
                   (ts, actor_id, action, resource, case_key, result, sensitive, detail_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self.clock(),
                    actor_id,
                    action,
                    resource,
                    case_key,
                    result,
                    1 if sensitive else 0,
                    json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    # ------------------------------------------------------------------ 病例

    def register_case(
        self, actor: dict, case_key: str, note: str | None = None
    ) -> dict:
        validate_case_key(case_key)
        with self.store.tx() as c:
            exists = c.execute("SELECT 1 FROM cases WHERE case_key=?", (case_key,)).fetchone()
            if exists:
                raise ConflictError("病例已存在", code="case_exists")
            c.execute(
                "INSERT INTO cases (case_key, created_at, created_by, note) VALUES (?,?,?,?)",
                (case_key, self.clock(), actor["user_id"], note),
            )
        return self.get_case(actor, case_key)

    def get_case(self, actor: dict, case_key: str) -> dict:
        case = self.store.one("SELECT * FROM cases WHERE case_key=?", (case_key,))
        if not case:
            raise NotFoundError("病例不存在")
        return case

    def list_cases(self, actor: dict) -> list[dict]:
        return self.store.all(
            "SELECT case_key, created_at, created_by, note FROM cases ORDER BY case_key"
        )

    def assign_specialist(
        self, actor: dict, case_key: str, user_id: str, discipline: str
    ) -> None:
        self.get_case(actor, case_key)
        if discipline not in ALL_DISCIPLINES:
            raise PayloadError("未知专业")
        target = self.get_user(user_id)
        if not target:
            raise NotFoundError("用户不存在")
        if discipline not in self.user_disciplines(user_id):
            raise PayloadError("该用户不具备对应专业资质")
        with self.store.tx() as c:
            c.execute(
                """INSERT INTO case_assignments (case_key, user_id, discipline)
                   VALUES (?,?,?)
                   ON CONFLICT(case_key, user_id) DO UPDATE SET discipline=excluded.discipline""",
                (case_key, user_id, discipline),
            )

    def link_patient(self, actor: dict, case_key: str, patient_ref: str) -> None:
        """登记去标识化患者引用与病例的关联（同意操作的前置条件）。"""
        self.get_case(actor, case_key)
        if not patient_ref or not patient_ref.strip():
            raise PayloadError("患者去标识化引用不能为空")
        with self.store.tx() as c:
            c.execute(
                """INSERT INTO patient_links (case_key, patient_ref, linked_at)
                   VALUES (?,?,?)
                   ON CONFLICT(case_key, patient_ref) DO NOTHING""",
                (case_key, patient_ref.strip(), self.clock()),
            )

    def patient_links(self, case_key: str) -> list[str]:
        return [
            r["patient_ref"]
            for r in self.store.all(
                "SELECT patient_ref FROM patient_links WHERE case_key=? ORDER BY patient_ref",
                (case_key,),
            )
        ]

    # -------------------------------------------------------------- 材料版本

    def add_evidence(
        self,
        actor: dict,
        case_key: str,
        kind: str,
        title: str,
        content: str | None,
        *,
        sensitive: bool = False,
    ) -> dict:
        self.get_case(actor, case_key)
        if kind not in [k.value for k in EvidenceKind]:
            raise PayloadError("未知材料类别")
        if not title or not title.strip():
            raise PayloadError("材料标题不能为空")
        digest = _sha256_text(content or "")
        with self.store.tx() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM evidence WHERE case_key=? AND kind=?",
                (case_key, kind),
            ).fetchone()
            version = row["v"] + 1
            cur = c.execute(
                """INSERT INTO evidence
                   (case_key, kind, version, title, sha256, content, sensitive, active, uploaded_at, uploaded_by)
                   VALUES (?,?,?,?,?,?,?,1,?,?)""",
                (
                    case_key,
                    kind,
                    version,
                    title.strip(),
                    digest,
                    content,
                    1 if sensitive else 0,
                    self.clock(),
                    actor["user_id"],
                ),
            )
            evidence_id = cur.lastrowid
            # 同类别旧版本停用
            c.execute(
                "UPDATE evidence SET active=0 WHERE case_key=? AND kind=? AND version<>?",
                (case_key, kind, version),
            )
            self._invalidate_for_evidence(c, case_key, kind, evidence_id, version)
        return self.get_evidence(actor, case_key, kind, version)

    def _invalidate_for_evidence(
        self, c, case_key: str, kind: str, evidence_id: int, version: int
    ) -> None:
        """关键材料改版：作废受影响专业的确认意见、候选方案并要求重新确认。"""
        affected = EVIDENCE_IMPACT.get(kind, [])
        reason = f"evidence_updated:{kind}@v{version}"
        # 该病例尚未关闭的会议中，受影响专业的 confirmed 意见作废
        c.execute(
            """UPDATE opinions SET state='invalidated', invalidated_at=?,
                   invalidate_reason=?, new_evidence_id=?
               WHERE state='confirmed' AND discipline IN (%s)
                 AND meeting_id IN (SELECT meeting_id FROM meetings WHERE case_key=? AND status<>'closed')"""
            % ",".join("?" for _ in affected),
            (self.clock(), reason, evidence_id, *affected, case_key),
        )
        # 待补审的缺席例外失去依托：标记需要重新补审（保持 pending 并顺延需人工处理，
        # 这里直接作废弃，要求重新登记），已批准的同样作废。
        c.execute(
            """UPDATE absence_exceptions SET state='rejected', reviewed_at=?,
                   reviewer_id='system', late=0,
                   review_note=?
               WHERE state IN ('pending','approved')
                 AND discipline IN (%s)
                 AND meeting_id IN (SELECT meeting_id FROM meetings WHERE case_key=? AND status<>'closed')"""
            % ",".join("?" for _ in affected),
            (self.clock(), f"材料改版，缺席例外作废：{reason}", *affected, case_key),
        )
        # 候选方案（含已签发）标记失效；任何关键材料改版后旧签名都不得沿用，
        # 已签发方案与对应同意一并标记 superseded。
        c.execute(
            """UPDATE candidate_plans SET status='invalidated', invalidated_at=?,
                   invalidate_reason=?, new_evidence_id=?
               WHERE case_key=? AND status IN ('candidate','issued')""",
            (self.clock(), reason, evidence_id, case_key),
        )
        c.execute(
            """UPDATE issued_plans SET status='superseded'
               WHERE case_key=? AND status='active'
                 AND candidate_id IN (
                     SELECT candidate_id FROM candidate_plans
                     WHERE case_key=? AND new_evidence_id=?)""",
            (case_key, case_key, evidence_id),
        )
        c.execute(
            """UPDATE consents SET status='superseded', superseded_at=?
               WHERE status='granted' AND plan_id IN (
                   SELECT plan_id FROM issued_plans
                   WHERE case_key=? AND status='superseded'
                     AND candidate_id IN (
                         SELECT candidate_id FROM candidate_plans WHERE new_evidence_id=?))""",
            (self.clock(), case_key, evidence_id),
        )

    def get_evidence(
        self, actor: dict, case_key: str, kind: str, version: int
    ) -> dict:
        row = self.store.one(
            "SELECT * FROM evidence WHERE case_key=? AND kind=? AND version=?",
            (case_key, kind, version),
        )
        if not row:
            raise NotFoundError("材料版本不存在")
        return row

    def list_evidence(
        self, actor: dict, case_key: str, *, include_content: bool
    ) -> list[dict]:
        cols = (
            "evidence_id, case_key, kind, version, title, sha256, content, sensitive, active, uploaded_at, uploaded_by"
            if include_content
            else "evidence_id, case_key, kind, version, title, sha256, sensitive, active, uploaded_at, uploaded_by"
        )
        rows = self.store.all(
            f"SELECT {cols} FROM evidence WHERE case_key=? ORDER BY kind, version",
            (case_key,),
        )
        return rows

    def current_versions(self, case_key: str) -> dict[str, int]:
        rows = self.store.all(
            "SELECT kind, MAX(version) AS v FROM evidence WHERE case_key=? GROUP BY kind",
            (case_key,),
        )
        return {r["kind"]: r["v"] for r in rows}

    # ------------------------------------------------------------------ 会议

    def schedule_meeting(
        self,
        actor: dict,
        case_key: str,
        required: list[str],
        *,
        emergency: bool = False,
    ) -> dict:
        self.get_case(actor, case_key)
        if not required:
            raise PayloadError("至少指定一个必需专业")
        bad = [d for d in required if d not in ALL_DISCIPLINES]
        if bad:
            raise PayloadError(f"未知专业：{bad}")
        required = sorted(set(required))
        with self.store.tx() as c:
            cur = c.execute(
                """INSERT INTO meetings (case_key, emergency, status, scheduled_at, created_by)
                   VALUES (?,?, 'scheduled', ?, ?)""",
                (case_key, 1 if emergency else 0, self.clock(), actor["user_id"]),
            )
            meeting_id = cur.lastrowid
            c.executemany(
                "INSERT INTO meeting_required (meeting_id, discipline) VALUES (?,?)",
                [(meeting_id, d) for d in required],
            )
        return self.get_meeting(meeting_id)

    def get_meeting(self, meeting_id: int) -> dict:
        m = self.store.one("SELECT * FROM meetings WHERE meeting_id=?", (meeting_id,))
        if not m:
            raise NotFoundError("会议不存在")
        m["required"] = [
            r["discipline"]
            for r in self.store.all(
                "SELECT discipline FROM meeting_required WHERE meeting_id=? ORDER BY discipline",
                (meeting_id,),
            )
        ]
        return m

    def list_meetings(self, case_key: str) -> list[dict]:
        return self.store.all(
            "SELECT * FROM meetings WHERE case_key=? ORDER BY meeting_id", (case_key,)
        )

    def required_disciplines(self, meeting_id: int) -> list[str]:
        return [
            r["discipline"]
            for r in self.store.all(
                "SELECT discipline FROM meeting_required WHERE meeting_id=? ORDER BY discipline",
                (meeting_id,),
            )
        ]

    # ------------------------------------------------------------- 授权委托

    def grant_delegation(
        self,
        actor: dict,
        grantee_id: str,
        scope: str,
        *,
        discipline: str | None = None,
        case_key: str | None = None,
        ttl_seconds: float = 86400,
    ) -> int:
        if scope not in ("opinion", "sign"):
            raise PayloadError("委托范围必须是 opinion 或 sign")
        if discipline is not None and discipline not in ALL_DISCIPLINES:
            raise PayloadError("未知专业")
        grantee = self.get_user(grantee_id)
        if not grantee:
            raise NotFoundError("被委托人不存在")
        if scope == "opinion" and discipline not in self.user_disciplines(grantee_id):
            raise PayloadError("被委托人不具备该专业资质，不能承接意见提交")
        if scope == "sign" and not grantee["authorized_signer"]:
            raise PayloadError("被委托人不是授权签发医生")
        now = self.clock()
        with self.store.tx() as c:
            cur = c.execute(
                """INSERT INTO delegations
                   (granter_id, grantee_id, scope, discipline, case_key, granted_at, expires_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    actor["user_id"],
                    grantee_id,
                    scope,
                    discipline,
                    case_key,
                    now,
                    now + ttl_seconds,
                ),
            )
            return cur.lastrowid

    def revoke_delegation(self, actor: dict, delegation_id: int) -> None:
        with self.store.tx() as c:
            row = c.execute(
                "SELECT * FROM delegations WHERE delegation_id=?", (delegation_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("委托不存在")
            if row["granter_id"] != actor["user_id"]:
                raise PermissionDeniedError("只有委托人本人可以撤销委托")
            if row["revoked_at"] is not None:
                raise ConflictError("委托已撤销")
            c.execute(
                "UPDATE delegations SET revoked_at=? WHERE delegation_id=?",
                (self.clock(), delegation_id),
            )

    def _find_delegation(
        self,
        c,
        *,
        granter_id: str,
        grantee_id: str,
        scope: str,
        discipline: str | None,
        case_key: str,
    ) -> dict | None:
        now = self.clock()
        rows = c.execute(
            """SELECT * FROM delegations
               WHERE granter_id=? AND grantee_id=? AND scope=?
                 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)
               ORDER BY delegation_id DESC""",
            (granter_id, grantee_id, scope, now),
        ).fetchall()
        for d in rows:
            if d["discipline"] is not None and d["discipline"] != discipline:
                continue
            if d["case_key"] is not None and d["case_key"] != case_key:
                continue
            return d
        return None

    # ------------------------------------------------------------------ 意见

    def submit_opinion(
        self,
        actor: dict,
        meeting_id: int,
        discipline: str,
        body: str,
        evidence_refs: list[dict],
        *,
        coi_disclosed: bool,
        coi_detail: str | None = None,
        on_behalf_of: str | None = None,
    ) -> dict:
        if not body or not body.strip():
            raise PayloadError("意见正文不能为空")
        if discipline not in ALL_DISCIPLINES:
            raise PayloadError("未知专业")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise PayloadError("意见必须引用至少一个材料版本")
        refs = [_evidence_ref_key(r) for r in evidence_refs]
        meeting = self.get_meeting(meeting_id)
        if meeting["status"] == "closed":
            raise ConflictError("会议已关闭，不能再提交意见")
        case_key = meeting["case_key"]
        actor_disciplines = self.user_disciplines(actor["user_id"])
        delegation_id = None
        if on_behalf_of:
            if on_behalf_of == actor["user_id"]:
                raise PayloadError("on_behalf_of 不能是本人")
            granter = self.get_user(on_behalf_of)
            if not granter:
                raise NotFoundError("被代表医生不存在")
            if discipline not in self.user_disciplines(on_behalf_of):
                raise PayloadError("被代表医生不具备该专业资质")
        else:
            if discipline not in actor_disciplines:
                raise PermissionDeniedError("不能提交其他专业的意见")

        with self.store.tx() as c:
            if on_behalf_of:
                d = self._find_delegation(
                    c,
                    granter_id=on_behalf_of,
                    grantee_id=actor["user_id"],
                    scope="opinion",
                    discipline=discipline,
                    case_key=case_key,
                )
                if not d:
                    raise PermissionDeniedError("缺少有效的意见提交委托")
                delegation_id = d["delegation_id"]

            # 必需专业校验
            req = [
                r["discipline"]
                for r in c.execute(
                    "SELECT discipline FROM meeting_required WHERE meeting_id=?",
                    (meeting_id,),
                )
            ]
            if discipline not in req:
                raise PayloadError("该专业不在本次会议的必需专业内")

            # 引用必须存在；且不得引用已被新版本取代的材料
            for kind, version in refs:
                ev = c.execute(
                    "SELECT * FROM evidence WHERE case_key=? AND kind=? AND version=?",
                    (case_key, kind, version),
                ).fetchone()
                if not ev:
                    raise PayloadError(f"引用的材料不存在：{kind}@v{version}")
                if not ev["active"]:
                    raise ConflictError(
                        f"材料 {kind}@v{version} 已被新版本取代，请引用当前版本后重新提交"
                    )

            # 重复提交：同一会议+专业只允许一条有效（confirmed/draft）意见
            dup = c.execute(
                """SELECT opinion_id FROM opinions
                   WHERE meeting_id=? AND discipline=? AND state<>'invalidated'""",
                (meeting_id, discipline),
            ).fetchone()
            if dup:
                raise ConflictError(
                    "该专业已有有效意见，重复提交被拒绝", code="duplicate_opinion"
                )

            cur = c.execute(
                """INSERT INTO opinions
                   (meeting_id, discipline, author_id, on_behalf_of, delegation_id, body,
                    evidence_refs, coi_disclosed, coi_detail, state, created_at, confirmed_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'confirmed', ?, ?)""",
                (
                    meeting_id,
                    discipline,
                    actor["user_id"],
                    on_behalf_of,
                    delegation_id,
                    body.strip(),
                    json.dumps(
                        [{"kind": k, "version": v} for k, v in refs],
                        ensure_ascii=False,
                    ),
                    1 if coi_disclosed else 0,
                    coi_detail,
                    self.clock(),
                    self.clock(),
                ),
            )
            opinion_id = cur.lastrowid
        return self.get_opinion(opinion_id)

    def get_opinion(self, opinion_id: int) -> dict:
        row = self.store.one("SELECT * FROM opinions WHERE opinion_id=?", (opinion_id,))
        if not row:
            raise NotFoundError("意见不存在")
        row["evidence_refs"] = json.loads(row["evidence_refs"])
        return row

    def list_opinions(self, meeting_id: int) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM opinions WHERE meeting_id=? ORDER BY opinion_id",
            (meeting_id,),
        )
        for r in rows:
            r["evidence_refs"] = json.loads(r["evidence_refs"])
        return rows

    # ------------------------------------------------------------- 缺席例外

    def grant_absence_exception(
        self,
        actor: dict,
        meeting_id: int,
        discipline: str,
        reason: str,
        *,
        review_window_seconds: float = 172800,
    ) -> dict:
        meeting = self.get_meeting(meeting_id)
        if not meeting["emergency"]:
            raise PayloadError("缺席例外只适用于紧急会议")
        if discipline not in meeting["required"]:
            raise PayloadError("该专业不在必需专业内")
        if not reason or not reason.strip():
            raise PayloadError("缺席原因不能为空")
        now = self.clock()
        with self.store.tx() as c:
            dup = c.execute(
                "SELECT 1 FROM absence_exceptions WHERE meeting_id=? AND discipline=? AND state IN ('pending','approved')",
                (meeting_id, discipline),
            ).fetchone()
            if dup:
                raise ConflictError("该专业已存在未结案的缺席例外")
            cur = c.execute(
                """INSERT INTO absence_exceptions
                   (meeting_id, discipline, reason, granted_by, created_at, review_deadline, state)
                   VALUES (?,?,?,?,?,?,'pending')""",
                (
                    meeting_id,
                    discipline,
                    reason.strip(),
                    actor["user_id"],
                    now,
                    now + review_window_seconds,
                ),
            )
            eid = cur.lastrowid
        return self.get_exception(eid)

    def review_absence_exception(
        self,
        actor: dict,
        exception_id: int,
        approve: bool,
        note: str | None = None,
        *,
        _override_now: float | None = None,
    ) -> dict:
        now = _override_now or self.clock()
        with self.store.tx() as c:
            ex = c.execute(
                "SELECT * FROM absence_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if not ex:
                raise NotFoundError("缺席例外不存在")
            if ex["state"] != "pending":
                raise ConflictError("该缺席例外已结案")
            late = now > ex["review_deadline"]
            if late:
                # 超时补审允许留痕，但必须显式标记 late，且不能使方案再视为齐备
                state = "approved" if approve else "rejected"
                c.execute(
                    """UPDATE absence_exceptions SET state=?, reviewed_at=?, reviewer_id=?,
                           late=1, review_note=? WHERE exception_id=?""",
                    (state, now, actor["user_id"], note, exception_id),
                )
            else:
                state = "approved" if approve else "rejected"
                c.execute(
                    """UPDATE absence_exceptions SET state=?, reviewed_at=?, reviewer_id=?,
                           late=0, review_note=? WHERE exception_id=?""",
                    (state, now, actor["user_id"], note, exception_id),
                )
        return self.get_exception(exception_id)

    def get_exception(self, exception_id: int) -> dict:
        row = self.store.one(
            "SELECT * FROM absence_exceptions WHERE exception_id=?", (exception_id,)
        )
        if not row:
            raise NotFoundError("缺席例外不存在")
        return row

    def list_exceptions(self, meeting_id: int) -> list[dict]:
        return self.store.all(
            "SELECT * FROM absence_exceptions WHERE meeting_id=? ORDER BY exception_id",
            (meeting_id,),
        )

    def _expire_pending_exceptions(self, c, meeting_id: int, now: float) -> None:
        c.execute(
            """UPDATE absence_exceptions SET state='overdue'
               WHERE meeting_id=? AND state='pending' AND review_deadline<=?""",
            (meeting_id, now),
        )

    # ------------------------------------------------------------- 齐备性判定

    def _readiness(self, c, meeting: dict, now: float) -> dict:
        """返回会议当前的齐备性快照（事务内调用）。"""
        meeting_id = meeting["meeting_id"]
        required = [
            r["discipline"]
            for r in c.execute(
                "SELECT discipline FROM meeting_required WHERE meeting_id=? ORDER BY discipline",
                (meeting_id,),
            )
        ]
        opinions = c.execute(
            "SELECT * FROM opinions WHERE meeting_id=? AND state='confirmed'",
            (meeting_id,),
        ).fetchall()
        confirmed_disciplines = {o["discipline"] for o in opinions}
        missing = [d for d in required if d not in confirmed_disciplines]

        exceptions = c.execute(
            "SELECT * FROM absence_exceptions WHERE meeting_id=?", (meeting_id,)
        ).fetchall()
        valid_excused: set[str] = set()
        overdue_pending = False
        late_reviews: list[dict] = []
        for ex in exceptions:
            if ex["state"] == "pending" and ex["review_deadline"] <= now:
                overdue_pending = True
            if ex["state"] == "approved" and not ex["late"]:
                valid_excused.add(ex["discipline"])
            if ex["late"]:
                late_reviews.append(
                    {"exception_id": ex["exception_id"], "discipline": ex["discipline"]}
                )

        # 仍缺意见且没有“按时批准”的例外覆盖（超时补审不授予覆盖资格；
        # 但若该专业事后亲自提交了已确认意见，则不再视为缺失，超时事实仍留痕）
        uncovered = [d for d in missing if d not in valid_excused]

        # 利益冲突：每条有效意见都必须已披露
        coi_ok = all(o["coi_disclosed"] == 1 for o in opinions)
        undisclosed = [
            o["discipline"] for o in opinions if o["coi_disclosed"] != 1
        ]

        ready = (
            not uncovered
            and not undisclosed
            and not overdue_pending
            and len(required) > 0
        )
        return {
            "required": required,
            "confirmed": sorted(confirmed_disciplines),
            "missing": missing,
            "excused": sorted(valid_excused),
            "uncovered": uncovered,
            "coi_ok": coi_ok,
            "undisclosed": undisclosed,
            "overdue_pending_exceptions": overdue_pending,
            "late_reviews": late_reviews,
            "ready": ready,
        }

    def meeting_status_view(self, meeting_id: int) -> dict:
        meeting = self.get_meeting(meeting_id)
        with self.store.tx() as c:
            now = self.clock()
            self._expire_pending_exceptions(c, meeting_id, now)
            readiness = self._readiness(c, meeting, now)
            plan = c.execute(
                "SELECT * FROM candidate_plans WHERE meeting_id=? ORDER BY candidate_id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
            issued = c.execute(
                "SELECT * FROM issued_plans WHERE meeting_id=? ORDER BY plan_id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
        result = dict(meeting)
        result["readiness"] = readiness
        result["opinions"] = self.list_opinions(meeting_id)
        result["exceptions"] = self.list_exceptions(meeting_id)
        result["candidate_plan"] = self._plan_brief(plan) if plan else None
        result["issued_plan"] = self._issued_brief(issued) if issued else None
        result["evidence_versions"] = self.current_versions(meeting["case_key"])
        return result

    def _plan_brief(self, plan: dict | None) -> dict | None:
        if not plan:
            return None
        out = {k: plan[k] for k in (
            "candidate_id", "status", "formed_at", "formed_by",
            "invalidated_at", "invalidate_reason",
        )}
        out["snapshot"] = json.loads(plan["snapshot"])
        return out

    def _issued_brief(self, issued: dict | None) -> dict | None:
        if not issued:
            return None
        consent = self.store.one(
            "SELECT * FROM consents WHERE plan_id=? ORDER BY consent_id DESC LIMIT 1",
            (issued["plan_id"],),
        )
        return {
            "plan_id": issued["plan_id"],
            "candidate_id": issued["candidate_id"],
            "signer_id": issued["signer_id"],
            "on_behalf_of": issued["on_behalf_of"],
            "signed_at": issued["signed_at"],
            "status": issued["status"],
            "consent": self._consent_brief(consent) if consent else None,
        }

    def _consent_brief(self, c_row: dict) -> dict:
        return {
            "consent_id": c_row["consent_id"],
            "status": c_row["status"],
            "preference_version": c_row["preference_version"],
            "patient_ref": c_row["patient_ref"],
            "granted_at": c_row["granted_at"],
            "withdrawn_at": c_row["withdrawn_at"],
            "superseded_at": c_row["superseded_at"],
        }

    # ------------------------------------------------------------- 候选方案

    def form_candidate_plan(self, actor: dict, meeting_id: int) -> dict:
        meeting = self.get_meeting(meeting_id)
        if meeting["status"] == "closed":
            raise ConflictError("会议已关闭")
        with self.store.tx() as c:
            now = self.clock()
            self._expire_pending_exceptions(c, meeting_id, now)
            readiness = self._readiness(c, meeting, now)
            if not readiness["ready"]:
                raise ConflictError(
                    "必需专业未齐备或利益冲突/补审未完成，不能形成候选方案",
                    code="not_ready",
                )
            latest = c.execute(
                "SELECT * FROM candidate_plans WHERE meeting_id=? ORDER BY candidate_id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
            if latest and latest["status"] == "candidate":
                raise ConflictError("候选方案已存在", code="plan_exists")
            if latest and latest["status"] == "issued":
                raise ConflictError("方案已签发", code="plan_issued")
            # 上一版候选为 invalidated 时：受影响专业已按 readiness 要求重新确认，
            # 允许在同一会议形成新一版候选，沿革中保留历次记录。
            snapshot = self._build_snapshot(c, meeting, readiness)
            cur = c.execute(
                """INSERT INTO candidate_plans
                   (meeting_id, case_key, status, snapshot, formed_at, formed_by)
                   VALUES (?,?, 'candidate', ?, ?, ?)""",
                (
                    meeting_id,
                    meeting["case_key"],
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    now,
                    actor["user_id"],
                ),
            )
            candidate_id = cur.lastrowid
        return self.get_candidate_plan(candidate_id)

    def _build_snapshot(self, c, meeting: dict, readiness: dict) -> dict:
        """固化形成结论时的证据版本与确认状态。"""
        opinions = c.execute(
            "SELECT * FROM opinions WHERE meeting_id=? AND state='confirmed' ORDER BY discipline",
            (meeting["meeting_id"],),
        ).fetchall()
        current = {
            r["kind"]: r["v"]
            for r in c.execute(
                "SELECT kind, MAX(version) AS v FROM evidence WHERE case_key=? GROUP BY kind",
                (meeting["case_key"],),
            )
        }
        opinion_blocks = []
        for o in opinions:
            refs = json.loads(o["evidence_refs"])
            stale = [
                f"{r['kind']}@v{r['version']}"
                for r in refs
                if current.get(r["kind"]) != r["version"]
            ]
            opinion_blocks.append(
                {
                    "discipline": o["discipline"],
                    "author_id": o["author_id"],
                    "on_behalf_of": o["on_behalf_of"],
                    "state": o["state"],
                    "coi_disclosed": bool(o["coi_disclosed"]),
                    "evidence_refs": refs,
                    "stale_refs": stale,
                    "opinion_id": o["opinion_id"],
                }
            )
        return {
            "case_key": meeting["case_key"],
            "meeting_id": meeting["meeting_id"],
            "emergency": bool(meeting["emergency"]),
            "required": readiness["required"],
            "confirmed": readiness["confirmed"],
            "excused": readiness["excused"],
            "evidence_versions": current,
            "opinions": opinion_blocks,
            "notice": "本记录仅为院内多学科讨论留痕，不构成自动诊断或处方。",
        }

    def get_candidate_plan(self, candidate_id: int) -> dict:
        row = self.store.one(
            "SELECT * FROM candidate_plans WHERE candidate_id=?", (candidate_id,)
        )
        if not row:
            raise NotFoundError("候选方案不存在")
        out = dict(row)
        out["snapshot"] = json.loads(row["snapshot"])
        return out

    # ------------------------------------------------------------------ 签发

    def issue_plan(
        self,
        actor: dict,
        candidate_id: int,
        *,
        on_behalf_of: str | None = None,
        _override_now: float | None = None,
    ) -> dict:
        now = _override_now or self.clock()
        with self.store.tx() as c:
            plan = c.execute(
                "SELECT * FROM candidate_plans WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not plan:
                raise NotFoundError("候选方案不存在")
            meeting = c.execute(
                "SELECT * FROM meetings WHERE meeting_id=?", (plan["meeting_id"],)
            ).fetchone()
            if plan["status"] == "invalidated":
                raise ConflictError(
                    "候选方案因材料改版已失效，不能签发", code="plan_invalidated"
                )
            # 签发瞬间再次校验齐备性（材料可能在形成候选后改版）
            self._expire_pending_exceptions(c, plan["meeting_id"], now)
            readiness = self._readiness(c, meeting, now)
            if not readiness["ready"]:
                raise ConflictError("签发前复核未通过：专业确认状态已变化", code="not_ready")

            delegation_id = None
            signer_id = actor["user_id"]
            if on_behalf_of:
                granter = c.execute(
                    "SELECT * FROM users WHERE user_id=? AND active=1", (on_behalf_of,)
                ).fetchone()
                if not granter or not granter["authorized_signer"]:
                    raise PermissionDeniedError("被代表者不是授权签发医生")
                d = self._find_delegation(
                    c,
                    granter_id=on_behalf_of,
                    grantee_id=signer_id,
                    scope="sign",
                    discipline=None,
                    case_key=plan["case_key"],
                )
                if not d:
                    raise PermissionDeniedError("缺少有效的签发委托")
                delegation_id = d["delegation_id"]
            elif not actor["authorized_signer"]:
                raise PermissionDeniedError("只有授权签发医生可以签发方案")

            # 并发签发：候选方案上的 CAS 保证只有一个请求成功
            cur = c.execute(
                """UPDATE candidate_plans SET status='issued'
                   WHERE candidate_id=? AND status='candidate'""",
                (candidate_id,),
            )
            if cur.rowcount != 1:
                raise ConflictError("方案已被签发或已失效", code="concurrent_issue")
            cur = c.execute(
                """INSERT INTO issued_plans
                   (candidate_id, meeting_id, case_key, signer_id, on_behalf_of, delegation_id, signed_at, status)
                   VALUES (?,?,?,?,?,?,?, 'active')""",
                (
                    candidate_id,
                    plan["meeting_id"],
                    plan["case_key"],
                    signer_id,
                    on_behalf_of,
                    delegation_id,
                    now,
                ),
            )
            plan_id = cur.lastrowid
        return self.get_issued_plan(plan_id)

    def get_issued_plan(self, plan_id: int) -> dict:
        row = self.store.one("SELECT * FROM issued_plans WHERE plan_id=?", (plan_id,))
        if not row:
            raise NotFoundError("已签发方案不存在")
        candidate = self.store.one(
            "SELECT * FROM candidate_plans WHERE candidate_id=?", (row["candidate_id"],)
        )
        out = dict(row)
        out["candidate"] = self._plan_brief(candidate)
        consents = self.store.all(
            "SELECT * FROM consents WHERE plan_id=? ORDER BY consent_id", (plan_id,)
        )
        out["consents"] = [self._consent_brief(x) for x in consents]
        out["consent"] = out["consents"][-1] if out["consents"] else None
        return out

    # ------------------------------------------------------------------ 同意

    def grant_consent(
        self,
        actor: dict,
        plan_id: int,
        patient_ref: str,
        *,
        _override_now: float | None = None,
    ) -> dict:
        if not patient_ref or not patient_ref.strip():
            raise PayloadError("患者去标识化引用不能为空")
        now = _override_now or self.clock()
        with self.store.tx() as c:
            plan = c.execute(
                "SELECT * FROM issued_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if not plan:
                raise NotFoundError("已签发方案不存在")
            if plan["status"] != "active":
                raise ConflictError(
                    "方案已因材料改版失效，旧签名不能沿用，请就新方案重新同意",
                    code="plan_invalidated",
                )
            pref = c.execute(
                """SELECT version FROM evidence
                   WHERE case_key=? AND kind='preference_record' AND active=1""",
                (plan["case_key"],),
            ).fetchone()
            if not pref:
                raise ConflictError("缺少当前有效的生育意愿记录，不能同意")
            linked = c.execute(
                "SELECT 1 FROM patient_links WHERE case_key=? AND patient_ref=?",
                (plan["case_key"], patient_ref.strip()),
            ).fetchone()
            if not linked:
                raise PermissionDeniedError("患者引用与病例未关联，不能对该方案同意")
            old = c.execute(
                "SELECT * FROM consents WHERE plan_id=? AND status='granted'", (plan_id,)
            ).fetchone()
            if old:
                raise ConflictError("该方案已有有效同意，请勿重复提交", code="duplicate_consent")
            cur = c.execute(
                """INSERT INTO consents
                   (case_key, plan_id, preference_version, patient_ref, status, granted_at)
                   VALUES (?,?,?,?, 'granted', ?)""",
                (plan["case_key"], plan_id, pref["version"], patient_ref.strip(), now),
            )
            consent_id = cur.lastrowid
        return self._consent_brief(self.store.one(
            "SELECT * FROM consents WHERE consent_id=?", (consent_id,)
        ))

    def withdraw_consent(
        self, actor: dict, plan_id: int, *, _override_now: float | None = None
    ) -> dict:
        now = _override_now or self.clock()
        with self.store.tx() as c:
            plan = c.execute(
                "SELECT * FROM issued_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if not plan:
                raise NotFoundError("已签发方案不存在")
            cur = c.execute(
                """UPDATE consents SET status='withdrawn', withdrawn_at=?
                   WHERE plan_id=? AND status='granted'""",
                (now, plan_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("没有可撤回的有效同意")
            c.execute(
                "UPDATE issued_plans SET status='consent_withdrawn' WHERE plan_id=? AND status='active'",
                (plan_id,),
            )
        return self.get_issued_plan(plan_id)

    def change_preference(self, actor: dict, case_key: str, title: str, content: str) -> dict:
        """生育意愿变更走材料改版通道：旧共识与旧签名随即失效。"""
        return self.add_evidence(
            actor,
            case_key,
            EvidenceKind.PREFERENCE_RECORD,
            title,
            content,
            sensitive=True,
        )

    # ------------------------------------------------------------- 会议关闭

    def close_meeting(self, actor: dict, meeting_id: int) -> dict:
        meeting = self.get_meeting(meeting_id)
        if meeting["status"] == "closed":
            raise ConflictError("会议已关闭")
        with self.store.tx() as c:
            c.execute(
                "UPDATE meetings SET status='closed', closed_at=? WHERE meeting_id=? AND status<>'closed'",
                (self.clock(), meeting_id),
            )
        return self.get_meeting(meeting_id)

    # ------------------------------------------------------------------ 导出

    def _timeline(self, c, case_key: str) -> list[dict]:
        events: list[dict] = []
        for r in c.execute(
            "SELECT meeting_id, emergency, status, scheduled_at, closed_at FROM meetings WHERE case_key=? ORDER BY meeting_id",
            (case_key,),
        ):
            events.append({"type": "meeting", "at": r["scheduled_at"], "data": dict(r)})
        for r in c.execute(
            """SELECT opinion_id, meeting_id, discipline, author_id, on_behalf_of, state,
                      created_at, confirmed_at, invalidated_at, invalidate_reason
               FROM opinions WHERE meeting_id IN (SELECT meeting_id FROM meetings WHERE case_key=?)
               ORDER BY opinion_id""",
            (case_key,),
        ):
            events.append({"type": "opinion", "at": r["created_at"], "data": dict(r)})
        for r in c.execute(
            """SELECT exception_id, meeting_id, discipline, state, created_at,
                      review_deadline, reviewed_at, late FROM absence_exceptions
               WHERE meeting_id IN (SELECT meeting_id FROM meetings WHERE case_key=?)
               ORDER BY exception_id""",
            (case_key,),
        ):
            events.append({"type": "absence_exception", "at": r["created_at"], "data": dict(r)})
        for r in c.execute(
            """SELECT candidate_id, meeting_id, status, formed_at, invalidated_at, invalidate_reason
               FROM candidate_plans WHERE case_key=? ORDER BY candidate_id""",
            (case_key,),
        ):
            events.append({"type": "candidate_plan", "at": r["formed_at"], "data": dict(r)})
        for r in c.execute(
            """SELECT ip.plan_id, ip.meeting_id, ip.signer_id, ip.on_behalf_of,
                      ip.signed_at, ip.status AS plan_status
               FROM issued_plans ip WHERE ip.case_key=? ORDER BY ip.plan_id""",
            (case_key,),
        ):
            events.append({"type": "issue", "at": r["signed_at"], "data": dict(r)})
        for r in c.execute(
            """SELECT con.consent_id, con.plan_id, con.status, con.preference_version,
                      con.granted_at, con.withdrawn_at, con.superseded_at
               FROM consents con WHERE con.case_key=? ORDER BY con.consent_id""",
            (case_key,),
        ):
            at = r["granted_at"]
            events.append({"type": "consent", "at": at, "data": dict(r)})
        for r in c.execute(
            """SELECT evidence_id, kind, version, title, active, uploaded_at
               FROM evidence WHERE case_key=? ORDER BY evidence_id""",
            (case_key,),
        ):
            events.append({"type": "evidence", "at": r["uploaded_at"], "data": dict(r)})
        events.sort(key=lambda e: (e["at"], e["type"]))
        return events

    def export_case(self, case_key: str) -> dict:
        """导出会议纪要与决定沿革（结构化数据）。"""
        case = self.store.one("SELECT * FROM cases WHERE case_key=?", (case_key,))
        if not case:
            raise NotFoundError("病例不存在")
        with self.store.tx() as c:
            timeline = self._timeline(c, case_key)
            meetings = []
            for m in c.execute(
                "SELECT * FROM meetings WHERE case_key=? ORDER BY meeting_id", (case_key,)
            ):
                mid = m["meeting_id"]
                readiness = self._readiness(c, m, self.clock())
                plans = [
                    self._plan_brief(dict(x))
                    for x in c.execute(
                        "SELECT * FROM candidate_plans WHERE meeting_id=? ORDER BY candidate_id",
                        (mid,),
                    )
                ]
                issued = c.execute(
                    "SELECT * FROM issued_plans WHERE meeting_id=? ORDER BY plan_id DESC LIMIT 1",
                    (mid,),
                ).fetchone()
                meetings.append(
                    {
                        "meeting": dict(m),
                        "required": self.required_disciplines(mid),
                        "readiness": readiness,
                        "opinions": [
                            {
                                k: (json.loads(v) if k == "evidence_refs" else v)
                                for k, v in o.items()
                            }
                            for o in c.execute(
                                "SELECT * FROM opinions WHERE meeting_id=? ORDER BY opinion_id",
                                (mid,),
                            )
                        ],
                        "exceptions": [
                            dict(x)
                            for x in c.execute(
                                "SELECT * FROM absence_exceptions WHERE meeting_id=? ORDER BY exception_id",
                                (mid,)
                            )
                        ],
                        "candidate_plans": plans,
                        "issued_plan": self._issued_brief(issued) if issued else None,
                    }
                )
        return {
            "case": {
                "case_key": case["case_key"],
                "created_at": case["created_at"],
                "note": case["note"],
            },
            "evidence_versions": self.current_versions(case_key),
            "meetings": meetings,
            "timeline": timeline,
            "generated_at": self.clock(),
            "notice": "本导出仅为院内决策留痕，不构成自动诊断或处方。",
        }

    # ------------------------------------------------------------------ 审计查询

    def list_audit(
        self,
        *,
        case_key: str | None = None,
        sensitive_only: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if case_key:
            sql += " AND case_key=?"
            params.append(case_key)
        if sensitive_only:
            sql += " AND sensitive=1"
        sql += " ORDER BY audit_id DESC LIMIT ?"
        params.append(min(limit, 1000))
        rows = self.store.all(sql, tuple(params))
        for r in rows:
            r["detail"] = json.loads(r["detail_json"] or "{}")
            del r["detail_json"]
        return rows

    # ----------------------------------------------------------- 重启后的状态恢复

    def recover_on_startup(self) -> dict:
        """重启后把已过补审期限但仍 pending 的缺席例外标记为 overdue。"""
        now = self.clock()
        with self.store.tx() as c:
            cur = c.execute(
                "UPDATE absence_exceptions SET state='overdue' WHERE state='pending' AND review_deadline<=?",
                (now,),
            )
            expired = cur.rowcount
        return {"expired_exceptions": expired, "recovered_at": now}
