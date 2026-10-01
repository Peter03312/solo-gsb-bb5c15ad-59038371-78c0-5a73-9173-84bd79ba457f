#!/usr/bin/env bash
# 端到端调用示例: 录入资料 -> 预演 -> 发布 -> 评审人读取匿名稿
# 用法: ./examples/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"

echo "== 0. 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1. 会务方录入评审人 (编号/凭据/擅长主题/机构/容量/回避论文) =="
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R1", "credential": "r1-secret", "topics": ["AI", "ML"],
  "institution": "Inst-A", "capacity": 2, "avoid_papers": []
}'; echo
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R2", "credential": "r2-secret", "topics": ["DB"],
  "institution": "Inst-B", "capacity": 2, "avoid_papers": ["P2"]
}'; echo
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R3", "credential": "r3-secret", "topics": ["AI"],
  "institution": "Inst-C", "capacity": 2, "avoid_papers": []
}'; echo
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R4", "credential": "r4-secret", "topics": ["ML"],
  "institution": "Univ-X", "capacity": 2, "avoid_papers": []
}'; echo

echo "== 2. 会务方录入论文 (编号/匿名稿/主题/作者机构) =="
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]
}'; echo
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["ML"],
  "institutions": ["Univ-Y"]
}'; echo
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P3", "manuscript": "P3 匿名稿全文...", "topics": ["DB"],
  "institutions": ["Univ-Z"]
}'; echo

echo "== 3. 预演 (返回方案 + 资料修订号; R4 因与 P1 作者同机构被排除, R2 回避 P2) =="
DRY=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG")
echo "$DRY"
REV=$(echo "$DRY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["revision"])')
echo "资料修订号: $REV"

echo "== 4. 资料变动后用旧修订号发布 -> 409 拒绝 =="
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P4", "manuscript": "P4 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-W"]
}'; echo
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"

echo "== 5. 重新预演并用新修订号发布 =="
DRY=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG")
REV=$(echo "$DRY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["revision"])')
curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}"; echo

echo "== 6. 会务方查询已发布分配与排除原因 =="
curl -s "$BASE/assignment" -H "$ORG"; echo

echo "== 7. 评审人凭自身凭据读取已分配匿名稿 (仅本人名下) =="
curl -s "$BASE/reviewer/assignments" -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'; echo

echo "== 8. 错误演示: 失效凭据 / 非法容量 / 重复编号 =="
curl -s -o /dev/null -w '失效凭据        -> HTTP %{http_code}\n' "$BASE/reviewer/assignments" \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: wrong'
curl -s -o /dev/null -w '非法容量        -> HTTP %{http_code}\n' -X POST "$BASE/reviewers" \
  -H "$ORG" -H 'Content-Type: application/json' -d '{
    "reviewer_id": "R9", "credential": "x", "topics": [], "institution": "I",
    "capacity": 0, "avoid_papers": []}'
curl -s -o /dev/null -w '重复编号        -> HTTP %{http_code}\n' -X POST "$BASE/papers" \
  -H "$ORG" -H 'Content-Type: application/json' -d '{
    "paper_id": "P1", "manuscript": "x", "topics": [], "institutions": []}'
