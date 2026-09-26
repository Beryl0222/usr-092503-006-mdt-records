"""端到端 API 测试：真实 HTTP 服务 + 临时 SQLite 库。

覆盖：细粒度权限、重复提交幂等、并发签署唯一胜者、材料改版级联失效、
授权委托（授予/使用/撤销/过期）、知情同意撤回与生育意愿变更、
紧急会议缺席例外限时补审、超时补审、重启恢复、审计与导出、
去标识化与反自动诊断守卫。
"""

import concurrent.futures
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from src.mdt_records.server import create_server

CASE_KEY = "CASE-0123456789ABCDEF"
CASE_KEY_2 = "CASE-FEDCBA9876543210"

USERS = {
    "coord": "coord1",
    "signatory": "surg1",
    "surgeon2": "surg2",
    "path": "path1",
    "rad": "rad1",
    "fert": "fert1",
    "auditor": "auditor1",
}

OPINION_TEXT = {
    "surgery": "外科意见：基于当前材料的人工评估记录",
    "pathology": "病理意见：基于当前病理报告的人工评估记录",
    "radiology": "影像意见：基于当前影像资料的人工评估记录",
    "fertility_counseling": "生育咨询意见：结合患者生育意愿的人工评估记录",
}


class FakeClock:
    def __init__(self):
        self._t = datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self._t

    def advance(self, **kwargs):
        self._t += timedelta(**kwargs)


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdt-test-")
        self.db_path = f"{self.tmp}/mdt.db"
        self.clock = FakeClock()
        self._start_server()

    def _start_server(self):
        self.server = create_server(
            self.db_path, clock=self.clock, exception_ttl=timedelta(hours=1)
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.service.store.close()

    def tearDown(self):
        self._stop_server()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------- HTTP 辅助 ----------------
    def api(self, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw.decode("utf-8")) if raw else {}

    def token(self, user_id):
        if not hasattr(self, "_tokens"):
            self._tokens = {}
        if user_id not in self._tokens:
            status, data = self.api(
                "POST", "/sessions", body={"user_id": user_id, "secret": f"secret-{user_id}"}
            )
            self.assertEqual(status, 201, data)
            self._tokens[user_id] = data["token"]
        return self._tokens[user_id]

    # ---------------- 流程辅助 ----------------
    def create_case(self, key=CASE_KEY, preference="preserve"):
        status, data = self.api(
            "POST",
            "/cases",
            token=self.token(USERS["coord"]),
            body={"case_key": key, "fertility_preference": preference},
        )
        self.assertEqual(status, 201, data)
        return data

    def add_material(self, key, kind, content):
        status, data = self.api(
            "POST",
            f"/cases/{key}/materials",
            token=self.token(USERS["coord"]),
            body={"kind": kind, "content": content},
        )
        self.assertEqual(status, 201, data)
        return data

    def upload_all_materials(self, key=CASE_KEY):
        self.add_material(key, "pathology_report", {"summary": "病理报告v1", "figo": "IB1"})
        self.add_material(key, "imaging_report", {"summary": "影像报告v1", "lesion_mm": 12})
        self.add_material(
            key, "preference_record", {"summary": "意愿记录v1", "fertility_preference": "preserve"}
        )
        self.add_material(key, "risk_review", {"summary": "手术风险评估v1"})

    def submit_opinion(self, user_key, key, discipline, confirm=True, coi=True):
        return self.api(
            "POST",
            f"/cases/{key}/opinions",
            token=self.token(USERS[user_key]),
            body={
                "discipline": discipline,
                "content": OPINION_TEXT[discipline],
                "coi_disclosed": coi,
                "coi_detail": "无利益冲突" if coi else "",
                "confirm": confirm,
            },
        )

    def confirm_all_disciplines(self, key=CASE_KEY):
        opinion_ids = {}
        for user_key, discipline in [
            ("signatory", "surgery"),
            ("path", "pathology"),
            ("rad", "radiology"),
            ("fert", "fertility_counseling"),
        ]:
            status, data = self.submit_opinion(user_key, key, discipline)
            self.assertEqual(status, 201, data)
            opinion_ids[discipline] = data["opinion"]["id"]
        return opinion_ids

    def create_meeting(self, key=CASE_KEY, kind="regular", absent=None):
        body = {"kind": kind}
        if absent:
            body["absent_disciplines"] = absent
        status, data = self.api(
            "POST", f"/cases/{key}/meetings", token=self.token(USERS["coord"]), body=body
        )
        self.assertEqual(status, 201, data)
        return data

    def prepare_case(self, key=CASE_KEY):
        """登记病例、上传全部材料、四专业确认、常规会议 → 可形成候选方案。"""
        self.create_case(key)
        self.upload_all_materials(key)
        opinion_ids = self.confirm_all_disciplines(key)
        self.create_meeting(key)
        return opinion_ids

    def form_candidate(self, key=CASE_KEY):
        return self.api(
            "POST", f"/cases/{key}/candidate", token=self.token(USERS["coord"]), body={}
        )

    def issue_plan(self, user_key, key, candidate_id, content="最终方案：授权医生人工签发文本"):
        return self.api(
            "POST",
            f"/cases/{key}/plans/{candidate_id}/issue",
            token=self.token(USERS[user_key]),
            body={"content": content},
        )

    def prepare_issued_plan(self, key=CASE_KEY):
        """走完到签发方案的全流程，返回 (candidate_id, plan_id)。"""
        self.prepare_case(key)
        status, data = self.form_candidate(key)
        self.assertEqual(status, 201, data)
        candidate_id = data["candidate"]["id"]
        status, data = self.issue_plan("signatory", key, candidate_id)
        self.assertEqual(status, 201, data)
        return candidate_id, data["plan"]["id"]


class AuthAndPermissionTests(ApiTestBase):
    def test_unauthenticated_rejected(self):
        status, data = self.api("GET", f"/cases/{CASE_KEY}/status")
        self.assertEqual(status, 401)
        status, data = self.api("GET", f"/cases/{CASE_KEY}/status", token="bad-token")
        self.assertEqual(status, 401)

    def test_wrong_credentials_rejected(self):
        status, _ = self.api(
            "POST", "/sessions", body={"user_id": "coord1", "secret": "wrong"}
        )
        self.assertEqual(status, 401)

    def test_role_based_permissions(self):
        # 医师无权登记病例
        status, _ = self.api(
            "POST",
            "/cases",
            token=self.token(USERS["path"]),
            body={"case_key": CASE_KEY, "fertility_preference": "preserve"},
        )
        self.assertEqual(status, 403)
        # 审计员无权登记病例
        status, _ = self.api(
            "POST",
            "/cases",
            token=self.token(USERS["auditor"]),
            body={"case_key": CASE_KEY, "fertility_preference": "preserve"},
        )
        self.assertEqual(status, 403)
        self.create_case()
        self.upload_all_materials()
        # 影像医师不能提交病理意见
        status, _ = self.submit_opinion("rad", CASE_KEY, "pathology")
        self.assertEqual(status, 403)
        # 审计员不能提交意见
        status, _ = self.api(
            "POST",
            f"/cases/{CASE_KEY}/opinions",
            token=self.token(USERS["auditor"]),
            body={"discipline": "pathology", "content": "x", "coi_disclosed": True},
        )
        self.assertEqual(status, 403)
        # 非审计员不能读审计日志
        status, _ = self.api("GET", "/audit", token=self.token(USERS["coord"]))
        self.assertEqual(status, 403)
        status, data = self.api("GET", "/audit", token=self.token(USERS["auditor"]))
        self.assertEqual(status, 200)
        # 医师不能导出会议纪要
        status, _ = self.api(
            "GET", f"/cases/{CASE_KEY}/minutes", token=self.token(USERS["path"])
        )
        self.assertEqual(status, 403)

    def test_case_key_validation(self):
        status, data = self.api(
            "POST",
            "/cases",
            token=self.token(USERS["coord"]),
            body={"case_key": "CASE-bad", "fertility_preference": "preserve"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_case_key")


class GuardTests(ApiTestBase):
    def test_unknown_fields_rejected(self):
        status, data = self.api(
            "POST",
            "/cases",
            token=self.token(USERS["coord"]),
            body={
                "case_key": CASE_KEY,
                "fertility_preference": "preserve",
                "auto_diagnosis": "尝试注入",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "unknown_fields")

    def test_auto_diagnosis_content_rejected(self):
        self.create_case()
        self.upload_all_materials()
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/opinions",
            token=self.token(USERS["path"]),
            body={
                "discipline": "pathology",
                "content": "本结论由系统自动诊断生成",
                "coi_disclosed": True,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "auto_content_rejected")

    def test_deidentification_enforced(self):
        self.create_case()
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/materials",
            token=self.token(USERS["coord"]),
            body={"kind": "risk_review", "content": {"patient_name": "张三"}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "deidentification_violation")
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/materials",
            token=self.token(USERS["coord"]),
            body={"kind": "risk_review", "content": {"summary": "联系电话13800138000"}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "deidentification_violation")

    def test_responses_carry_no_autodiagnosis_notice(self):
        self.create_case()
        status, data = self.api(
            "GET", f"/cases/{CASE_KEY}/status", token=self.token(USERS["coord"])
        )
        self.assertEqual(status, 200)
        self.assertIn("不提供自动诊断或处方", data["notice"])


class CandidateGatingTests(ApiTestBase):
    def test_candidate_requires_meeting_opinions_and_coi(self):
        self.create_case()
        self.upload_all_materials()
        # 无会议、无意见 → 受阻
        status, data = self.form_candidate()
        self.assertEqual(status, 409)
        self.assertIn("no_meeting", data["error"]["message"])
        self.assertIn("awaiting_confirmation:surgery", data["error"]["message"])
        self.create_meeting()
        self.confirm_all_disciplines()
        # 已确认意见的披露不可撤回 → 409
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/opinions",
            token=self.token(USERS["path"]),
            body={
                "discipline": "pathology",
                "content": OPINION_TEXT["pathology"],
                "coi_disclosed": False,
                "confirm": True,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "coi_disclosure_locked")
        # 换一条新病例验证 COI 门槛
        key2 = CASE_KEY_2
        self.create_case(key2)
        self.upload_all_materials(key2)
        self.create_meeting(key2)
        for user_key, discipline in [
            ("signatory", "surgery"),
            ("rad", "radiology"),
            ("fert", "fertility_counseling"),
        ]:
            self.submit_opinion(user_key, key2, discipline)
        status, data = self.api(
            "POST",
            f"/cases/{key2}/opinions",
            token=self.token(USERS["path"]),
            body={
                "discipline": "pathology",
                "content": OPINION_TEXT["pathology"],
                "coi_disclosed": False,
                "confirm": True,
            },
        )
        self.assertEqual(status, 201)
        status, data = self.form_candidate(key2)
        self.assertEqual(status, 409)
        self.assertIn("coi_not_disclosed:pathology", data["error"]["message"])
        # 作者补充披露后放行
        status, data = self.submit_opinion("path", key2, "pathology", coi=True)
        self.assertEqual(status, 200)
        self.assertTrue(data["opinion"]["coi_disclosed"])
        status, data = self.form_candidate(key2)
        self.assertEqual(status, 201, data)

    def test_candidate_response_shows_evidence_and_confirmations(self):
        self.prepare_case()
        status, data = self.form_candidate()
        self.assertEqual(status, 201, data)
        candidate = data["candidate"]
        self.assertEqual(
            candidate["evidence_snapshot"],
            {
                "pathology_report": 1,
                "imaging_report": 1,
                "preference_record": 1,
                "risk_review": 1,
            },
        )
        for discipline in (
            "surgery",
            "pathology",
            "radiology",
            "fertility_counseling",
        ):
            entry = candidate["confirmations"][discipline]
            self.assertEqual(entry["state"], "confirmed")
            self.assertTrue(entry["coi_disclosed"])
            self.assertEqual(entry["evidence_snapshot"]["pathology_report"], 1)
        self.assertIn("不提供自动诊断或处方", data["notice"])

    def test_duplicate_candidate_submission_idempotent(self):
        self.prepare_case()
        status, first = self.form_candidate()
        self.assertEqual(status, 201)
        status, second = self.form_candidate()
        self.assertEqual(status, 200)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["candidate"]["id"], second["candidate"]["id"])


class DuplicateSubmissionTests(ApiTestBase):
    def test_repeat_submit_and_confirm_idempotent(self):
        self.create_case()
        self.upload_all_materials()
        status, first = self.submit_opinion("path", CASE_KEY, "pathology")
        self.assertEqual(status, 201)
        opinion_id = first["opinion"]["id"]
        # 重复提交（相同内容）→ 幂等返回原意见
        status, second = self.submit_opinion("path", CASE_KEY, "pathology")
        self.assertEqual(status, 200)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["opinion"]["id"], opinion_id)
        # 重复确认 → 幂等
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/opinions/{opinion_id}/confirm",
            token=self.token(USERS["path"]),
            body={},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["opinion"]["state"], "confirmed")
        # 已确认意见内容不可改
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/opinions",
            token=self.token(USERS["path"]),
            body={
                "discipline": "pathology",
                "content": "试图改写已确认意见",
                "coi_disclosed": True,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "opinion_already_confirmed")
        # 全库仅一条该专业 confirmed 意见
        status, data = self.api(
            "GET", f"/cases/{CASE_KEY}/opinions", token=self.token(USERS["coord"])
        )
        confirmed = [o for o in data["opinions"] if o["state"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)

    def test_discipline_confirmed_by_other_rejected(self):
        self.create_case()
        self.upload_all_materials()
        status, _ = self.submit_opinion("signatory", CASE_KEY, "surgery")
        self.assertEqual(status, 201)
        status, data = self.submit_opinion("surgeon2", CASE_KEY, "surgery")
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "discipline_already_confirmed")

    def test_draft_then_confirm_flow(self):
        self.create_case()
        self.upload_all_materials()
        status, data = self.submit_opinion("rad", CASE_KEY, "radiology", confirm=False)
        self.assertEqual(status, 201)
        self.assertEqual(data["opinion"]["state"], "draft")
        opinion_id = data["opinion"]["id"]
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/opinions/{opinion_id}/confirm",
            token=self.token(USERS["rad"]),
            body={},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["opinion"]["state"], "confirmed")
        # 他人不能确认该意见
        self.create_case(CASE_KEY_2)
        self.upload_all_materials(CASE_KEY_2)
        status, data = self.submit_opinion("rad", CASE_KEY_2, "radiology", confirm=False)
        draft_id = data["opinion"]["id"]
        status, _ = self.api(
            "POST",
            f"/cases/{CASE_KEY_2}/opinions/{draft_id}/confirm",
            token=self.token(USERS["path"]),
            body={},
        )
        self.assertEqual(status, 403)

    def test_concurrent_duplicate_submission_single_record(self):
        self.create_case()
        self.upload_all_materials()

        def submit():
            return self.submit_opinion("path", CASE_KEY, "pathology")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: submit(), range(6)))
        ids = {data["opinion"]["id"] for _, data in results}
        self.assertEqual(len(ids), 1, results)
        self.assertEqual(sum(1 for status, _ in results if status == 201), 1)
        status, data = self.api(
            "GET", f"/cases/{CASE_KEY}/opinions", token=self.token(USERS["coord"])
        )
        confirmed = [o for o in data["opinions"] if o["state"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)


class MaterialRevisionTests(ApiTestBase):
    def test_revision_invalidates_affected_disciplines_only(self):
        opinion_ids = self.prepare_case()
        status, data = self.form_candidate()
        self.assertEqual(status, 201)
        # 病理报告改版
        material = self.add_material(
            CASE_KEY, "pathology_report", {"summary": "病理报告v2", "figo": "IB2"}
        )
        self.assertEqual(material["version"], 2)
        self.assertEqual(
            set(material["invalidated_opinions"]),
            {opinion_ids["pathology"], opinion_ids["surgery"]},
        )
        # 状态：病理与外科失效，影像与生育咨询保持 confirmed
        status, state = self.api(
            "GET", f"/cases/{CASE_KEY}/status", token=self.token(USERS["coord"])
        )
        self.assertEqual(state["disciplines"]["pathology"]["state"], "invalidated")
        self.assertEqual(state["disciplines"]["surgery"]["state"], "invalidated")
        self.assertEqual(state["disciplines"]["radiology"]["state"], "confirmed")
        self.assertEqual(state["disciplines"]["fertility_counseling"]["state"], "confirmed")
        self.assertEqual(state["candidate_plan"]["status"], "superseded")
        self.assertFalse(state["ready_for_candidate"])
        self.assertIn("awaiting_confirmation:pathology", state["blocking_reasons"])
        self.assertIn("awaiting_confirmation:surgery", state["blocking_reasons"])
        # 未重确认前不能形成新候选
        status, _ = self.form_candidate()
        self.assertEqual(status, 409)
        # 受影响专业基于新版本重确认
        status, data = self.submit_opinion("path", CASE_KEY, "pathology")
        self.assertEqual(status, 201)
        self.assertEqual(data["opinion"]["evidence_snapshot"]["pathology_report"], 2)
        status, data = self.submit_opinion("signatory", CASE_KEY, "surgery")
        self.assertEqual(status, 201)
        # 新候选方案基于 v2 证据
        status, data = self.form_candidate()
        self.assertEqual(status, 201, data)
        self.assertEqual(data["candidate"]["evidence_snapshot"]["pathology_report"], 2)
        # 未受影响专业沿用原确认（无需重确认）
        self.assertEqual(
            data["candidate"]["confirmations"]["radiology"]["opinion_id"],
            opinion_ids["radiology"],
        )

    def test_revision_invalidates_issued_plan_and_consent(self):
        _, plan_id = self.prepare_issued_plan()
        status, consent = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 201)
        # 影像改版 → 签发方案与同意一并失效
        self.add_material(CASE_KEY, "imaging_report", {"summary": "影像报告v2", "lesion_mm": 18})
        status, state = self.api(
            "GET", f"/cases/{CASE_KEY}/status", token=self.token(USERS["coord"])
        )
        self.assertEqual(state["issued_plan"]["status"], "invalidated")
        self.assertEqual(state["consent"]["status"], "invalidated")
        self.assertEqual(state["consent"]["invalidated_reason"], "plan_invalidated")


class ConcurrentIssueTests(ApiTestBase):
    def test_concurrent_issue_single_winner(self):
        self.prepare_case()
        status, data = self.form_candidate()
        candidate_id = data["candidate"]["id"]
        # surg1 委托 surg2，两人并发签署同一候选方案
        status, _ = self.api(
            "POST",
            "/delegations",
            token=self.token(USERS["signatory"]),
            body={
                "delegate_id": USERS["surgeon2"],
                "case_key": CASE_KEY,
                "expires_in_seconds": 3600,
            },
        )
        self.assertEqual(status, 201)
        tokens = [self.token(USERS["signatory"]), self.token(USERS["surgeon2"])]

        def attempt(i):
            return self.api(
                "POST",
                f"/cases/{CASE_KEY}/plans/{candidate_id}/issue",
                token=tokens[i % 2],
                body={"content": "最终方案：授权医生人工签发文本"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses.count(201), 1, results)
        self.assertEqual(statuses.count(409), 7, results)
        winners = [data["plan"] for status, data in results if status == 201]
        self.assertEqual(len(winners), 1)
        # 全库仅一个 active 签发方案
        status, state = self.api(
            "GET", f"/cases/{CASE_KEY}/status", token=self.token(USERS["coord"])
        )
        self.assertEqual(state["issued_plan"]["status"], "active")
        self.assertEqual(state["issued_plan"]["id"], winners[0]["id"])


class DelegationTests(ApiTestBase):
    def test_delegate_issue_revoke_and_expiry(self):
        self.prepare_case()
        status, data = self.form_candidate()
        candidate_id = data["candidate"]["id"]
        # 无授权 → 403
        status, _ = self.issue_plan("surgeon2", CASE_KEY, candidate_id)
        self.assertEqual(status, 403)
        # 限定病例的委托 → 受托人可签发
        status, delegation = self.api(
            "POST",
            "/delegations",
            token=self.token(USERS["signatory"]),
            body={
                "delegate_id": USERS["surgeon2"],
                "case_key": CASE_KEY,
                "expires_in_seconds": 3600,
            },
        )
        self.assertEqual(status, 201)
        status, issued = self.issue_plan("surgeon2", CASE_KEY, candidate_id)
        self.assertEqual(status, 201, issued)
        self.assertEqual(issued["plan"]["via_delegation_id"], delegation["id"])
        # 委托不覆盖其他病例
        self.prepare_case(CASE_KEY_2)
        status, data = self.form_candidate(CASE_KEY_2)
        candidate2 = data["candidate"]["id"]
        status, _ = self.issue_plan("surgeon2", CASE_KEY_2, candidate2)
        self.assertEqual(status, 403)
        # 全局委托 + 撤销后失效
        status, delegation2 = self.api(
            "POST",
            "/delegations",
            token=self.token(USERS["signatory"]),
            body={"delegate_id": USERS["surgeon2"], "expires_in_seconds": 3600},
        )
        self.assertEqual(status, 201)
        status, _ = self.api(
            "POST",
            f"/delegations/{delegation2['id']}/revoke",
            token=self.token(USERS["signatory"]),
            body={},
        )
        self.assertEqual(status, 200)
        status, _ = self.issue_plan("surgeon2", CASE_KEY_2, candidate2)
        self.assertEqual(status, 403)
        # 过期委托失效
        status, delegation3 = self.api(
            "POST",
            "/delegations",
            token=self.token(USERS["signatory"]),
            body={
                "delegate_id": USERS["surgeon2"],
                "case_key": CASE_KEY_2,
                "expires_in_seconds": 1800,
            },
        )
        self.assertEqual(status, 201)
        self.clock.advance(hours=1)
        status, _ = self.issue_plan("surgeon2", CASE_KEY_2, candidate2)
        self.assertEqual(status, 403)
        # 签发人本人始终可签发
        status, issued = self.issue_plan("signatory", CASE_KEY_2, candidate2)
        self.assertEqual(status, 201, issued)
        self.assertIsNone(issued["plan"]["via_delegation_id"])

    def test_non_signatory_cannot_delegate(self):
        status, _ = self.api(
            "POST",
            "/delegations",
            token=self.token(USERS["surgeon2"]),
            body={"delegate_id": USERS["path"], "expires_in_seconds": 3600},
        )
        self.assertEqual(status, 403)


class ConsentTests(ApiTestBase):
    def test_withdraw_and_re_sign(self):
        _, plan_id = self.prepare_issued_plan()
        # 重复登记幂等
        status, first = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 201)
        status, second = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 200)
        self.assertEqual(first["consent"]["id"], second["consent"]["id"])
        # 撤回
        consent_id = first["consent"]["id"]
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/consents/{consent_id}/withdraw",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["consent"]["status"], "withdrawn")
        # 重新签署是新签名而非沿用旧签名
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 201)
        self.assertNotEqual(data["consent"]["id"], consent_id)
        self.assertEqual(data["consent"]["status"], "active")

    def test_preference_change_forbids_reusing_old_signature(self):
        _, plan_id = self.prepare_issued_plan()
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 201)
        consent_id = data["consent"]["id"]
        # 患者改变生育意愿
        status, material = self.api(
            "POST",
            f"/cases/{CASE_KEY}/materials",
            token=self.token(USERS["coord"]),
            body={
                "kind": "preference_record",
                "content": {"summary": "意愿记录v2", "fertility_preference": "not_preserve"},
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(material["preference_changed"])
        status, state = self.api(
            "GET", f"/cases/{CASE_KEY}/status", token=self.token(USERS["coord"])
        )
        self.assertEqual(state["fertility_preference"], "not_preserve")
        self.assertEqual(state["consent"]["id"], consent_id)
        self.assertEqual(state["consent"]["status"], "invalidated")
        self.assertEqual(
            state["consent"]["invalidated_reason"], "fertility_preference_changed"
        )
        self.assertEqual(state["issued_plan"]["status"], "invalidated")
        self.assertEqual(state["candidate_plan"]["status"], "superseded")
        # 旧方案不得再登记同意（旧签名不得沿用）
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "plan_not_active")
        # 生育咨询与外科须重新确认
        self.assertIn("awaiting_confirmation:fertility_counseling", state["blocking_reasons"])
        self.assertIn("awaiting_confirmation:surgery", state["blocking_reasons"])

    def test_consent_only_on_issued_plan(self):
        self.prepare_case()
        status, data = self.form_candidate()
        candidate_id = data["candidate"]["id"]
        # 候选方案未签发时不能登记同意
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{candidate_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 404)


class EmergencyExceptionTests(ApiTestBase):
    def prepare_emergency_case(self):
        """三专业已确认、影像缺席的紧急会议病例。"""
        self.create_case()
        self.upload_all_materials()
        for user_key, discipline in [
            ("signatory", "surgery"),
            ("path", "pathology"),
            ("fert", "fertility_counseling"),
        ]:
            self.submit_opinion(user_key, CASE_KEY, discipline)
        meeting = self.create_meeting(CASE_KEY, kind="emergency", absent=["radiology"])
        return meeting["exceptions"][0]["id"]

    def test_exception_blocks_candidate_until_reviewed(self):
        exception_id = self.prepare_emergency_case()
        # 缺席专业未确认且例外未补审 → 受阻
        status, data = self.form_candidate()
        self.assertEqual(status, 409)
        self.assertIn(f"exception_pending:{exception_id}", data["error"]["message"])
        # 缺席专业补交意见但尚未补审 → 仍受阻
        self.submit_opinion("rad", CASE_KEY, "radiology")
        status, data = self.form_candidate()
        self.assertEqual(status, 409)
        # 限期内补审 → 放行
        status, review = self.api(
            "POST",
            f"/cases/{CASE_KEY}/exceptions/{exception_id}/review",
            token=self.token(USERS["rad"]),
            body={},
        )
        self.assertEqual(status, 200)
        self.assertEqual(review["state"], "fulfilled")
        self.assertFalse(review["late"])
        status, data = self.form_candidate()
        self.assertEqual(status, 201, data)

    def test_review_requires_confirmed_opinion(self):
        exception_id = self.prepare_emergency_case()
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/exceptions/{exception_id}/review",
            token=self.token(USERS["rad"]),
            body={},
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "review_requires_confirmed_opinion")
        # 其他专业不能代为补审
        self.submit_opinion("rad", CASE_KEY, "radiology")
        status, _ = self.api(
            "POST",
            f"/cases/{CASE_KEY}/exceptions/{exception_id}/review",
            token=self.token(USERS["path"]),
            body={},
        )
        self.assertEqual(status, 403)

    def test_overdue_exception_blocks_and_late_review_flagged(self):
        exception_id = self.prepare_emergency_case()
        self.submit_opinion("rad", CASE_KEY, "radiology")
        # 超过补审时限
        self.clock.advance(hours=2)
        status, state = self.api(
            "GET", f"/cases/{CASE_KEY}/status", token=self.token(USERS["coord"])
        )
        self.assertEqual(state["exceptions"][0]["state"], "overdue")
        status, data = self.form_candidate()
        self.assertEqual(status, 409)
        self.assertIn(f"exception_overdue:{exception_id}", data["error"]["message"])
        # 超时补审：允许但标记 late
        status, review = self.api(
            "POST",
            f"/cases/{CASE_KEY}/exceptions/{exception_id}/review",
            token=self.token(USERS["rad"]),
            body={},
        )
        self.assertEqual(status, 200)
        self.assertEqual(review["state"], "fulfilled")
        self.assertTrue(review["late"])
        status, data = self.form_candidate()
        self.assertEqual(status, 201, data)

    def test_regular_meeting_rejects_absence(self):
        self.create_case()
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/meetings",
            token=self.token(USERS["coord"]),
            body={"kind": "regular", "absent_disciplines": ["radiology"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "absent_not_allowed")


class RestartRecoveryTests(ApiTestBase):
    def test_state_survives_restart(self):
        # 病例一：全流程到同意签署；导出一次纪要留下审计记录
        _, plan_id = self.prepare_issued_plan()
        status, data = self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        self.assertEqual(status, 201)
        status, _ = self.api(
            "GET", f"/cases/{CASE_KEY}/minutes", token=self.token(USERS["coord"])
        )
        self.assertEqual(status, 200)
        # 病例二：紧急会议缺席例外（补审时限 1 小时）
        self.create_case(CASE_KEY_2)
        self.upload_all_materials(CASE_KEY_2)
        meeting = self.create_meeting(CASE_KEY_2, kind="emergency", absent=["radiology"])
        exception_id = meeting["exceptions"][0]["id"]
        coord_token = self.token(USERS["coord"])
        auditor_token = self.token(USERS["auditor"])

        # 重启：同一数据库文件重新拉起服务
        self._stop_server()
        self._start_server()

        # 旧令牌仍有效，状态完整恢复
        status, state = self.api("GET", f"/cases/{CASE_KEY}/status", token=coord_token)
        self.assertEqual(status, 200)
        self.assertEqual(
            state["material_versions"],
            {
                "pathology_report": 1,
                "imaging_report": 1,
                "preference_record": 1,
                "risk_review": 1,
            },
        )
        for discipline in (
            "surgery",
            "pathology",
            "radiology",
            "fertility_counseling",
        ):
            self.assertEqual(state["disciplines"][discipline]["state"], "confirmed")
        self.assertEqual(state["issued_plan"]["id"], plan_id)
        self.assertEqual(state["issued_plan"]["status"], "active")
        self.assertEqual(state["consent"]["status"], "active")
        # 重启后时钟推进超过补审时限 → 例外仍被正确判定超时
        self.clock.advance(hours=2)
        status, state2 = self.api("GET", f"/cases/{CASE_KEY_2}/status", token=coord_token)
        self.assertEqual(status, 200)
        self.assertEqual(state2["exceptions"][0]["id"], exception_id)
        self.assertEqual(state2["exceptions"][0]["state"], "overdue")
        # 重启前的审计记录仍在
        status, audit = self.api("GET", "/audit", token=auditor_token)
        self.assertEqual(status, 200)
        actions = [entry["action"] for entry in audit["entries"]]
        self.assertIn("export_minutes", actions)
        # 重启后决定沿革完整
        status, history = self.api(
            "GET", f"/cases/{CASE_KEY}/history", token=coord_token
        )
        self.assertEqual(status, 200)
        types = [event["type"] for event in history["events"]]
        for expected in (
            "case_registered",
            "material_version_added",
            "opinion_confirmed",
            "candidate_formed",
            "plan_issued",
            "consent_recorded",
        ):
            self.assertIn(expected, types)


class AuditAndExportTests(ApiTestBase):
    def test_minutes_and_history_exports(self):
        _, plan_id = self.prepare_issued_plan()
        self.api(
            "POST",
            f"/cases/{CASE_KEY}/plans/{plan_id}/consent",
            token=self.token(USERS["coord"]),
            body={},
        )
        # 会议纪要：展示证据版本与确认状态
        status, minutes = self.api(
            "GET", f"/cases/{CASE_KEY}/minutes", token=self.token(USERS["coord"])
        )
        self.assertEqual(status, 200)
        self.assertEqual(minutes["document"], "meeting_minutes")
        self.assertEqual(minutes["material_versions"]["pathology_report"], 1)
        states = {o["discipline"]: o["state"] for o in minutes["opinions"]}
        self.assertTrue(all(s == "confirmed" for s in states.values()))
        for opinion in minutes["opinions"]:
            self.assertIn("evidence_snapshot", opinion)
            self.assertIn("coi_disclosed", opinion)
        self.assertEqual(minutes["issued_plan"]["id"], plan_id)
        self.assertEqual(minutes["consent"]["status"], "active")
        self.assertIn("不提供自动诊断或处方", minutes["notice"])
        # 决定沿革：事件有序且完整
        status, history = self.api(
            "GET", f"/cases/{CASE_KEY}/history", token=self.token(USERS["auditor"])
        )
        self.assertEqual(status, 200)
        seqs = [event["seq"] for event in history["events"]]
        self.assertEqual(seqs, sorted(seqs))
        types = [event["type"] for event in history["events"]]
        self.assertEqual(types[0], "case_registered")
        self.assertIn("plan_issued", types)
        self.assertIn("consent_recorded", types)

    def test_sensitive_access_is_audited(self):
        self.prepare_case()
        coord = self.token(USERS["coord"])
        self.api("GET", f"/cases/{CASE_KEY}", token=coord)
        self.api("GET", f"/cases/{CASE_KEY}/materials", token=coord)
        self.api("GET", f"/cases/{CASE_KEY}/minutes", token=coord)
        self.api("GET", f"/cases/{CASE_KEY}/history", token=coord)
        status, audit = self.api("GET", "/audit", token=self.token(USERS["auditor"]))
        self.assertEqual(status, 200)
        entries = [
            (e["actor"], e["action"], e["resource"]) for e in audit["entries"]
        ]
        self.assertIn(("coord1", "read_case_detail", f"case:{CASE_KEY}"), entries)
        self.assertIn(("coord1", "read_materials", f"case:{CASE_KEY}"), entries)
        self.assertIn(("coord1", "export_minutes", f"case:{CASE_KEY}"), entries)
        self.assertIn(("coord1", "export_history", f"case:{CASE_KEY}"), entries)
        self.assertIn(("coord1", "login", "user:coord1"), entries)

    def test_coi_detail_restricted_to_privileged_roles(self):
        self.prepare_case()
        status, data = self.api(
            "GET", f"/cases/{CASE_KEY}/opinions", token=self.token(USERS["rad"])
        )
        others = [o for o in data["opinions"] if o["author"] != USERS["rad"]]
        self.assertTrue(others)
        for opinion in others:
            self.assertNotIn("coi_detail", opinion)
        status, data = self.api(
            "GET", f"/cases/{CASE_KEY}/opinions", token=self.token(USERS["coord"])
        )
        self.assertTrue(all("coi_detail" in o for o in data["opinions"]))


if __name__ == "__main__":
    unittest.main()
