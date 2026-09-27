"""现场情报核验板服务端语义的无第三方依赖验证（python3 tests/test_verifications.py）。

不导入 app.main（其依赖 fastapi，本地最小环境可能缺失），而是直接在
app.db 建表的 SQLite 上调用 app.verifications 域函数，复刻 main.py 路由在
state_lock 临界区内对命题与报告的操作次序，校验：

1. 创建命题：至少两个参与组的白名单（域层信任路由的组数校验，这里直接构造），
   去重保序；
2. 每组一份：首次提交插入，再次提交原地覆盖（revision +1、立场/依据替换），
   同组多次提交只保留一行、绝不计作多份证据；
3. 撤回：删除报告行后可重新提交；无报告撤回抛 404；
4. 状态推导（compute_status）：
   - 零报告 / 仅单组报告 → pending；
   - 支持 ≥2 且反对 0 → confirmed；反对 ≥2 且支持 0 → refuted；
   - 两边均有报告（哪怕 1:1）→ disputed；
   - 已证实后出现一份反对 → disputed；反对全部撤回后回到 confirmed；
5. 每次提交/覆盖/撤回后由持久化行即时重算（视图函数现查现算）；
6. 重置：清空报告、命题与白名单保留；
7. 级联：删除参与组/房间时白名单与报告随之清理；
8. 视野：主持人视图含逐组报告；参与者视图只有本组报告与立场组数；
9. 全新 SCHEMA 含核验三表与索引。
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db as dbmod  # noqa: E402
from app import verifications as vm  # noqa: E402


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(dbmod.SCHEMA)
    return conn


def make_room(conn, code="VFY001", name="核验房"):
    rid = conn.execute(
        "INSERT INTO rooms (code,name,status,elapsed_ms,last_resume_at,created_at)"
        " VALUES (?,?, 'idle', 0, NULL, ?)",
        (code, name, time.time()),
    ).lastrowid
    conn.commit()
    return rid


def add_group(conn, rid, name, code):
    gid = conn.execute(
        "INSERT INTO groups (room_id,name,join_code,created_at) VALUES (?,?,?,?)",
        (rid, name, code, time.time()),
    ).lastrowid
    conn.commit()
    return gid


def make_claim(conn, rid, gids, title="命题", detail=""):
    return vm.create_claim(conn, rid, title, detail, gids)


def host_view(conn, rid, claim_id):
    claim = vm.get_claim(conn, rid, claim_id)
    return vm.claim_host_view(conn, claim)


def test_create_claim_and_whitelist():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV001")
        g2 = add_group(conn, rid, "蓝队", "VVV002")
        g3 = add_group(conn, rid, "绿队", "VVV003")
        claim = make_claim(conn, rid, [g3, g1, g3, g2])
        # 去重（插入保序，读取按 id 升序）
        assert vm.claim_group_ids(conn, claim["id"]) == [g1, g2, g3]
        view = host_view(conn, rid, claim["id"])
        assert view["status"] == "pending"
        assert view["support_count"] == 0 and view["oppose_count"] == 0
        assert view["reports"] == []
        # 只有白名单组能在玩家视图里看到该命题
        g4 = add_group(conn, rid, "黄队", "VVV004")
        assert len(vm.player_views(conn, rid, g4)) == 0
        assert len(vm.player_views(conn, rid, g1)) == 1
        conn.close()
    print("ok: claim created with dedup >=2 group whitelist, views scoped")


def test_one_report_per_group_overwrite_not_multi_evidence():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV011")
        g2 = add_group(conn, rid, "蓝队", "VVV012")
        claim = make_claim(conn, rid, [g1, g2])

        row, first = vm.submit_report(conn, rid, claim, g1, "support", "依据一")
        assert first and row["revision"] == 1 and row["stance"] == "support"
        # 同组再次提交 = 覆盖：立场/依据替换、revision+1，仍只有一行
        row2, first2 = vm.submit_report(conn, rid, claim, g1, "oppose", "依据二")
        assert not first2 and row2["revision"] == 2 and row2["stance"] == "oppose"
        assert row2["id"] == row["id"]
        rows = conn.execute(
            "SELECT * FROM verification_reports WHERE claim_id=? AND group_id=?",
            (claim["id"], g1)).fetchall()
        assert len(rows) == 1
        assert rows[0]["basis"] == "依据二"
        # 只有这一组：反对组数 1，不足两组 → 仍待核验（绝不是已否定）
        view = host_view(conn, rid, claim["id"])
        assert view["oppose_count"] == 1 and view["support_count"] == 0
        assert view["status"] == "pending"
        conn.close()
    print("ok: same group overwrite counts once (revision bump, not new evidence)")


def test_status_confirmed_refuted_disputed():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV021")
        g2 = add_group(conn, rid, "蓝队", "VVV022")
        g3 = add_group(conn, rid, "绿队", "VVV023")
        claim = make_claim(conn, rid, [g1, g2, g3])

        # 单组支持 → 待核验
        vm.submit_report(conn, rid, claim, g1, vm.STANCE_SUPPORT, "s1")
        assert host_view(conn, rid, claim["id"])["status"] == "pending"
        # 两组支持、零反对 → 已证实
        vm.submit_report(conn, rid, claim, g2, vm.STANCE_SUPPORT, "s2")
        assert host_view(conn, rid, claim["id"])["status"] == "confirmed"
        # 第三组反对 → 两边均有 → 有争议
        vm.submit_report(conn, rid, claim, g3, vm.STANCE_OPPOSE, "o1")
        assert host_view(conn, rid, claim["id"])["status"] == "disputed"
        # 反对组撤回 → 回到已证实
        vm.withdraw_report(conn, claim, g3)
        assert host_view(conn, rid, claim["id"])["status"] == "confirmed"
        # 两个支持组都改成反对（覆盖）：反对 ≥2、支持为零 → 已否定
        vm.submit_report(conn, rid, claim, g1, vm.STANCE_OPPOSE, "o2")
        assert host_view(conn, rid, claim["id"])["status"] == "disputed"  # 仍有 g2 支持
        vm.submit_report(conn, rid, claim, g2, vm.STANCE_OPPOSE, "o3")
        assert host_view(conn, rid, claim["id"])["status"] == "refuted"
        conn.close()
    print("ok: status flips pending/confirmed/disputed/refuted on submit & withdraw")


def test_withdraw_then_resubmit_and_missing_404():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV031")
        g2 = add_group(conn, rid, "蓝队", "VVV032")
        claim = make_claim(conn, rid, [g1, g2])
        vm.submit_report(conn, rid, claim, g1, "support", "")
        vm.withdraw_report(conn, claim, g1)
        assert vm.report_of_group(conn, claim["id"], g1) is None
        # 撤回后可重新提交（revision 从 1 重新计）
        row, first = vm.submit_report(conn, rid, claim, g1, "support", "重新提交")
        assert first and row["revision"] == 1
        # 无报告撤回 → 404
        try:
            vm.withdraw_report(conn, claim, g2)
            raise AssertionError("expected VerificationError")
        except vm.VerificationError as e:
            assert e.status_code == 404
        conn.close()
    print("ok: withdraw deletes report, resubmit allowed, missing withdraw is 404")


def test_reset_keeps_claims_clears_reports():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV041")
        g2 = add_group(conn, rid, "蓝队", "VVV042")
        claim = make_claim(conn, rid, [g1, g2], title="跨重置命题")
        vm.submit_report(conn, rid, claim, g1, "support", "a")
        vm.submit_report(conn, rid, claim, g2, "support", "b")
        assert host_view(conn, rid, claim["id"])["status"] == "confirmed"

        n = vm.reset_reports(conn, rid)
        assert n == 2
        # 命题与白名单保留
        assert vm.get_claim(conn, rid, claim["id"]) is not None
        assert vm.claim_group_ids(conn, claim["id"]) == [g1, g2]
        # 报告清空、状态回到待核验
        view = host_view(conn, rid, claim["id"])
        assert view["status"] == "pending" and view["reports"] == []
        conn.close()
    print("ok: reset clears reports but keeps claims and whitelist")


def test_player_view_hides_other_groups():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV051")
        g2 = add_group(conn, rid, "蓝队", "VVV052")
        g3 = add_group(conn, rid, "绿队", "VVV053")
        claim = make_claim(conn, rid, [g1, g2, g3])
        vm.submit_report(conn, rid, claim, g1, "support", "红队机密依据")
        vm.submit_report(conn, rid, claim, g2, "oppose", "蓝队机密依据")

        pv = vm.claim_player_view(conn, claim, g3)
        assert pv["my_report"] is None
        assert pv["support_count"] == 1 and pv["oppose_count"] == 1
        assert pv["status"] == "disputed"
        # 玩家视图中绝不能出现任何他组报告的立场归属或依据
        serialized = repr(pv)
        assert "红队机密依据" not in serialized and "蓝队机密依据" not in serialized

        pv1 = vm.claim_player_view(conn, claim, g1)
        assert pv1["my_report"]["stance"] == "support"
        assert pv1["my_report"]["basis"] == "红队机密依据"
        # 同样不带他组依据
        assert "蓝队机密依据" not in repr(pv1)
        conn.close()
    print("ok: player view exposes only own report body plus aggregate status")


def test_cascade_on_group_and_room_delete():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn)
        g1 = add_group(conn, rid, "红队", "VVV061")
        g2 = add_group(conn, rid, "蓝队", "VVV062")
        claim = make_claim(conn, rid, [g1, g2])
        vm.submit_report(conn, rid, claim, g1, "support", "a")
        vm.submit_report(conn, rid, claim, g2, "support", "b")
        # 删组：白名单关联与该组报告级联清理，状态回到待核验（仅剩一组报告）
        conn.execute("DELETE FROM groups WHERE id = ?", (g2,))
        conn.commit()
        assert vm.claim_group_ids(conn, claim["id"]) == [g1]
        view = host_view(conn, rid, claim["id"])
        assert view["support_count"] == 1 and view["status"] == "pending"
        # 删房间：命题/白名单/报告全部级联
        conn.execute("DELETE FROM rooms WHERE id = ?", (rid,))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) c FROM verification_claims WHERE room_id=?", (rid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM verification_reports WHERE room_id=?", (rid,)
        ).fetchone()["c"] == 0
        conn.close()
    print("ok: group/room deletion cascades claim groups and reports")


def test_compute_status_table():
    cs = vm.compute_status
    assert cs(0, 0) == "pending"
    assert cs(1, 0) == "pending"
    assert cs(0, 1) == "pending"
    assert cs(2, 0) == "confirmed"
    assert cs(3, 0) == "confirmed"
    assert cs(0, 2) == "refuted"
    assert cs(0, 3) == "refuted"
    assert cs(1, 1) == "disputed"
    assert cs(2, 1) == "disputed"
    assert cs(1, 2) == "disputed"
    print("ok: compute_status truth table (groups, not submissions)")


def main():
    test_create_claim_and_whitelist()
    test_one_report_per_group_overwrite_not_multi_evidence()
    test_status_confirmed_refuted_disputed()
    test_withdraw_then_resubmit_and_missing_404()
    test_reset_keeps_claims_clears_reports()
    test_player_view_hides_other_groups()
    test_cascade_on_group_and_room_delete()
    test_compute_status_table()
    print("all verification tests passed")


if __name__ == "__main__":
    main()
