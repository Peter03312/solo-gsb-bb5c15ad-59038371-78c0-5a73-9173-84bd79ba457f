#!/usr/bin/env bash
# 本轮用法端到端示例:
#   发布 -> 评审人确认/回避 -> 回避即时断稿并成为硬回避 ->
#   会务方补位预演 -> 补位发布 (双版本复核) -> 按新发布版取稿, 确认状态保留
# 前置: 先运行 ./examples/demo.sh 建立数据, 或在空库上执行本脚本 (本脚本会自行建数据)。
# 用法: ./examples/backfill.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文 (注意先清空库或使用新库) =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'

echo "== 2. 普通预演 + 发布 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
echo "$PUB"
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CRED_A="r$(echo "$RA" | sed 's/R//')-secret"
CRED_B="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 分配: $RA, $RB"

echo "== 3. $RA 确认分配 (有效决定推进资料修订号) =="
curl -s -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'; echo

echo "== 3b. 重复确认 -> 幂等, changed=false, 修订号不变 =="
curl -s -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'; echo

echo "== 4. $RB 提交非空回避原因 (空原因会 422, 回避后不能再确认 409) =="
curl -s -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者有合作论文"}'; echo
curl -s -o /dev/null -w '空回避原因      -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "recuse", "reason": "  "}'
curl -s -o /dev/null -w '回避后再确认    -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'

echo "== 5. 回避即时生效: $RB 的取稿列表中已无 P1 (recused 仅留编号与原因) =="
curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B"; echo

echo "== 6. 当前发布版仍可供会务方核对 (槽位状态 confirmed/recused/pending) =="
curl -s "$BASE/assignment" -H "$ORG"; echo

echo "== 7. 会务方补位预演: 已确认且未回避的关系固定, 其余位置按原约束/优化次序重算 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY"
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')

echo "== 8. 补位发布必须同时复核资料修订号与发布序号; 演示过期拒绝 =="
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 稿", "topics": ["AI"], "institutions": ["Univ-Y"]}' >/dev/null
curl -s -o /dev/null -w '过期修订号补位发布 -> HTTP %{http_code}\n' -X POST \
  "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}"
# 删除演示论文以免影响补位 (补位仅覆盖当前发布版论文, P2 会被忽略, 但删除更干净)
curl -s -X DELETE "$BASE/papers/P2" -H "$ORG" >/dev/null

echo "== 9. 重新补位预演 + 发布成功 (仍在方案中的确认状态保留) =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')
curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}"; echo

echo "== 10. 按新发布版授权: $RA 仍可取稿且状态为 confirmed =="
curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A"; echo

echo "== 11. 未分配任务拒绝 (404): R4 对 P1 提交决定, 若未分配则 404 =="
RN=$(curl -s "$BASE/assignment" -H "$ORG" | j '[r for r in d["plan"]["P1"] if r not in ["'"$RA"'","'"$RB"'"]][0]')
echo "补位后的另一名评审人: $RN (其凭据为 r${RN#R}-secret, 可自行取稿验证)"
