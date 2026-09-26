"""开发/演示与测试共用的固定种子数据。

包含协调员、四个专业的专科医生、第二位外科医生（委托/并发用）、
授权签发医生、审计员与一名去标识化患者，以及对应的 Bearer 令牌。
"""

from __future__ import annotations

from .store import Store

SEED_USERS = [
    # user_id, display_name, role, signer, disciplines
    ("u-coord", "病例协调员", "coordinator", 0, []),
    ("u-surg", "外科医生甲", "specialist", 1, ["surgery"]),
    ("u-surg2", "外科医生乙", "specialist", 1, ["surgery"]),
    ("u-path", "病理医生", "specialist", 0, ["pathology"]),
    ("u-rad", "影像医生", "specialist", 0, ["radiology"]),
    ("u-fert", "生育咨询医生", "specialist", 0, ["fertility_counseling"]),
    ("u-audit", "审计员", "auditor", 0, []),
    ("p-0001", "患者去标识化引用 P-0001", "patient", 0, []),
]

SEED_TOKENS = {
    "tok-coord": "u-coord",
    "tok-surg": "u-surg",
    "tok-surg2": "u-surg2",
    "tok-path": "u-path",
    "tok-rad": "u-rad",
    "tok-fert": "u-fert",
    "tok-audit": "u-audit",
    "tok-patient": "p-0001",
}


def seed(store: Store) -> dict[str, str]:
    with store.tx() as c:
        for user_id, name, role, signer, disciplines in SEED_USERS:
            c.execute(
                """INSERT INTO users (user_id, display_name, role, authorized_signer, active)
                   VALUES (?,?,?,?,1)
                   ON CONFLICT(user_id) DO UPDATE SET
                     display_name=excluded.display_name,
                     role=excluded.role,
                     authorized_signer=excluded.authorized_signer,
                     active=1""",
                (user_id, name, role, signer),
            )
            for d in disciplines:
                c.execute(
                    """INSERT OR IGNORE INTO user_disciplines (user_id, discipline)
                       VALUES (?,?)""",
                    (user_id, d),
                )
        for token, user_id in SEED_TOKENS.items():
            c.execute(
                "INSERT OR IGNORE INTO tokens (token, user_id) VALUES (?,?)",
                (token, user_id),
            )
    return dict(SEED_TOKENS)
