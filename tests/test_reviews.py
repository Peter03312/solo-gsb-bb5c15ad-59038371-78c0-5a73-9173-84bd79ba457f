"""本轮功能测试: 已发布评审任务的正式评分与评语收集。

覆盖需求:
- 评审人凭现有凭据对当前发布版中"已确认且未回避"槽位提交 1~5 整数评分与非空评语;
- 同一有效任务只收一份: 完全相同重试返回原收据 (幂等), 内容不同报 409 冲突;
- 提交事务内核对发布序号、分配关系与决定状态:
  过期/超前序号 409, 未发布/不存在/未分配/已移出 404, pending/已回避 409,
  非法评分/空评语 422, 被拒请求不落库且不推进修订号;
- 会务方可按论文查看两名评审人的提交进度与评语, 含往次发布被移出槽位的追溯评语;
- 补位发布: 连续保留的确认槽位沿用原评语 (同收据), 移出槽位评语仅归档不计进度,
  同一人后来重新获得该稿时不得复用旧评语;
- 评审人仅可查看本人当前有效任务的评语, 看不到另一评审人或作者机构。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-reviews-test-")
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
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
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


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"], dry
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def submit_review(client, rid, pid, serial, score, comment):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def setup_p1(client, reviewers=("R1", "R2", "R3", "R4")):
    for rid in reviewers:
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    return publish(client)


# ------------------------------------------------------------ 提交与幂等/冲突

def test_confirmed_reviewer_can_submit_review(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    revision_before = client.get("/meta", headers=ORG).json()["revision"]

    r = submit_review(client, rid, "P1", serial, 4, "选题有价值, 实验可再补消融。")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["score"] == 4 and body["receipt"].startswith("rvw-")
    assert body["submitted_at"]
    # 评语收集不改变资料修订号
    assert client.get("/meta", headers=ORG).json()["revision"] == revision_before


def test_identical_retry_returns_same_receipt(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    first = submit_review(client, rid, "P1", serial, 3, "结论可靠。").json()
    # 完全相同重试 (含首尾空白差异, 归一化后相同)
    retry = submit_review(client, rid, "P1", serial, 3, "  结论可靠。 ")
    assert retry.status_code == 200
    j = retry.json()
    assert j["changed"] is False
    assert j["receipt"] == first["receipt"]
    assert j["submitted_at"] == first["submitted_at"]
    assert j["comment"] == "结论可靠。"


def test_different_content_is_conflict_and_keeps_original(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    first = submit_review(client, rid, "P1", serial, 3, "初稿评语").json()

    r = submit_review(client, rid, "P1", serial, 5, "改成五分")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["existing"]["receipt"] == first["receipt"]

    r = submit_review(client, rid, "P1", serial, 3, "评语文字也不同")
    assert r.status_code == 409

    # 原评语不变, 相同内容仍可取回原收据
    again = submit_review(client, rid, "P1", serial, 3, "初稿评语").json()
    assert again["receipt"] == first["receipt"]


def test_score_must_be_integer_1_to_5(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    url = f"/reviewer/assignments/P1/review"
    for bad in (0, 6, 2.5, "5", None):
        r = client.post(
            url, headers=reviewer_headers(rid),
            json={"paper_id": "P1", "serial": serial, "score": bad, "comment": "x"},
        )
        assert r.status_code == 422, bad


def test_comment_must_be_nonempty(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    r = submit_review(client, rid, "P1", serial, 5, "   ")
    assert r.status_code == 422
    r = client.post(
        "/reviewer/assignments/P1/review",
        headers=reviewer_headers(rid),
        json={"paper_id": "P1", "serial": serial, "score": 5},
    )
    assert r.status_code == 422


def test_path_paper_id_must_match_body(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    r = client.post(
        "/reviewer/assignments/P1/review",
        headers=reviewer_headers(rid),
        json={"paper_id": "P9", "serial": serial, "score": 5, "comment": "x"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------ 发布序号 / 分配 / 状态核对

def test_stale_serial_rejected(client):
    pub = setup_p1(client)
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    submit_review(client, rid, "P1", pub["serial"], 4, "旧版评语")
    # 一次补位发布推进发布序号 (另一槽位 pending 不影响方案)
    newer = backfill_publish(client)
    assert newer["serial"] == pub["serial"] + 1
    # 用旧序号提交 -> 409 且不落库
    r = submit_review(client, rid, "P1", pub["serial"], 4, "旧序号重试内容")
    assert r.status_code == 409
    # 超前序号同样 409
    r = submit_review(client, rid, "P1", newer["serial"] + 10, 4, "x")
    assert r.status_code == 409


def test_pending_and_recused_slots_reject_review(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    # 未确认 (pending) -> 409
    r = submit_review(client, rid_a, "P1", serial, 4, "还没确认就交评语")
    assert r.status_code == 409
    decide(client, rid_a, "P1", "confirm")
    # 已回避 -> 409
    decide(client, rid_b, "P1", "recuse", reason="利益冲突")
    r = submit_review(client, rid_b, "P1", serial, 4, "回避后交评语")
    assert r.status_code == 409


def test_unassigned_or_missing_paper_or_unpublished_rejected(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    assert pub["plan"]["P1"] == ["R1", "R2"]  # 字典序最小
    # 未分配给本人的评审人 -> 404
    r = submit_review(client, "R4", "P1", serial, 4, "x")
    assert r.status_code == 404
    # 不存在的论文 -> 404
    r = submit_review(client, "R1", "NOPE", serial, 4, "x")
    assert r.status_code == 404


def test_review_without_any_publication_is_404(client):
    add_reviewer(client, "R1", "I1")
    add_paper(client)
    r = submit_review(client, "R1", "P1", 1, 4, "x")
    assert r.status_code == 404


def test_reject_does_not_persist_or_bump(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid = pub["plan"]["P1"][0]
    rev_before = client.get("/meta", headers=ORG).json()["revision"]
    submit_review(client, rid, "P1", serial, 9, "非法评分且未确认")
    submit_review(client, rid, "P1", serial + 1, 4, "过期序号")
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before
    decide(client, rid, "P1", "confirm")
    # 会务方进度显示未提交
    prog = client.get("/papers/P1/reviews", headers=ORG).json()
    assert prog["progress"]["submitted"] == 0


def test_review_requires_valid_credentials(client):
    pub = setup_p1(client)
    r = client.post(
        "/reviewer/assignments/P1/review",
        headers={"X-Reviewer-Id": "R1", "X-Reviewer-Credential": "wrong"},
        json={"paper_id": "P1", "serial": pub["serial"], "score": 5, "comment": "x"},
    )
    assert r.status_code == 401
    assert client.get("/reviewer/reviews",
                     headers={"X-Reviewer-Id": "R1", "X-Reviewer-Credential": "bad"}
                     ).status_code == 401


# ------------------------------------------------------------ 会务方按论文查看

def test_organizer_sees_progress_and_both_reviews(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    submit_review(client, rid_a, "P1", serial, 5, "甲的评语")

    r = client.get("/papers/P1/reviews", headers=ORG)
    assert r.status_code == 200
    j = r.json()
    assert j["serial"] == serial
    assert j["progress"] == {"slots": 2, "confirmed": 2, "submitted": 1, "complete": False}
    by_id = {s["reviewer_id"]: s for s in j["slots"]}
    assert by_id[rid_a]["submitted"] is True
    assert by_id[rid_a]["review"]["score"] == 5
    assert by_id[rid_a]["review"]["comment"] == "甲的评语"
    assert by_id[rid_b]["submitted"] is False and by_id[rid_b]["review"] is None
    assert j["archived_reviews"] == []

    submit_review(client, rid_b, "P1", serial, 2, "乙的评语")
    j = client.get("/papers/P1/reviews", headers=ORG).json()
    assert j["progress"]["submitted"] == 2 and j["progress"]["complete"] is True


def test_organizer_review_endpoint_scoping(client):
    setup_p1(client)
    assert client.get("/papers/P1/reviews").status_code == 401
    assert client.get("/papers/NOPE/reviews", headers=ORG).status_code == 404


# ------------------------------------------------------------ 评审人仅见本人评语

def test_reviewer_sees_only_own_current_reviews(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    submit_review(client, rid_a, "P1", serial, 5, "只属于甲的评语")
    submit_review(client, rid_b, "P1", serial, 1, "只属于乙的评语")

    view_a = client.get("/reviewer/reviews", headers=reviewer_headers(rid_a)).json()
    assert view_a["serial"] == serial
    assert [x["paper_id"] for x in view_a["reviews"]] == ["P1"]
    assert view_a["reviews"][0]["comment"] == "只属于甲的评语"
    assert all(x["comment"] != "只属于乙的评语" for x in view_a["reviews"])
    view_b = client.get("/reviewer/reviews", headers=reviewer_headers(rid_b)).json()
    assert view_b["reviews"][0]["comment"] == "只属于乙的评语"
    # 不含作者机构信息
    assert "Author-P1" not in str(view_a)
    assert "institution" not in str(view_a)


def test_reviewer_reviews_empty_without_publication(client):
    add_reviewer(client, "R1", "I1")
    view = client.get("/reviewer/reviews", headers=reviewer_headers("R1")).json()
    assert view == {"reviewer_id": "R1", "serial": None, "reviews": []}


# ------------------------------------------------------------ 补位: 沿用 / 归档 / 不复用

def test_backfill_carries_review_of_retained_confirmed_slot(client):
    pub = setup_p1(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    rev_a = submit_review(client, rid_a, "P1", serial, 4, "保留槽位的评语").json()

    out = backfill_publish(client)
    new_rid = next(x for x in out["plan"]["P1"] if x != rid_a)
    assert out["carried_confirmations"] == 1
    assert out["carried_reviews"] == 1
    assert rid_a in out["plan"]["P1"] and rid_b not in out["plan"]["P1"]

    # 保留槽位: 同一评语同收据, 评审人按新序号仍可见
    view = client.get("/reviewer/reviews", headers=reviewer_headers(rid_a)).json()
    assert view["serial"] == out["serial"]
    assert view["reviews"][0]["receipt"] == rev_a["receipt"]
    assert view["reviews"][0]["comment"] == "保留槽位的评语"

    # 会务方进度只计当前槽位; 被移出的 rid_b 无评语可归档
    j = client.get("/papers/P1/reviews", headers=ORG).json()
    ids = {s["reviewer_id"] for s in j["slots"]}
    assert ids == {rid_a, new_rid}
    assert j["progress"]["submitted"] == 1
    by_id = {s["reviewer_id"]: s for s in j["slots"]}
    assert by_id[rid_a]["review"]["receipt"] == rev_a["receipt"]
    assert by_id[new_rid]["submitted"] is False


def test_removed_slot_review_is_archived_not_counted_and_cannot_be_reused(client):
    # R1/R2 初始确认; 通过机构变更使 R2 的固定位置失效被移出, 再恢复使其重新获稿
    for rid in ("R1", "R2", "R3"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, institutions=["Author-A"])
    pub = publish(client)
    serial1 = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    old_review = submit_review(client, rid_b, "P1", serial1, 3, "旧方案评语").json()

    # R2 机构变得与固定的 R1 相同 -> 固定对违反不同机构, R2 位置在补位中释放, 由 R3 补入
    client.put(
        f"/reviewers/{rid_b}",
        headers=ORG,
        json={
            "credential": f"cred-{rid_b}", "topics": ["AI"],
            "institution": f"Inst-{rid_a}", "capacity": 3, "avoid_papers": [],
        },
    )
    out2 = backfill_publish(client)
    assert set(out2["plan"]["P1"]) == {rid_a, "R3"}
    # R2 已不在方案, 评语仅归档; 本人当前视图为空
    assert client.get("/reviewer/reviews", headers=reviewer_headers(rid_b)).json()["reviews"] == []

    # R2 机构恢复, 再次补位: pending 位置重排, 字典序最小使 R2 重新获稿
    client.put(
        f"/reviewers/{rid_b}",
        headers=ORG,
        json={
            "credential": f"cred-{rid_b}", "topics": ["AI"],
            "institution": f"Inst-{rid_b}", "capacity": 3, "avoid_papers": [],
        },
    )
    out3 = backfill_publish(client)
    assert set(out3["plan"]["P1"]) == {rid_a, rid_b}

    # 新槽位为 pending, 旧评语未被复用: 未确认前不能交, 确认后须重新交一份新的
    assert submit_review(client, rid_b, "P1", out3["serial"], 3, "旧方案评语").status_code == 409
    decide(client, rid_b, "P1", "confirm")
    view_before = client.get("/reviewer/reviews", headers=reviewer_headers(rid_b)).json()
    assert view_before["reviews"] == []
    new_review = submit_review(client, rid_b, "P1", out3["serial"], 4, "重新获稿后的新评语")
    assert new_review.status_code == 200
    assert new_review.json()["receipt"] != old_review["receipt"]

    # 会务方: 进度只计新方案 (rid_a 的评语留在 serial 1, 不沿用);
    # rid_b 重新提交后进度为 1, rid_a 在新序号尚未交;
    # 往次序号的旧评语在 archived 中可追溯
    j = client.get("/papers/P1/reviews", headers=ORG).json()
    assert j["serial"] == out3["serial"]
    assert j["progress"] == {"slots": 2, "confirmed": 2, "submitted": 1, "complete": False}
    archived_serials = {(a["serial"], a["reviewer_id"]) for a in j["archived_reviews"]}
    assert (serial1, rid_b) in archived_serials
    assert all(a["receipt"] != new_review.json()["receipt"] for a in j["archived_reviews"])


def test_normal_republish_does_not_carry_reviews(client):
    pub = setup_p1(client)
    serial1 = pub["serial"]
    rid = pub["plan"]["P1"][0]
    decide(client, rid, "P1", "confirm")
    old = submit_review(client, rid, "P1", serial1, 4, "旧发布评语").json()

    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    republished = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev}
    ).json()
    assert republished["serial"] == serial1 + 1
    # 新序号槽位全部 pending, 旧评语不沿用
    assert submit_review(
        client, rid, "P1", republished["serial"], 4, "旧发布评语"
    ).status_code == 409
    decide(client, rid, "P1", "confirm")
    fresh = submit_review(client, rid, "P1", republished["serial"], 4, "新发布评语").json()
    assert fresh["receipt"] != old["receipt"]
    j = client.get("/papers/P1/reviews", headers=ORG).json()
    assert j["progress"]["submitted"] == 1
    assert [(a["serial"], a["comment"]) for a in j["archived_reviews"]] == [
        (serial1, "旧发布评语")
    ]
