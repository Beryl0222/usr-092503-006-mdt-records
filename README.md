# 肿瘤多学科决策记录服务

院内多学科讨论（MDT）决策留痕后端。系统只记录**人工形成**的决策与确认状态：
不输出自动诊断或处方建议；患者信息在进入系统前应完成去标识化处理，
写入侧会对疑似身份标识（姓名/证件号/手机号等字段）直接拒绝入库。

## 运行

```bash
python3 -m src.mdt_records [db_path] [port]   # 默认 mdt_records.db / 8080
```

演示账号（生产应对接院内身份系统）：`coord1`（协调员）、`surg1`（外科主任，签发权）、
`surg2`（外科医师）、`path1`（病理）、`rad1`（影像）、`fert1`（生育咨询）、
`auditor1`（审计）；口令均为 `secret-<账号>`。

## 校验方式

```bash
python3 -m unittest discover -s tests -v   # 测试
python3 -m compileall -q src               # 构建检查
```

## 工作流与核心规则

1. 协调员登记去标识化病例（`CASE-` + 16 位大写十六进制键）并逐版上传材料
   （病理/影像/生育意愿/手术风险四类，版本单调递增）。
2. 各专业医师在本人专业权限内提交并确认意见，确认时快照当前材料版本；
   利益冲突必须披露，已确认意见的披露不可撤回。
3. 候选方案仅在四专业全部确认、利益冲突均已披露、无未完成的缺席例外时形成；
   响应中携带 `evidence_snapshot` 与逐专业 `confirmations`。
4. 任一关键材料改版 → 受影响专业（见下表）的确认失效须重确认，
   旧候选方案废止、旧签发方案及其上的知情同意一并失效。
5. 紧急会议允许登记缺席例外，缺席专业须在限时内补审（默认 24h，可注入时钟测试）；
   超时例外阻断候选方案，补审允许迟到但标记 `late`。
6. 授权签发医生（或有效受托人）签发最终方案；同一病例至多一个生效方案，
   并发签署由数据库唯一约束保证只有一个胜者。
7. 知情同意只针对生效中的签发方案；撤回后须重新签署（新签名），
   生育意愿变更后旧签名一律不得沿用。
8. 敏感读取（病例详情、材料、状态）与导出（纪要、沿革）全部写入审计日志。

材料改版影响面：`pathology_report`→病理+外科；`imaging_report`→影像+外科；
`preference_record`→生育咨询+外科；`risk_review`→外科。

## API 概览

| 方法与路径 | 权限 | 说明 |
| --- | --- | --- |
| `POST /sessions` | 公开 | 登录换取令牌 |
| `POST /cases` | 协调员 | 登记去标识化病例 |
| `GET /cases/{key}` / `status` | 院内角色 | 病例详情 / 结论的证据版本与确认状态（审计） |
| `POST /cases/{key}/materials` | 协调员 | 上传材料新版本（触发改版级联失效） |
| `GET /cases/{key}/materials` | 院内角色 | 材料版本列表（审计） |
| `POST /cases/{key}/opinions` | 本专业医师 | 提交意见（幂等；`confirm=true` 直接确认） |
| `POST /cases/{key}/opinions/{id}/confirm` | 意见作者 | 确认草稿意见 |
| `POST /cases/{key}/meetings` | 协调员 | 常规/紧急会议；紧急会议可登记缺席例外 |
| `POST /cases/{key}/exceptions/{id}/review` | 缺席专业医师 | 限时补审（须先确认本专业意见） |
| `POST /cases/{key}/candidate` | 协调员 | 形成候选方案（幂等） |
| `POST /cases/{key}/plans/{cid}/issue` | 签发医生/受托人 | 签发最终方案（全库唯一生效） |
| `POST /cases/{key}/plans/{pid}/consent` | 协调员 | 登记患者知情同意（幂等） |
| `POST /cases/{key}/consents/{id}/withdraw` | 协调员 | 撤回知情同意 |
| `POST /delegations` / `POST /delegations/{id}/revoke` | 签发医生 | 授权委托（限时、可限定病例、可撤销） |
| `GET /cases/{key}/minutes` | 协调员/审计 | 会议纪要导出（审计） |
| `GET /cases/{key}/history` | 协调员/审计 | 决定沿革导出（审计） |
| `GET /audit` | 审计员 | 访问审计日志 |

错误统一为 `{"error": {"code", "message"}}`；结论类响应均附
`notice: "本系统仅记录院内人工决策与确认状态，不提供自动诊断或处方建议。"`
