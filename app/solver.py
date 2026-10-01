"""双盲评审分配求解器。

硬约束:
  1. 每篇论文恰好分配给 2 名评审人;
  2. 同一篇论文的两名评审人必须来自不同机构;
  3. 两名评审人中至少一人擅长该论文主题
     (评审人擅长主题与论文主题交集非空; 论文未标注主题时视为所有评审人均满足);
  4. 评审人不得与论文任一作者机构相同;
  5. 评审人不得评审自己声明回避的论文;
  6. 评审人已分配论文数不得超过其容量。

优化目标 (按优先级):
  1. 最小化全体评审人中最高的"已用容量比例"(已分配数 / 容量);
  2. 在此前提下, 按 (论文编号升序, 每篇的评审人编号对升序) 展开的全局序列
     取字典序最小的方案。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations


@dataclass(frozen=True)
class Paper:
    paper_id: str
    topics: frozenset
    institutions: frozenset


@dataclass(frozen=True)
class Reviewer:
    reviewer_id: str
    topics: frozenset
    institution: str
    capacity: int
    avoid_papers: frozenset


def is_expert(reviewer: Reviewer, paper: Paper) -> bool:
    """评审人是否擅长该论文主题。论文无主题时视为任何评审人均满足。"""
    if not paper.topics:
        return True
    return not reviewer.topics.isdisjoint(paper.topics)


def analyze_paper(paper: Paper, reviewers, recused_pairs=()):
    """返回 (eligible, excluded, pairs)。

    eligible: 通过机构回避与回避名单过滤的评审人 (按编号升序);
    excluded: 被排除评审人及原因 (评审人通过决定接口声明的硬回避单独标注);
    pairs:    忽略容量时该论文的全部合法评审人对 (元组内与元组间均按编号升序)。
    """
    eligible, excluded = [], []
    for r in reviewers:  # callers 保证 reviewers 已按 reviewer_id 升序
        if r.institution in paper.institutions:
            excluded.append({"reviewer_id": r.reviewer_id, "reason": "same_institution_as_author"})
        elif paper.paper_id in r.avoid_papers:
            reason = (
                "hard_recusal_after_decline"
                if (r.reviewer_id, paper.paper_id) in recused_pairs
                else "on_reviewer_avoid_list"
            )
            excluded.append({"reviewer_id": r.reviewer_id, "reason": reason})
        else:
            eligible.append(r)
    pairs = []
    for a, b in combinations(eligible, 2):
        if a.institution == b.institution:
            continue
        if not (is_expert(a, paper) or is_expert(b, paper)):
            continue
        pairs.append((a.reviewer_id, b.reviewer_id))
    return eligible, excluded, pairs


class Solver:
    def __init__(self, papers, reviewers, recused_pairs=()):
        self.papers = sorted(papers, key=lambda p: p.paper_id)
        self.reviewers = sorted(reviewers, key=lambda r: r.reviewer_id)
        self.info = {}
        for p in self.papers:
            eligible, excluded, pairs = analyze_paper(p, self.reviewers, recused_pairs)
            self.info[p.paper_id] = {"eligible": eligible, "excluded": excluded, "pairs": pairs}

    # ---------- 完整方案 ----------

    def find_first(self, caps):
        """在容量上限 caps 下求字典序最小的完整方案; 不可行返回 None。

        论文按编号升序处理, 每篇的评审人对按字典序枚举, 第一个完整解即全局字典序最小。
        """
        order = [p.paper_id for p in self.papers]
        n = len(order)
        pairs_of = {pid: self.info[pid]["pairs"] for pid in order}
        loads = {rid: 0 for rid in caps}
        assignment = {}

        def rest_ok(i):
            remaining = sum(caps[rid] - loads[rid] for rid in caps)
            if remaining < 2 * (n - i):
                return False
            for pid in order[i:]:
                if not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in pairs_of[pid]):
                    return False
            return True

        def backtrack(i):
            if i == n:
                return True
            pid = order[i]
            for a, b in pairs_of[pid]:
                if loads[a] < caps[a] and loads[b] < caps[b]:
                    loads[a] += 1
                    loads[b] += 1
                    assignment[pid] = (a, b)
                    if rest_ok(i + 1) and backtrack(i + 1):
                        return True
                    del assignment[pid]
                    loads[a] -= 1
                    loads[b] -= 1
            return False

        if rest_ok(0) and backtrack(0):
            return assignment
        return None

    def minimal_ratio_caps(self):
        """返回使方案可行的、最小化最高已用容量比例所对应的容量上限; 完全不可行返回 None。"""
        if not self.papers:
            return {r.reviewer_id: 0 for r in self.reviewers}
        if not self.reviewers:
            return None
        candidates = sorted({
            Fraction(k, r.capacity)
            for r in self.reviewers
            for k in range(1, r.capacity + 1)
        })

        def caps_for(t: Fraction):
            return {
                r.reviewer_id: (t * r.capacity).numerator // (t * r.capacity).denominator
                for r in self.reviewers
            }

        if self.find_first(caps_for(candidates[-1])) is None:
            return None
        lo, hi = 0, len(candidates) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.find_first(caps_for(candidates[mid])) is not None:
                hi = mid
            else:
                lo = mid + 1
        return caps_for(candidates[lo])

    # ---------- 不可行时的部分方案与诊断 ----------

    def max_partial(self, caps):
        """求最多论文被完整分配的部分方案 (并列时按全局序列字典序最小)。

        返回 (assignment, loads)。
        """
        order = [p.paper_id for p in self.papers]
        n = len(order)
        pairs_of = {pid: self.info[pid]["pairs"] for pid in order}
        loads = {rid: 0 for rid in caps}
        assignment = {}
        best = {"count": -1, "key": None, "assignment": None, "loads": None}

        def key_of(assign):
            seq = []
            for pid in order:
                if pid in assign:
                    a, b = assign[pid]
                    seq.append((0, a, b))
                else:
                    seq.append((1, "", ""))
            return tuple(seq)

        def backtrack(i, count):
            if best["count"] > count + (n - i):
                return  # 已不可能取得更多分配
            if i == n:
                k = key_of(assignment)
                if count > best["count"] or (count == best["count"] and (best["key"] is None or k < best["key"])):
                    best["count"] = count
                    best["key"] = k
                    best["assignment"] = dict(assignment)
                    best["loads"] = dict(loads)
                return
            pid = order[i]
            for a, b in pairs_of[pid]:
                if loads[a] < caps[a] and loads[b] < caps[b]:
                    loads[a] += 1
                    loads[b] += 1
                    assignment[pid] = (a, b)
                    backtrack(i + 1, count + 1)
                    del assignment[pid]
                    loads[a] -= 1
                    loads[b] -= 1
            backtrack(i + 1, count)  # 跳过该论文

        backtrack(0, 0)
        return best["assignment"] or {}, best["loads"] or {rid: 0 for rid in caps}

    def diagnose(self, unassigned_ids, loads, caps):
        """对每篇未分配论文给出限制原因。"""
        paper_by_id = {p.paper_id: p for p in self.papers}
        out = {}
        for pid in unassigned_ids:
            paper = paper_by_id[pid]
            info = self.info[pid]
            eligible = info["eligible"]
            pairs = info["pairs"]
            reasons = []
            if len(eligible) < 2:
                reasons.append("fewer_than_two_eligible_reviewers")
            full = [r.reviewer_id for r in eligible if loads[r.reviewer_id] >= caps[r.reviewer_id]]
            if eligible and len(eligible) - len(full) < 2:
                reasons.append("eligible_reviewers_at_capacity")
            if not any(is_expert(r, paper) for r in eligible):
                reasons.append("no_expert_among_eligible_reviewers")
            if len(eligible) >= 2 and len({r.institution for r in eligible}) < 2:
                reasons.append("eligible_reviewers_share_single_institution")
            if not pairs:
                reasons.append("no_valid_reviewer_pair")
            elif not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in pairs):
                reasons.append("all_valid_pairs_blocked_by_capacity")
            out[pid] = {
                "eligible_reviewers": [r.reviewer_id for r in eligible],
                "eligible_at_capacity": full,
                "excluded": info["excluded"],
                "reasons": reasons,
            }
        return out


def _loads_of(plan):
    loads = {}
    for a, b in plan.values():
        loads[a] = loads.get(a, 0) + 1
        loads[b] = loads.get(b, 0) + 1
    return loads


def compute_assignment(papers, reviewers, recused_pairs=()):
    """求解入口。返回可 JSON 序列化的结果字典。"""
    solver = Solver(papers, reviewers, recused_pairs)
    reviewer_by_id = {r.reviewer_id: r for r in solver.reviewers}
    explanations = {
        pid: {
            "eligible_reviewers": [r.reviewer_id for r in info["eligible"]],
            "excluded": info["excluded"],
        }
        for pid, info in solver.info.items()
    }

    def paper_entries(plan):
        return {
            pid: {"assigned": list(plan[pid]), **explanations[pid]}
            for pid in sorted(plan)
        }

    caps = solver.minimal_ratio_caps()
    if caps is not None:
        plan = solver.find_first(caps)
        loads = _loads_of(plan)
        ratio = Fraction(0)
        for r in solver.reviewers:
            ratio = max(ratio, Fraction(loads.get(r.reviewer_id, 0), r.capacity))
        return {
            "feasible": True,
            "plan": {pid: list(pair) for pid, pair in sorted(plan.items())},
            "unassigned": [],
            "max_used_capacity_ratio": str(ratio),
            "max_used_capacity_ratio_value": float(ratio),
            "loads": {rid: loads.get(rid, 0) for rid in sorted(reviewer_by_id)},
            "papers": paper_entries(plan),
            "diagnostics": {},
        }

    # 无完整方案: 给出最大部分方案与每篇未分配论文的限制原因, 发布版不受影响
    full_caps = {r.reviewer_id: r.capacity for r in solver.reviewers}
    partial, loads = solver.max_partial(full_caps)
    unassigned = [p.paper_id for p in solver.papers if p.paper_id not in partial]
    return {
        "feasible": False,
        "plan": {pid: list(pair) for pid, pair in sorted(partial.items())},
        "unassigned": unassigned,
        "max_used_capacity_ratio": None,
        "max_used_capacity_ratio_value": None,
        "loads": {rid: loads.get(rid, 0) for rid in sorted(reviewer_by_id)},
        "papers": paper_entries(partial),
        "diagnostics": solver.diagnose(unassigned, loads, full_caps),
    }


# ---------------------------------------------------------------- 补位求解

def compute_backfill(papers, reviewers, fixed_map, recused_pairs=()):
    """补位预演: 仅面向当前发布版内的论文 (papers 已按该范围过滤)。

    fixed_map:     {paper_id: [reviewer_id, ...]} 当前发布版中"已确认且未回避"
                   的关系, 这些位置固定不动; 其余位置在原有硬约束与优化次序下重算。
    recused_pairs: {(reviewer_id, paper_id)} 评审人通过决定接口声明的硬回避
                   (与会务方录入的 avoid_papers 等价, 但诊断原因单独标注)。

    数据变更导致固定关系失效时 (评审人被删除/与作者变为同机构/硬回避等),
    该固定位置释放为可补位置, 并在 papers[pid].fixed_problems 中说明。
    """
    recused_pairs = set(recused_pairs)
    solver = Solver(papers, reviewers, recused_pairs)
    reviewer_by_id = {r.reviewer_id: r for r in solver.reviewers}
    paper_by_id = {p.paper_id: p for p in solver.papers}

    fixed = {}          # pid -> [合法固定评审人 id]
    fixed_problems = {}  # pid -> [{reviewer_id, reason}]

    for pid in sorted(fixed_map):
        if pid not in paper_by_id:
            continue  # 论文已删除: 该发布关系随论文一并消失
        info = solver.info[pid]
        eligible_ids = {r.reviewer_id for r in info["eligible"]}
        excluded_reason = {e["reviewer_id"]: e["reason"] for e in info["excluded"]}
        kept, problems = [], []
        seen = set()
        for rid in fixed_map[pid]:
            if rid in seen:
                continue
            seen.add(rid)
            if rid not in reviewer_by_id:
                problems.append({"reviewer_id": rid, "reason": "fixed_reviewer_not_found"})
            elif rid in excluded_reason:
                if (rid, pid) in recused_pairs:
                    problems.append({"reviewer_id": rid, "reason": "hard_recusal_after_decline"})
                else:
                    problems.append({"reviewer_id": rid, "reason": excluded_reason[rid]})
            else:
                kept.append(rid)
        if len(kept) >= 2:
            a, b = kept[0], kept[1]
            ra, rb = reviewer_by_id[a], reviewer_by_id[b]
            if ra.institution == rb.institution:
                problems.append({"reviewer_id": b, "reason": "fixed_pair_violates_institution_rule"})
                kept = [a]
            elif not (is_expert(ra, paper_by_id[pid]) or is_expert(rb, paper_by_id[pid])):
                problems.append({"reviewer_id": b, "reason": "fixed_pair_violates_expert_rule"})
                kept = [a]
        fixed[pid] = kept
        if problems:
            fixed_problems[pid] = problems

    # 固定位置带来的初始负载; 超过容量时确定性地释放编号最大论文上的多余固定位置
    loads = {r.reviewer_id: 0 for r in solver.reviewers}
    for pid in sorted(fixed):
        for rid in fixed[pid]:
            loads[rid] += 1
    for rid in sorted(loads):
        cap = reviewer_by_id[rid].capacity
        if loads[rid] <= cap:
            continue
        for pid in sorted(
            (p for p in fixed if rid in fixed[p]), reverse=True
        ):
            if loads[rid] <= cap:
                break
            fixed[pid].remove(rid)
            fixed_problems.setdefault(pid, []).append(
                {"reviewer_id": rid, "reason": "fixed_assignments_exceed_capacity"}
            )
            loads[rid] -= 1

    full_caps = {r.reviewer_id: r.capacity for r in solver.reviewers}
    fixed_pairs = {pid: tuple(ids) for pid, ids in fixed.items() if len(ids) == 2}
    free_pids = [p.paper_id for p in solver.papers if p.paper_id not in fixed_pairs]

    def candidate_pairs(pid):
        required = set(fixed.get(pid, ()))
        return [pair for pair in solver.info[pid]["pairs"] if required.issubset(pair)]

    pairs_of = {pid: candidate_pairs(pid) for pid in free_pids}

    def rest_ok(cur_loads, i, caps):
        # 剩余自由槽位需求: 每个自由论文 2 个位置, 单固定论文的固定成员已由预占负载覆盖
        needed_slots = sum(2 - len(fixed.get(pid, ())) for pid in free_pids[i:])
        remaining = sum(caps[r] - cur_loads[r] for r in caps)
        if remaining < needed_slots:
            return False
        for pid in free_pids[i:]:
            required = set(fixed.get(pid, ()))
            if required:
                # 固定成员已预占负载且校验过容量, 只需存在一名有余量的搭档
                (rid,) = tuple(required)
                partners = {a if b == rid else b for a, b in pairs_of[pid]}
                if not any(cur_loads[other] < caps[other] for other in partners):
                    return False
            elif not any(cur_loads[a] < caps[a] and cur_loads[b] < caps[b] for a, b in pairs_of[pid]):
                return False
        return True

    def search(caps):
        """固定位置满足容量上限时, 在剩余位置上求字典序最小完整补位方案。"""
        cur = dict(loads)
        if any(cur[r] > caps[r] for r in caps):
            return None
        chosen = {}

        def backtrack(i):
            if i == len(free_pids):
                return True
            pid = free_pids[i]
            required = set(fixed.get(pid, ()))
            for a, b in pairs_of[pid]:
                # 固定成员的占用已预载在 cur 中, 只需其当前负载不超上限;
                # 新占用容量的自由成员才检查余量
                free_a, free_b = a not in required, b not in required
                ok_a = (cur[a] < caps[a]) if free_a else (cur[a] <= caps[a])
                ok_b = (cur[b] < caps[b]) if free_b else (cur[b] <= caps[b])
                if ok_a and ok_b:
                    if free_a:
                        cur[a] += 1
                    if free_b:
                        cur[b] += 1
                    chosen[pid] = (a, b)
                    if rest_ok(cur, i + 1, caps) and backtrack(i + 1):
                        return True
                    del chosen[pid]
                    if free_a:
                        cur[a] -= 1
                    if free_b:
                        cur[b] -= 1
            return False

        if rest_ok(cur, 0, caps) and backtrack(0):
            return chosen
        return None

    plan = dict(fixed_pairs)

    # ---- 最小化最高已用容量比例 (含固定负载) ----
    if solver.reviewers:
        candidates = sorted({
            Fraction(k, r.capacity)
            for r in solver.reviewers
            for k in range(1, r.capacity + 1)
        })

        def caps_for(t):
            return {
                r.reviewer_id: (t * r.capacity).numerator // (t * r.capacity).denominator
                for r in solver.reviewers
            }

        chosen = search(caps_for(candidates[-1]))
        if chosen is not None:
            lo, hi = 0, len(candidates) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if search(caps_for(candidates[mid])) is not None:
                    hi = mid
                else:
                    lo = mid + 1
            chosen = search(caps_for(candidates[lo]))
            plan.update(chosen)
            final_loads = {rid: 0 for rid in full_caps}
            for a, b in plan.values():
                final_loads[a] += 1
                final_loads[b] += 1
            ratio = max(
                (Fraction(final_loads[r.reviewer_id], r.capacity) for r in solver.reviewers),
                default=Fraction(0),
            )
            return {
                "feasible": True,
                "plan": {pid: list(plan[pid]) for pid in sorted(plan)},
                "unassigned": [],
                "max_used_capacity_ratio": str(ratio),
                "max_used_capacity_ratio_value": float(ratio),
                "loads": {rid: final_loads[rid] for rid in sorted(final_loads)},
                "fixed": {pid: list(fixed.get(pid, ())) for pid in sorted(plan)},
                "fixed_problems": fixed_problems,
                "papers": _backfill_paper_entries(solver, plan, fixed, fixed_problems, recused_pairs),
                "diagnostics": {},
            }

    # ---- 无完整补位方案: 最大部分补位 + 诊断 ----
    cur = dict(loads)
    best = {"count": -1, "key": None, "chosen": None}
    n = len(free_pids)
    chosen_partial = {}

    def key_of(chosen):
        seq = []
        for pid in free_pids:
            if pid in chosen:
                seq.append((0,) + chosen[pid])
            else:
                seq.append((1, "", ""))
        return tuple(seq)

    def backtrack_partial(i, count):
        if best["count"] > count + (n - i):
            return
        if i == n:
            k = key_of(chosen_partial)
            if count > best["count"] or (
                count == best["count"] and (best["key"] is None or k < best["key"])
            ):
                best["count"] = count
                best["key"] = k
                best["chosen"] = dict(chosen_partial)
            return
        pid = free_pids[i]
        required = set(fixed.get(pid, ()))
        for a, b in pairs_of[pid]:
            free_a, free_b = a not in required, b not in required
            ok_a = (cur[a] < full_caps[a]) if free_a else (cur[a] <= full_caps[a])
            ok_b = (cur[b] < full_caps[b]) if free_b else (cur[b] <= full_caps[b])
            if ok_a and ok_b:
                if free_a:
                    cur[a] += 1
                if free_b:
                    cur[b] += 1
                chosen_partial[pid] = (a, b)
                backtrack_partial(i + 1, count + 1)
                del chosen_partial[pid]
                if free_a:
                    cur[a] -= 1
                if free_b:
                    cur[b] -= 1
        backtrack_partial(i + 1, count)

    backtrack_partial(0, 0)
    partial_plan = dict(fixed_pairs)
    if best["chosen"]:
        partial_plan.update(best["chosen"])
    final_loads = {rid: 0 for rid in full_caps}
    for a, b in partial_plan.values():
        final_loads[a] += 1
        final_loads[b] += 1
    unassigned = [p.paper_id for p in solver.papers if p.paper_id not in partial_plan]
    return {
        "feasible": False,
        "plan": {pid: list(partial_plan[pid]) for pid in sorted(partial_plan)},
        "unassigned": unassigned,
        "max_used_capacity_ratio": None,
        "max_used_capacity_ratio_value": None,
        "loads": {rid: final_loads[rid] for rid in sorted(final_loads)},
        "fixed": {pid: list(fixed.get(pid, ())) for pid in sorted(partial_plan)},
        "fixed_problems": fixed_problems,
        "papers": _backfill_paper_entries(
            solver, partial_plan, fixed, fixed_problems, recused_pairs
        ),
        "diagnostics": _backfill_diagnose(
            solver, unassigned, final_loads, full_caps, pairs_of, fixed, fixed_problems,
            recused_pairs,
        ),
    }


def _backfill_paper_entries(solver, plan, fixed, fixed_problems, recused_pairs):
    out = {}
    for pid in sorted(plan):
        out[pid] = {
            "assigned": list(plan[pid]),
            "fixed": list(fixed.get(pid, ())),
            "eligible_reviewers": [r.reviewer_id for r in solver.info[pid]["eligible"]],
            "excluded": [dict(e) for e in solver.info[pid]["excluded"]],
            "fixed_problems": fixed_problems.get(pid, []),
        }
    return out


def _backfill_diagnose(solver, unassigned_ids, loads, caps, pairs_of, fixed,
                       fixed_problems, recused_pairs):
    paper_by_id = {p.paper_id: p for p in solver.papers}
    out = {}
    for pid in unassigned_ids:
        paper = paper_by_id[pid]
        info = solver.info[pid]
        eligible = info["eligible"]
        pairs = pairs_of.get(pid, [])
        reasons = []
        if len(eligible) < 2:
            reasons.append("fewer_than_two_eligible_reviewers")
        full = [r.reviewer_id for r in eligible if loads[r.reviewer_id] >= caps[r.reviewer_id]]
        if eligible and len(eligible) - len(full) < 2:
            reasons.append("eligible_reviewers_at_capacity")
        if not any(is_expert(r, paper) for r in eligible):
            reasons.append("no_expert_among_eligible_reviewers")
        if len(eligible) >= 2 and len({r.institution for r in eligible}) < 2:
            reasons.append("eligible_reviewers_share_single_institution")
        if fixed.get(pid):
            if not pairs:
                reasons.append("no_valid_pair_containing_fixed_reviewer")
            elif not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in pairs):
                reasons.append("all_fixed_compatible_pairs_blocked_by_capacity")
        else:
            if not info["pairs"]:
                reasons.append("no_valid_reviewer_pair")
            elif not any(loads[a] < caps[a] and loads[b] < caps[b] for a, b in info["pairs"]):
                reasons.append("all_valid_pairs_blocked_by_capacity")
        out[pid] = {
            "eligible_reviewers": [r.reviewer_id for r in eligible],
            "eligible_at_capacity": full,
            "excluded": [dict(e) for e in info["excluded"]],
            "fixed": list(fixed.get(pid, ())),
            "fixed_problems": fixed_problems.get(pid, []),
            "reasons": reasons,
        }
    return out
