"""HTTP API：细粒度权限、Bearer 认证、审计与导出装配。

仅使用 Python 标准库。权限模型：

- ``coordinator``  病例协调员：登记病例/材料版本、安排会议、登记缺席例外、
  形成候选方案、查看与导出全部病例数据。
- ``specialist``   专科医生：仅能访问被分配的病例；只能提交本专业意见
  （或凭有效委托代提交）；``authorized_signer`` 者方可签发。
- ``patient``      去标识化患者：仅能对关联到本人引用的已签发方案
  提交/撤回同意，以及更新本人的生育意愿记录。
- ``auditor``      只读全部数据、查询审计日志、导出记录。

任何接口都不返回自动诊断或处方内容。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import (
    AuthError,
    ConflictError,
    MdtError,
    NotFoundError,
    PayloadError,
    PermissionDeniedError,
)
from .export import render_markdown
from .store import Store
from .workflow import MdtService

# --------------------------------------------------------------------------- HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "MDTRecords/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> MdtService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静：测试环境不打访问日志
        if getattr(self.server, "access_log", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- 公共工具

    def _send_json(self, status: int, payload: Any, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, content_type: str, body: bytes, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise PayloadError("Content-Length 不合法")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayloadError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise PayloadError("请求体必须是 JSON 对象")
        self._raw_body = raw
        return data

    def _authenticate(self) -> dict:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthError("缺少 Bearer 令牌")
        token = header[len("Bearer ") :].strip()
        user = self.service.user_for_token(token)
        if not user:
            raise AuthError("令牌无效或用户已停用")
        return user

    def _require_role(self, user: dict, *roles: str) -> None:
        if user["role"] not in roles:
            raise PermissionDeniedError(f"需要角色：{'/'.join(roles)}")

    def _case(self, case_key: str) -> dict:
        case = self.service.get_case({"user_id": "-"}, case_key)
        return case

    def _require_case_member(self, user: dict, case_key: str) -> None:
        """staff 中协调员/审计员可访问全部；专科医生须被分配到该病例。"""
        if user["role"] in ("coordinator", "auditor"):
            return
        if user["role"] == "specialist":
            row = self.service.store.one(
                "SELECT 1 FROM case_assignments WHERE case_key=? AND user_id=?",
                (case_key, user["user_id"]),
            )
            if not row:
                raise PermissionDeniedError("未参与该病例，无权访问")
            return
        raise PermissionDeniedError("该角色无权访问病例材料")

    def _query(self) -> dict[str, str]:
        q = parse_qs(urlsplit(self.path).query)
        return {k: v[-1] for k, v in q.items()}

    def _patient_case_check(self, user: dict, case_key: str) -> None:
        if user["role"] != "patient":
            raise PermissionDeniedError("该操作仅面向患者本人")
        row = self.service.store.one(
            "SELECT 1 FROM patient_links WHERE case_key=? AND patient_ref=?",
            (case_key, user["user_id"]),
        )
        if not row:
            raise PermissionDeniedError("患者引用与该病例未关联")

    def _case_hint(self, path: str) -> str | None:
        """尽力把不含病例键的路径解析到病例，用于失败访问的审计关联。"""
        svc = self.service
        try:
            m = re.fullmatch(r"/api/v1/meetings/(\d+)(?:/.*)?", path)
            if m:
                return svc.get_meeting(int(m.group(1)))["case_key"]
            m = re.fullmatch(r"/api/v1/absence-exceptions/(\d+)(?:/.*)?", path)
            if m:
                ex = svc.get_exception(int(m.group(1)))
                return svc.get_meeting(ex["meeting_id"])["case_key"]
            m = re.fullmatch(r"/api/v1/candidate-plans/(\d+)(?:/.*)?", path)
            if m:
                return svc.get_candidate_plan(int(m.group(1)))["case_key"]
            m = re.fullmatch(r"/api/v1/issued-plans/(\d+)(?:/.*)?", path)
            if m:
                return svc.get_issued_plan(int(m.group(1)))["case_key"]
        except MdtError:
            return None
        return None

    # ------------------------------------------------------------- 入口

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        user: dict | None = None
        action = f"{method} {path}"
        # 预提取病例键：即使路由内抛异常（如权限拒绝），审计也能关联到病例
        m_case = re.search(r"(CASE-[A-F0-9]{16})", path)
        case_key: str | None = m_case.group(1) if m_case else None
        sensitive = False
        try:
            if path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            user = self._authenticate()
            hint = self._case_hint(path)
            if hint:
                case_key = hint
            payload, status, headers, sens, ck = self._route(method, path, user)
            sensitive = sens
            if ck:
                case_key = ck
            if isinstance(payload, bytes):
                self._send_bytes(status, headers["Content-Type"], payload, headers)
            else:
                self._send_json(status, payload, headers)
            self._audit(user, action, case_key, "allowed", sensitive)
        except MdtError as exc:
            result = "denied" if exc.status_code in (401, 403) else "rejected"
            self._send_json(exc.status_code, exc.to_dict())
            if user:
                self._audit(user, action, case_key, result, sensitive)
        except sqlite3.IntegrityError as exc:
            # 并发下撞唯一约束（如候选方案唯一活跃行）的统一兜底
            self._send_json(409, {"error": "conflict", "message": "并发冲突，请重试"})
            if user:
                self._audit(user, action, case_key, "rejected", sensitive,
                            detail={"constraint": str(exc)})
        except Exception as exc:  # pragma: no cover - 防御性兜底
            self._send_json(500, {"error": "internal_error", "message": "服务内部错误"})
            if user:
                self._audit(user, action, case_key, "error", sensitive,
                            detail={"exception": type(exc).__name__})

    def _audit(
        self,
        user: dict,
        action: str,
        case_key: str | None,
        result: str,
        sensitive: bool,
        detail: dict | None = None,
    ) -> None:
        try:
            self.service.audit(
                user["user_id"],
                action,
                case_key=case_key,
                result=result,
                sensitive=sensitive,
                detail=detail,
            )
        except Exception:  # pragma: no cover - 审计失败不应影响响应
            pass

    # ------------------------------------------------------------- 路由

    def _route(
        self, method: str, path: str, user: dict
    ) -> tuple[Any, int, dict, bool, str | None]:
        """返回 (payload, status, headers, sensitive, case_key)。"""
        svc = self.service
        q = self._query()
        headers: dict[str, str] = {}
        sensitive = False
        case_key: str | None = None

        # ---- 病例 ------------------------------------------------------
        if method == "POST" and path == "/api/v1/cases":
            self._require_role(user, "coordinator")
            body = self._read_json()
            case = svc.register_case(user, body["case_key"], body.get("note"))
            return case, 201, headers, False, case["case_key"]

        if method == "GET" and path == "/api/v1/cases":
            self._require_role(user, "coordinator", "specialist", "auditor")
            return svc.list_cases(user), 200, headers, False, None

        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})", path)
        if method == "GET" and m:
            case_key = m.group(1)
            self._require_case_member(user, case_key)
            out = svc.get_case(user, case_key)
            out["patient_links"] = svc.patient_links(case_key)
            return out, 200, headers, False, case_key

        # ---- 分配 / 患者关联 -------------------------------------------
        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})/assignments", path)
        if method == "POST" and m:
            case_key = m.group(1)
            self._require_role(user, "coordinator")
            body = self._read_json()
            svc.assign_specialist(
                user, case_key, body["user_id"], body["discipline"]
            )
            return {"status": "assigned"}, 201, headers, False, case_key

        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})/patient-links", path)
        if method == "POST" and m:
            case_key = m.group(1)
            self._require_role(user, "coordinator")
            body = self._read_json()
            svc.link_patient(user, case_key, body["patient_ref"])
            return {"status": "linked"}, 201, headers, True, case_key

        # ---- 材料版本 --------------------------------------------------
        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})/evidence", path)
        if method == "POST" and m:
            case_key = m.group(1)
            self._require_role(user, "coordinator")
            body = self._read_json()
            ev = svc.add_evidence(
                user,
                case_key,
                body["kind"],
                body.get("title", ""),
                body.get("content"),
                sensitive=bool(body.get("sensitive", False)),
            )
            ev_out = {k: ev[k] for k in ev if k != "content"}
            return ev_out, 201, headers, bool(ev["sensitive"]), case_key

        if method == "GET" and m:
            case_key = m.group(1)
            self._require_case_member(user, case_key)
            full = q.get("full") == "1"
            rows = svc.list_evidence(user, case_key, include_content=full)
            if not full:
                rows = [{k: v for k, v in r.items() if k != "content"} for r in rows]
            sensitive = full and any(r["sensitive"] for r in rows)
            return rows, 200, headers, sensitive, case_key

        m = re.fullmatch(
            r"/api/v1/cases/(CASE-[A-F0-9]{16})/evidence/([a-z_]+)/versions/(\d+)", path
        )
        if method == "GET" and m:
            case_key, kind, version = m.group(1), m.group(2), int(m.group(3))
            self._require_case_member(user, case_key)
            full = q.get("full") == "1"
            ev = svc.get_evidence(user, case_key, kind, version)
            if not full:
                ev = {k: v for k, v in ev.items() if k != "content"}
            sensitive = full and bool(ev["sensitive"])
            return ev, 200, headers, sensitive, case_key

        # ---- 会议 ------------------------------------------------------
        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})/meetings", path)
        if method == "POST" and m:
            case_key = m.group(1)
            self._require_role(user, "coordinator")
            body = self._read_json()
            meeting = svc.schedule_meeting(
                user,
                case_key,
                body["required"],
                emergency=bool(body.get("emergency", False)),
            )
            return meeting, 201, headers, False, case_key
        if method == "GET" and m:
            case_key = m.group(1)
            self._require_case_member(user, case_key)
            return svc.list_meetings(case_key), 200, headers, False, case_key

        m = re.fullmatch(r"/api/v1/meetings/(\d+)", path)
        if method == "GET" and m:
            meeting_id = int(m.group(1))
            view = svc.meeting_status_view(int(meeting_id))
            self._require_case_member(user, view["case_key"])
            return view, 200, headers, False, view["case_key"]

        m = re.fullmatch(r"/api/v1/meetings/(\d+)/close", path)
        if method == "POST" and m:
            meeting_id = int(m.group(1))
            self._require_role(user, "coordinator")
            meeting = svc.get_meeting(meeting_id)
            self._require_case_member(user, meeting["case_key"])
            out = svc.close_meeting(user, meeting_id)
            return out, 200, headers, False, meeting["case_key"]

        # ---- 意见 ------------------------------------------------------
        m = re.fullmatch(r"/api/v1/meetings/(\d+)/opinions", path)
        if method == "POST" and m:
            meeting_id = int(m.group(1))
            self._require_role(user, "specialist")
            meeting = svc.get_meeting(meeting_id)
            self._require_case_member(user, meeting["case_key"])
            body = self._read_json()
            fn = lambda: svc.submit_opinion(
                user,
                meeting_id,
                body["discipline"],
                body["body"],
                body.get("evidence_refs", []),
                coi_disclosed=bool(body.get("coi_disclosed", False)),
                coi_detail=body.get("coi_detail"),
                on_behalf_of=body.get("on_behalf_of"),
            )
            op = self._with_idempotency(user, fn)
            return op, 201, headers, False, meeting["case_key"]
        if method == "GET" and m:
            meeting_id = int(m.group(1))
            meeting = svc.get_meeting(meeting_id)
            self._require_case_member(user, meeting["case_key"])
            return svc.list_opinions(meeting_id), 200, headers, False, meeting["case_key"]

        # ---- 缺席例外 --------------------------------------------------
        m = re.fullmatch(r"/api/v1/meetings/(\d+)/absence-exceptions", path)
        if method == "POST" and m:
            meeting_id = int(m.group(1))
            self._require_role(user, "coordinator")
            meeting = svc.get_meeting(meeting_id)
            body = self._read_json()
            ex = svc.grant_absence_exception(
                user,
                meeting_id,
                body["discipline"],
                body["reason"],
                review_window_seconds=float(body.get("review_window_seconds", 172800)),
            )
            return ex, 201, headers, False, meeting["case_key"]
        if method == "GET" and m:
            meeting_id = int(m.group(1))
            meeting = svc.get_meeting(meeting_id)
            self._require_case_member(user, meeting["case_key"])
            return svc.list_exceptions(meeting_id), 200, headers, False, meeting["case_key"]

        m = re.fullmatch(r"/api/v1/absence-exceptions/(\d+)/review", path)
        if method == "POST" and m:
            exception_id = int(m.group(1))
            self._require_role(user, "specialist")
            ex = svc.get_exception(exception_id)
            meeting = svc.get_meeting(ex["meeting_id"])
            if ex["discipline"] not in svc.user_disciplines(user["user_id"]):
                raise PermissionDeniedError("只能由对应专业的医生补审")
            self._require_case_member(user, meeting["case_key"])
            body = self._read_json()
            out = svc.review_absence_exception(
                user, exception_id, bool(body.get("approve", False)), body.get("note")
            )
            return out, 200, headers, False, meeting["case_key"]

        # ---- 候选方案 / 签发 -------------------------------------------
        m = re.fullmatch(r"/api/v1/meetings/(\d+)/candidate-plans", path)
        if method == "POST" and m:
            meeting_id = int(m.group(1))
            self._require_role(user, "coordinator")
            meeting = svc.get_meeting(meeting_id)
            plan = svc.form_candidate_plan(user, meeting_id)
            return plan, 201, headers, False, meeting["case_key"]

        m = re.fullmatch(r"/api/v1/candidate-plans/(\d+)", path)
        if method == "GET" and m:
            plan = svc.get_candidate_plan(int(m.group(1)))
            self._require_case_member(user, plan["case_key"])
            return plan, 200, headers, False, plan["case_key"]

        m = re.fullmatch(r"/api/v1/candidate-plans/(\d+)/issue", path)
        if method == "POST" and m:
            candidate_id = int(m.group(1))
            self._require_role(user, "specialist")
            plan0 = svc.get_candidate_plan(candidate_id)
            self._require_case_member(user, plan0["case_key"])
            body = self._read_json()
            on_behalf = body.get("on_behalf_of")
            if on_behalf is None and not user["authorized_signer"]:
                raise PermissionDeniedError("只有授权签发医生可以签发方案")
            issued = self._with_idempotency(
                user, lambda: svc.issue_plan(user, candidate_id, on_behalf_of=on_behalf)
            )
            return issued, 201, headers, False, plan0["case_key"]

        m = re.fullmatch(r"/api/v1/issued-plans/(\d+)", path)
        if method == "GET" and m:
            issued = svc.get_issued_plan(int(m.group(1)))
            # 患者本人可查看自己方案的状态；staff 走病例成员校验
            if user["role"] == "patient":
                linked = svc.store.one(
                    "SELECT 1 FROM patient_links WHERE case_key=? AND patient_ref=?",
                    (issued["case_key"], user["user_id"]),
                )
                if not linked:
                    raise PermissionDeniedError("无权查看该方案")
            else:
                self._require_case_member(user, issued["case_key"])
            return issued, 200, headers, True, issued["case_key"]

        # ---- 同意 ------------------------------------------------------
        m = re.fullmatch(r"/api/v1/issued-plans/(\d+)/consent", path)
        if method == "POST" and m:
            plan_id = int(m.group(1))
            issued = svc.get_issued_plan(plan_id)
            self._patient_case_check(user, issued["case_key"])
            body = self._read_json()
            patient_ref = body.get("patient_ref", user["user_id"])
            if patient_ref != user["user_id"]:
                raise PermissionDeniedError("只能使用本人的去标识化引用")
            consent = self._with_idempotency(
                user, lambda: svc.grant_consent(user, plan_id, patient_ref)
            )
            return consent, 201, headers, True, issued["case_key"]

        m = re.fullmatch(r"/api/v1/issued-plans/(\d+)/consent/withdraw", path)
        if method == "POST" and m:
            plan_id = int(m.group(1))
            issued = svc.get_issued_plan(plan_id)
            self._patient_case_check(user, issued["case_key"])
            out = svc.withdraw_consent(user, plan_id)
            return out, 200, headers, True, issued["case_key"]

        # ---- 生育意愿变更（患者本人） ----------------------------------
        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})/preference-change", path)
        if method == "POST" and m:
            case_key = m.group(1)
            self._patient_case_check(user, case_key)
            body = self._read_json()
            ev = svc.change_preference(
                user, case_key, body.get("title", "生育意愿变更"), body.get("content", "")
            )
            return {k: v for k, v in ev.items() if k != "content"}, 201, headers, True, case_key

        # ---- 委托 ------------------------------------------------------
        if method == "POST" and path == "/api/v1/delegations":
            self._require_role(user, "specialist")
            body = self._read_json()
            did = svc.grant_delegation(
                user,
                body["grantee_id"],
                body["scope"],
                discipline=body.get("discipline"),
                case_key=body.get("case_key"),
                ttl_seconds=float(body.get("ttl_seconds", 86400)),
            )
            return {"delegation_id": did, "status": "granted"}, 201, headers, False, body.get("case_key")

        m = re.fullmatch(r"/api/v1/delegations/(\d+)/revoke", path)
        if method == "POST" and m:
            self._require_role(user, "specialist")
            svc.revoke_delegation(user, int(m.group(1)))
            return {"status": "revoked"}, 200, headers, False, None

        # ---- 审计查询 --------------------------------------------------
        if method == "GET" and path == "/api/v1/audit":
            self._require_role(user, "auditor")
            rows = svc.list_audit(
                case_key=q.get("case_key") or None,
                sensitive_only=q.get("sensitive_only") == "1",
                limit=int(q.get("limit", "200")),
            )
            return rows, 200, headers, False, q.get("case_key")

        # ---- 导出 ------------------------------------------------------
        m = re.fullmatch(r"/api/v1/cases/(CASE-[A-F0-9]{16})/export", path)
        if method == "GET" and m:
            case_key = m.group(1)
            self._require_role(user, "coordinator", "auditor")
            data = svc.export_case(case_key)
            fmt = q.get("format", "json")
            if fmt == "md":
                md = render_markdown(data).encode("utf-8")
                headers["Content-Type"] = "text/markdown; charset=utf-8"
                headers["Content-Disposition"] = (
                    f'attachment; filename="{case_key}-minutes.md"'
                )
                return md, 200, headers, True, case_key
            return data, 200, headers, True, case_key

        raise NotFoundError("接口不存在", code="route_not_found", status_code=404)

    # ------------------------------------------------------------- 幂等

    def _with_idempotency(self, user: dict, fn: Callable[[], Any]) -> Any:
        key = self.headers.get("Idempotency-Key", "").strip()
        if not key:
            return fn()
        raw = getattr(self, "_raw_body", b"") or b""
        request_hash = hashlib.sha256(raw).hexdigest()
        store = self.service.store
        # 先查既有记录（重放）
        existing = store.one(
            "SELECT * FROM idempotency_keys WHERE idem_key=? AND user_id=?",
            (key, user["user_id"]),
        )
        if existing:
            if existing["request_hash"] != request_hash:
                raise ConflictError("幂等键已用于不同的请求体", code="idempotency_reuse")
            return json.loads(existing["body_json"])
        result = fn()
        body_json = json.dumps(result, ensure_ascii=False, default=str)
        try:
            with store.tx() as c:
                c.execute(
                    """INSERT INTO idempotency_keys
                       (idem_key, user_id, request_hash, status_code, body_json, created_at)
                       VALUES (?,?,?,200,?,?)""",
                    (
                        key,
                        user["user_id"],
                        request_hash,
                        body_json,
                        self.service.clock(),
                    ),
                )
        except Exception:
            raced = store.one(
                "SELECT * FROM idempotency_keys WHERE idem_key=? AND user_id=?",
                (key, user["user_id"]),
            )
            if raced:
                if raced["request_hash"] != request_hash:
                    raise ConflictError(
                        "幂等键已用于不同的请求体", code="idempotency_reuse"
                    )
                return json.loads(raced["body_json"])
            raise
        return result


# --------------------------------------------------------------------------- 装配


def create_server(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    clock: Callable[[], float] | None = None,
    access_log: bool = False,
) -> tuple[ThreadingHTTPServer, MdtService, Store]:
    store = Store(db_path)
    service = MdtService(store, clock=clock) if clock else MdtService(store)
    service.recover_on_startup()

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    server.store = store  # type: ignore[attr-defined]
    server.access_log = access_log  # type: ignore[attr-defined]
    return server, service, store
