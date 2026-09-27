"""现场情报核验板的 FastAPI 端到端集成测试（需 fastapi/httpx）。

运行：/tmp/venv/bin/python tests/integration_verifications.py
覆盖：创建命题（含校验）、参与组提交/覆盖/撤回、状态即时翻转、
同组覆盖不算多份证据、非白名单 404、视野过滤（主持人逐组 / 参与组只看本组 /
观察者完全不可见）、重置清报告保命题、组删除级联。
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "integ.db")

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import app  # noqa: E402

client = TestClient(app)
with client as c:
    # ---- 建房间 + 3 个参与组 ----
    room = c.post("/api/rooms", json={"name": "核验演练"}).json()
    code = room["code"]
    codes = {}
    for name in ("红队", "蓝队", "绿队"):
        g = c.post(f"/api/rooms/{code}/groups", json={"name": name}).json()
        codes[name] = g["join_code"]
    gids = {}
    host0 = c.get(f"/api/rooms/{code}").json()
    for g in host0["groups"]:
        gids[g["name"]] = g["id"]
    red, blue, green = codes["红队"], codes["蓝队"], codes["绿队"]
    rg, bg, gg = gids["红队"], gids["蓝队"], gids["绿队"]

    # ---- 创建命题校验 ----
    r = c.post(f"/api/rooms/{code}/verifications",
               json={"title": "T", "group_ids": [rg]})
    assert r.status_code == 422, r.status_code  # 少于两组 → pydantic 422
    r = c.post(f"/api/rooms/{code}/verifications",
               json={"title": "T", "group_ids": [rg, 99999]})
    assert r.status_code == 404, r.status_code  # 组不属于本房间

    r = c.post(f"/api/rooms/{code}/verifications", json={
        "title": "起火点在 B 区？", "detail": "据目击者称",
        "group_ids": [rg, bg, gg]})
    assert r.status_code == 201, r.text
    claim = r.json()
    cid = claim["id"]
    assert claim["status"] == "pending" and claim["reports"] == []
    assert claim["group_ids"] == sorted([rg, bg, gg])

    # ---- 非白名单组（另建房间的组）不可见、404 ----
    other = c.post("/api/rooms", json={"name": "别的房间"}).json()["code"]
    og = c.post(f"/api/rooms/{other}/groups", json={"name": "外人"}).json()["join_code"]
    r = c.put(f"/api/join/{og}/verifications/{cid}/report",
              json={"stance": "support", "basis": "x"})
    assert r.status_code == 404
    r = c.get(f"/api/join/{og}")
    assert [v["id"] for v in r.json().get("verifications", [])] == []

    # ---- 红队单组支持 → 仍待核验 ----
    r = c.put(f"/api/join/{red}/verifications/{cid}/report",
              json={"stance": "support", "basis": "看到烟雾"})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["status"] == "pending" and pv["my_report"]["stance"] == "support"
    assert pv["my_report"]["revision"] == 1
    # 参与者视图不含逐组报告/他组依据
    assert "reports" not in pv and "group_ids" not in pv

    # 同组反复修改覆盖：revision 递增，始终一行；仍只有 1 个支持组 → 待核验
    for i in range(3):
        r = c.put(f"/api/join/{red}/verifications/{cid}/report",
                  json={"stance": "support", "basis": f"依据{i}"})
        assert r.status_code == 200
    assert r.json()["my_report"]["revision"] == 4
    host = c.get(f"/api/rooms/{code}").json()
    v = [x for x in host["verifications"] if x["id"] == cid][0]
    assert v["support_count"] == 1 and v["oppose_count"] == 0 and v["status"] == "pending"
    assert len(v["reports"]) == 1 and v["reports"][0]["basis"] == "依据2"

    # 蓝队也支持 → 已证实
    r = c.put(f"/api/join/{blue}/verifications/{cid}/report",
              json={"stance": "support", "basis": "热成像确认"})
    assert r.json()["status"] == "confirmed"
    # 绿队反对 → 有争议
    assert c.put(f"/api/join/{green}/verifications/{cid}/report",
                 json={"stance": "oppose", "basis": "风向不对"}).json()["status"] == "disputed"
    # 绿队撤回 → 回到已证实
    r = c.delete(f"/api/join/{green}/verifications/{cid}/report")
    assert r.status_code == 200
    assert c.get(f"/api/join/{green}").json()["verifications"][0]["status"] == "confirmed"
    # 再撤回 → 404
    assert c.delete(f"/api/join/{green}/verifications/{cid}/report").status_code == 404

    # ---- 立场校验 ----
    r = c.put(f"/api/join/{red}/verifications/{cid}/report",
              json={"stance": "maybe", "basis": ""})
    assert r.status_code == 422

    # ---- 蓝队改投反对（覆盖）→ 有争议；红队也改反对 → 已否定 ----
    c.put(f"/api/join/{blue}/verifications/{cid}/report",
          json={"stance": "oppose", "basis": "热成像其实是 A 区"})
    assert c.get(f"/api/join/{red}").json()["verifications"][0]["status"] == "disputed"
    c.put(f"/api/join/{red}/verifications/{cid}/report",
          json={"stance": "oppose", "basis": "看错了"})
    host = c.get(f"/api/rooms/{code}").json()
    v = [x for x in host["verifications"] if x["id"] == cid][0]
    assert v["status"] == "refuted" and v["support_count"] == 0 and v["oppose_count"] == 2

    # 参与者视图只见本组依据，不含他组
    redview = c.get(f"/api/join/{red}").json()["verifications"][0]
    assert redview["my_report"]["basis"] == "看错了"
    blob = str(redview)
    assert "热成像其实是 A 区" not in blob

    # ---- 观察席：完全不可见 ----
    share = c.post(f"/api/rooms/{code}/shares",
                   json={"group_ids": [rg, bg, gg], "minutes": 30}).json()
    obs = c.get(f"/api/observe/{share['token']}").json()
    assert "verifications" not in obs
    # 观察 WS 快照同样不带
    with c.websocket_connect(f"/ws/observe/{share['token']}?last_event_id=0") as ws:
        snap = ws.receive_json()
        assert "verifications" not in snap

    # 参与者 WS 快照带脱敏命题
    with c.websocket_connect(f"/ws/{red}?last_event_id=0") as ws:
        snap = ws.receive_json()
        pv0 = [x for x in snap["verifications"] if x["id"] == cid][0]
        assert pv0["status"] == "refuted" and "reports" not in pv0

    # ---- 重置：清空报告、命题保留、状态待核验 ----
    # running 需先暂停；当前 idle，直接重置会 409（idle 不能 reset）
    assert c.post(f"/api/rooms/{code}/start").status_code == 200
    assert c.post(f"/api/rooms/{code}/pause").status_code == 200
    assert c.post(f"/api/rooms/{code}/reset").status_code == 200
    host = c.get(f"/api/rooms/{code}").json()
    assert len(host["verifications"]) == 1
    v = host["verifications"][0]
    assert v["status"] == "pending" and v["reports"] == []
    assert v["group_ids"] == sorted([rg, bg, gg])  # 白名单保留
    rv = c.get(f"/api/join/{red}").json()["verifications"][0]
    assert rv["my_report"] is None and rv["status"] == "pending"

    # ---- 删除参与组：级联且状态即时重算 ----
    # 先造两组支持 → confirmed，再删其中一组 → pending
    c.put(f"/api/join/{red}/verifications/{cid}/report",
          json={"stance": "support", "basis": "a"})
    c.put(f"/api/join/{blue}/verifications/{cid}/report",
          json={"stance": "support", "basis": "b"})
    assert c.get(f"/api/rooms/{code}").json()["verifications"][0]["status"] == "confirmed"
    assert c.delete(f"/api/rooms/{code}/groups/{bg}").status_code == 200
    v = c.get(f"/api/rooms/{code}").json()["verifications"][0]
    assert v["status"] == "pending" and bg not in v["group_ids"]
    assert len(v["reports"]) == 1  # 蓝队报告随组删除，仅剩红队

print("integration: all verification API scenarios passed")
