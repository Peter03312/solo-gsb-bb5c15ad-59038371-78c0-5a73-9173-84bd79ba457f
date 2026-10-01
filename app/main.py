"""双盲论文评审分配服务 - HTTP API。

角色:
  - 会务方: 凭本地密钥 (请求头 X-Organizer-Key) 维护论文/评审人资料, 预演并发布分配方案;
  - 评审人: 凭自身凭据 (X-Reviewer-Id + X-Reviewer-Credential) 仅可读取已分配给自己的匿名稿。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    AssignmentDecisionIn,
    BackfillPublishIn,
    PaperIn,
    PaperUpdate,
    PublishIn,
    ReviewerIn,
    ReviewerUpdate,
    ReviewSubmissionIn,
)
from .solver import Paper, Reviewer, compute_assignment, compute_backfill

ORGANIZER_KEY = os.environ.get("ORGANIZER_KEY", "dev-organizer-key")

app = FastAPI(title="双盲论文评审分配服务", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "detail": "请求参数校验失败 (例如容量不是 >=1 的整数、缺少必填字段)",
            "errors": jsonable_encoder(exc.errors()),
        },
    )


# ---------------------------------------------------------------- 鉴权

def require_organizer(x_organizer_key: str = Header(default=None)) -> bool:
    if not x_organizer_key or x_organizer_key != ORGANIZER_KEY:
        raise HTTPException(status_code=401, detail="会务方密钥无效 (invalid organizer key)")
    return True


def require_reviewer(
    x_reviewer_id: str = Header(default=None),
    x_reviewer_credential: str = Header(default=None),
) -> str:
    if not x_reviewer_id or not x_reviewer_credential:
        raise HTTPException(status_code=401, detail="缺少评审人编号或凭据 (missing reviewer credentials)")
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT credential FROM reviewers WHERE reviewer_id = ?", (x_reviewer_id,)
        ).fetchone()
    if row is None or row["credential"] != x_reviewer_credential:
        raise HTTPException(status_code=401, detail="评审人凭据无效 (invalid reviewer credentials)")
    return x_reviewer_id


# ---------------------------------------------------------------- 工具

def _hard_recusals(conn):
    """返回 {reviewer_id: set(paper_id)}: 评审人通过决定接口声明过的硬回避。"""
    out = {}
    for r in conn.execute("SELECT reviewer_id, paper_id FROM reviewer_recusals").fetchall():
        out.setdefault(r["reviewer_id"], set()).add(r["paper_id"])
    return out


def _recused_pairs(conn):
    """返回 {(reviewer_id, paper_id)}: 供补位求解器单独标注诊断原因。"""
    return {
        (r["reviewer_id"], r["paper_id"])
        for r in conn.execute("SELECT reviewer_id, paper_id FROM reviewer_recusals").fetchall()
    }


def _load_domain(conn):
    """加载当前资料。评审人声明过的硬回避与其录入的 avoid_papers 合并后生效。"""
    hard = _hard_recusals(conn)
    papers = [
        Paper(
            paper_id=r["paper_id"],
            topics=frozenset(json.loads(r["topics"])),
            institutions=frozenset(json.loads(r["institutions"])),
        )
        for r in conn.execute("SELECT * FROM papers").fetchall()
    ]
    reviewers = []
    for r in conn.execute("SELECT * FROM reviewers").fetchall():
        avoids = set(json.loads(r["avoid_papers"])) | hard.get(r["reviewer_id"], set())
        reviewers.append(
            Reviewer(
                reviewer_id=r["reviewer_id"],
                topics=frozenset(json.loads(r["topics"])),
                institution=r["institution"],
                capacity=r["capacity"],
                avoid_papers=frozenset(avoids),
            )
        )
    return papers, reviewers


def _paper_row_to_dict(row):
    return {
        "paper_id": row["paper_id"],
        "manuscript": row["manuscript"],
        "topics": json.loads(row["topics"]),
        "institutions": json.loads(row["institutions"]),
    }


def _reviewer_row_to_dict(row):
    return {
        "reviewer_id": row["reviewer_id"],
        "credential": row["credential"],
        "topics": json.loads(row["topics"]),
        "institution": row["institution"],
        "capacity": row["capacity"],
        "avoid_papers": json.loads(row["avoid_papers"]),
    }


def _load_published(conn):
    """读取当前发布版; 不存在返回 None。"""
    return conn.execute(
        "SELECT serial, revision, plan, explanations, published_at FROM published WHERE id = 1"
    ).fetchone()


def _load_decisions(conn, serial):
    """读取某发布序号下每个分配槽位的决定。返回 {(paper_id, reviewer_id): row}。"""
    return {
        (d["paper_id"], d["reviewer_id"]): d
        for d in conn.execute(
            "SELECT paper_id, reviewer_id, state, reason, decided_at"
            " FROM assignment_decisions WHERE serial = ?",
            (serial,),
        ).fetchall()
    }


def _load_reviews(conn, serial):
    """读取某发布序号下每个分配槽位的评语。返回 {(paper_id, reviewer_id): row}。"""
    return {
        (r["paper_id"], r["reviewer_id"]): r
        for r in conn.execute(
            "SELECT paper_id, reviewer_id, score, comment, receipt, submitted_at"
            " FROM assignment_reviews WHERE serial = ?",
            (serial,),
        ).fetchall()
    }


def _review_receipt(serial, paper_id, reviewer_id, score, comment, submitted_at):
    """评语收据号: 由任务标识与内容哈希生成; 补位沿用时收据号不变, 同一收据号即同一份评语。"""
    raw = f"{serial}|{paper_id}|{reviewer_id}|{score}|{comment}|{submitted_at}"
    return "rv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _confirmed_map(plan, decisions):
    """已确认且未回避的关系: {paper_id: [reviewer_id, ...]}, 作为补位固定位置。"""
    fixed = {}
    for pid, pair in plan.items():
        kept = [
            rid for rid in pair
            if (d := decisions.get((pid, rid))) is not None and d["state"] == "confirmed"
        ]
        if kept:
            fixed[pid] = kept
    return fixed


def _build_backfill(conn, published_row):
    """基于当前发布版与当前资料执行补位求解, 返回求解结果字典。"""
    plan = json.loads(published_row["plan"])
    serial = published_row["serial"]
    decisions = _load_decisions(conn, serial)
    fixed = _confirmed_map(plan, decisions)
    all_papers, all_reviewers = _load_domain(conn)
    paper_ids = {p.paper_id for p in all_papers}
    scoped_papers = [p for p in all_papers if p.paper_id in plan and p.paper_id in paper_ids]
    result = compute_backfill(
        scoped_papers, all_reviewers, fixed, recused_pairs=_recused_pairs(conn)
    )
    result["serial"] = serial
    result["published_revision"] = published_row["revision"]
    return result


# ---------------------------------------------------------------- 基础

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/meta")
def meta(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        published = _load_published(conn)
        recusals = [
            {
                "reviewer_id": r["reviewer_id"],
                "paper_id": r["paper_id"],
                "reason": r["reason"],
                "created_serial": r["created_serial"],
                "created_revision": r["created_revision"],
            }
            for r in conn.execute(
                "SELECT reviewer_id, paper_id, reason, created_serial, created_revision"
                " FROM reviewer_recusals ORDER BY reviewer_id, paper_id"
            ).fetchall()
        ]
    return {
        "revision": rev,
        "published": (
            {"serial": published["serial"], "revision": published["revision"],
             "published_at": published["published_at"]}
            if published
            else None
        ),
        "hard_recusals": recusals,
    }


# ---------------------------------------------------------------- 论文资料 (会务方)

@app.post("/papers", status_code=201)
def create_paper(body: PaperIn, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (body.paper_id,)).fetchone():
            raise HTTPException(status_code=409, detail=f"论文编号重复: {body.paper_id}")
        conn.execute(
            "INSERT INTO papers(paper_id, manuscript, topics, institutions) VALUES (?,?,?,?)",
            (body.paper_id, body.manuscript, json.dumps(body.topics), json.dumps(body.institutions)),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": body.paper_id, "revision": rev}


@app.put("/papers/{paper_id}")
def update_paper(paper_id: str, body: PaperUpdate, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if not conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"论文不存在: {paper_id}")
        conn.execute(
            "UPDATE papers SET manuscript = ?, topics = ?, institutions = ? WHERE paper_id = ?",
            (body.manuscript, json.dumps(body.topics), json.dumps(body.institutions), paper_id),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": paper_id, "revision": rev}


@app.delete("/papers/{paper_id}")
def delete_paper(paper_id: str, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        cur = conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"论文不存在: {paper_id}")
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": paper_id, "revision": rev}


@app.get("/papers")
def list_papers(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rows = conn.execute("SELECT * FROM papers ORDER BY paper_id").fetchall()
    return {"papers": [_paper_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------- 评审人资料 (会务方)

@app.post("/reviewers", status_code=201)
def create_reviewer(body: ReviewerIn, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if conn.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (body.reviewer_id,)
        ).fetchone():
            raise HTTPException(status_code=409, detail=f"评审人编号重复: {body.reviewer_id}")
        conn.execute(
            "INSERT INTO reviewers(reviewer_id, credential, topics, institution, capacity, avoid_papers)"
            " VALUES (?,?,?,?,?,?)",
            (
                body.reviewer_id,
                body.credential,
                json.dumps(body.topics),
                body.institution,
                body.capacity,
                json.dumps(body.avoid_papers),
            ),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": body.reviewer_id, "revision": rev}


@app.put("/reviewers/{reviewer_id}")
def update_reviewer(reviewer_id: str, body: ReviewerUpdate, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if not conn.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (reviewer_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        conn.execute(
            "UPDATE reviewers SET credential = ?, topics = ?, institution = ?, capacity = ?,"
            " avoid_papers = ? WHERE reviewer_id = ?",
            (
                body.credential,
                json.dumps(body.topics),
                body.institution,
                body.capacity,
                json.dumps(body.avoid_papers),
                reviewer_id,
            ),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": reviewer_id, "revision": rev}


@app.delete("/reviewers/{reviewer_id}")
def delete_reviewer(reviewer_id: str, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        cur = conn.execute("DELETE FROM reviewers WHERE reviewer_id = ?", (reviewer_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": reviewer_id, "revision": rev}


@app.get("/reviewers")
def list_reviewers(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rows = conn.execute("SELECT * FROM reviewers ORDER BY reviewer_id").fetchall()
    return {"reviewers": [_reviewer_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------- 预演 / 发布 (会务方)

@app.post("/assignment/dry-run")
def dry_run(_: bool = Depends(require_organizer)):
    """基于当前资料试算分配方案; 不改动已发布版本。返回所依据的资料修订号。"""
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        papers, reviewers = _load_domain(conn)
        recused = _recused_pairs(conn)
    result = compute_assignment(papers, reviewers, recused)
    result["revision"] = rev
    return result


@app.post("/assignment/publish")
def publish(body: PublishIn, _: bool = Depends(require_organizer)):
    """发布分配方案。必须携带预演所用资料修订号; 资料已变动则拒绝 (409)。

    校验修订号、重算方案、写入发布版在同一个写事务内完成,
    与资料修改并发时不会产生基于旧版资料的方案。
    """
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        if body.base_revision != rev:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"资料已变更: 预演基于修订号 {body.base_revision}, "
                    f"当前修订号为 {rev}, 请重新预演后再发布"
                ),
            )
        papers, reviewers = _load_domain(conn)
        result = compute_assignment(papers, reviewers, _recused_pairs(conn))
        if not result["feasible"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前资料下不存在完整分配方案, 无法发布",
                    "unassigned": result["unassigned"],
                    "diagnostics": result["diagnostics"],
                },
            )
        conn.execute(
            "INSERT INTO published(id, serial, revision, plan, explanations, published_at)"
            " VALUES (1, 1, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET serial = published.serial + 1,"
            " revision = excluded.revision, plan = excluded.plan,"
            " explanations = excluded.explanations, published_at = excluded.published_at",
            (
                rev,
                json.dumps(result["plan"], ensure_ascii=False),
                json.dumps(result["papers"], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        serial = conn.execute("SELECT serial FROM published WHERE id = 1").fetchone()["serial"]
    return {"ok": True, "serial": serial, "revision": rev, "plan": result["plan"]}


@app.get("/assignment")
def get_published(_: bool = Depends(require_organizer)):
    """会务方查询当前已发布方案、每个分配槽位的确认/回避状态及排除原因。"""
    with db.read_txn() as conn:
        row = _load_published(conn)
        if row is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案")
        decisions = _load_decisions(conn, row["serial"])
        plan = json.loads(row["plan"])
        papers = json.loads(row["explanations"])
    for pid, pair in plan.items():
        slots = []
        for rid in pair:
            d = decisions.get((pid, rid))
            slots.append(
                {
                    "reviewer_id": rid,
                    "state": d["state"] if d else "pending",
                    "reason": d["reason"] if d else None,
                    "decided_at": d["decided_at"] if d else None,
                }
            )
        papers.setdefault(pid, {"assigned": pair})["slots"] = slots
    return {
        "serial": row["serial"],
        "revision": row["revision"],
        "published_at": row["published_at"],
        "plan": plan,
        "papers": papers,
    }


@app.get("/assignment/reviews")
def organizer_review_progress(_: bool = Depends(require_organizer)):
    """会务方按论文查看当前发布版两名评审人的评语提交进度与评语内容。

    - 进度仅统计当前发布序号上的评语 (随连续保留的确认槽位沿用过来的评语计入);
    - 历史发布序号上的评语 (移出槽位 / 重新发布遗留) 列入 archived 仅供追溯,
      不计入当前进度; carried_into_current 标记该份评语是否被沿用至当前发布版。
    """
    with db.read_txn() as conn:
        row = _load_published(conn)
        if row is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案")
        serial = row["serial"]
        plan = json.loads(row["plan"])
        decisions = _load_decisions(conn, serial)
        current = _load_reviews(conn, serial)
        current_receipts = {r["receipt"] for r in current.values()}
        papers = {}
        for pid in sorted(plan):
            slots, submitted = [], 0
            for rid in plan[pid]:
                d = decisions.get((pid, rid))
                rv = current.get((pid, rid))
                if rv is not None:
                    submitted += 1
                slots.append(
                    {
                        "reviewer_id": rid,
                        "state": d["state"] if d else "pending",
                        "submitted": rv is not None,
                        "review": (
                            {
                                "score": rv["score"],
                                "comment": rv["comment"],
                                "receipt": rv["receipt"],
                                "submitted_at": rv["submitted_at"],
                            }
                            if rv is not None
                            else None
                        ),
                    }
                )
            papers[pid] = {
                "expected": len(plan[pid]),
                "submitted": submitted,
                "reviewers": slots,
            }
        archived = [
            {
                "serial": r["serial"],
                "paper_id": r["paper_id"],
                "reviewer_id": r["reviewer_id"],
                "score": r["score"],
                "comment": r["comment"],
                "receipt": r["receipt"],
                "submitted_at": r["submitted_at"],
                "carried_into_current": r["receipt"] in current_receipts,
            }
            for r in conn.execute(
                "SELECT * FROM assignment_reviews WHERE serial != ?"
                " ORDER BY serial, paper_id, reviewer_id",
                (serial,),
            ).fetchall()
        ]
    return {
        "serial": serial,
        "revision": row["revision"],
        "papers": papers,
        "archived": archived,
    }


# ---------------------------------------------------------------- 评审人视图

# ---------------------------------------------------------------- 评审人决定与视图

@app.post("/reviewer/assignments/{paper_id}/decision")
def submit_decision(
    paper_id: str,
    body: AssignmentDecisionIn,
    reviewer_id: str = Depends(require_reviewer),
):
    """评审人对当前发布版中分配给自己的论文确认接受或声明回避。

    - 同一决定重复提交幂等 (不推进修订号);
    - 已回避的任务不能再确认 (409);
    - 未分配给本人 / 论文不存在 / 尚未发布 -> 404 (视为未分配任务, 拒绝操作);
    - 回避原因必须非空 (422);
    - 有效决定 (新确认、首次回避、确认后改回避) 推进资料修订号,
      回避立即撤销该评审人对本匿名稿的读取, 并成为后续分配的硬回避。
    """
    if body.paper_id != paper_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的论文编号 ({paper_id}) 与请求体 ({body.paper_id}) 不一致",
        )
    if body.decision == "recuse":
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(status_code=422, detail="回避原因必须非空 (recuse reason is required)")
    else:
        reason = None

    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 不存在可操作的任务")
        plan = json.loads(published["plan"])
        pair = plan.get(paper_id)
        if not pair or reviewer_id not in pair:
            raise HTTPException(
                status_code=404,
                detail=f"当前发布版中论文 {paper_id} 未分配给 {reviewer_id}, 拒绝操作",
            )
        serial = published["serial"]
        existing = conn.execute(
            "SELECT state, reason FROM assignment_decisions"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (serial, paper_id, reviewer_id),
        ).fetchone()

        if body.decision == "confirm":
            if existing is not None and existing["state"] == "recused":
                raise HTTPException(
                    status_code=409,
                    detail="该任务已声明回避, 不能再确认; 如需参与请联系会务方",
                )
            if existing is not None and existing["state"] == "confirmed":
                changed = False
                rev = db.get_revision(conn)
            else:
                conn.execute(
                    "INSERT INTO assignment_decisions"
                    "(serial, paper_id, reviewer_id, state, reason, decided_at)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(serial, paper_id, reviewer_id)"
                    " DO UPDATE SET state = excluded.state, reason = excluded.reason,"
                    " decided_at = excluded.decided_at",
                    (serial, paper_id, reviewer_id, "confirmed", None, now),
                )
                rev = db.bump_revision(conn)
                changed = True
            return {
                "ok": True,
                "paper_id": paper_id,
                "reviewer_id": reviewer_id,
                "state": "confirmed",
                "changed": changed,
                "revision": rev,
                "serial": serial,
            }

        # decision == "recuse"
        if existing is not None and existing["state"] == "recused":
            changed = False
            rev = db.get_revision(conn)
        else:
            conn.execute(
                "INSERT INTO assignment_decisions"
                "(serial, paper_id, reviewer_id, state, reason, decided_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(serial, paper_id, reviewer_id)"
                " DO UPDATE SET state = excluded.state, reason = excluded.reason,"
                " decided_at = excluded.decided_at",
                (serial, paper_id, reviewer_id, "recused", reason, now),
            )
            conn.execute(
                "INSERT INTO reviewer_recusals"
                "(reviewer_id, paper_id, reason, created_serial, created_revision, created_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(reviewer_id, paper_id) DO UPDATE SET"
                " reason = excluded.reason, created_serial = excluded.created_serial,"
                " created_revision = excluded.created_revision, created_at = excluded.created_at",
                (reviewer_id, paper_id, reason, serial, db.get_revision(conn) + 1, now),
            )
            rev = db.bump_revision(conn)
            changed = True
        return {
            "ok": True,
            "paper_id": paper_id,
            "reviewer_id": reviewer_id,
            "state": "recused",
            "reason": reason if changed else existing["reason"],
            "changed": changed,
            "revision": rev,
            "serial": serial,
        }


@app.post("/reviewer/assignments/{paper_id}/review")
def submit_review(
    paper_id: str,
    body: ReviewSubmissionIn,
    reviewer_id: str = Depends(require_reviewer),
):
    """评审人对当前发布版中本人已确认且未回避的任务提交正式评语 (评分 + 评语)。

    - 在同一个写事务内核对: 发布序号 (过期 409)、分配关系 (未分配/论文不存在 404)、
      决定状态 (未确认/已回避 409); 所有拒绝均不落库;
    - 同一有效任务只收一份评语: 完全相同的重试返回原收据 (changed=false),
      内容不同则 409 冲突;
    - 评分须为 1~5 的整数, 评语须非空 (422);
    - 评语不推进资料修订号 (不影响分配方案的版本判定)。
    """
    if body.paper_id != paper_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的论文编号 ({paper_id}) 与请求体 ({body.paper_id}) 不一致",
        )
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=422, detail="评语必须非空 (comment is required)")

    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 不存在可操作的任务")
        serial = published["serial"]
        if body.serial != serial:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"发布序号已过期: 提交针对发布序号 {body.serial}, "
                    f"当前发布序号为 {serial}, 请核对当前发布版后重新提交"
                ),
            )
        plan = json.loads(published["plan"])
        pair = plan.get(paper_id)
        if not pair or reviewer_id not in pair:
            raise HTTPException(
                status_code=404,
                detail=f"当前发布版中论文 {paper_id} 未分配给 {reviewer_id}, 拒绝操作",
            )
        decision = conn.execute(
            "SELECT state FROM assignment_decisions"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (serial, paper_id, reviewer_id),
        ).fetchone()
        if decision is not None and decision["state"] == "recused":
            raise HTTPException(status_code=409, detail="该任务已声明回避, 不能提交评语")
        if decision is None or decision["state"] != "confirmed":
            raise HTTPException(
                status_code=409,
                detail="该任务尚未确认, 仅已确认且未回避的任务可提交评语",
            )
        existing = conn.execute(
            "SELECT score, comment, receipt, submitted_at FROM assignment_reviews"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (serial, paper_id, reviewer_id),
        ).fetchone()
        if existing is not None:
            if existing["score"] == body.score and existing["comment"] == comment:
                # 完全相同的重试: 幂等返回原收据
                return {
                    "ok": True,
                    "paper_id": paper_id,
                    "reviewer_id": reviewer_id,
                    "serial": serial,
                    "score": existing["score"],
                    "comment": existing["comment"],
                    "receipt": existing["receipt"],
                    "submitted_at": existing["submitted_at"],
                    "changed": False,
                }
            raise HTTPException(
                status_code=409,
                detail="该任务已提交过评语, 同一有效任务只收一份; 内容不同的重复提交被拒绝",
            )
        receipt = _review_receipt(serial, paper_id, reviewer_id, body.score, comment, now)
        conn.execute(
            "INSERT INTO assignment_reviews"
            "(serial, paper_id, reviewer_id, score, comment, receipt, submitted_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (serial, paper_id, reviewer_id, body.score, comment, receipt, now),
        )
    return {
        "ok": True,
        "paper_id": paper_id,
        "reviewer_id": reviewer_id,
        "serial": serial,
        "score": body.score,
        "comment": comment,
        "receipt": receipt,
        "submitted_at": now,
        "changed": True,
    }


@app.get("/reviewer/assignments")
def reviewer_assignments(reviewer_id: str = Depends(require_reviewer)):
    """评审人凭自身凭据读取已分配给自己的匿名稿 (仅当前发布版, 不含作者机构)。

    - 已回避的论文立即从取稿列表中移除 (仅在 recused 中保留编号与本人填写的原因);
    - 补位重新发布后, 不在新方案中的关系不再授权取稿;
    - reviews 仅列出本人当前有效 (已确认) 任务已提交的评语,
      不包含另一评审人的评语。
    """
    with db.read_txn() as conn:
        row = _load_published(conn)
        if row is None:
            return {"reviewer_id": reviewer_id, "serial": None,
                    "assignments": [], "recused": [], "reviews": []}
        serial = row["serial"]
        plan = json.loads(row["plan"])
        decisions = _load_decisions(conn, serial)
        reviews = _load_reviews(conn, serial)
        mine = sorted(pid for pid, rs in plan.items() if reviewer_id in rs)
        out, recused, states, my_reviews = [], [], {}, []
        for pid in mine:
            d = decisions.get((pid, reviewer_id))
            state = d["state"] if d else "pending"
            states[pid] = state
            if state == "recused":
                recused.append({"paper_id": pid, "reason": d["reason"], "decided_at": d["decided_at"]})
                continue
            if state == "confirmed":
                rv = reviews.get((pid, reviewer_id))
                if rv is not None:
                    my_reviews.append(
                        {
                            "paper_id": pid,
                            "score": rv["score"],
                            "comment": rv["comment"],
                            "receipt": rv["receipt"],
                            "submitted_at": rv["submitted_at"],
                        }
                    )
            paper = conn.execute(
                "SELECT paper_id, topics, manuscript FROM papers WHERE paper_id = ?", (pid,)
            ).fetchone()
            if paper is not None:
                out.append(
                    {
                        "paper_id": paper["paper_id"],
                        "topics": json.loads(paper["topics"]),
                        "manuscript": paper["manuscript"],
                    }
                )
    return {
        "reviewer_id": reviewer_id,
        "serial": serial,
        "assignments": out,
        "recused": recused,
        "states": states,
        "reviews": my_reviews,
    }


# ---------------------------------------------------------------- 补位 (会务方)

@app.post("/assignment/backfill/dry-run")
def backfill_dry_run(_: bool = Depends(require_organizer)):
    """以当前发布版及资料修订号预演补位, 不改变发布版。

    已确认且未回避的关系固定, 其余位置在原有硬约束与优化次序下重算;
    无完整补位方案时返回未补齐论文及限制原因 (feasible=false)。
    响应中的 revision 与 serial 供补位发布复核。
    """
    with db.read_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无可补位版本")
        rev = db.get_revision(conn)
        result = _build_backfill(conn, published)
    result["revision"] = rev
    return result


@app.post("/assignment/backfill/publish")
def backfill_publish(body: BackfillPublishIn, _: bool = Depends(require_organizer)):
    """补位发布。必须同时复核资料修订号与发布序号, 任一过期则 409。

    成功后:
      - 当前发布版被新发布版替换 (发布序号 +1), 仅按新发布版授权取稿;
      - 仍在新方案中的 (论文, 评审人) 槽位保留其确认状态, 并连同该槽位
        已提交的评语一并沿用 (收据号不变, 计入新方案进度);
      - 被移出方案的槽位评语保留在原发布序号下, 仅供会务方追溯, 不计入新进度;
      - 无完整补位方案时 422, 当前发布版保持不变。
    """
    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无可补位版本")
        rev = db.get_revision(conn)
        serial = published["serial"]
        if body.base_revision != rev or body.base_serial != serial:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"补位预演已过期: 预演基于 修订号 {body.base_revision}/发布序号 {body.base_serial},"
                    f" 当前为 修订号 {rev}/发布序号 {serial},"
                    " 期间存在资料变更或新的发布, 请重新预演"
                ),
            )
        result = _build_backfill(conn, published)
        if not result["feasible"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前发布版无法补齐为完整方案, 补位发布拒绝",
                    "unassigned": result["unassigned"],
                    "diagnostics": result["diagnostics"],
                },
            )
        new_plan = result["plan"]
        old_decisions = _load_decisions(conn, serial)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE published SET serial = serial + 1, revision = ?, plan = ?,"
            " explanations = ?, published_at = ? WHERE id = 1",
            (
                rev,
                json.dumps(new_plan, ensure_ascii=False),
                json.dumps(result["papers"], ensure_ascii=False),
                now,
            ),
        )
        new_serial = serial + 1
        # 仍在新方案中的槽位保留确认状态 (已回避槽位不可能出现在新方案中),
        # 其已提交的评语一并沿用到新发布序号下 (收据号与内容不变)
        old_reviews = _load_reviews(conn, serial)
        carried = 0
        carried_reviews = 0
        for pid, pair in new_plan.items():
            for rid in pair:
                d = old_decisions.get((pid, rid))
                if d is not None and d["state"] == "confirmed":
                    conn.execute(
                        "INSERT INTO assignment_decisions"
                        "(serial, paper_id, reviewer_id, state, reason, decided_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (new_serial, pid, rid, "confirmed", None, d["decided_at"]),
                    )
                    carried += 1
                    rv = old_reviews.get((pid, rid))
                    if rv is not None:
                        conn.execute(
                            "INSERT INTO assignment_reviews"
                            "(serial, paper_id, reviewer_id, score, comment, receipt, submitted_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (new_serial, pid, rid, rv["score"], rv["comment"],
                             rv["receipt"], rv["submitted_at"]),
                        )
                        carried_reviews += 1
    return {
        "ok": True,
        "serial": new_serial,
        "revision": rev,
        "carried_confirmations": carried,
        "carried_reviews": carried_reviews,
        "plan": new_plan,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
