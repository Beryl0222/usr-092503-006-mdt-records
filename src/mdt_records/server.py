"""HTTP API 层：路由、令牌认证、细粒度权限与严格请求校验。

仅使用标准库。所有响应为 JSON；涉及结论的响应均携带证据版本
（evidence_snapshot / material_versions）与各专业确认状态。
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import ApiError, bad_request, not_found, unauthorized
from .guards import NOTICE
from .service import (
    ROLE_AUDITOR,
    ROLE_COORDINATOR,
    MdtService,
)
from .store import Store

MAX_BODY_BYTES = 1 << 20


class Ctx:
    def __init__(self, service: MdtService, user: dict | None, body: Any):
        self.service = service
        self.user = user
        self.body = body if isinstance(body, dict) else {}


def _require_roles(ctx: Ctx, *roles: str) -> None:
    if ctx.user is None:
        raise unauthorized()
    if ctx.user["role"] not in roles:
        from .errors import forbidden

        raise forbidden()


def _fields(ctx: Ctx, spec: dict[str, type], optional: dict[str, type] | None = None) -> dict:
    """严格校验请求体：缺字段、类型不符、未知字段一律 400。"""
    optional = optional or {}
    body = ctx.body
    allowed = set(spec) | set(optional)
    extra = set(body) - allowed
    if extra:
        raise bad_request("unknown_fields", f"未知字段: {sorted(extra)}")
    out = {}
    for name, typ in spec.items():
        if name not in body:
            raise bad_request("missing_field", f"缺少必填字段: {name}")
        value = body[name]
        if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            raise bad_request("invalid_field", f"字段 {name} 类型不正确")
        out[name] = value
    for name, typ in optional.items():
        if name in body:
            value = body[name]
            if value is not None and not isinstance(value, typ):
                raise bad_request("invalid_field", f"字段 {name} 类型不正确")
            out[name] = value
        else:
            out[name] = None
    return out


# ----------------------------------------------------------------------
# 处理器
# ----------------------------------------------------------------------
def h_health(ctx: Ctx, **_) -> tuple[int, dict]:
    return 200, {"ok": True, "notice": NOTICE}


def h_login(ctx: Ctx, **_) -> tuple[int, dict]:
    data = _fields(ctx, {"user_id": str, "secret": str})
    return 201, ctx.service.create_session(**data)


def h_create_case(ctx: Ctx, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR)
    data = _fields(ctx, {"case_key": str, "fertility_preference": str})
    return 201, ctx.service.create_case(ctx.user, **data)


def h_case_detail(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR, ROLE_AUDITOR, "physician", "signatory")
    return 200, ctx.service.get_case_detail(ctx.user, key)


def h_case_status(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR, ROLE_AUDITOR, "physician", "signatory")
    return 200, ctx.service.get_case_status(ctx.user, key)


def h_add_material(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR)
    data = _fields(ctx, {"kind": str, "content": dict})
    return 201, ctx.service.add_material(ctx.user, key, **data)


def h_list_materials(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR, ROLE_AUDITOR, "physician", "signatory")
    return 200, ctx.service.list_materials(ctx.user, key)


def h_submit_opinion(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, "physician", "signatory")
    data = _fields(
        ctx,
        {"discipline": str, "content": str, "coi_disclosed": bool},
        {"coi_detail": str, "confirm": bool},
    )
    opinion, created = ctx.service.submit_opinion(
        ctx.user,
        key,
        discipline=data["discipline"],
        content=data["content"],
        coi_disclosed=data["coi_disclosed"],
        coi_detail=data.get("coi_detail") or "",
        confirm=bool(data.get("confirm")),
    )
    return (201 if created else 200), {"opinion": opinion, "deduplicated": not created, "notice": NOTICE}


def h_list_opinions(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR, ROLE_AUDITOR, "physician", "signatory")
    return 200, ctx.service.list_opinions(ctx.user, key)


def h_confirm_opinion(ctx: Ctx, key: str, oid: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, "physician", "signatory")
    return 200, {"opinion": ctx.service.confirm_opinion(ctx.user, key, int(oid)), "notice": NOTICE}


def h_create_meeting(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR)
    data = _fields(ctx, {"kind": str}, {"absent_disciplines": list})
    return 201, ctx.service.create_meeting(
        ctx.user, key, kind=data["kind"], absent_disciplines=data.get("absent_disciplines") or []
    )


def h_review_exception(ctx: Ctx, key: str, eid: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, "physician", "signatory")
    return 200, ctx.service.review_exception(ctx.user, key, int(eid))


def h_form_candidate(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR)
    _fields(ctx, {})
    candidate, created = ctx.service.form_candidate(ctx.user, key)
    return (201 if created else 200), {
        "candidate": candidate,
        "deduplicated": not created,
        "notice": NOTICE,
    }


def h_issue_plan(ctx: Ctx, key: str, cid: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, "physician", "signatory")
    data = _fields(ctx, {"content": str})
    plan = ctx.service.issue_plan(ctx.user, key, int(cid), data["content"])
    return 201, {"plan": plan, "notice": NOTICE}


def h_record_consent(ctx: Ctx, key: str, pid: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR)
    _fields(ctx, {})
    consent, created = ctx.service.record_consent(ctx.user, key, int(pid))
    return (201 if created else 200), {"consent": consent, "deduplicated": not created}


def h_withdraw_consent(ctx: Ctx, key: str, cid: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR)
    _fields(ctx, {})
    return 200, {"consent": ctx.service.withdraw_consent(ctx.user, key, int(cid))}


def h_create_delegation(ctx: Ctx, **_) -> tuple[int, dict]:
    _require_roles(ctx, "signatory")
    data = _fields(
        ctx, {"delegate_id": str, "expires_in_seconds": int}, {"case_key": str}
    )
    return 201, ctx.service.create_delegation(ctx.user, **data)


def h_revoke_delegation(ctx: Ctx, did: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, "signatory")
    _fields(ctx, {})
    return 200, ctx.service.revoke_delegation(ctx.user, int(did))


def h_list_delegations(ctx: Ctx, **_) -> tuple[int, dict]:
    _require_roles(ctx, "physician", "signatory", ROLE_COORDINATOR, ROLE_AUDITOR)
    return 200, ctx.service.list_delegations(ctx.user)


def h_export_minutes(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR, ROLE_AUDITOR)
    return 200, ctx.service.export_minutes(ctx.user, key)


def h_export_history(ctx: Ctx, key: str, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_COORDINATOR, ROLE_AUDITOR)
    return 200, ctx.service.export_history(ctx.user, key)


def h_audit(ctx: Ctx, **_) -> tuple[int, dict]:
    _require_roles(ctx, ROLE_AUDITOR)
    return 200, ctx.service.list_audit(ctx.user)


ROUTES: list[tuple[str, re.Pattern, Callable, bool]] = [
    ("GET", re.compile(r"^/health$"), h_health, False),
    ("POST", re.compile(r"^/sessions$"), h_login, False),
    ("POST", re.compile(r"^/cases$"), h_create_case, True),
    ("GET", re.compile(r"^/cases/(?P<key>[^/]+)$"), h_case_detail, True),
    ("GET", re.compile(r"^/cases/(?P<key>[^/]+)/status$"), h_case_status, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/materials$"), h_add_material, True),
    ("GET", re.compile(r"^/cases/(?P<key>[^/]+)/materials$"), h_list_materials, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/opinions$"), h_submit_opinion, True),
    ("GET", re.compile(r"^/cases/(?P<key>[^/]+)/opinions$"), h_list_opinions, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/opinions/(?P<oid>\d+)/confirm$"), h_confirm_opinion, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/meetings$"), h_create_meeting, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/exceptions/(?P<eid>\d+)/review$"), h_review_exception, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/candidate$"), h_form_candidate, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/plans/(?P<cid>\d+)/issue$"), h_issue_plan, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/plans/(?P<pid>\d+)/consent$"), h_record_consent, True),
    ("POST", re.compile(r"^/cases/(?P<key>[^/]+)/consents/(?P<cid>\d+)/withdraw$"), h_withdraw_consent, True),
    ("POST", re.compile(r"^/delegations$"), h_create_delegation, True),
    ("GET", re.compile(r"^/delegations$"), h_list_delegations, True),
    ("POST", re.compile(r"^/delegations/(?P<did>\d+)/revoke$"), h_revoke_delegation, True),
    ("GET", re.compile(r"^/cases/(?P<key>[^/]+)/minutes$"), h_export_minutes, True),
    ("GET", re.compile(r"^/cases/(?P<key>[^/]+)/history$"), h_export_history, True),
    ("GET", re.compile(r"^/audit$"), h_audit, True),
]


class App:
    def __init__(self, service: MdtService):
        self.service = service

    def handle(self, method: str, path: str, body: Any, headers: dict[str, str]) -> tuple[int, dict]:
        for route_method, pattern, handler, needs_auth in ROUTES:
            match = pattern.match(path)
            if not match or route_method != method:
                continue
            user = None
            if needs_auth:
                auth = headers.get("authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                if not token:
                    raise unauthorized()
                user = self.service.resolve_token(token)
            ctx = Ctx(self.service, user, body)
            return handler(ctx, **match.groupdict())
        raise not_found(f"{method} {path}")


def _make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    raise bad_request("body_too_large", "请求体过大")
                raw = self.rfile.read(length) if length else b""
                body: Any = None
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise bad_request("invalid_json", "请求体不是合法 JSON") from exc
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, payload = app.handle(method, self.path.split("?", 1)[0], body, headers)
            except ApiError as exc:
                status, payload = exc.status, exc.body()
            except Exception as exc:  # noqa: BLE001 - 兜底，避免泄露内部细节
                print(f"internal error: {exc!r}", file=sys.stderr)
                status, payload = 500, {"error": {"code": "internal", "message": "服务器内部错误"}}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *_args) -> None:
            pass

    return Handler


def create_server(
    db_path: str,
    clock=None,
    exception_ttl=None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """构建 HTTP 服务。clock / exception_ttl 可注入以便测试。"""
    store = Store(db_path)
    kwargs: dict = {}
    if clock is not None:
        kwargs["clock"] = clock
    if exception_ttl is not None:
        kwargs["exception_ttl"] = exception_ttl
    service = MdtService(store, **kwargs)
    service.seed_users()
    server = ThreadingHTTPServer((host, port), _make_handler(App(service)))
    server.daemon_threads = True
    server.service = service  # type: ignore[attr-defined]
    return server
