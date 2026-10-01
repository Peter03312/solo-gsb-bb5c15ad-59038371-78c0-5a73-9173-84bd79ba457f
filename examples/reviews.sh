#!/usr/bin/env bash
# 本轮用法端到端示例: 正式评分与评语收集
#   发布 -> 两名评审人确认 -> 提交 1~5 分整数评分与非空评语 (同一任务只收一份) ->
#   相同重试返回原收据 / 内容不同报冲突 -> 会务方按论文看进度与评语 ->
#   评审人只看本人评语 -> 回避释放 -> 补位发布 (保留槽位沿用评语, 移出槽位评语仅归档)
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/reviews.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
rh() { echo "X-Reviewer-Id: $1"; echo "X-Reviewer-Credential: $2"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'

echo "== 2. 预演 + 发布 (记下发布序号 serial) =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
echo "$PUB"
SER=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CA="r$(echo "$RA" | sed 's/R//')-secret"
CB="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 (serial=$SER) 分配: $RA, $RB"

echo "== 3. 两人先确认 (仅已确认且未回避的槽位可交正式评语) =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'

echo "== 4. $RA 提交正式评语: 4 分 + 非空评语 (返回收据 receipt) =="
RV=$(curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"选题重要, 建议补充消融实验。\"}")
echo "$RV"

echo "== 4b. 完全相同的重试 -> changed=false, 原样返回同一收据 =="
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"   选题重要, 建议补充消融实验。  \"}"; echo

echo "== 4c. 内容不同 (改分数) -> 409 冲突, 原评语不变 =="
curl -s -w '\n-> HTTP %{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 5, \"comment\": \"选题重要, 建议补充消融实验。\"}"

echo "== 4d. 非法输入: 6 分 -> 422; 空白评语 -> 422 =="
curl -s -o /dev/null -w 'score=6        -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 6, \"comment\": \"x\"}"
curl -s -o /dev/null -w '空白评语        -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"   \"}"

echo "== 5. $RB 也提交评语 =="
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 2, \"comment\": \"实验设计有漏洞, 结论偏强。\"}"; echo

echo "== 6. 会务方按论文查看两名评审人的提交进度与评语 =="
curl -s "$BASE/papers/P1/reviews" -H "$ORG"; echo

echo "== 7. 评审人只可查看本人当前有效任务的评语 (看不到对方评语/作者机构) =="
curl -s "$BASE/reviewer/reviews" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA"; echo

echo "== 8. $RB 改声明回避: 其评语仅供会务方追溯, 不再出现在本人有效任务视图 =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者合作"}'
curl -s "$BASE/reviewer/reviews" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB"; echo

echo "== 9. 补位预演 + 发布: $RA 的确认槽位连续保留, 其评语沿用原收据 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')
BP=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}")
echo "$BP"
NSER=$(echo "$BP" | j 'd["serial"]')
echo "carried_reviews=$(echo "$BP" | j 'd["carried_reviews"]') (保留槽位沿用的评语份数)"

echo "== 9b. 用过期序号再交 -> 409; $RA 沿用的评语在新序号下仍可见且收据不变 =="
curl -s -o /dev/null -w '过期发布序号    -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"x\"}"
curl -s "$BASE/reviewer/reviews" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA"; echo

echo "== 10. 会务方进度只计新方案槽位; 被移出的 $RB 旧评语在 archived_reviews 中追溯 =="
curl -s "$BASE/papers/P1/reviews" -H "$ORG"; echo

echo "== 11. 补入的新评审人 ($(echo "$BP" | j '[x for x in d["plan"]["P1"] if x != "'"$RA"'"][0]')) 需自行确认并交新评语 =="
RC=$(echo "$BP" | j '[x for x in d["plan"]["P1"] if x != "'"$RA"'"][0]')
CC="r$(echo "$RC" | sed 's/R//')-secret"
curl -s -o /dev/null -w '未确认先交评语  -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RC" -H "X-Reviewer-Credential: $CC" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $NSER, \"score\": 5, \"comment\": \"新评审人的评语\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RC" -H "X-Reviewer-Credential: $CC" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RC" -H "X-Reviewer-Credential: $CC" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $NSER, \"score\": 5, \"comment\": \"新评审人的评语\"}"; echo
curl -s "$BASE/papers/P1/reviews" -H "$ORG" | j 'd["progress"]'
