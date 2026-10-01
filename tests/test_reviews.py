"""本轮功能测试: 已确认任务的正式评语收集。

覆盖需求:
- 评审人凭现有凭据, 对当前发布版中已确认且未回避的任务提交 发布序号 + 1~5 整数评分 + 非空评语;
- 同一事务内核对发布序号/分配关系/决定状态: 过期序号 409, 未分配 404,
  未确认/已回避 409, 被移出任务拒绝, 且均不落库;
- 同一有效任务只收一份: 完全相同的重试返回原收据 (changed=false), 内容不同 409;
- 会务方按论文查看当前两名评审人的提交进度与评语; 历史评语仅供追溯 (archived);
- 补位发布: 连续保留确认的槽位沿用原评语 (收据号不变, 计入新进度);
  被移出槽位不计入; 同一人重新获稿不得复用旧评语;
- 评审人只能看到本人当前有效任务的评语, 看不到另一评审人的评语与作者机构。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-comments-test-")
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
            "assignment_decisions", "reviewer_recusals", "assignment_reviews",
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
    for rid in reviewers:
        add_reviewer(client, rid, f"Inst-{rid}")
    for pid in papers:
        add_paper(client, pid, institutions=[f"Author-{pid}"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def decide(client, rid, pid, decision, reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def current_serial(client, rid):
    return client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()["serial"]


def submit_review(client, rid, pid, score=4, comment="方法扎实, 实验充分", serial=None):
    if serial is None:
        serial = current_serial(client, rid)
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------ 提交评语

def test_submit_review_happy_path(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    rev_before = client.get("/meta", headers=ORG).json()["revision"]

    r = submit_review(client, rid, "P1", score=5, comment="  创新点明确, 建议接收  ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["score"] == 5
    assert body["comment"] == "创新点明确, 建议接收"  # 首尾空白被裁剪
    assert body["receipt"].startswith("rv_")
    assert body["serial"] == current_serial(client, rid)
    assert body["submitted_at"]
    # 评语不推进资料修订号
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before
    # 会务方进度可见
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["serial"] == body["serial"]
    p1 = progress["papers"]["P1"]
    assert p1["expected"] == 2 and p1["submitted"] == 1
    slot = next(s for s in p1["reviewers"] if s["reviewer_id"] == rid)
    assert slot["submitted"] is True and slot["state"] == "confirmed"
    assert slot["review"]["score"] == 5 and slot["review"]["receipt"] == body["receipt"]
    other = next(s for s in p1["reviewers"] if s["reviewer_id"] != rid)
    assert other["submitted"] is False and other["review"] is None
    assert progress["archived"] == []
    # 评审人本人视图可见
    mine = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
    assert [rv["receipt"] for rv in mine["reviews"]] == [body["receipt"]]


def test_identical_retry_returns_original_receipt(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    first = submit_review(client, rid, "P1").json()
    second = submit_review(client, rid, "P1")
    assert second.status_code == 200
    assert second.json()["changed"] is False
    assert second.json()["receipt"] == first["receipt"]
    assert second.json()["submitted_at"] == first["submitted_at"]
    # 仍然只有一份评语
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["papers"]["P1"]["submitted"] == 1


def test_different_content_conflicts(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    submit_review(client, rid, "P1", score=4, comment="可以接收")
    r = submit_review(client, rid, "P1", score=5, comment="可以接收")
    assert r.status_code == 409
    r = submit_review(client, rid, "P1", score=4, comment="换个评语")
    assert r.status_code == 409
    # 冲突不落库: 进度仍按原评语计
    progress = client.get("/assignment/reviews", headers=ORG).json()
    slot = next(s for s in progress["papers"]["P1"]["reviewers"] if s["reviewer_id"] == rid)
    assert slot["review"]["score"] == 4 and slot["review"]["comment"] == "可以接收"


def test_stale_serial_rejected_and_nothing_persisted(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    serial = current_serial(client, rid)
    # 过期/未来序号均拒绝
    for bad in (serial + 1, serial - 1, 0):
        r = submit_review(client, rid, "P1", serial=bad)
        assert r.status_code == 409, (bad, r.text)
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["papers"]["P1"]["submitted"] == 0  # 拒绝不落库
    # 发布序号前进后, 旧序号提交同样过期
    decide(client, plan["P1"][1], "P1", "recuse", reason="冲突")
    backfill_publish(client)
    r = submit_review(client, rid, "P1", serial=serial)
    assert r.status_code == 409


def test_review_requires_confirmed_task(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    # 未确认 (pending) -> 409
    r = submit_review(client, rid_a, "P1")
    assert r.status_code == 409
    # 已回避 -> 409
    decide(client, rid_b, "P1", "recuse", reason="利益冲突")
    r = submit_review(client, rid_b, "P1")
    assert r.status_code == 409
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["papers"]["P1"]["submitted"] == 0


def test_review_unassigned_or_unknown_paper_rejected(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    # 未分配给本人的论文
    r = submit_review(client, "R4", "P1")
    assert r.status_code == 404
    # 不存在的论文
    r = submit_review(client, rid, "NOPE")
    assert r.status_code == 404


def test_review_requires_published(client):
    add_reviewer(client, "R1", "Inst-1")
    add_paper(client, "P1")
    r = submit_review(client, "R1", "P1", serial=1)
    assert r.status_code == 404


def test_review_requires_organizer_key_for_progress(client):
    assert client.get("/assignment/reviews").status_code == 401
    r = client.get("/assignment/reviews", headers=reviewer_headers("R1"))
    assert r.status_code == 401  # 评审人凭据不能代替会务方密钥


def test_review_validation(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    serial = current_serial(client, rid)

    def post(pid_path, body, headers=None):
        return client.post(
            f"/reviewer/assignments/{pid_path}/review",
            headers=headers or reviewer_headers(rid),
            json=body,
        )

    base = {"paper_id": "P1", "serial": serial, "score": 4, "comment": "ok"}
    # 评分必须为 1~5 的整数
    for bad_score in (0, 6, -1, 2.5, "3", True, None):
        assert post("P1", {**base, "score": bad_score}).status_code == 422, bad_score
    # 评语必须非空
    for bad_comment in ("", "   ", None):
        assert post("P1", {**base, "comment": bad_comment}).status_code == 422, repr(bad_comment)
    # 缺字段
    for key in ("paper_id", "serial", "score", "comment"):
        body = {k: v for k, v in base.items() if k != key}
        assert post("P1", body).status_code == 422, key
    # 路径与请求体论文编号不一致
    assert post("P1", {**base, "paper_id": "P2"}).status_code == 422
    # 凭据失效
    bad = {"X-Reviewer-Id": rid, "X-Reviewer-Credential": "wrong"}
    assert post("P1", base, headers=bad).status_code == 401
    # 全部拒绝后仍无评语落库
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["papers"]["P1"]["submitted"] == 0


# ------------------------------------------------------------ 可见性

def test_reviewer_sees_only_own_reviews_and_no_institutions(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    submit_review(client, rid_a, "P1", score=5, comment="甲的评语-建议接收")
    submit_review(client, rid_b, "P1", score=2, comment="乙的评语-建议拒稿")

    view_a = client.get("/reviewer/assignments", headers=reviewer_headers(rid_a))
    assert view_a.status_code == 200
    reviews_a = view_a.json()["reviews"]
    assert len(reviews_a) == 1
    assert reviews_a[0]["comment"] == "甲的评语-建议接收"
    # 看不到另一评审人的评语, 也看不到作者机构
    assert "乙的评语" not in view_a.text
    assert "institutions" not in view_a.text
    assert "Author-P1" not in view_a.text
    view_b = client.get("/reviewer/assignments", headers=reviewer_headers(rid_b))
    assert "甲的评语" not in view_b.text


def test_recuse_after_review_hides_from_reviewer_view(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    submit_review(client, rid, "P1")
    decide(client, rid, "P1", "recuse", reason="事后发现的冲突")
    # 任务不再有效: 本人视图不再列出该评语
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
    assert view["reviews"] == []
    # 会务方仍可在当前发布版看到该槽位已提交的评语 (状态为已回避)
    progress = client.get("/assignment/reviews", headers=ORG).json()
    slot = next(s for s in progress["papers"]["P1"]["reviewers"] if s["reviewer_id"] == rid)
    assert slot["state"] == "recused"
    assert slot["submitted"] is True and slot["review"] is not None


# ------------------------------------------------------------ 补位联动

def test_backfill_carries_review_for_retained_confirmed_slot(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    first = submit_review(client, rid_a, "P1", score=5, comment="沿用我").json()
    decide(client, rid_b, "P1", "recuse", reason="冲突")

    pub = backfill_publish(client)
    assert pub["carried_confirmations"] == 1
    assert pub["carried_reviews"] == 1
    assert rid_a in pub["plan"]["P1"] and rid_b not in pub["plan"]["P1"]

    # 新发布版进度直接计入沿用的评语, 收据号不变
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["serial"] == pub["serial"]
    p1 = progress["papers"]["P1"]
    assert p1["submitted"] == 1
    slot = next(s for s in p1["reviewers"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["receipt"] == first["receipt"]
    assert slot["review"]["comment"] == "沿用我"
    # 原发布序号下的那一份进入追溯区, 并标记已沿用至当前发布版
    assert [a["receipt"] for a in progress["archived"]] == [first["receipt"]]
    assert progress["archived"][0]["carried_into_current"] is True
    # 评审人视图在新发布版仍可见本人评语
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid_a)).json()
    assert [rv["receipt"] for rv in view["reviews"]] == [first["receipt"]]
    # 在新发布序号下完全相同重试 -> 幂等返回原收据
    retry = submit_review(client, rid_a, "P1", score=5, comment="沿用我")
    assert retry.json()["changed"] is False
    assert retry.json()["receipt"] == first["receipt"]


def test_removed_slot_review_archived_not_counted(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    old = submit_review(client, rid_a, "P1", score=4, comment="旧评语").json()
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    backfill_publish(client)  # rid_a 确认槽位保留, 评语沿用
    # 资料变更使 rid_a 的固定槽位失效 (与作者同机构) -> 补位后 rid_a 被移出
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
    pub = backfill_publish(client)
    assert rid_a not in pub["plan"]["P1"]
    assert pub["carried_reviews"] == 0

    progress = client.get("/assignment/reviews", headers=ORG).json()
    p1 = progress["papers"]["P1"]
    assert p1["submitted"] == 0  # 移出槽位的评语不计入新方案进度
    assert all(s["reviewer_id"] != rid_a for s in p1["reviewers"])
    archived = [a for a in progress["archived"] if a["receipt"] == old["receipt"]]
    assert archived and all(a["carried_into_current"] is False for a in archived)
    # 被移出的评审人不再拥有该任务: 提交评语 404, 视图无该论文
    r = submit_review(client, rid_a, "P1")
    assert r.status_code == 404
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid_a)).json()
    assert view["assignments"] == [] and view["reviews"] == []


def test_reassigned_reviewer_cannot_reuse_old_review(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    old = submit_review(client, rid_a, "P1", score=5, comment="旧评语内容").json()
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    backfill_publish(client)  # serial 2: rid_a 保留, 评语沿用
    # rid_a 机构变为作者机构 -> 补位移出 (serial 3)
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
    pub3 = backfill_publish(client)
    assert rid_a not in pub3["plan"]["P1"]
    # rid_a 机构改回 -> 重新获得该稿 (serial 4)
    client.put(
        f"/reviewers/{rid_a}",
        headers=ORG,
        json={
            "credential": f"cred-{rid_a}",
            "topics": ["AI"],
            "institution": f"Inst-{rid_a}",
            "capacity": 3,
            "avoid_papers": [],
        },
    )
    pub4 = backfill_publish(client)
    assert rid_a in pub4["plan"]["P1"]

    # 旧评语不得复用: 本人视图为空, 新进度为零
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid_a)).json()
    assert view["states"]["P1"] == "pending"
    assert view["reviews"] == []
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["papers"]["P1"]["submitted"] == 0
    # 须重新确认后才能提交
    assert submit_review(client, rid_a, "P1").status_code == 409
    decide(client, rid_a, "P1", "confirm")
    # 即使内容与旧评语完全相同, 也是一份新评语 (新收据)
    r = submit_review(client, rid_a, "P1", score=5, comment="旧评语内容")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert r.json()["receipt"] != old["receipt"]
    # 旧评语仍仅供会务方追溯
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["papers"]["P1"]["submitted"] == 1
    receipts_archived = {a["receipt"] for a in progress["archived"]}
    assert old["receipt"] in receipts_archived
    assert r.json()["receipt"] not in receipts_archived


def test_normal_republish_does_not_carry_reviews(client):
    plan = setup_and_publish(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    old = submit_review(client, rid, "P1").json()
    # 普通重新发布: 槽位状态重置, 评语不沿用
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200
    progress = client.get("/assignment/reviews", headers=ORG).json()
    assert progress["serial"] == r.json()["serial"]
    assert progress["papers"]["P1"]["submitted"] == 0
    assert [a["receipt"] for a in progress["archived"]] == [old["receipt"]]
    # 重新确认前不能提交; 确认后可提交新评语
    assert submit_review(client, rid, "P1").status_code == 409
    decide(client, rid, "P1", "confirm")
    r = submit_review(client, rid, "P1", score=3, comment="重新评审后的评语")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert r.json()["receipt"] != old["receipt"]


def test_organizer_progress_requires_published(client):
    add_reviewer(client, "R1", "Inst-1")
    add_paper(client, "P1")
    assert client.get("/assignment/reviews", headers=ORG).status_code == 404
