"""多学科讨论决策留痕的业务规则层。

核心不变量：
- 候选方案只有在四个必需专业均已确认、利益冲突均已披露、
  且不存在未完成的缺席例外时才能形成；
- 任何关键材料改版都会使受影响专业的确认失效，并使旧候选方案、
  旧签发方案及其上的知情同意一并失效；
- 生育意愿改变后旧同意签名不得沿用；
- 同一病例同一时刻至多一个 active 候选方案与一个 active 签发方案；
- 所有状态变迁写入 case_events（决定沿革），敏感读取写入 access_audit。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .contracts import ConfirmationState, Discipline, EvidenceKind, validate_case_key
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized
from .guards import NOTICE, assert_deidentified, assert_human_authored
from .store import Store

ROLE_COORDINATOR = "coordinator"
ROLE_PHYSICIAN = "physician"
ROLE_SIGNATORY = "signatory"
ROLE_AUDITOR = "auditor"
ROLES = {ROLE_COORDINATOR, ROLE_PHYSICIAN, ROLE_SIGNATORY, ROLE_AUDITOR}

ALL_DISCIPLINES = [d.value for d in Discipline]
ALL_KINDS = [k.value for k in EvidenceKind]
FERTILITY_PREFERENCES = {"preserve", "not_preserve", "undecided"}
MEETING_KINDS = {"regular", "emergency"}

#: 材料类别 → 受影响专业。材料改版只要求受影响专业重新确认。
AFFECTED_DISCIPLINES = {
    EvidenceKind.PATHOLOGY_REPORT.value: {Discipline.PATHOLOGY.value, Discipline.SURGERY.value},
    EvidenceKind.IMAGING_REPORT.value: {Discipline.RADIOLOGY.value, Discipline.SURGERY.value},
    EvidenceKind.PREFERENCE_RECORD.value: {
        Discipline.FERTILITY_COUNSELING.value,
        Discipline.SURGERY.value,
    },
    EvidenceKind.RISK_REVIEW.value: {Discipline.SURGERY.value},
}

#: 演示用院内账号（生产环境应对接院内身份系统）。
SEED_USERS = [
    ("coord1", "病例协调员-01", ROLE_COORDINATOR, []),
    ("surg1", "外科主任医师-01", ROLE_SIGNATORY, [Discipline.SURGERY.value]),
    ("surg2", "外科医师-02", ROLE_PHYSICIAN, [Discipline.SURGERY.value]),
    ("path1", "病理医师-01", ROLE_PHYSICIAN, [Discipline.PATHOLOGY.value]),
    ("rad1", "影像医师-01", ROLE_PHYSICIAN, [Discipline.RADIOLOGY.value]),
    ("fert1", "生育咨询医师-01", ROLE_PHYSICIAN, [Discipline.FERTILITY_COUNSELING.value]),
    ("auditor1", "质控审计员-01", ROLE_AUDITOR, []),
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class MdtService:
    """全部业务操作。clock 可注入以便测试时间相关规则。"""

    def __init__(
        self,
        store: Store,
        clock=lambda: datetime.now(timezone.utc),
        exception_ttl: timedelta = timedelta(hours=24),
    ):
        self.store = store
        self.clock = clock
        self.exception_ttl = exception_ttl

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock()

    @staticmethod
    def _event(conn, case_key: str, event_type: str, actor: str, detail: dict, at: str) -> None:
        conn.execute(
            "INSERT INTO case_events (case_key, event_type, actor, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (case_key, event_type, actor, _json(detail), at),
        )

    def _audit(self, actor: str, action: str, resource: str, detail: dict) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO access_audit (actor, action, resource, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (actor, action, resource, _json(detail), _iso(self._now())),
            )

    def _get_case(self, case_key: str) -> sqlite3.Row:
        row = self.store.read_one("SELECT * FROM cases WHERE case_key = ?", (case_key,))
        if row is None:
            raise not_found(f"case {case_key}")
        return row

    @staticmethod
    def _current_versions(conn, case_key: str) -> dict[str, int]:
        rows = conn.execute(
            "SELECT kind, MAX(version) AS v FROM materials WHERE case_key = ? GROUP BY kind",
            (case_key,),
        ).fetchall()
        return {row["kind"]: row["v"] for row in rows}

    def _sweep_overdue(self, conn, case_key: str, now: datetime) -> None:
        """在写事务内把已超时的待补审例外落库为 overdue。"""
        rows = conn.execute(
            "SELECT id, discipline, deadline FROM absence_exceptions"
            " WHERE case_key = ? AND state = 'pending'",
            (case_key,),
        ).fetchall()
        for row in rows:
            if _parse(row["deadline"]) < now:
                conn.execute(
                    "UPDATE absence_exceptions SET state = 'overdue' WHERE id = ?", (row["id"],)
                )
                self._event(
                    conn,
                    case_key,
                    "exception_overdue",
                    "system",
                    {"exception_id": row["id"], "discipline": row["discipline"]},
                    _iso(now),
                )

    # ------------------------------------------------------------------
    # 账号与会话
    # ------------------------------------------------------------------
    def seed_users(self) -> None:
        with self.store.tx() as conn:
            for user_id, name, role, disciplines in SEED_USERS:
                conn.execute(
                    "INSERT OR IGNORE INTO users (user_id, name, role, disciplines, secret)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (user_id, name, role, _json(disciplines), f"secret-{user_id}"),
                )

    def create_session(self, user_id: str, secret: str) -> dict:
        row = self.store.read_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if row is None or row["secret"] != secret:
            raise unauthorized("账号或口令不正确")
        token = secrets.token_hex(24)
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                (token, user_id, _iso(self._now())),
            )
        self._audit(user_id, "login", f"user:{user_id}", {})
        return {"token": token, "user": self._user_dict(row)}

    def resolve_token(self, token: str) -> dict:
        row = self.store.read_one(
            "SELECT u.* FROM sessions s JOIN users u ON u.user_id = s.user_id WHERE s.token = ?",
            (token,),
        )
        if row is None:
            raise unauthorized()
        return self._user_dict(row)

    @staticmethod
    def _user_dict(row: sqlite3.Row) -> dict:
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "role": row["role"],
            "disciplines": json.loads(row["disciplines"]),
        }

    # ------------------------------------------------------------------
    # 行 → 字典
    # ------------------------------------------------------------------
    @staticmethod
    def _material_dict(row) -> dict:
        return {
            "id": row["id"],
            "case_key": row["case_key"],
            "kind": row["kind"],
            "version": row["version"],
            "content": json.loads(row["content"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _opinion_dict(row, include_coi_detail: bool = True) -> dict:
        data = {
            "id": row["id"],
            "case_key": row["case_key"],
            "discipline": row["discipline"],
            "author": row["author"],
            "content": row["content"],
            "coi_disclosed": bool(row["coi_disclosed"]),
            "evidence_snapshot": json.loads(row["evidence_snapshot"]),
            "state": row["state"],
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidated_reason": row["invalidated_reason"],
        }
        if include_coi_detail:
            data["coi_detail"] = row["coi_detail"]
        return data

    def _exception_dict(self, row, now: datetime | None = None) -> dict:
        now = now or self._now()
        state = row["state"]
        if state == "pending" and _parse(row["deadline"]) < now:
            state = "overdue"
        return {
            "id": row["id"],
            "meeting_id": row["meeting_id"],
            "case_key": row["case_key"],
            "discipline": row["discipline"],
            "deadline": row["deadline"],
            "state": state,
            "fulfilled_by": row["fulfilled_by"],
            "fulfilled_at": row["fulfilled_at"],
            "late": bool(row["late"]),
        }

    @staticmethod
    def _candidate_dict(row) -> dict:
        return {
            "id": row["id"],
            "case_key": row["case_key"],
            "meeting_id": row["meeting_id"],
            "evidence_snapshot": json.loads(row["evidence_snapshot"]),
            "confirmations": json.loads(row["confirmations"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "superseded_at": row["superseded_at"],
        }

    @staticmethod
    def _issued_dict(row) -> dict:
        return {
            "id": row["id"],
            "case_key": row["case_key"],
            "candidate_id": row["candidate_id"],
            "evidence_snapshot": json.loads(row["evidence_snapshot"]),
            "content": row["content"],
            "issued_by": row["issued_by"],
            "via_delegation_id": row["via_delegation_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidated_reason": row["invalidated_reason"],
        }

    @staticmethod
    def _consent_dict(row) -> dict:
        return {
            "id": row["id"],
            "plan_id": row["plan_id"],
            "case_key": row["case_key"],
            "status": row["status"],
            "recorded_by": row["recorded_by"],
            "created_at": row["created_at"],
            "withdrawn_at": row["withdrawn_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidated_reason": row["invalidated_reason"],
        }

    @staticmethod
    def _delegation_dict(row) -> dict:
        return {
            "id": row["id"],
            "delegator": row["delegator"],
            "delegate": row["delegate"],
            "case_key": row["case_key"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # 病例与材料
    # ------------------------------------------------------------------
    def create_case(self, actor: dict, case_key: str, fertility_preference: str) -> dict:
        if fertility_preference not in FERTILITY_PREFERENCES:
            raise bad_request("invalid_fertility_preference", "生育意愿取值不合法")
        try:
            validate_case_key(case_key)
        except ValueError as exc:
            raise bad_request("invalid_case_key", str(exc)) from exc
        now = _iso(self._now())
        with self.store.tx() as conn:
            if conn.execute("SELECT 1 FROM cases WHERE case_key = ?", (case_key,)).fetchone():
                raise conflict("case_exists", "病例键已登记")
            conn.execute(
                "INSERT INTO cases (case_key, fertility_preference, created_by, created_at)"
                " VALUES (?, ?, ?, ?)",
                (case_key, fertility_preference, actor["user_id"], now),
            )
            self._event(
                conn,
                case_key,
                "case_registered",
                actor["user_id"],
                {"fertility_preference": fertility_preference},
                now,
            )
        return {"case_key": case_key, "fertility_preference": fertility_preference, "created_at": now}

    def add_material(self, actor: dict, case_key: str, kind: str, content: dict) -> dict:
        if kind not in ALL_KINDS:
            raise bad_request("invalid_evidence_kind", "材料类别不合法")
        if not isinstance(content, dict) or not content:
            raise bad_request("invalid_content", "材料内容必须是非空 JSON 对象")
        assert_deidentified(content)
        if kind == EvidenceKind.PREFERENCE_RECORD.value:
            preference = content.get("fertility_preference")
            if preference not in FERTILITY_PREFERENCES:
                raise bad_request(
                    "invalid_fertility_preference", "生育意愿记录必须包含合法的 fertility_preference"
                )
        now_dt = self._now()
        now = _iso(now_dt)
        with self.store.tx() as conn:
            self._get_case(case_key)
            current = self._current_versions(conn, case_key)
            version = current.get(kind, 0) + 1
            conn.execute(
                "INSERT INTO materials (case_key, kind, version, content, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (case_key, kind, version, _json(content), actor["user_id"], now),
            )
            material_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key,
                "material_version_added",
                actor["user_id"],
                {"material_id": material_id, "kind": kind, "version": version},
                now,
            )

            # 1) 受影响专业的 confirmed 意见失效，要求重新确认。
            affected = sorted(AFFECTED_DISCIPLINES[kind])
            stale = conn.execute(
                f"SELECT id, discipline, author FROM opinions WHERE case_key = ?"
                f" AND state = 'confirmed' AND discipline IN ({','.join('?' * len(affected))})",
                (case_key, *affected),
            ).fetchall()
            for row in stale:
                conn.execute(
                    "UPDATE opinions SET state = ?, invalidated_at = ?, invalidated_reason = ?"
                    " WHERE id = ?",
                    (
                        ConfirmationState.INVALIDATED.value,
                        now,
                        f"material_updated:{kind}@v{version}",
                        row["id"],
                    ),
                )
                self._event(
                    conn,
                    case_key,
                    "opinion_invalidated",
                    "system",
                    {
                        "opinion_id": row["id"],
                        "discipline": row["discipline"],
                        "reason": f"material_updated:{kind}@v{version}",
                    },
                    now,
                )

            # 2) 旧候选方案失效。
            candidate = conn.execute(
                "SELECT id FROM candidate_plans WHERE case_key = ? AND status = 'candidate'",
                (case_key,),
            ).fetchone()
            if candidate:
                conn.execute(
                    "UPDATE candidate_plans SET status = 'superseded', superseded_at = ? WHERE id = ?",
                    (now, candidate["id"]),
                )
                self._event(
                    conn,
                    case_key,
                    "candidate_superseded",
                    "system",
                    {"candidate_id": candidate["id"], "reason": f"material_updated:{kind}@v{version}"},
                    now,
                )

            # 3) 生育意愿改变：旧同意签名不得沿用。
            preference_changed = False
            if kind == EvidenceKind.PREFERENCE_RECORD.value:
                case_row = conn.execute(
                    "SELECT fertility_preference FROM cases WHERE case_key = ?", (case_key,)
                ).fetchone()
                new_preference = content["fertility_preference"]
                if new_preference != case_row["fertility_preference"]:
                    preference_changed = True
                    conn.execute(
                        "UPDATE cases SET fertility_preference = ? WHERE case_key = ?",
                        (new_preference, case_key),
                    )
                    for consent in conn.execute(
                        "SELECT id FROM consents WHERE case_key = ? AND status = 'active'",
                        (case_key,),
                    ).fetchall():
                        conn.execute(
                            "UPDATE consents SET status = 'invalidated', invalidated_at = ?,"
                            " invalidated_reason = 'fertility_preference_changed' WHERE id = ?",
                            (now, consent["id"]),
                        )
                        self._event(
                            conn,
                            case_key,
                            "consent_invalidated",
                            "system",
                            {"consent_id": consent["id"], "reason": "fertility_preference_changed"},
                            now,
                        )
                    self._event(
                        conn,
                        case_key,
                        "fertility_preference_changed",
                        actor["user_id"],
                        {"from": case_row["fertility_preference"], "to": new_preference},
                        now,
                    )

            # 4) 旧签发方案失效，其上的有效同意一并失效。
            issued = conn.execute(
                "SELECT id FROM issued_plans WHERE case_key = ? AND status = 'active'", (case_key,)
            ).fetchone()
            if issued:
                reason = f"material_updated:{kind}@v{version}"
                conn.execute(
                    "UPDATE issued_plans SET status = 'invalidated', invalidated_at = ?,"
                    " invalidated_reason = ? WHERE id = ?",
                    (now, reason, issued["id"]),
                )
                self._event(
                    conn,
                    case_key,
                    "plan_invalidated",
                    "system",
                    {"plan_id": issued["id"], "reason": reason},
                    now,
                )
                for consent in conn.execute(
                    "SELECT id FROM consents WHERE plan_id = ? AND status = 'active'",
                    (issued["id"],),
                ).fetchall():
                    conn.execute(
                        "UPDATE consents SET status = 'invalidated', invalidated_at = ?,"
                        " invalidated_reason = 'plan_invalidated' WHERE id = ?",
                        (now, consent["id"]),
                    )
                    self._event(
                        conn,
                        case_key,
                        "consent_invalidated",
                        "system",
                        {"consent_id": consent["id"], "reason": "plan_invalidated"},
                        now,
                    )

        return {
            "id": material_id,
            "case_key": case_key,
            "kind": kind,
            "version": version,
            "invalidated_opinions": [row["id"] for row in stale],
            "preference_changed": preference_changed,
            "created_at": now,
        }

    def list_materials(self, actor: dict, case_key: str) -> dict:
        self._get_case(case_key)
        rows = self.store.read(
            "SELECT * FROM materials WHERE case_key = ? ORDER BY kind, version", (case_key,)
        )
        self._audit(actor["user_id"], "read_materials", f"case:{case_key}", {})
        return {"case_key": case_key, "materials": [self._material_dict(r) for r in rows]}

    # ------------------------------------------------------------------
    # 专业意见
    # ------------------------------------------------------------------
    def submit_opinion(
        self,
        actor: dict,
        case_key: str,
        discipline: str,
        content: str,
        coi_disclosed: bool,
        coi_detail: str,
        confirm: bool,
    ) -> tuple[dict, bool]:
        """提交意见。重复提交幂等：返回该作者现有的 draft/confirmed 意见。"""

        if discipline not in ALL_DISCIPLINES:
            raise bad_request("invalid_discipline", "专业不合法")
        if discipline not in actor["disciplines"]:
            raise forbidden("只能以本人所属专业提交意见")
        if not content.strip():
            raise bad_request("invalid_content", "意见内容不能为空")
        assert_human_authored(content)
        assert_deidentified(content, "content")
        if coi_disclosed is False and coi_detail:
            raise bad_request("invalid_coi", "未披露利益冲突时不应填写披露说明")
        now = _iso(self._now())
        with self.store.tx() as conn:
            self._get_case(case_key)
            existing = conn.execute(
                "SELECT * FROM opinions WHERE case_key = ? AND discipline = ? AND author = ?"
                " AND state IN ('draft', 'confirmed') ORDER BY id DESC LIMIT 1",
                (case_key, discipline, actor["user_id"]),
            ).fetchone()
            if existing is not None:
                if existing["state"] == "confirmed":
                    if existing["content"] != content:
                        raise conflict(
                            "opinion_already_confirmed",
                            "已确认的意见不可修改；材料改版致其失效后请重新提交",
                        )
                    if existing["coi_disclosed"] and not coi_disclosed:
                        raise conflict(
                            "coi_disclosure_locked",
                            "已确认意见的利益冲突披露不可撤回",
                        )
                # 作者可补充利益冲突披露；草稿内容以最新提交为准。
                amended = (
                    bool(existing["coi_disclosed"]) != coi_disclosed
                    or existing["coi_detail"] != coi_detail
                    or (existing["state"] == "draft" and existing["content"] != content)
                )
                if amended:
                    conn.execute(
                        "UPDATE opinions SET coi_disclosed = ?, coi_detail = ?, content = ?"
                        " WHERE id = ?",
                        (
                            1 if coi_disclosed else 0,
                            coi_detail,
                            content if existing["state"] == "draft" else existing["content"],
                            existing["id"],
                        ),
                    )
                    self._event(
                        conn,
                        case_key,
                        "opinion_amended",
                        actor["user_id"],
                        {"opinion_id": existing["id"], "discipline": discipline},
                        now,
                    )
                    existing = conn.execute(
                        "SELECT * FROM opinions WHERE id = ?", (existing["id"],)
                    ).fetchone()
                if existing["state"] == "confirmed" or not confirm:
                    return self._opinion_dict(existing), False
                # 已有草稿 + 请求确认 → 原地确认。
                opinion = self._confirm_opinion_row(conn, case_key, existing, now)
                return self._opinion_dict(opinion), False

            other = conn.execute(
                "SELECT id FROM opinions WHERE case_key = ? AND discipline = ? AND state = 'confirmed'",
                (case_key, discipline),
            ).fetchone()
            if other is not None:
                raise conflict(
                    "discipline_already_confirmed", "该专业已有其他医师确认意见，请先由其更新"
                )
            snapshot = self._current_versions(conn, case_key)
            state = ConfirmationState.CONFIRMED.value if confirm else ConfirmationState.DRAFT.value
            try:
                conn.execute(
                    "INSERT INTO opinions (case_key, discipline, author, content, coi_disclosed,"
                    " coi_detail, evidence_snapshot, state, created_at, confirmed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        case_key,
                        discipline,
                        actor["user_id"],
                        content,
                        1 if coi_disclosed else 0,
                        coi_detail,
                        _json(snapshot),
                        state,
                        now,
                        now if confirm else None,
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发重复提交：回查已有记录并幂等返回。
                existing = conn.execute(
                    "SELECT * FROM opinions WHERE case_key = ? AND discipline = ? AND author = ?"
                    " AND state IN ('draft', 'confirmed') ORDER BY id DESC LIMIT 1",
                    (case_key, discipline, actor["user_id"]),
                ).fetchone()
                if existing is not None:
                    return self._opinion_dict(existing), False
                raise conflict("discipline_already_confirmed", "该专业已有确认意见") from None
            opinion_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key,
                "opinion_confirmed" if confirm else "opinion_submitted",
                actor["user_id"],
                {"opinion_id": opinion_id, "discipline": discipline, "evidence_snapshot": snapshot},
                now,
            )
            row = conn.execute("SELECT * FROM opinions WHERE id = ?", (opinion_id,)).fetchone()
            return self._opinion_dict(row), True

    def _confirm_opinion_row(self, conn, case_key: str, row, now: str):
        if row["state"] == ConfirmationState.INVALIDATED.value:
            raise conflict("opinion_invalidated", "该意见已失效，请基于最新材料重新提交")
        if row["state"] == ConfirmationState.CONFIRMED.value:
            return row
        other = conn.execute(
            "SELECT id FROM opinions WHERE case_key = ? AND discipline = ? AND state = 'confirmed'"
            " AND id != ?",
            (case_key, row["discipline"], row["id"]),
        ).fetchone()
        if other is not None:
            raise conflict("discipline_already_confirmed", "该专业已有其他医师确认意见")
        snapshot = self._current_versions(conn, case_key)
        conn.execute(
            "UPDATE opinions SET state = 'confirmed', evidence_snapshot = ?, confirmed_at = ?"
            " WHERE id = ?",
            (_json(snapshot), now, row["id"]),
        )
        self._event(
            conn,
            case_key,
            "opinion_confirmed",
            row["author"],
            {"opinion_id": row["id"], "discipline": row["discipline"], "evidence_snapshot": snapshot},
            now,
        )
        return conn.execute("SELECT * FROM opinions WHERE id = ?", (row["id"],)).fetchone()

    def confirm_opinion(self, actor: dict, case_key: str, opinion_id: int) -> dict:
        now = _iso(self._now())
        with self.store.tx() as conn:
            self._get_case(case_key)
            row = conn.execute("SELECT * FROM opinions WHERE id = ?", (opinion_id,)).fetchone()
            if row is None or row["case_key"] != case_key:
                raise not_found(f"opinion {opinion_id}")
            if row["author"] != actor["user_id"]:
                raise forbidden("只能确认本人提交的意见")
            return self._opinion_dict(self._confirm_opinion_row(conn, case_key, row, now))

    def list_opinions(self, actor: dict, case_key: str) -> dict:
        self._get_case(case_key)
        rows = self.store.read(
            "SELECT * FROM opinions WHERE case_key = ? ORDER BY id", (case_key,)
        )
        include_detail = actor["role"] in (ROLE_COORDINATOR, ROLE_AUDITOR)
        return {
            "case_key": case_key,
            "opinions": [
                self._opinion_dict(r, include_detail or r["author"] == actor["user_id"])
                for r in rows
            ],
        }

    # ------------------------------------------------------------------
    # 会议与缺席例外
    # ------------------------------------------------------------------
    def create_meeting(
        self, actor: dict, case_key: str, kind: str, absent_disciplines: list[str]
    ) -> dict:
        if kind not in MEETING_KINDS:
            raise bad_request("invalid_meeting_kind", "会议类型不合法")
        unknown = set(absent_disciplines) - set(ALL_DISCIPLINES)
        if unknown:
            raise bad_request("invalid_discipline", f"未知专业: {sorted(unknown)}")
        if kind == "regular" and absent_disciplines:
            raise bad_request(
                "absent_not_allowed", "仅紧急会议允许登记缺席例外，常规会议须全员到会"
            )
        now_dt = self._now()
        now = _iso(now_dt)
        deadline = _iso(now_dt + self.exception_ttl)
        with self.store.tx() as conn:
            self._get_case(case_key)
            conn.execute(
                "INSERT INTO meetings (case_key, kind, created_by, created_at) VALUES (?, ?, ?, ?)",
                (case_key, kind, actor["user_id"], now),
            )
            meeting_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key,
                "meeting_created",
                actor["user_id"],
                {"meeting_id": meeting_id, "kind": kind, "absent": absent_disciplines},
                now,
            )
            exceptions = []
            for discipline in absent_disciplines:
                conn.execute(
                    "INSERT INTO absence_exceptions (meeting_id, case_key, discipline, deadline, state)"
                    " VALUES (?, ?, ?, ?, 'pending')",
                    (meeting_id, case_key, discipline, deadline),
                )
                exception_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._event(
                    conn,
                    case_key,
                    "exception_created",
                    actor["user_id"],
                    {"exception_id": exception_id, "discipline": discipline, "deadline": deadline},
                    now,
                )
                exceptions.append(
                    {
                        "id": exception_id,
                        "discipline": discipline,
                        "deadline": deadline,
                        "state": "pending",
                    }
                )
        return {
            "id": meeting_id,
            "case_key": case_key,
            "kind": kind,
            "created_at": now,
            "exceptions": exceptions,
        }

    def review_exception(self, actor: dict, case_key: str, exception_id: int) -> dict:
        """缺席专业限时补审：须先存在该专业的 confirmed 意见。"""
        now_dt = self._now()
        now = _iso(now_dt)
        with self.store.tx() as conn:
            self._get_case(case_key)
            self._sweep_overdue(conn, case_key, now_dt)
            row = conn.execute(
                "SELECT * FROM absence_exceptions WHERE id = ?", (exception_id,)
            ).fetchone()
            if row is None or row["case_key"] != case_key:
                raise not_found(f"exception {exception_id}")
            if row["discipline"] not in actor["disciplines"]:
                raise forbidden("只能由缺席专业的医师补审")
            if row["state"] == "fulfilled":
                return self._exception_dict(row, now_dt)
            opinion = conn.execute(
                "SELECT id FROM opinions WHERE case_key = ? AND discipline = ? AND state = 'confirmed'",
                (case_key, row["discipline"]),
            ).fetchone()
            if opinion is None:
                raise conflict(
                    "review_requires_confirmed_opinion", "补审前须先提交并确认该专业的意见"
                )
            late = _parse(row["deadline"]) < now_dt
            conn.execute(
                "UPDATE absence_exceptions SET state = 'fulfilled', fulfilled_by = ?,"
                " fulfilled_at = ?, late = ? WHERE id = ?",
                (actor["user_id"], now, 1 if late else 0, exception_id),
            )
            self._event(
                conn,
                case_key,
                "exception_fulfilled",
                actor["user_id"],
                {
                    "exception_id": exception_id,
                    "discipline": row["discipline"],
                    "late": late,
                    "opinion_id": opinion["id"],
                },
                now,
            )
            row = conn.execute(
                "SELECT * FROM absence_exceptions WHERE id = ?", (exception_id,)
            ).fetchone()
            return self._exception_dict(row, now_dt)

    # ------------------------------------------------------------------
    # 候选方案与签发
    # ------------------------------------------------------------------
    @staticmethod
    def _snapshot_covers(discipline: str, snapshot: dict, current: dict) -> bool:
        """该专业的意见快照在其受影响材料类别上是否与当前版本一致。"""
        for kind, disciplines in AFFECTED_DISCIPLINES.items():
            if discipline in disciplines and snapshot.get(kind, 0) != current.get(kind, 0):
                return False
        return True

    def _readiness(self, conn, case_key: str) -> tuple[dict, list[str]]:
        """汇总当前确认状态与候选方案就绪缺口。"""
        current = self._current_versions(conn, case_key)
        confirmed = {}
        for row in conn.execute(
            "SELECT * FROM opinions WHERE case_key = ? AND state = 'confirmed'", (case_key,)
        ).fetchall():
            confirmed[row["discipline"]] = row
        missing = []
        for kind in ALL_KINDS:
            if current.get(kind, 0) < 1:
                missing.append(f"missing_material:{kind}")
        for discipline in ALL_DISCIPLINES:
            opinion = confirmed.get(discipline)
            if opinion is None:
                missing.append(f"awaiting_confirmation:{discipline}")
            elif not opinion["coi_disclosed"]:
                missing.append(f"coi_not_disclosed:{discipline}")
            elif not self._snapshot_covers(
                discipline, json.loads(opinion["evidence_snapshot"]), current
            ):
                missing.append(f"stale_evidence:{discipline}")
        meetings = conn.execute(
            "SELECT id FROM meetings WHERE case_key = ? ORDER BY id", (case_key,)
        ).fetchall()
        if not meetings:
            missing.append("no_meeting")
        self._sweep_overdue(conn, case_key, self._now())
        for row in conn.execute(
            "SELECT id, state FROM absence_exceptions WHERE case_key = ? AND state != 'fulfilled'",
            (case_key,),
        ).fetchall():
            missing.append(f"exception_{row['state']}:{row['id']}")
        return {"current_versions": current, "confirmed": confirmed, "meetings": meetings}, missing

    def form_candidate(self, actor: dict, case_key: str) -> tuple[dict, bool]:
        """形成候选方案。重复调用幂等：已有同快照候选方案时直接返回。"""
        now = _iso(self._now())
        with self.store.tx() as conn:
            self._get_case(case_key)
            ctx, missing = self._readiness(conn, case_key)
            if missing:
                raise conflict("candidate_not_ready", "候选方案条件未满足: " + "; ".join(missing))
            current = ctx["current_versions"]
            existing = conn.execute(
                "SELECT * FROM candidate_plans WHERE case_key = ? AND status = 'candidate'",
                (case_key,),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["evidence_snapshot"]) == current:
                    return self._candidate_dict(existing), False
                raise conflict("candidate_stale", "已存在基于旧材料版本的候选方案")
            confirmations = {}
            for discipline, row in ctx["confirmed"].items():
                confirmations[discipline] = {
                    "opinion_id": row["id"],
                    "author": row["author"],
                    "state": row["state"],
                    "coi_disclosed": bool(row["coi_disclosed"]),
                    "evidence_snapshot": json.loads(row["evidence_snapshot"]),
                    "confirmed_at": row["confirmed_at"],
                }
            meeting_id = ctx["meetings"][-1]["id"]
            try:
                conn.execute(
                    "INSERT INTO candidate_plans (case_key, meeting_id, evidence_snapshot,"
                    " confirmations, status, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, 'candidate', ?, ?)",
                    (case_key, meeting_id, _json(current), _json(confirmations), actor["user_id"], now),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM candidate_plans WHERE case_key = ? AND status = 'candidate'",
                    (case_key,),
                ).fetchone()
                if existing is not None:
                    return self._candidate_dict(existing), False
                raise
            candidate_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key,
                "candidate_formed",
                actor["user_id"],
                {"candidate_id": candidate_id, "evidence_snapshot": current},
                now,
            )
            row = conn.execute(
                "SELECT * FROM candidate_plans WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._candidate_dict(row), True

    def _issuing_authority(self, conn, actor: dict, case_key: str) -> int | None:
        """返回签发凭据：None 表示无权，0 表示本人签发权，>0 为委托记录 id。"""
        if actor["role"] == ROLE_SIGNATORY:
            return 0
        now = self._now()
        rows = conn.execute(
            "SELECT * FROM delegations WHERE delegate = ? AND revoked_at IS NULL"
            " AND (case_key IS NULL OR case_key = ?)",
            (actor["user_id"], case_key),
        ).fetchall()
        for row in rows:
            if _parse(row["expires_at"]) > now:
                return row["id"]
        return None

    def issue_plan(self, actor: dict, case_key: str, candidate_id: int, content: str) -> dict:
        """授权医生签发最终方案。同一病例至多一个 active 签发方案。"""
        if not content.strip():
            raise bad_request("invalid_content", "签发方案内容不能为空")
        assert_human_authored(content)
        assert_deidentified(content, "content")
        now = _iso(self._now())
        with self.store.tx() as conn:
            self._get_case(case_key)
            candidate = conn.execute(
                "SELECT * FROM candidate_plans WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None or candidate["case_key"] != case_key:
                raise not_found(f"candidate {candidate_id}")
            if candidate["status"] != "candidate":
                raise conflict("candidate_not_active", "候选方案已失效，请重新形成候选方案")
            current = self._current_versions(conn, case_key)
            if json.loads(candidate["evidence_snapshot"]) != current:
                raise conflict("candidate_stale", "候选方案基于旧材料版本，已不能签发")
            authority = self._issuing_authority(conn, actor, case_key)
            if authority is None:
                raise forbidden("仅被授权的签发医生或有效受托人可签发方案")
            via_delegation = authority if authority > 0 else None
            try:
                conn.execute(
                    "INSERT INTO issued_plans (case_key, candidate_id, evidence_snapshot, content,"
                    " issued_by, via_delegation_id, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    (
                        case_key,
                        candidate_id,
                        candidate["evidence_snapshot"],
                        content,
                        actor["user_id"],
                        via_delegation,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise conflict("already_issued", "该病例已存在生效中的签发方案") from None
            plan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key,
                "plan_issued",
                actor["user_id"],
                {
                    "plan_id": plan_id,
                    "candidate_id": candidate_id,
                    "via_delegation_id": via_delegation,
                },
                now,
            )
            row = conn.execute("SELECT * FROM issued_plans WHERE id = ?", (plan_id,)).fetchone()
            return self._issued_dict(row)

    # ------------------------------------------------------------------
    # 知情同意
    # ------------------------------------------------------------------
    def record_consent(self, actor: dict, case_key: str, plan_id: int) -> tuple[dict, bool]:
        """登记患者知情同意，仅针对生效中的签发方案。重复登记幂等。"""
        now = _iso(self._now())
        with self.store.tx() as conn:
            self._get_case(case_key)
            plan = conn.execute("SELECT * FROM issued_plans WHERE id = ?", (plan_id,)).fetchone()
            if plan is None or plan["case_key"] != case_key:
                raise not_found(f"plan {plan_id}")
            if plan["status"] != "active":
                raise conflict(
                    "plan_not_active", "方案已失效，旧签名不得沿用，须基于新方案重新签署"
                )
            existing = conn.execute(
                "SELECT * FROM consents WHERE plan_id = ? AND status = 'active'", (plan_id,)
            ).fetchone()
            if existing is not None:
                return self._consent_dict(existing), False
            try:
                conn.execute(
                    "INSERT INTO consents (plan_id, case_key, status, recorded_by, created_at)"
                    " VALUES (?, ?, 'active', ?, ?)",
                    (plan_id, case_key, actor["user_id"], now),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM consents WHERE plan_id = ? AND status = 'active'", (plan_id,)
                ).fetchone()
                if existing is not None:
                    return self._consent_dict(existing), False
                raise
            consent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key,
                "consent_recorded",
                actor["user_id"],
                {"consent_id": consent_id, "plan_id": plan_id},
                now,
            )
            row = conn.execute("SELECT * FROM consents WHERE id = ?", (consent_id,)).fetchone()
            return self._consent_dict(row), True

    def withdraw_consent(self, actor: dict, case_key: str, consent_id: int) -> dict:
        now = _iso(self._now())
        with self.store.tx() as conn:
            self._get_case(case_key)
            row = conn.execute("SELECT * FROM consents WHERE id = ?", (consent_id,)).fetchone()
            if row is None or row["case_key"] != case_key:
                raise not_found(f"consent {consent_id}")
            if row["status"] == "withdrawn":
                return self._consent_dict(row)
            if row["status"] != "active":
                raise conflict("consent_not_active", "该同意已失效，无法撤回")
            conn.execute(
                "UPDATE consents SET status = 'withdrawn', withdrawn_at = ? WHERE id = ?",
                (now, consent_id),
            )
            self._event(
                conn,
                case_key,
                "consent_withdrawn",
                actor["user_id"],
                {"consent_id": consent_id, "plan_id": row["plan_id"]},
                now,
            )
            row = conn.execute("SELECT * FROM consents WHERE id = ?", (consent_id,)).fetchone()
            return self._consent_dict(row)

    # ------------------------------------------------------------------
    # 授权委托
    # ------------------------------------------------------------------
    def create_delegation(
        self, actor: dict, delegate_id: str, case_key: str | None, expires_in_seconds: int
    ) -> dict:
        if expires_in_seconds <= 0:
            raise bad_request("invalid_expiry", "委托必须设置正的有效期（秒）")
        delegate = self.store.read_one("SELECT * FROM users WHERE user_id = ?", (delegate_id,))
        if delegate is None:
            raise not_found(f"user {delegate_id}")
        if delegate["role"] not in (ROLE_PHYSICIAN, ROLE_SIGNATORY):
            raise bad_request("invalid_delegate", "受托人必须是医师")
        if delegate_id == actor["user_id"]:
            raise bad_request("invalid_delegate", "不能委托给本人")
        if case_key is not None:
            self._get_case(case_key)
        now_dt = self._now()
        now = _iso(now_dt)
        expires_at = _iso(now_dt + timedelta(seconds=expires_in_seconds))
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO delegations (delegator, delegate, case_key, expires_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (actor["user_id"], delegate_id, case_key, expires_at, now),
            )
            delegation_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._event(
                conn,
                case_key or "GLOBAL",
                "delegation_created",
                actor["user_id"],
                {
                    "delegation_id": delegation_id,
                    "delegate": delegate_id,
                    "case_key": case_key,
                    "expires_at": expires_at,
                },
                now,
            )
            row = conn.execute("SELECT * FROM delegations WHERE id = ?", (delegation_id,)).fetchone()
            return self._delegation_dict(row)

    def revoke_delegation(self, actor: dict, delegation_id: int) -> dict:
        now = _iso(self._now())
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM delegations WHERE id = ?", (delegation_id,)).fetchone()
            if row is None:
                raise not_found(f"delegation {delegation_id}")
            if row["delegator"] != actor["user_id"]:
                raise forbidden("只能撤销本人发起的委托")
            if row["revoked_at"] is None:
                conn.execute(
                    "UPDATE delegations SET revoked_at = ? WHERE id = ?", (now, delegation_id)
                )
                self._event(
                    conn,
                    row["case_key"] or "GLOBAL",
                    "delegation_revoked",
                    actor["user_id"],
                    {"delegation_id": delegation_id},
                    now,
                )
            row = conn.execute("SELECT * FROM delegations WHERE id = ?", (delegation_id,)).fetchone()
            return self._delegation_dict(row)

    def list_delegations(self, actor: dict) -> dict:
        rows = self.store.read(
            "SELECT * FROM delegations WHERE delegator = ? OR delegate = ? ORDER BY id",
            (actor["user_id"], actor["user_id"]),
        )
        return {"delegations": [self._delegation_dict(r) for r in rows]}

    # ------------------------------------------------------------------
    # 状态、导出与审计
    # ------------------------------------------------------------------
    def get_case_status(self, actor: dict, case_key: str) -> dict:
        self._get_case(case_key)
        now = self._now()
        with self.store.tx() as conn:
            self._sweep_overdue(conn, case_key, now)
        case = self.store.read_one("SELECT * FROM cases WHERE case_key = ?", (case_key,))
        current = self.store.read(
            "SELECT kind, MAX(version) AS v FROM materials WHERE case_key = ? GROUP BY kind",
            (case_key,),
        )
        versions = {row["kind"]: row["v"] for row in current}
        opinions = self.store.read(
            "SELECT * FROM opinions WHERE case_key = ? ORDER BY id", (case_key,)
        )
        disciplines = {}
        for discipline in ALL_DISCIPLINES:
            latest = None
            confirmed = None
            for row in opinions:
                if row["discipline"] != discipline:
                    continue
                latest = row
                if row["state"] == "confirmed":
                    confirmed = row
            entry = {"state": "absent", "opinion": None}
            focus = confirmed or latest
            if focus is not None:
                entry = {
                    "state": focus["state"],
                    "opinion": self._opinion_dict(focus, include_coi_detail=False),
                }
            disciplines[discipline] = entry
        exceptions = [
            self._exception_dict(r, now)
            for r in self.store.read(
                "SELECT * FROM absence_exceptions WHERE case_key = ? ORDER BY id", (case_key,)
            )
        ]
        candidate = self.store.read_one(
            "SELECT * FROM candidate_plans WHERE case_key = ? ORDER BY id DESC LIMIT 1", (case_key,)
        )
        issued = self.store.read_one(
            "SELECT * FROM issued_plans WHERE case_key = ? ORDER BY id DESC LIMIT 1", (case_key,)
        )
        consent = None
        if issued is not None:
            consent = self.store.read_one(
                "SELECT * FROM consents WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (issued["id"],)
            )
        with self.store.tx() as conn:
            _, missing = self._readiness(conn, case_key)
        self._audit(actor["user_id"], "read_status", f"case:{case_key}", {})
        return {
            "case_key": case_key,
            "fertility_preference": case["fertility_preference"],
            "material_versions": versions,
            "disciplines": disciplines,
            "exceptions": exceptions,
            "candidate_plan": self._candidate_dict(candidate) if candidate else None,
            "issued_plan": self._issued_dict(issued) if issued else None,
            "consent": self._consent_dict(consent) if consent else None,
            "ready_for_candidate": not missing,
            "blocking_reasons": missing,
            "notice": NOTICE,
        }

    def get_case_detail(self, actor: dict, case_key: str) -> dict:
        self._get_case(case_key)
        status = self.get_case_status(actor, case_key)
        opinions = self.list_opinions(actor, case_key)["opinions"]
        meetings = [
            {"id": r["id"], "kind": r["kind"], "created_by": r["created_by"], "created_at": r["created_at"]}
            for r in self.store.read("SELECT * FROM meetings WHERE case_key = ? ORDER BY id", (case_key,))
        ]
        self._audit(actor["user_id"], "read_case_detail", f"case:{case_key}", {})
        return {**status, "opinions": opinions, "meetings": meetings}

    def export_minutes(self, actor: dict, case_key: str) -> dict:
        """会议纪要导出：材料版本、意见确认状态、缺席例外与方案沿革快照。"""
        self._get_case(case_key)
        now = self._now()
        with self.store.tx() as conn:
            self._sweep_overdue(conn, case_key, now)
        detail = self.get_case_detail(actor, case_key)
        materials = self.store.read(
            "SELECT * FROM materials WHERE case_key = ? ORDER BY kind, version", (case_key,)
        )
        minutes = {
            "document": "meeting_minutes",
            "case_key": case_key,
            "generated_by": actor["user_id"],
            "generated_at": _iso(now),
            "fertility_preference": detail["fertility_preference"],
            "material_versions": detail["material_versions"],
            "meetings": detail["meetings"],
            "exceptions": detail["exceptions"],
            "opinions": detail["opinions"],
            "candidate_plan": detail["candidate_plan"],
            "issued_plan": detail["issued_plan"],
            "consent": detail["consent"],
            "materials": [self._material_dict(r) for r in materials],
            "notice": NOTICE,
        }
        self._audit(actor["user_id"], "export_minutes", f"case:{case_key}", {})
        return minutes

    def export_history(self, actor: dict, case_key: str) -> dict:
        """决定沿革导出：按序返回该病例全部状态变迁事件。"""
        self._get_case(case_key)
        rows = self.store.read(
            "SELECT * FROM case_events WHERE case_key = ? ORDER BY id", (case_key,)
        )
        history = {
            "document": "decision_history",
            "case_key": case_key,
            "generated_by": actor["user_id"],
            "generated_at": _iso(self._now()),
            "events": [
                {
                    "seq": r["id"],
                    "type": r["event_type"],
                    "actor": r["actor"],
                    "detail": json.loads(r["detail"]),
                    "at": r["created_at"],
                }
                for r in rows
            ],
            "notice": NOTICE,
        }
        self._audit(actor["user_id"], "export_history", f"case:{case_key}", {})
        return history

    def list_audit(self, actor: dict) -> dict:
        rows = self.store.read("SELECT * FROM access_audit ORDER BY id")
        result = {
            "entries": [
                {
                    "id": r["id"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "resource": r["resource"],
                    "detail": json.loads(r["detail"]),
                    "at": r["created_at"],
                }
                for r in rows
            ]
        }
        self._audit(actor["user_id"], "read_audit_log", "access_audit", {})
        return result
