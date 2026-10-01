import os
import tempfile

# 必须在导入 app 之前设置环境
_TMPDIR = tempfile.mkdtemp(prefix="review-assign-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        conn.execute("DELETE FROM papers")
        conn.execute("DELETE FROM reviewers")
        conn.execute("DELETE FROM published")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=2, topics=None, avoid=None, credential=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": credential or f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid, topics=None, institutions=None, manuscript=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": manuscript if manuscript is not None else f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else ["Univ-X"],
        },
    )


# ------------------------------------------------------------ 基础校验

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_organizer_key_required(client):
    assert client.get("/papers").status_code == 401
    assert client.get("/papers", headers={"X-Organizer-Key": "wrong"}).status_code == 401


def test_duplicate_paper_id_rejected(client):
    assert add_paper(client, "P1").status_code == 201
    r = add_paper(client, "P1")
    assert r.status_code == 409
    assert "重复" in r.json()["detail"]


def test_duplicate_reviewer_id_rejected(client):
    assert add_reviewer(client, "R1", "Inst-A").status_code == 201
    r = add_reviewer(client, "R1", "Inst-B")
    assert r.status_code == 409
    assert "重复" in r.json()["detail"]


@pytest.mark.parametrize("bad_capacity", [0, -3, 2.5, "many", True])
def test_invalid_capacity_rejected(client, bad_capacity):
    r = client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": "R1",
            "credential": "c",
            "topics": [],
            "institution": "Inst-A",
            "capacity": bad_capacity,
            "avoid_papers": [],
        },
    )
    assert r.status_code == 422


def test_update_missing_returns_404(client):
    r = client.put(
        "/papers/NOPE", headers=ORG,
        json={"manuscript": "x", "topics": [], "institutions": []},
    )
    assert r.status_code == 404
    r = client.delete("/reviewers/NOPE", headers=ORG)
    assert r.status_code == 404


def test_revision_bumps_on_data_change(client):
    r1 = add_paper(client, "P1").json()["revision"]
    r2 = add_reviewer(client, "R1", "Inst-A").json()["revision"]
    r3 = client.put(
        "/papers/P1", headers=ORG,
        json={"manuscript": "m", "topics": ["AI"], "institutions": ["Univ-X"]},
    ).json()["revision"]
    r4 = client.delete("/reviewers/R1", headers=ORG).json()["revision"]
    assert r1 < r2 < r3 < r4
    assert client.get("/meta", headers=ORG).json()["revision"] == r4


# ------------------------------------------------------------ 分配规则

def _standard_setup(client):
    # 3 名评审人, 3 个不同机构, 容量均为 2; 3 篇论文
    add_reviewer(client, "R1", "Inst-A", capacity=2)
    add_reviewer(client, "R2", "Inst-B", capacity=2)
    add_reviewer(client, "R3", "Inst-C", capacity=2)
    add_paper(client, "P1", institutions=["Univ-X"])
    add_paper(client, "P2", institutions=["Univ-Y"])
    add_paper(client, "P3", institutions=["Univ-Z"])


def test_dry_run_feasible_and_constraints(client):
    _standard_setup(client)
    r = client.post("/assignment/dry-run", headers=ORG)
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is True
    assert body["unassigned"] == []
    plan = body["plan"]
    assert set(plan) == {"P1", "P2", "P3"}
    inst = {"R1": "Inst-A", "R2": "Inst-B", "R3": "Inst-C"}
    loads = {}
    for pid, pair in plan.items():
        assert len(pair) == 2 and pair[0] != pair[1]
        assert inst[pair[0]] != inst[pair[1]]  # 两名评审人不同机构
        for rid in pair:
            loads[rid] = loads.get(rid, 0) + 1
    assert all(v <= 2 for v in loads.values())  # 不超容量


def test_same_institution_and_avoid_list_excluded(client):
    add_reviewer(client, "R1", "Univ-X")            # 与作者同机构
    add_reviewer(client, "R2", "Inst-B", avoid=["P1"])  # 回避 P1
    add_reviewer(client, "R3", "Inst-C")
    add_reviewer(client, "R4", "Inst-D")
    add_paper(client, "P1", institutions=["Univ-X"])
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assigned = body["plan"]["P1"]
    assert "R1" not in assigned and "R2" not in assigned
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    assert reasons["R1"] == "same_institution_as_author"
    assert reasons["R2"] == "on_reviewer_avoid_list"


def test_at_least_one_expert_required(client):
    add_reviewer(client, "R1", "Inst-A", topics=["DB"])
    add_reviewer(client, "R2", "Inst-B", topics=["DB"])
    add_reviewer(client, "R3", "Inst-C", topics=["AI"])
    add_paper(client, "P1", topics=["AI"])
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert "R3" in body["plan"]["P1"]  # 唯一懂 AI 的评审人必须入选


def test_ratio_minimized_then_lexicographic(client):
    # R1 容量 2, R2/R3 容量 4, 3 篇论文共需 6 个位置。
    # 若只看字典序: P1=(R1,R2), P2=(R1,R2), P3=(R2,R3) -> 最高比例 = 3/4? 否: R1 用满 2/2 = 1。
    # 最小化最高比例 => 3/4, 容量上限 (1,3,3), 字典序最小方案:
    #   P1=(R1,R2), P2=(R2,R3), P3=(R2,R3)
    add_reviewer(client, "R1", "Inst-A", capacity=2)
    add_reviewer(client, "R2", "Inst-B", capacity=4)
    add_reviewer(client, "R3", "Inst-C", capacity=4)
    for pid in ("P1", "P2", "P3"):
        add_paper(client, pid)
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["max_used_capacity_ratio"] == "3/4"
    assert body["plan"] == {"P1": ["R1", "R2"], "P2": ["R2", "R3"], "P3": ["R2", "R3"]}


def test_infeasible_reports_unassigned_and_reasons(client):
    add_reviewer(client, "R1", "Univ-X")                 # 同机构被排除
    add_reviewer(client, "R2", "Inst-B", avoid=["P1"])   # 回避
    add_reviewer(client, "R3", "Inst-C")                 # 唯一合格者
    add_paper(client, "P1", institutions=["Univ-X"])
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["unassigned"] == ["P1"]
    diag = body["diagnostics"]["P1"]
    assert "fewer_than_two_eligible_reviewers" in diag["reasons"]
    reasons = {e["reviewer_id"]: e["reason"] for e in diag["excluded"]}
    assert reasons == {"R1": "same_institution_as_author", "R2": "on_reviewer_avoid_list"}


def test_capacity_shortage_diagnostic(client):
    add_reviewer(client, "R1", "Inst-A", capacity=1)
    add_reviewer(client, "R2", "Inst-B", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")  # 两人容量共 2, 只能满足一篇
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert len(body["plan"]) == 1 and len(body["unassigned"]) == 1
    diag = body["diagnostics"][body["unassigned"][0]]
    assert "all_valid_pairs_blocked_by_capacity" in diag["reasons"]


# ------------------------------------------------------------ 发布与并发

def test_publish_and_stale_revision_rejected(client):
    _standard_setup(client)
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    # 资料变动后, 旧修订号发布必须被拒绝
    add_paper(client, "P4")
    add_reviewer(client, "R4", "Inst-D", capacity=2)
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 409
    # 重新预演拿到新修订号后可以发布
    rev2 = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    assert rev2 > rev
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev2})
    assert r.status_code == 200
    pub = client.get("/assignment", headers=ORG).json()
    assert pub["revision"] == rev2
    assert set(pub["plan"]) == {"P1", "P2", "P3", "P4"}


def test_dry_run_does_not_change_published(client):
    _standard_setup(client)
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    before = client.get("/assignment", headers=ORG).json()
    # 让资料变得不可行, 再预演
    client.delete("/reviewers/R3", headers=ORG)
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    after = client.get("/assignment", headers=ORG).json()
    assert before == after  # 原发布版不变


def test_publish_infeasible_rejected_and_published_kept(client):
    _standard_setup(client)
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    client.delete("/reviewers/R3", headers=ORG)
    rev2 = client.get("/meta", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev2})
    assert r.status_code == 422
    assert client.get("/assignment", headers=ORG).json()["revision"] == rev


def test_concurrent_publish_and_modify_never_stale(client):
    import threading

    _standard_setup(client)
    errors = []

    def modifier():
        try:
            for i in range(20):
                client.put(
                    "/papers/P1", headers=ORG,
                    json={"manuscript": f"v{i}", "topics": ["AI"], "institutions": ["Univ-X"]},
                )
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def publisher():
        try:
            for _ in range(20):
                rev = client.get("/meta", headers=ORG).json()["revision"]
                r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
                assert r.status_code in (200, 409, 422)
                if r.status_code == 200:
                    pub = client.get("/assignment", headers=ORG).json()
                    assert pub["revision"] >= rev  # 发布的方案绝不基于旧版资料
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=modifier), threading.Thread(target=publisher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # 最终: 再发布一次必然成功且与当前修订号一致
    rev = client.get("/meta", headers=ORG).json()["revision"]
    assert client.post("/assignment/publish", headers=ORG, json={"base_revision": rev}).status_code == 200
    assert client.get("/assignment", headers=ORG).json()["revision"] == rev


# ------------------------------------------------------------ 评审人视图

def test_reviewer_reads_only_own_manuscripts(client):
    _standard_setup(client)
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    plan = client.get("/assignment", headers=ORG).json()["plan"]

    seen = {}
    for rid in ("R1", "R2", "R3"):
        r = client.get(
            "/reviewer/assignments",
            headers={"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"},
        )
        assert r.status_code == 200
        mine = r.json()["assignments"]
        expected = sorted(pid for pid, pair in plan.items() if rid in pair)
        assert sorted(p["paper_id"] for p in mine) == expected
        for p in mine:
            assert set(p) == {"paper_id", "topics", "manuscript"}  # 无作者机构
            assert p["manuscript"] == f"manuscript-of-{p['paper_id']}"
        seen[rid] = {p["paper_id"] for p in mine}
    # 每名评审人只能看到自己名下的论文
    for pid, pair in plan.items():
        for rid in ("R1", "R2", "R3"):
            assert (pid in seen[rid]) == (rid in pair)


def test_reviewer_cannot_see_unpublished_plan(client):
    _standard_setup(client)
    client.post("/assignment/dry-run", headers=ORG)  # 仅预演, 未发布
    r = client.get(
        "/reviewer/assignments",
        headers={"X-Reviewer-Id": "R1", "X-Reviewer-Credential": "cred-R1"},
    )
    assert r.json()["assignments"] == []


def test_invalid_reviewer_credential(client):
    add_reviewer(client, "R1", "Inst-A")
    assert client.get("/reviewer/assignments").status_code == 401
    r = client.get(
        "/reviewer/assignments",
        headers={"X-Reviewer-Id": "R1", "X-Reviewer-Credential": "wrong"},
    )
    assert r.status_code == 401
    r = client.get(
        "/reviewer/assignments",
        headers={"X-Reviewer-Id": "GHOST", "X-Reviewer-Credential": "x"},
    )
    assert r.status_code == 401
