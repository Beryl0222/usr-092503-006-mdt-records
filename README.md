# 肿瘤多学科决策记录服务（MDT Decision Records）

仅服务于**院内决策留痕**的后端系统：妇科肿瘤中心为早期宫颈癌患者组织多学科讨论时，
用本系统登记去标识化病例与材料版本、按专业权限提交意见、在专业齐备且利益冲突已披露后
形成候选方案，并对签发、患者知情同意、缺席补审、材料改版与审计进行全程留痕。

> 本系统只记录**医生本人提交**的专业意见与流程结论，**不产生、不输出任何自动诊断或处方**。
> 所有接口响应与导出文件都携带该声明，且不存在诊断/处方类字段。患者信息在进入系统前
> 应完成去标识化处理。

## 技术形态

- 纯 Python 标准库（`http.server` + `sqlite3`），Python ≥ 3.11，无第三方运行时依赖。
- SQLite（WAL）单库持久化；写入以 `BEGIN IMMEDIATE` 立即事务串行化，
  关键状态变更采用条件更新（CAS），保证并发下只成功一次。
- Bearer 令牌认证；线程化 HTTP 服务。

## 模块

| 文件 | 职责 |
| --- | --- |
| `contracts.py` | 稳定领域契约：专业、材料类别、确认状态、病例键校验 |
| `store.py` | SQLite schema、连接/事务原语 |
| `workflow.py` | 全部领域规则：改版失效、齐备性判定、候选/签发/同意、缺席补审、导出数据装配 |
| `app.py` | HTTP API、细粒度权限、认证、幂等键、审计 |
| `export.py` | 会议纪要与决定沿革的 Markdown 渲染 |
| `seed.py` | 开发/测试用固定用户与令牌 |
| `__main__.py` | 服务启动入口 |

## 启动

```bash
python3 -m src.mdt_records --db data/mdt.db --host 127.0.0.1 --port 8080 --seed
```

`--seed` 写入开发用固定令牌（仅用于开发/测试，生产环境不应使用）。

种子令牌：`tok-coord`（协调员）、`tok-surg`/`tok-surg2`（授权外科）、`tok-path`、
`tok-rad`、`tok-fert`、`tok-audit`（审计员）、`tok-patient`（去标识化患者）。

## 角色与细粒度权限

| 能力 | coordinator | specialist | patient | auditor |
| --- | --- | --- | --- | --- |
| 登记病例 / 材料版本 / 安排会议 | ✅ | ❌ | ❌ | ❌ |
| 分配医生 / 关联患者引用 | ✅ | ❌ | ❌ | ❌ |
| 查看病例数据 | 全部 | 仅被分配的病例 | 仅本人关联方案状态 | 全部 |
| 提交专业意见 | ❌ | 本专业（或凭委托代提交） | ❌ | ❌ |
| 形成候选方案 / 登记缺席例外 | ✅ | ❌ | ❌ | ❌ |
| 签发方案 | ❌ | 仅 `authorized_signer` | ❌ | ❌ |
| 同意 / 撤回 / 变更生育意愿 | ❌ | ❌ | 仅本人关联病例 | ❌ |
| 导出纪要 / 查审计日志 | ✅ | ❌ | ❌ | ✅ |

专科医生只能访问被分配到的病例；跨专业提交、无委托代提交、未授权签发、
患者越权访问等一律 `403` 并写审计。

## 关键工作流规则

1. **材料版本化**：每类材料（病理/影像/生育意愿/风险）版本单调递增，上传新版本自动停用旧版本。
2. **改版即失效**：任何关键材料更新，立即作废受影响专业（按材料类别映射）的已确认意见、
   候选方案与**已签发方案**，对应患者同意标记 `superseded`；必须基于新版本重新确认后
   才能形成新一版候选（旧版完整保留于决定沿革）。引用已停用版本的意见提交被拒绝。
3. **齐备性门禁**：必需专业全部 `confirmed`（紧急会议可用“限时内补审通过”的缺席例外覆盖）、
   且每条意见均已披露利益冲突，方可形成候选；签发瞬间再次复核。
4. **紧急缺席例外**：仅紧急会议可登记，带补审期限；超时补审仍允许留痕但 `late=1`，
   **不计为齐备**，不能形成/签发方案。
5. **签发授权**：仅授权签发医生可签发，支持凭有效 `sign` 委托代签发；同一候选并发签发
   只有一个成功（`201`），其余得到 `409 concurrent_issue`。
6. **知情同意**：同意只针对**已签发**方案，且固化所基于的生育意愿版本；撤回同意后
   方案标记 `consent_withdrawn`，不能对旧方案补同意；生育意愿变更后旧签名一律不得沿用。
7. **审计**：敏感字段读取（`evidence?full=1` 中敏感材料）、所有导出、权限拒绝均写审计；
   审计员可按病例/仅敏感记录查询。
8. **重启恢复**：服务启动时将已过补审期限仍 `pending` 的缺席例外标记为 `overdue`。
9. **幂等**：签发、意见提交、同意等支持 `Idempotency-Key` 请求头；同键同体重放返回首次结果，
   同键不同请求体返回 `409 idempotency_reuse`；不带键的重复提交按业务规则拒绝
   （如 `duplicate_opinion`）。

## 主要接口（前缀 `/api/v1`）

```
POST /cases                                 登记病例
POST /cases/{case}/assignments              分配专科医生
POST /cases/{case}/patient-links            关联去标识化患者引用
POST /cases/{case}/evidence                 上传材料新版本
GET  /cases/{case}/evidence[?full=1]        材料版本清单（full 含正文，访问敏感字段记审计）
POST /cases/{case}/meetings                 安排会议 {required, emergency}
GET  /meetings/{id}                         会议状态：齐备性、意见确认状态、证据版本、方案
POST /meetings/{id}/opinions                提交/代提交专业意见（含 evidence_refs、coi_disclosed）
POST /meetings/{id}/absence-exceptions      登记紧急缺席例外（补审时限）
POST /absence-exceptions/{id}/review        专业医生限时补审
POST /meetings/{id}/candidate-plans         齐备时形成候选方案（含证据版本快照）
POST /candidate-plans/{id}/issue            授权医生（或凭委托）签发
GET  /issued-plans/{id}                     签发方案与同意状态
POST /issued-plans/{id}/consent             患者同意
POST /issued-plans/{id}/consent/withdraw    患者撤回同意
POST /cases/{case}/preference-change        患者变更生育意愿（触发改版失效）
POST /delegations · /delegations/{id}/revoke 签发/意见委托
GET  /cases/{case}/export?format=json|md    会议纪要与决定沿革导出
GET  /audit[?case_key=&sensitive_only=1]    审计查询（auditor）
```

所有会议视图与导出都明确呈现：**结论引用的证据版本**（`evidence_versions` / 每意见 `evidence_refs`
及 `stale_refs`）、**各专业确认状态**（draft/confirmed/invalidated）、利益冲突披露、
缺席例外与补审状态、候选/签发/失效沿革与同意状态。

## 校验

```bash
python3 -m unittest discover -s tests -v   # 19 个端到端测试（真实 HTTP + SQLite）
python3 -m compileall -q src tests
```

测试覆盖：并发签署、重复提交（幂等键）、材料改版失效与重新确认、授权委托（意见/签发、撤销）、
同意撤回与生育意愿变更、紧急会议限时补审与超时补审、重启恢复，以及权限矩阵、审计留痕、
导出中的证据版本/确认状态展示与“无自动诊断字段”断言。
