"""端到端测试：真实 HTTP 服务（线程内）+ SQLite 持久化。

覆盖：并发签署、重复提交（含幂等键）、材料改版失效、授权委托、
同意撤回/意愿变更、超时补审、重启恢复，以及细粒度权限与审计、
证据版本/确认状态展示和“不输出自动诊断或处方”的约束。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from src.mdt_records.app import create_server
from src.mdt_records.seed import seed

TOK = {
    "coord": "tok-coord",
    "surg": "tok-surg",
    "surg2": "tok-surg2",
    "path": "tok-path",
    "rad": "tok-rad",
    "fert": "tok-fert",
    "audit": "tok-audit",
    "patient": "tok-patient",
}
CASE = "CASE-0123456789ABCDEF"
ALL_DISCIPLINES = ["surgery", "pathology", "radiology", "fertility_counseling"]
REFS_V1 = {
    "surgery": [
        {"kind": "pathology_report", "version": 1},
        {"kind": "imaging_report", "version": 1},
        {"kind": "preference_record", "version": 1},
        {"kind": "risk_review", "version": 1},
    ],
    "pathology": [{"kind": "pathology_report", "version": 1}],
    "radiology": [{"kind": "imaging_report", "version": 1}],
    "fertility_counseling": [{"kind": "preference_record", "version": 1}],
}


class ApiTestCase(unittest.TestCase):
    clock_now = 1_700_000_000.0

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mdt.db")
        self.server, self.service, self.store = create_server(
            self.db_path, "127.0.0.1", 0, clock=lambda: type(self).clock_now
        )
        seed(self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ 工具

    def req(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict | None = None,
        headers: dict | None = None,
        raw: bool = False,
    ):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if headers:
            h.update(headers)
        request = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload, dict(resp.headers)
                return resp.status, json.loads(payload or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if raw:
                return exc.code, payload, dict(exc.headers)
            return exc.code, json.loads(payload or b"null")

    def provision_user(self, user_id: str, role: str, disciplines, signer: bool = False):
        token = f"tok-{user_id}"
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO users (user_id, display_name, role, authorized_signer, active) VALUES (?,?,?,?,1)",
                (user_id, user_id, role, 1 if signer else 0),
            )
            for d in disciplines or []:
                c.execute(
                    "INSERT INTO user_disciplines (user_id, discipline) VALUES (?,?)",
                    (user_id, d),
                )
            c.execute("INSERT INTO tokens (token, user_id) VALUES (?,?)", (token, user_id))
        return token

    def advance(self, seconds: float):
        type(self).clock_now += seconds

    # ------------------------------------------------------------ 装配助手

    def setup_case_with_evidence(self, case: str = CASE):
        st, _ = self.req("POST", "/api/v1/cases", TOK["coord"], {"case_key": case})
        self.assertEqual(st, 201)
        for uid, disc in [
            ("u-surg", "surgery"),
            ("u-path", "pathology"),
            ("u-rad", "radiology"),
            ("u-fert", "fertility_counseling"),
        ]:
            st, _ = self.req(
                "POST", f"/api/v1/cases/{case}/assignments", TOK["coord"],
                {"user_id": uid, "discipline": disc},
            )
            self.assertEqual(st, 201)
        st, _ = self.req(
            "POST", f"/api/v1/cases/{case}/patient-links", TOK["coord"],
            {"patient_ref": "p-0001"},
        )
        self.assertEqual(st, 201)
        for kind, title, content, sens in [
            ("pathology_report", "病理报告 v1", "path-v1", False),
            ("imaging_report", "影像报告 v1", "image-v1", False),
            ("preference_record", "生育意愿 v1", "希望保留生育功能", True),
            ("risk_review", "手术风险 v1", "risk-v1", False),
        ]:
            st, ev = self.req(
                "POST", f"/api/v1/cases/{case}/evidence", TOK["coord"],
                {"kind": kind, "title": title, "content": content, "sensitive": sens},
            )
            self.assertEqual(st, 201, ev)
            self.assertEqual(ev["version"], 1)

    def schedule_and_opine(self, case: str = CASE, *, emergency: bool = False):
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{case}/meetings", TOK["coord"],
            {"required": ALL_DISCIPLINES, "emergency": emergency},
        )
        self.assertEqual(st, 201, meeting)
        mid = meeting["meeting_id"]
        for who, disc in [("surg", "surgery"), ("path", "pathology"), ("rad", "radiology"), ("fert", "fertility_counseling")]:
            st, op = self.req(
                "POST", f"/api/v1/meetings/{mid}/opinions", TOK[who],
                {
                    "discipline": disc,
                    "body": f"{disc} 专业意见（医生记录原文，非系统生成）",
                    "evidence_refs": REFS_V1[disc],
                    "coi_disclosed": True,
                },
            )
            self.assertEqual(st, 201, op)
            self.assertEqual(op["state"], "confirmed")
        return mid

    def form_issue_consent(self, mid: int):
        st, plan = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 201, plan)
        st, issued = self.req(
            "POST", f"/api/v1/candidate-plans/{plan['candidate_id']}/issue",
            TOK["surg"], {},
        )
        self.assertEqual(st, 201, issued)
        st, consent = self.req(
            "POST", f"/api/v1/issued-plans/{issued['plan_id']}/consent",
            TOK["patient"], {"patient_ref": "p-0001"},
        )
        self.assertEqual(st, 201, consent)
        return plan["candidate_id"], issued["plan_id"]

    # =================================================================
    # 1. 并发签发：同一候选方案只能被成功签发一次
    # =================================================================

    def test_concurrent_signing_only_one_wins(self):
        self.setup_case_with_evidence()
        # 第二位授权签发外科医生也参与该病例
        st, _ = self.req(
            "POST", f"/api/v1/cases/{CASE}/assignments", TOK["coord"],
            {"user_id": "u-surg2", "discipline": "surgery"},
        )
        self.assertEqual(st, 201)
        mid = self.schedule_and_opine()
        st, plan = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 201)
        cid = plan["candidate_id"]
        barrier = threading.Barrier(2)

        def issue(tok):
            barrier.wait()
            return self.req("POST", f"/api/v1/candidate-plans/{cid}/issue", tok, {})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(issue, [TOK["surg"], TOK["surg2"]]))
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [201, 409], results)
        winner = next(r[1] for r in results if r[0] == 201)
        loser = next(r[1] for r in results if r[0] == 409)
        self.assertEqual(loser["error"], "concurrent_issue")
        # 库里只有一条签发记录
        st, got = self.req("GET", f"/api/v1/candidate-plans/{cid}", TOK["coord"])
        self.assertEqual(st, 200)
        self.assertEqual(got["status"], "issued")
        self.assertIn(winner["signer_id"], ("u-surg", "u-surg2"))

    # =================================================================
    # 2. 重复提交：无幂等键拒绝；同键同体重放；同键异体拒绝
    # =================================================================

    def test_duplicate_opinion_rejected(self):
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        # 再来一条外科意见 → 409
        st, err = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["surg"],
            {"discipline": "surgery", "body": "重复意见",
             "evidence_refs": REFS_V1["surgery"], "coi_disclosed": True},
        )
        self.assertEqual(st, 409)
        self.assertEqual(err["error"], "duplicate_opinion")
        st, ops = self.req("GET", f"/api/v1/meetings/{mid}/opinions", TOK["coord"])
        self.assertEqual(len(ops), 4)

    def test_idempotency_key_replay_and_reuse_guard(self):
        self.setup_case_with_evidence()
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ALL_DISCIPLINES},
        )
        mid = meeting["meeting_id"]
        body = {
            "discipline": "pathology",
            "body": "病理意见（幂等测试）",
            "evidence_refs": [{"kind": "pathology_report", "version": 1}],
            "coi_disclosed": True,
        }
        st, first = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["path"], body,
            headers={"Idempotency-Key": "idem-001"},
        )
        self.assertEqual(st, 201)
        st, replay = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["path"], body,
            headers={"Idempotency-Key": "idem-001"},
        )
        self.assertEqual(st, 201)
        self.assertEqual(replay["opinion_id"], first["opinion_id"])
        st, ops = self.req("GET", f"/api/v1/meetings/{mid}/opinions", TOK["coord"])
        self.assertEqual(len(ops), 1)
        # 同键不同请求体 → 409
        body2 = dict(body, body="被改写过的正文")
        st, err = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["path"], body2,
            headers={"Idempotency-Key": "idem-001"},
        )
        self.assertEqual(st, 409)
        self.assertEqual(err["error"], "idempotency_reuse")

    # =================================================================
    # 3. 材料改版：旧意见/候选/签发/同意全部失效，重新确认后可再形成结论
    # =================================================================

    def test_evidence_revision_invalidates_and_requires_reconfirm(self):
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        cid, pid = self.form_issue_consent(mid)

        # 影像材料改版 v2
        st, ev = self.req(
            "POST", f"/api/v1/cases/{CASE}/evidence", TOK["coord"],
            {"kind": "imaging_report", "title": "影像报告 v2", "content": "image-v2"},
        )
        self.assertEqual(st, 201)
        self.assertEqual(ev["version"], 2)
        self.assertEqual(ev["active"], 1)

        # 会议视图：影像与外科意见失效，方案未齐备
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertEqual(st, 200)
        self.assertFalse(view["readiness"]["ready"])
        self.assertEqual(set(view["readiness"]["missing"]), {"radiology", "surgery"})
        states = {o["discipline"]: o["state"] for o in view["opinions"]}
        self.assertEqual(states["radiology"], "invalidated")
        self.assertEqual(states["surgery"], "invalidated")
        self.assertEqual(states["pathology"], "confirmed")
        self.assertEqual(view["candidate_plan"]["status"], "invalidated")
        self.assertIn("imaging_report@v2", view["candidate_plan"]["invalidate_reason"])
        self.assertEqual(view["evidence_versions"]["imaging_report"], 2)

        # 已签发方案与旧同意失效
        st, issued = self.req("GET", f"/api/v1/issued-plans/{pid}", TOK["coord"])
        self.assertEqual(issued["status"], "superseded")
        self.assertEqual(issued["consent"]["status"], "superseded")

        # 未齐备时不能形成候选
        st, err = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 409)
        self.assertEqual(err["error"], "not_ready")

        # 引用旧版本的重新提交被拒绝
        st, err = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["rad"],
            {"discipline": "radiology", "body": "仍引用旧影像",
             "evidence_refs": [{"kind": "imaging_report", "version": 1}],
             "coi_disclosed": True},
        )
        self.assertEqual(st, 409)

        # 受影响专业基于 v2 重新确认
        st, _ = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["rad"],
            {"discipline": "radiology", "body": "影像意见基于 v2",
             "evidence_refs": [{"kind": "imaging_report", "version": 2}],
             "coi_disclosed": True},
        )
        self.assertEqual(st, 201)
        surg_refs = [
            {"kind": "pathology_report", "version": 1},
            {"kind": "imaging_report", "version": 2},
            {"kind": "preference_record", "version": 1},
            {"kind": "risk_review", "version": 1},
        ]
        st, _ = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["surg"],
            {"discipline": "surgery", "body": "外科意见基于 v2",
             "evidence_refs": surg_refs, "coi_disclosed": True},
        )
        self.assertEqual(st, 201)
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertTrue(view["readiness"]["ready"], view["readiness"])

        # 形成新一版候选，快照固化 v2
        st, plan2 = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 201, plan2)
        self.assertNotEqual(plan2["candidate_id"], cid)
        self.assertEqual(plan2["snapshot"]["evidence_versions"]["imaging_report"], 2)
        st, issued2 = self.req(
            "POST", f"/api/v1/candidate-plans/{plan2['candidate_id']}/issue", TOK["surg"], {}
        )
        self.assertEqual(st, 201)
        st, consent = self.req(
            "POST", f"/api/v1/issued-plans/{issued2['plan_id']}/consent",
            TOK["patient"], {"patient_ref": "p-0001"},
        )
        self.assertEqual(st, 201)
        self.assertEqual(consent["status"], "granted")
        self.assertEqual(consent["preference_version"], 1)

    # =================================================================
    # 4. 授权委托：意见代提交与签发委托，撤销/无委托即拒绝
    # =================================================================

    def test_opinion_delegation_flow(self):
        self.setup_case_with_evidence()
        tok_path2 = self.provision_user("u-path2", "specialist", ["pathology"])
        # 分配到病例才有访问权
        st, _ = self.req(
            "POST", f"/api/v1/cases/{CASE}/assignments", TOK["coord"],
            {"user_id": "u-path2", "discipline": "pathology"},
        )
        self.assertEqual(st, 201)
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ALL_DISCIPLINES},
        )
        mid = meeting["meeting_id"]
        body = {
            "discipline": "pathology",
            "body": "受委托代提交的病理意见",
            "evidence_refs": [{"kind": "pathology_report", "version": 1}],
            "coi_disclosed": True,
            "on_behalf_of": "u-path",
        }
        # 尚无委托 → 403
        st, err = self.req("POST", f"/api/v1/meetings/{mid}/opinions", tok_path2, body)
        self.assertEqual(st, 403, err)
        # u-path 授予病例级、病理专业的意见委托
        st, d = self.req(
            "POST", "/api/v1/delegations", TOK["path"],
            {"grantee_id": "u-path2", "scope": "opinion",
             "discipline": "pathology", "case_key": CASE, "ttl_seconds": 3600},
        )
        self.assertEqual(st, 201, d)
        st, op = self.req("POST", f"/api/v1/meetings/{mid}/opinions", tok_path2, body)
        self.assertEqual(st, 201, op)
        self.assertEqual(op["author_id"], "u-path2")
        self.assertEqual(op["on_behalf_of"], "u-path")
        # 撤销后不能再次代提交（新会议）
        st, _ = self.req("POST", f"/api/v1/delegations/{d['delegation_id']}/revoke", TOK["path"], {})
        self.assertEqual(st, 200)
        st, meeting2 = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ["pathology"]},
        )
        body["evidence_refs"] = [{"kind": "pathology_report", "version": 1}]
        st, err = self.req(
            "POST", f"/api/v1/meetings/{meeting2['meeting_id']}/opinions", tok_path2, body
        )
        self.assertEqual(st, 403)

    def test_sign_delegation_requires_authorization(self):
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        st, plan = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        cid = plan["candidate_id"]
        # 第三位授权外科医生：未获委托时代签发 → 403
        tok_surg3 = self.provision_user("u-surg3", "specialist", ["surgery"], signer=True)
        self.req("POST", f"/api/v1/cases/{CASE}/assignments", TOK["coord"],
                 {"user_id": "u-surg3", "discipline": "surgery"})
        st, err = self.req(
            "POST", f"/api/v1/candidate-plans/{cid}/issue", tok_surg3,
            {"on_behalf_of": "u-surg"},
        )
        self.assertEqual(st, 403, err)
        # 非授权签发医生不能被授予 sign 委托
        st, err = self.req(
            "POST", "/api/v1/delegations", TOK["surg"],
            {"grantee_id": "u-path", "scope": "sign", "case_key": CASE},
        )
        self.assertEqual(st, 422, err)
        # 正式委托后代签发成功
        st, d = self.req(
            "POST", "/api/v1/delegations", TOK["surg"],
            {"grantee_id": "u-surg3", "scope": "sign", "case_key": CASE},
        )
        self.assertEqual(st, 201)
        st, issued = self.req(
            "POST", f"/api/v1/candidate-plans/{cid}/issue", tok_surg3,
            {"on_behalf_of": "u-surg"},
        )
        self.assertEqual(st, 201, issued)
        self.assertEqual(issued["signer_id"], "u-surg3")
        self.assertEqual(issued["on_behalf_of"], "u-surg")

    # =================================================================
    # 5. 同意撤回 & 生育意愿变更：旧签名不得沿用
    # =================================================================

    def test_consent_withdrawal_blocks_old_signature(self):
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        _, pid = self.form_issue_consent(mid)
        st, out = self.req("POST", f"/api/v1/issued-plans/{pid}/consent/withdraw", TOK["patient"], {})
        self.assertEqual(st, 200, out)
        self.assertEqual(out["status"], "consent_withdrawn")
        self.assertEqual(out["consents"][-1]["status"], "withdrawn")
        # 撤回后不能再对同一方案“补同意”
        st, err = self.req(
            "POST", f"/api/v1/issued-plans/{pid}/consent",
            TOK["patient"], {"patient_ref": "p-0001"},
        )
        self.assertEqual(st, 409, err)

    def test_preference_change_supersedes_consent(self):
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        _, pid = self.form_issue_consent(mid)
        # 患者本人改变生育意愿 → 旧共识/旧签名失效
        st, ev = self.req(
            "POST", f"/api/v1/cases/{CASE}/preference-change", TOK["patient"],
            {"title": "生育意愿 v2", "content": "经再次咨询后调整意愿"},
        )
        self.assertEqual(st, 201, ev)
        self.assertEqual(ev["version"], 2)
        st, issued = self.req("GET", f"/api/v1/issued-plans/{pid}", TOK["patient"])
        self.assertEqual(st, 200)
        self.assertEqual(issued["status"], "superseded")
        self.assertEqual(issued["consent"]["status"], "superseded")
        # 不能就已失效方案再次同意
        st, err = self.req(
            "POST", f"/api/v1/issued-plans/{pid}/consent",
            TOK["patient"], {"patient_ref": "p-0001"},
        )
        self.assertEqual(st, 409)

    # =================================================================
    # 6. 紧急会议缺席例外：限时补审、超时留痕但不计齐备
    # =================================================================

    def test_emergency_absence_on_time_and_late_review(self):
        self.setup_case_with_evidence()
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ALL_DISCIPLINES, "emergency": True},
        )
        mid = meeting["meeting_id"]
        # 非紧急会议不能登记缺席例外
        st, normal = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ["surgery"]},
        )
        st, err = self.req(
            "POST", f"/api/v1/meetings/{normal['meeting_id']}/absence-exceptions",
            TOK["coord"], {"discipline": "surgery", "reason": "抢救"},
        )
        self.assertEqual(st, 422)

        # 外科/影像/生育到场，病理缺席
        for who, disc in [("surg", "surgery"), ("rad", "radiology"), ("fert", "fertility_counseling")]:
            st, _ = self.req(
                "POST", f"/api/v1/meetings/{mid}/opinions", TOK[who],
                {"discipline": disc, "body": f"{disc} 意见",
                 "evidence_refs": REFS_V1[disc], "coi_disclosed": True},
            )
            self.assertEqual(st, 201)
        st, ex = self.req(
            "POST", f"/api/v1/meetings/{mid}/absence-exceptions", TOK["coord"],
            {"discipline": "pathology", "reason": "急诊手术", "review_window_seconds": 3600},
        )
        self.assertEqual(st, 201, ex)
        eid = ex["exception_id"]
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertFalse(view["readiness"]["ready"])

        # 窗口内补审通过 → 齐备
        self.advance(10)
        st, reviewed = self.req(
            "POST", f"/api/v1/absence-exceptions/{eid}/review", TOK["path"],
            {"approve": True, "note": "会后阅片无异议"},
        )
        self.assertEqual(st, 200, reviewed)
        self.assertEqual(reviewed["state"], "approved")
        self.assertEqual(reviewed["late"], 0)
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertTrue(view["readiness"]["ready"], view["readiness"])
        self.assertIn("pathology", view["readiness"]["excused"])
        # 例外覆盖视同齐备，可形成并签发
        st, plan = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 201, plan)
        self.assertIn("pathology", plan["snapshot"]["excused"])
        st, issued = self.req(
            "POST", f"/api/v1/candidate-plans/{plan['candidate_id']}/issue", TOK["surg"], {}
        )
        self.assertEqual(st, 201, issued)

    def test_emergency_absence_late_review_not_counted(self):
        self.setup_case_with_evidence()
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ALL_DISCIPLINES, "emergency": True},
        )
        mid = meeting["meeting_id"]
        for who, disc in [("surg", "surgery"), ("rad", "radiology"), ("fert", "fertility_counseling")]:
            self.req("POST", f"/api/v1/meetings/{mid}/opinions", TOK[who],
                     {"discipline": disc, "body": f"{disc} 意见",
                      "evidence_refs": REFS_V1[disc], "coi_disclosed": True})
        st, ex = self.req(
            "POST", f"/api/v1/meetings/{mid}/absence-exceptions", TOK["coord"],
            {"discipline": "pathology", "reason": "急诊手术", "review_window_seconds": 3600},
        )
        eid = ex["exception_id"]
        self.advance(3601)
        # 超时补审仍被留痕，但 late=1
        st, reviewed = self.req(
            "POST", f"/api/v1/absence-exceptions/{eid}/review", TOK["path"],
            {"approve": True, "note": "迟到的补审"},
        )
        self.assertEqual(st, 200, reviewed)
        self.assertEqual(reviewed["late"], 1)
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertFalse(view["readiness"]["ready"], view["readiness"])
        self.assertTrue(view["readiness"]["late_reviews"])
        # 仍然不能形成候选
        st, err = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 409)
        # 病理医生事后亲自到会确认：超时事实仍留痕，但专业齐备性恢复
        st, _ = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["path"],
            {"discipline": "pathology", "body": "迟到但亲自补确认",
             "evidence_refs": REFS_V1["pathology"], "coi_disclosed": True},
        )
        self.assertEqual(st, 201)
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertTrue(view["readiness"]["ready"], view["readiness"])
        self.assertTrue(view["readiness"]["late_reviews"], "超时补审记录应保留")

    # =================================================================
    # 7. 重启恢复：补审逾期自动 overdue，数据不丢
    # =================================================================

    def test_restart_recovery_marks_overdue_and_keeps_data(self):
        self.setup_case_with_evidence()
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ALL_DISCIPLINES, "emergency": True},
        )
        mid = meeting["meeting_id"]
        st, ex = self.req(
            "POST", f"/api/v1/meetings/{mid}/absence-exceptions", TOK["coord"],
            {"discipline": "pathology", "reason": "院外会诊", "review_window_seconds": 3600},
        )
        eid = ex["exception_id"]
        # 关闭服务，时钟走过补审期限后用同一数据库重启
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.advance(3601)
        server2, service2, store2 = create_server(
            self.db_path, "127.0.0.1", 0, clock=lambda: type(self).clock_now
        )
        try:
            self.assertGreaterEqual(service2.recover_on_startup()["expired_exceptions"], 0)
            row = service2.get_exception(eid)
            self.assertEqual(row["state"], "overdue")
            # 病例与会议数据仍在
            self.assertEqual(service2.get_case({"user_id": "-"}, CASE)["case_key"], CASE)
            got = service2.get_meeting(mid)
            self.assertEqual(sorted(got["required"]), sorted(ALL_DISCIPLINES))
        finally:
            server2.server_close()
            store2.close()

    # =================================================================
    # 利益冲突未披露 → 不能形成候选
    # =================================================================
    def test_undisclosed_coi_blocks_plan(self):
        self.setup_case_with_evidence()
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ["surgery", "pathology"]},
        )
        mid = meeting["meeting_id"]
        st, _ = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["surg"],
            {"discipline": "surgery", "body": "外科意见",
             "evidence_refs": REFS_V1["surgery"], "coi_disclosed": False},
        )
        self.assertEqual(st, 201)
        st, _ = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["path"],
            {"discipline": "pathology", "body": "病理意见",
             "evidence_refs": REFS_V1["pathology"], "coi_disclosed": True},
        )
        self.assertEqual(st, 201)
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        self.assertFalse(view["readiness"]["coi_ok"])
        self.assertIn("surgery", view["readiness"]["undisclosed"])
        st, err = self.req("POST", f"/api/v1/meetings/{mid}/candidate-plans", TOK["coord"], {})
        self.assertEqual(st, 409)

    def test_cross_discipline_opinion_forbidden(self):
        self.setup_case_with_evidence()
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ["surgery", "pathology"]},
        )
        mid = meeting["meeting_id"]
        # 外科医生不能提交病理专业意见
        st, err = self.req(
            "POST", f"/api/v1/meetings/{mid}/opinions", TOK["surg"],
            {"discipline": "pathology", "body": "越专业提交",
             "evidence_refs": REFS_V1["pathology"], "coi_disclosed": True},
        )
        self.assertEqual(st, 403)

    def test_expired_delegation_rejected(self):
        self.setup_case_with_evidence()
        tok_path2 = self.provision_user("u-path2", "specialist", ["pathology"])
        self.req("POST", f"/api/v1/cases/{CASE}/assignments", TOK["coord"],
                 {"user_id": "u-path2", "discipline": "pathology"})
        st, d = self.req(
            "POST", "/api/v1/delegations", TOK["path"],
            {"grantee_id": "u-path2", "scope": "opinion",
             "discipline": "pathology", "case_key": CASE, "ttl_seconds": 60},
        )
        self.assertEqual(st, 201)
        st, meeting = self.req(
            "POST", f"/api/v1/cases/{CASE}/meetings", TOK["coord"],
            {"required": ["pathology"]},
        )
        mid = meeting["meeting_id"]
        body = {"discipline": "pathology", "body": "委托代提交",
                "evidence_refs": REFS_V1["pathology"],
                "coi_disclosed": True, "on_behalf_of": "u-path"}
        self.advance(61)
        st, err = self.req("POST", f"/api/v1/meetings/{mid}/opinions", tok_path2, body)
        self.assertEqual(st, 403, err)

    # =================================================================
    # 细粒度权限与审计
    # =================================================================

    def test_permission_matrix_and_audit(self):
        self.setup_case_with_evidence()
        # 未认证
        st, err = self.req("GET", "/api/v1/cases")
        self.assertEqual(st, 401)
        # 专科医生不能登记病例
        st, err = self.req("POST", "/api/v1/cases", TOK["surg"], {"case_key": "CASE-AAAAAAAAAAAAAAAA"})
        self.assertEqual(st, 403)
        # 审计员只读
        st, err = self.req(
            "POST", f"/api/v1/cases/{CASE}/evidence", TOK["audit"],
            {"kind": "risk_review", "title": "x", "content": "y"},
        )
        self.assertEqual(st, 403)
        # 患者不能浏览病例
        st, err = self.req("GET", f"/api/v1/cases/{CASE}", TOK["patient"])
        self.assertEqual(st, 403)
        # 未被分配到该病例的专科医生无权访问
        tok_other = self.provision_user("u-other", "specialist", ["surgery"])
        st, err = self.req("GET", f"/api/v1/cases/{CASE}", tok_other)
        self.assertEqual(st, 403)
        st, err = self.req("GET", "/api/v1/audit", TOK["surg"])
        self.assertEqual(st, 403)

        # 触发敏感字段访问（full=1）与导出，二者必须留审计
        st, rows = self.req("GET", f"/api/v1/cases/{CASE}/evidence?full=1", TOK["coord"])
        self.assertEqual(st, 200)
        self.assertTrue(any(r["sensitive"] for r in rows))
        st, _ = self.req("GET", f"/api/v1/cases/{CASE}/export", TOK["coord"])
        self.assertEqual(st, 200)
        st, audit = self.req("GET", f"/api/v1/audit?case_key={CASE}", TOK["audit"])
        self.assertEqual(st, 200)
        sensitive_rows = [r for r in audit if r["sensitive"] == 1]
        self.assertTrue(sensitive_rows, "敏感字段访问必须审计")
        actions = " ".join(r["action"] for r in audit)
        self.assertIn("/evidence", actions)
        self.assertIn("/export", actions)
        # 拒绝访问也应留痕
        denied = [r for r in audit if r["result"] == "denied"]
        self.assertTrue(denied)

    def test_patient_consent_requires_link(self):
        # 另一个未关联患者不能同意
        tok_p2 = self.provision_user("p-0002", "patient", [])
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        self.form_issue_consent(mid)
        st, view = self.req("GET", f"/api/v1/meetings/{mid}", TOK["coord"])
        pid = view["issued_plan"]["plan_id"]
        st, err = self.req(
            "POST", f"/api/v1/issued-plans/{pid}/consent", tok_p2, {"patient_ref": "p-0002"}
        )
        self.assertEqual(st, 403)

    # =================================================================
    # 导出：会议纪要 + 决定沿革，明确证据版本与确认状态
    # =================================================================

    def test_export_json_and_markdown(self):
        self.setup_case_with_evidence()
        mid = self.schedule_and_opine()
        self.form_issue_consent(mid)
        st, data = self.req("GET", f"/api/v1/cases/{CASE}/export?format=json", TOK["audit"])
        self.assertEqual(st, 200)
        self.assertEqual(data["evidence_versions"]["imaging_report"], 1)
        block = data["meetings"][0]
        self.assertEqual(len(block["opinions"]), 4)
        self.assertTrue(block["readiness"]["ready"])
        self.assertTrue(block["candidate_plans"])
        snap = block["candidate_plans"][0]["snapshot"]
        self.assertEqual(snap["evidence_versions"]["pathology_report"], 1)
        self.assertEqual({o["state"] for o in snap["opinions"]}, {"confirmed"})
        types = {e["type"] for e in data["timeline"]}
        self.assertIn("issue", types)
        self.assertIn("consent", types)
        # 全结构不得出现诊断/处方类字段
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    self.assertNotIn(k, ("diagnosis", "prescription", "recommendation",
                                        "auto_diagnosis", "treatment_plan_text"))
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data)

        st, md_bytes, headers = self.req(
            "GET", f"/api/v1/cases/{CASE}/export?format=md", TOK["coord"], raw=True
        )
        self.assertEqual(st, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        md = md_bytes.decode()
        self.assertIn(CASE, md)
        self.assertIn("已确认", md)
        self.assertIn("证据版本", md)
        self.assertIn("不构成自动诊断或处方", md)

    def test_healthz(self):
        st, body = self.req("GET", "/healthz")
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
