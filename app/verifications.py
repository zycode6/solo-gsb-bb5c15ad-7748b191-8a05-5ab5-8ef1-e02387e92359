"""现场情报核验板 —— 待核验命题与逐组报告域（不依赖 fastapi，便于无第三方依赖测试）。

与 app.main 同一把 state_lock、同一个 SQLite 连接下被调用，因此本模块
不再自行加锁，只负责：

- 命题：主持人创建待核验命题并指定至少两个参与组（核验白名单）；
  命题持久化、跨重置保留（重置只清报告）
- 报告：白名单内参与组凭组加入码对本组命题提交「支持 / 反对」立场与
  简短依据；每组对每条命题仅保留一份报告（UNIQUE(claim_id, group_id)），
  同组再次提交即覆盖原报告（revision +1），也可撤回（删除行）
- 状态：完全按「不同参与组的最新报告」推导——每组只算一份，同组多次
  提交绝不计作多份证据：
  · 同一立场（支持或反对）至少两组 且 对立立场为零 → confirmed（已证实）
    / refuted（已否定）；
  · 两边均有报告 → disputed（有争议）；
  · 其余（含仅单组报告、零报告）→ pending（待核验）。
  每次提交、覆盖或撤回后由调用方即时重算（本模块 compute_status 纯推导），
  不做任何冗余存储，重连与重启后状态自然一致。
- 重置：删除全房间报告行，命题与其参与组白名单全部保留供新一轮使用。

视野：
- 主持人：每条命题的逐组最新报告（立场、依据、版本、时间）与汇总状态；
- 参与组：只看到白名单包含本组的命题、本组报告与命题汇总状态，
  看不到任何他组报告的立场或依据；
- 观察者：完全不可见（快照不带、verification_* 事件不下发，由 main 过滤）。
"""

from __future__ import annotations

import sqlite3
import time

STANCE_SUPPORT = "support"   # 支持命题
STANCE_OPPOSE = "oppose"     # 反对命题
STANCES = (STANCE_SUPPORT, STANCE_OPPOSE)

STATUS_PENDING = "pending"       # 待核验：无报告 / 仅单组报告 / 其余不足判定情形
STATUS_CONFIRMED = "confirmed"   # 已证实：支持 ≥ 2 组且反对为 0
STATUS_REFUTED = "refuted"       # 已否定：反对 ≥ 2 组且支持为 0
STATUS_DISPUTED = "disputed"     # 有争议：两种立场均有报告

MIN_CLAIM_GROUPS = 2  # 每条命题至少指定两个参与组
MAX_CLAIM_GROUPS = 8  # 与房间参与组上限一致
MAX_BASIS_LEN = 500   # 简短依据长度上限


class VerificationError(Exception):
    """核验域校验错误。main.py 捕获后转成对应 HTTP 状态码（detail 为消息文本）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------- 命题

def create_claim(
    conn: sqlite3.Connection,
    room_id: int,
    title: str,
    detail: str,
    group_ids: list[int],
    *,
    now: float | None = None,
) -> sqlite3.Row:
    """创建一条待核验命题并写入参与组白名单（至少两组）。

    调用方须先完成输入校验（标题非空、参与组均属于本房间、去重后 ≥ 2 组）。
    """
    now = time.time() if now is None else now
    cur = conn.execute(
        "INSERT INTO verification_claims (room_id, title, detail, created_at)"
        " VALUES (?, ?, ?, ?)",
        (room_id, title, detail, now),
    )
    claim_id = cur.lastrowid
    for gid in dict.fromkeys(group_ids):  # 去重保序
        conn.execute(
            "INSERT INTO verification_claim_groups (claim_id, group_id) VALUES (?, ?)",
            (claim_id, gid),
        )
    conn.commit()
    return conn.execute(
        "SELECT * FROM verification_claims WHERE id = ?", (claim_id,)
    ).fetchone()


def get_claim(conn: sqlite3.Connection, room_id: int, claim_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verification_claims WHERE id = ? AND room_id = ?",
        (claim_id, room_id),
    ).fetchone()


def claim_group_ids(conn: sqlite3.Connection, claim_id: int) -> list[int]:
    """命题的参与组白名单（升序）。"""
    return [r["group_id"] for r in conn.execute(
        "SELECT group_id FROM verification_claim_groups WHERE claim_id = ? ORDER BY group_id",
        (claim_id,),
    )]


def claims_for_group(conn: sqlite3.Connection, room_id: int, group_id: int) -> list[sqlite3.Row]:
    """白名单包含该组的全部命题（按 id 升序，即创建顺序）。"""
    return conn.execute(
        """SELECT c.* FROM verification_claims c
           JOIN verification_claim_groups cg ON cg.claim_id = c.id
           WHERE c.room_id = ? AND cg.group_id = ?
           ORDER BY c.id""",
        (room_id, group_id),
    ).fetchall()


def list_claims(conn: sqlite3.Connection, room_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM verification_claims WHERE room_id = ? ORDER BY id", (room_id,)
    ).fetchall()


# ---------------------------------------------------------------- 报告

def report_of_group(conn: sqlite3.Connection, claim_id: int,
                    group_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verification_reports WHERE claim_id = ? AND group_id = ?",
        (claim_id, group_id),
    ).fetchone()


def latest_reports(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    """命题下各参与组的最新报告（每组至多一条，UNIQUE 约束保证）。

    同组反复提交只是原地更新同一行，因此这里不会把同组多次提交数成多份证据。
    """
    return conn.execute(
        "SELECT * FROM verification_reports WHERE claim_id = ? ORDER BY group_id",
        (claim_id,),
    ).fetchall()


def submit_report(
    conn: sqlite3.Connection,
    room_id: int,
    claim: sqlite3.Row,
    group_id: int,
    stance: str,
    basis: str,
    *,
    now: float | None = None,
) -> tuple[sqlite3.Row, bool]:
    """参与组提交或覆盖本组报告。返回 (最新报告行, 是否首次提交)。

    调用方须先解析组身份、校验组在命题白名单内且 stance 合法。
    已有报告时原地覆盖：立场与依据整体替换、revision +1，旧报告不作为
    独立证据保留（状态推导只看这一行）。
    """
    now = time.time() if now is None else now
    existing = report_of_group(conn, claim["id"], group_id)
    if existing is None:
        cur = conn.execute(
            "INSERT INTO verification_reports"
            " (room_id, claim_id, group_id, stance, basis, revision, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (room_id, claim["id"], group_id, stance, basis, now, now),
        )
        first = True
        row_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE verification_reports SET stance = ?, basis = ?,"
            " revision = revision + 1, updated_at = ? WHERE id = ?",
            (stance, basis, now, existing["id"]),
        )
        first = False
        row_id = existing["id"]
    conn.commit()
    return conn.execute(
        "SELECT * FROM verification_reports WHERE id = ?", (row_id,)
    ).fetchone(), first


def withdraw_report(
    conn: sqlite3.Connection,
    claim: sqlite3.Row,
    group_id: int,
) -> sqlite3.Row:
    """参与组撤回本组报告（删除行；之后可重新提交）。

    无报告可撤时抛 404（幂等性通过 404 暴露不存在，与车辆取消申请同口径）。
    """
    row = report_of_group(conn, claim["id"], group_id)
    if row is None:
        raise VerificationError(404, "本组尚未提交核验报告")
    conn.execute("DELETE FROM verification_reports WHERE id = ?", (row["id"],))
    conn.commit()
    return row


# ---------------------------------------------------------------- 状态推导

def compute_status(support: int, oppose: int) -> str:
    """按不同参与组的最新报告计数推导命题状态（每组报告已由调用方去重）。

    - 两边均有报告 → disputed（有争议，优先判定）；
    - 支持 ≥ 2 且反对为 0 → confirmed（已证实）；
    - 反对 ≥ 2 且支持为 0 → refuted（已否定）；
    - 其余（零报告、仅单组报告）→ pending（待核验）。
    """
    if support > 0 and oppose > 0:
        return STATUS_DISPUTED
    if support >= 2 and oppose == 0:
        return STATUS_CONFIRMED
    if oppose >= 2 and support == 0:
        return STATUS_REFUTED
    return STATUS_PENDING


def status_counts(conn: sqlite3.Connection, claim_id: int) -> tuple[int, int]:
    """返回 (支持组数, 反对组数)：只数组（GROUP BY group_id 天然每组一份）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM verification_reports"
        " WHERE claim_id = ? AND stance = ?",
        (claim_id, STANCE_SUPPORT),
    ).fetchone()
    support = row["c"]
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM verification_reports"
        " WHERE claim_id = ? AND stance = ?",
        (claim_id, STANCE_OPPOSE),
    ).fetchone()
    return support, row["c"]


def reset_reports(conn: sqlite3.Connection, room_id: int) -> int:
    """重置演练：清空全房间核验报告；命题与白名单保留。返回清除条数。"""
    cur = conn.execute("DELETE FROM verification_reports WHERE room_id = ?", (room_id,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- 视野

def _report_dict(row: sqlite3.Row) -> dict:
    return {
        "group_id": row["group_id"],
        "stance": row["stance"],                      # support | oppose
        "basis": row["basis"],                        # 简短依据（含正文，仅主持人可见）
        "revision": row["revision"],                  # 覆盖次数（首次为 1）
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def host_view(conn: sqlite3.Connection, room_id: int) -> list[dict]:
    """主持人视野：全部命题 + 参与组白名单 + 逐组最新报告 + 实时汇总状态。"""
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM groups WHERE room_id = ?", (room_id,))}
    out = []
    for claim in list_claims(conn, room_id):
        group_ids = claim_group_ids(conn, claim["id"])
        reports = [_report_dict(r) for r in latest_reports(conn, claim["id"])]
        support = sum(1 for r in reports if r["stance"] == STANCE_SUPPORT)
        oppose = sum(1 for r in reports if r["stance"] == STANCE_OPPOSE)
        out.append({
            "id": claim["id"],
            "title": claim["title"],
            "detail": claim["detail"],
            "group_ids": group_ids,
            "group_names": [names.get(gid, f"#{gid}") for gid in group_ids],
            "status": compute_status(support, oppose),
            "support_count": support,
            "oppose_count": oppose,
            "reports": reports,
            "created_at": claim["created_at"],
        })
    return out


def claim_host_view(conn: sqlite3.Connection, claim: sqlite3.Row) -> dict:
    """单条命题的主持人视图（供提交/撤回/组删除事件载荷复用）。"""
    views = [v for v in host_view(conn, claim["room_id"]) if v["id"] == claim["id"]]
    return views[0]


def player_views(conn: sqlite3.Connection, room_id: int, group_id: int) -> list[dict]:
    """参与组视野：只含白名单包含本组的命题；只见本组报告与命题汇总状态。

    汇总只给立场组数（support_count/oppose_count）与状态，不含任何他组
    报告的立场归属、依据、版本——参与组无法从快照或事件识别具体某组的立场。
    """
    out = []
    for claim in claims_for_group(conn, room_id, group_id):
        support, oppose = status_counts(conn, claim["id"])
        mine = report_of_group(conn, claim["id"], group_id)
        out.append({
            "id": claim["id"],
            "title": claim["title"],
            "detail": claim["detail"],
            "status": compute_status(support, oppose),
            "support_count": support,
            "oppose_count": oppose,
            "my_report": _report_dict(mine) if mine is not None else None,
        })
    return out


def claim_player_view(conn: sqlite3.Connection, claim: sqlite3.Row,
                      group_id: int) -> dict:
    """单条命题的参与组视图（供事件按组脱敏载荷复用）。"""
    views = [v for v in player_views(conn, claim["room_id"], group_id)
             if v["id"] == claim["id"]]
    return views[0]
