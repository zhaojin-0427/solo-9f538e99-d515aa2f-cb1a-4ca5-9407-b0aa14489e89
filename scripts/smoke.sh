#!/usr/bin/env bash
# Live HTTP smoke run: cross-line rework + cleaning correction.
# Usage: BASE=http://127.0.0.1:8000 bash scripts/smoke.sh
set -euo pipefail
B=${BASE:-http://127.0.0.1:8077}
j() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

echo '== 1. 原料批次过敏原 =='
curl -s -X POST "$B/ingredients" -H 'Content-Type: application/json' \
  -d '{"batch_no":"I-EGG","name":"蛋液","allergens":["egg"]}' | j
curl -s -X POST "$B/ingredients" -H 'Content-Type: application/json' \
  -d '{"batch_no":"I-MILK","name":"奶粉","allergens":["milk","soy"]}' | j

echo '== 2. A线 seq1 P-A1 用蛋液，标签正确印 egg =='
curl -s -X POST "$B/operations/pack" -H 'Content-Type: application/json' \
  -d '{"seq":1,"line":"A","product_batch_no":"P-A1",
       "refs":[{"kind":"ingredient","ref_no":"I-EGG"}],
       "label_allergens":["egg"]}' | j

echo '== 3. B线 seq2 P-B1 用奶粉，标签只印 milk，漏 soy =='
curl -s -X POST "$B/operations/pack" -H 'Content-Type: application/json' \
  -d '{"seq":2,"line":"B","product_batch_no":"P-B1",
       "refs":[{"kind":"ingredient","ref_no":"I-MILK"}],
       "label_allergens":["milk"]}' | j

echo '== 4. A线 seq3 清洗：初判合格 =='
curl -s -X POST "$B/operations/clean" -H 'Content-Type: application/json' \
  -d '{"seq":3,"line":"A","passed":true}' | j

echo '== 5. A线 seq4 P-A2 无新用料（清洗合格，应无残留）=='
curl -s -X POST "$B/operations/pack" -H 'Content-Type: application/json' \
  -d '{"seq":4,"line":"A","product_batch_no":"P-A2","refs":[],
       "label_allergens":[]}' | j

echo '== 6. B线 seq5 P-B2 跨线回配 P-A2（此时应干净）=='
curl -s -X POST "$B/operations/pack" -H 'Content-Type: application/json' \
  -d '{"seq":5,"line":"B","product_batch_no":"P-B2",
       "refs":[{"kind":"rework","ref_no":"P-A2"}],
       "label_allergens":[]}' | j

echo '== 7. A线 seq6 P-A3 跨线回配更早期的 P-A1（清洗前成品，egg 仍可带入）=='
curl -s -X POST "$B/operations/pack" -H 'Content-Type: application/json' \
  -d '{"seq":6,"line":"A","product_batch_no":"P-A3",
       "refs":[{"kind":"rework","ref_no":"P-A1"}],
       "label_allergens":["egg"]}' | j

echo '== 8. 更正 seq3 清洗：合格 -> 不合格 =='
curl -s -X POST "$B/operations/clean/3/correct" -H 'Content-Type: application/json' \
  -d '{"passed":false}' | j

echo '== 9. 当前暂扣清单 =='
curl -s "$B/holds" | j

echo '== 10. 再改回合格，确认残留链清空 =='
curl -s -X POST "$B/operations/clean/3/correct" -H 'Content-Type: application/json' \
  -d '{"passed":true}' | j
