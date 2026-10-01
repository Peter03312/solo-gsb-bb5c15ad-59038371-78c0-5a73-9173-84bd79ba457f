"""本轮功能测试: 评审人确认/回避决定、硬回避、补位预演与补位发布。

覆盖需求:
- 评审人凭现有凭据对当前分配的论文确认或提交非空回避原因;
- 同一决定重复提交幂等; 已回避任务不能再确认; 未分配任务拒绝;
- 有效决定推进资料修订号;
- 回避立即停止取稿, 并成为后续分配的硬回避;
- 补位: 已确认且未回避的关系固定, 其余位置遵守原硬约束与优化次序;
- 无完整补位方案返回未补齐论文及限制原因, 不改变发布版;
- 补位发布复核两种版本, 过期拒绝; 成功后按新发布版授权, 确认状态保留。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-backfill-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def setup_and_publish(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1",)):
    """默认: 4 名不同机构、容量 3 的评审人, 1 篇论文。返回发布后的 plan。"""
    inst = {r: f"Inst-{r}" for r in reviewers}
    for rid in reviewers:
        add_reviewer(client, rid, inst[rid])
    for pid in papers:
        add_paper(client, pid, institutions=[f"Author-{pid}"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()["plan"], inst


def decide(client, rid, pid, decision, reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


# ------------------------------------------------------------ 确认 / 回避决定

def test_confirm_assignment(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    r = decide(client, rid, "P1", "confirm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "confirmed" and body["changed"] is True
    # 有效决定推进资料修订号
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == body["revision"]
    # 会务方视图可看到槽位状态
    pub = client.get("/assignment", headers=ORG).json()
    states = {s["reviewer_id"]: s["state"] for s in pub["papers"]["P1"]["slots"]}
    assert states[rid] == "confirmed"
    assert states[plan["P1"][1]] == "pending"


def test_repeated_confirm_is_idempotent_and_no_revision_bump(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    rev1 = decide(client, rid, "P1", "confirm").json()["revision"]
    r2 = decide(client, rid, "P1", "confirm")
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == rev1  # 重复提交不再推进修订号


def test_recuse_requires_nonempty_reason(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    r = decide(client, rid, "P1", "recuse", reason="   ")
    assert r.status_code == 422
    r = decide(client, rid, "P1", "recuse")
    assert r.status_code == 422


def test_recuse_removes_manuscript_access_immediately(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    r = decide(client, rid, "P1", "recuse", reason="与其中一位作者有合作")
    assert r.status_code == 200
    assert r.json()["changed"] is True and r.json()["state"] == "recused"
    # 立即不能再读取该匿名稿
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
    assert [p["paper_id"] for p in view["assignments"]] == []
    assert view["recused"] == [
        {"paper_id": "P1", "reason": "与其中一位作者有合作", "decided_at": view["recused"][0]["decided_at"]}
    ]
    assert view["states"]["P1"] == "recused"


def test_recused_task_cannot_be_confirmed(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "recuse", reason="利益冲突")
    r = decide(client, rid, "P1", "confirm")
    assert r.status_code == 409


def test_repeated_recuse_is_idempotent(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    r1 = decide(client, rid, "P1", "recuse", reason="原因甲")
    rev1 = r1.json()["revision"]
    # 重复回避 (即便原因文本不同) 幂等, 不推进修订号, 保留首次原因
    r2 = decide(client, rid, "P1", "recuse", reason="原因乙")
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == rev1
    assert r2.json()["reason"] == "原因甲"


def test_confirm_then_recuse_is_effective_change_and_bumps(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    rev1 = decide(client, rid, "P1", "confirm").json()["revision"]
    r = decide(client, rid, "P1", "recuse", reason="后来发现的冲突")
    assert r.json()["changed"] is True
    assert r.json()["revision"] > rev1


def test_unassigned_tasks_are_rejected(client):
    setup_and_publish(client, papers=("P1",))
    # 未分配给本人的论文
    r = decide(client, "R4", "P1", "confirm")
    assert r.status_code == 404
    # 不存在的论文
    r = decide(client, "R1", "NOPE", "confirm")
    assert r.status_code == 404


def test_decision_requires_valid_reviewer_credentials(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    r = client.post(
        f"/reviewer/assignments/P1/decision",
        headers={"X-Reviewer-Id": rid, "X-Reviewer-Credential": "wrong"},
        json={"paper_id": "P1", "decision": "confirm"},
    )
    assert r.status_code == 401


def test_other_reviewers_still_read_after_one_recusal(client):
    plan, _ = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "recuse", reason="冲突")
    view_b = client.get("/reviewer/assignments", headers=reviewer_headers(rid_b)).json()
    assert [p["paper_id"] for p in view_b["assignments"]] == ["P1"]
    # 当前发布版仍可供会务方核对
    pub = client.get("/assignment", headers=ORG).json()
    assert set(pub["plan"]["P1"]) == {rid_a, rid_b}


def test_recusal_becomes_hard_avoid_for_future_assignment(client):
    plan, _ = setup_and_publish(client)
    rid_a = plan["P1"][0]
    decide(client, rid_a, "P1", "recuse", reason="冲突")
    # 普通预演: 硬回避必须生效, rid_a 不能再被分到 P1
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert rid_a not in body["plan"]["P1"]
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    assert reasons[rid_a] == "hard_recusal_after_decline"
    # /meta 暴露硬回避清单
    meta = client.get("/meta", headers=ORG).json()
    rec = {(x["reviewer_id"], x["paper_id"]): x for x in meta["hard_recusals"]}
    assert (rid_a, "P1") in rec and rec[(rid_a, "P1")]["reason"] == "冲突"


# ------------------------------------------------------------ 补位预演

def test_backfill_dry_run_pins_confirmed_and_fills_rest(client):
    plan, _ = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    # rid_a 确认 -> 固定; rid_b 回避 -> 释放
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    body = client.post("/assignment/backfill/dry-run", headers=ORG)
    assert body.status_code == 200, body.text
    j = body.json()
    assert j["feasible"] is True
    assert j["plan"]["P1"][0] == rid_a  # 固定者保留 (sorted 在前时)
    assert rid_b not in j["plan"]["P1"]
    new_reviewer = next(x for x in j["plan"]["P1"] if x != rid_a)
    assert new_reviewer in {"R1", "R2", "R3", "R4"} - {rid_a, rid_b}
    assert j["fixed"] == {"P1": [rid_a]}
    assert j["papers"]["P1"]["fixed"] == [rid_a]
    # 补位预演不改变发布版
    pub = client.get("/assignment", headers=ORG).json()
    assert set(pub["plan"]["P1"]) == {rid_a, rid_b}


def test_pending_slot_is_not_pinned(client):
    """未确认 (pending) 的位置不固定, 可在补位中重排。"""
    plan, _ = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_b, "P1", "recuse", reason="冲突")  # rid_a 保持 pending
    j = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert j["fixed"] == {"P1": []}
    assert j["feasible"] is True
    assert rid_b not in j["plan"]["P1"]
    assert len(set(j["plan"]["P1"])) == 2


def test_backfill_dry_run_requires_published(client):
    add_reviewer(client, "R1", "I1")
    add_paper(client, "P1")
    r = client.post("/assignment/backfill/dry-run", headers=ORG)
    assert r.status_code == 404


def test_backfill_infeasible_returns_unassigned_and_reasons_without_change(client):
    # 仅 3 名评审人且机构均不同; 其中两人对 P1 先后处于"固定+回避"死局:
    add_reviewer(client, "R1", "Inst-1", capacity=3)
    add_reviewer(client, "R2", "Inst-2", capacity=3)
    add_reviewer(client, "R3", "Inst-3", capacity=3)
    add_paper(client, "P1", institutions=["Author-A"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    pub = client.get("/assignment", headers=ORG).json()
    a, b = pub["plan"]["P1"]
    remaining = next(rid for rid in ("R1", "R2", "R3") if rid not in (a, b))
    decide(client, a, "P1", "confirm")
    decide(client, b, "P1", "recuse", reason="冲突")
    # 删除唯一可补位的评审人, 使固定者之外凑不出第二名合格评审人
    client.delete(f"/reviewers/{remaining}", headers=ORG)
    before = client.get("/assignment", headers=ORG).json()
    j = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert j["feasible"] is False
    assert j["unassigned"] == ["P1"]
    assert "P1" in j["diagnostics"]
    # 发布版不变
    after = client.get("/assignment", headers=ORG).json()
    assert before["plan"] == after["plan"] == {**before["plan"]}
    assert after["plan"]["P1"] == [a, b]


def test_backfill_respects_capacity_and_other_hard_constraints(client):
    # 容量收紧场景: 全部容量 1, 两篇论文; R1 固定 P1 后, 补位仍须满足容量
    add_reviewer(client, "R1", "Inst-1", capacity=1)
    add_reviewer(client, "R2", "Inst-2", capacity=1)
    add_reviewer(client, "R3", "Inst-3", capacity=1)
    add_reviewer(client, "R4", "Inst-4", capacity=1)
    add_paper(client, "P1", institutions=["A"])
    add_paper(client, "P2", institutions=["B"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    pub = client.get("/assignment", headers=ORG).json()
    # 让 P1 的一人回避; 确认另一人
    a, b = pub["plan"]["P1"]
    decide(client, a, "P1", "confirm")
    decide(client, b, "P1", "recuse", reason="x")
    j = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert j["feasible"] is True
    loads = j["loads"]
    assert all(v <= 1 for v in loads.values())
    assert a in j["plan"]["P1"]
    # 固定者容量已被占, 不能再出现在 P2
    assert a not in j["plan"]["P2"]


# ------------------------------------------------------------ 补位发布

def test_backfill_publish_rechecks_versions_and_keeps_confirmation(client):
    plan, _ = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    rev, serial = dry["revision"], dry["serial"]

    # 期间发生一次成功的补位发布 -> 发布序号变化, 旧预演过期;
    # 该次发布同样保留 rid_a 的确认关系
    r1 = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": rev, "base_serial": serial},
    )
    assert r1.status_code == 200
    # 用过期的 (revision, serial) 再次补位发布 -> 409
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": rev, "base_serial": serial},
    )
    assert r.status_code == 409

    # 重新预演后发布成功, 确认状态在跨次补位发布中继续保留
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["serial"] == dry["serial"] + 1
    assert j["carried_confirmations"] == 1
    assert rid_a in j["plan"]["P1"] and rid_b not in j["plan"]["P1"]

    # 按新发布版授权: 新评审人可取稿, 被移出的 rid_b 不可, rid_a 确认状态保留
    new_rid = next(x for x in j["plan"]["P1"] if x != rid_a)
    assert [p["paper_id"] for p in
            client.get("/reviewer/assignments", headers=reviewer_headers(new_rid)).json()["assignments"]
            ] == ["P1"]
    view_a = client.get("/reviewer/assignments", headers=reviewer_headers(rid_a)).json()
    assert [p["paper_id"] for p in view_a["assignments"]] == ["P1"]
    assert view_a["states"]["P1"] == "confirmed"
    view_b = client.get("/reviewer/assignments", headers=reviewer_headers(rid_b)).json()
    assert view_b["assignments"] == []
    # 会务方看到的是新发布版且序号递增
    pub = client.get("/assignment", headers=ORG).json()
    assert pub["serial"] == j["serial"]
    assert set(pub["plan"]["P1"]) == {rid_a, new_rid}


def test_backfill_publish_stale_on_revision_change(client):
    plan, _ = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    # 资料变更使修订号前进
    add_paper(client, "P2", institutions=["Z"])
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 409


def test_backfill_publish_infeasible_rejected_and_plan_kept(client):
    add_reviewer(client, "R1", "Inst-1")
    add_reviewer(client, "R2", "Inst-2")
    add_paper(client, "P1", institutions=["A"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    pub = client.get("/assignment", headers=ORG).json()
    a, b = pub["plan"]["P1"]
    decide(client, a, "P1", "confirm")
    decide(client, b, "P1", "recuse", reason="冲突")
    # 仅剩固定者一名合格评审人 -> 无法补齐
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is False
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 422
    after = client.get("/assignment", headers=ORG).json()
    assert set(after["plan"]["P1"]) == {a, b}  # 发布版不变
    assert after["serial"] == dry["serial"]


def test_normal_publish_and_auth_conventions_still_work(client):
    # 普通预演/发布链路不受影响, 修订号过期仍 409
    plan, _ = setup_and_publish(client)
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    add_paper(client, "P9", institutions=["Q"])
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev}
    ).status_code == 409
    # 无凭据仍 401
    assert client.post("/assignment/backfill/dry-run").status_code == 401


def test_confirm_then_recuse_revokes_access(client):
    plan, _ = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    headers = reviewer_headers(rid)
    assert client.get("/reviewer/assignments", headers=headers).json()["states"]["P1"] == "confirmed"
    decide(client, rid, "P1", "recuse", reason="新发现的关系")
    view = client.get("/reviewer/assignments", headers=headers).json()
    assert [p["paper_id"] for p in view["assignments"]] == []
    assert view["states"]["P1"] == "recused"


def test_data_change_invalidating_fixed_releases_slot_with_reason(client):
    plan, inst = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    # 资料变更: rid_a 机构变成 P1 作者机构 -> 固定关系失效, 位置释放
    client.put(
        f"/reviewers/{rid_a}",
        headers=ORG,
        json={
            "credential": f"cred-{rid_a}",
            "topics": ["AI"],
            "institution": "Author-P1",
            "capacity": 3,
            "avoid_papers": [],
        },
    )
    j = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert j["feasible"] is True
    problems = {
        (p["reviewer_id"], p["reason"])
        for p in j["fixed_problems"].get("P1", [])
    }
    assert (rid_a, "same_institution_as_author") in problems
    assert j["fixed"].get("P1", []) == []
    assert rid_a not in j["plan"]["P1"]


def test_normal_republish_resets_slot_states_but_keeps_hard_recusal(client):
    plan, _ = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    # 普通重新发布: 槽位决定按新发布序号重新开始 (全部 pending), 但硬回避仍然生效
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200
    assert rid_b not in r.json()["plan"]["P1"]
    pub = client.get("/assignment", headers=ORG).json()
    assert all(s["state"] == "pending" for s in pub["papers"]["P1"]["slots"])
    # 新方案中 rid_b 依旧被硬回避挡在 P1 外
    assert rid_b not in pub["plan"]["P1"]
