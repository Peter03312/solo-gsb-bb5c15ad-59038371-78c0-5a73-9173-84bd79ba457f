#!/usr/bin/env bash
# 本轮用法端到端示例: 正式评语收集
#   发布 -> 评审人确认 -> 提交评语 (发布序号+评分+非空评语) -> 幂等重试/冲突/过期序号 ->
#   会务方按论文查看进度 -> 补位发布后确认槽位沿用原评语, 移出槽位评语仅供追溯
# 前置: 服务已启动; 建议对新库运行 (本脚本会自行建数据)。
# 用法: ./examples/reviews.sh [BASE_URL]
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

echo "== 2. 预演 + 发布 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CRED_A="r$(echo "$RA" | sed 's/R//')-secret"
CRED_B="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 分配: $RA, $RB (发布序号 $(echo "$PUB" | j 'd["serial"]'))"

echo "== 3. $RA 确认任务后提交评语 (serial + 1~5 整数评分 + 非空评语) =="
curl -s -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}' > /dev/null
SERIAL=$(curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" | j 'd["serial"]')
REVIEW="{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 5, \"comment\": \"创新点明确, 实验充分, 建议接收\"}"
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d "$REVIEW"; echo

echo "== 3b. 完全相同的重试 -> 幂等返回原收据 (changed=false, 收据号不变) =="
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' -d "$REVIEW"; echo

echo "== 3c. 内容不同 / 过期序号 / 未确认即提交 / 空评语 -> 409 / 409 / 409 / 422, 均不落库 =="
curl -s -o /dev/null -w '内容不同        -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 4, \"comment\": \"想改评语\"}"
curl -s -o /dev/null -w '过期序号        -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $((SERIAL + 1)), \"score\": 5, \"comment\": \"x\"}"
curl -s -o /dev/null -w '未确认提交      -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 3, \"comment\": \"还没确认\"}"
curl -s -o /dev/null -w '空评语          -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 5, \"comment\": \"  \"}"

echo "== 4. 会务方按论文查看两名评审人的提交进度与评语 =="
curl -s "$BASE/assignment/reviews" -H "$ORG"; echo

echo "== 5. 评审人查看本人评语 (仅本人当前有效任务, 看不到对方评语与作者机构) =="
curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A"; echo

echo "== 6. $RB 回避 -> 补位预演 -> 补位发布 (确认槽位的评语沿用, carried_reviews=1) =="
curl -s -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CRED_B" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者有合作"}' > /dev/null
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')
curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}"; echo

echo "== 7. 新发布版进度: 沿用评语计入 (收据号不变); 原序号下评语进入 archived 追溯 =="
curl -s "$BASE/assignment/reviews" -H "$ORG"; echo

echo "== 8. $RA 在新发布序号下相同重试 -> 仍幂等返回原收据 =="
curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" | j 'd["reviews"][0]["receipt"]'
SERIAL2=$(curl -s "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" | j 'd["serial"]')
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CRED_A" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL2, \"score\": 5, \"comment\": \"创新点明确, 实验充分, 建议接收\"}"; echo
