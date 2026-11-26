#!/usr/bin/env bash
# Live HTTP scenario for the two report fixes:
#   1. /holds running while an ingredient is backfilled and a packing is submitted
#      must judge only the snapshot taken at its first read and write nothing back;
#      the submissions must still go through.
#   2. Backfilling a previously missing ingredient must correct /operations and
#      /holds immediately, honouring residue and cross-line rework.
# Usage: BASE=http://127.0.0.1:8000 bash scripts/backfill_interleave.sh
set -euo pipefail
B=${BASE:-http://127.0.0.1:8000}
j() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }
post() { curl -s -X POST "$B$1" -H 'Content-Type: application/json' -d "$2"; }

echo '== 1. 原料与 A 线成品 =='
post /ingredients '{"batch_no":"S-EGG","name":"蛋液","allergens":["egg"]}' >/dev/null
post /operations/pack '{"seq":1,"line":"A","product_batch_no":"S-A1",
  "refs":[{"kind":"ingredient","ref_no":"S-EGG"}],"label_allergens":["egg"]}' >/dev/null

echo '== 2. 先登记一个尚未补登原料的成品（S-X 暂缺）=='
post /operations/pack '{"seq":2,"line":"A","product_batch_no":"S-A2",
  "refs":[{"kind":"ingredient","ref_no":"S-X"}],"label_allergens":[]}' | j
echo '--- 暂扣（应含 unknown_refs=ingredient:S-X）---'
curl -s "$B/holds" | j

echo '== 3. 补登原料 S-X（芝麻）；/operations 应立即重判，不必先查 /holds =='
post /ingredients '{"batch_no":"S-X","name":"芝麻","allergens":["sesame"]}' | j
echo '--- S-A2 在 /operations 中的判定 ---'
curl -s "$B/operations" | python3 -c '
import json,sys
for o in json.load(sys.stdin):
    if o.get("product_batch_no") in ("S-A2",):
        print(json.dumps(o, ensure_ascii=False, indent=2))'

echo '== 4. 补登后清洗合格，跨线回配 S-A2 =='
post /operations/clean '{"seq":3,"line":"A","passed":true}' >/dev/null
post /operations/pack '{"seq":4,"line":"B","product_batch_no":"S-B1",
  "refs":[{"kind":"rework","ref_no":"S-A2"}],"label_allergens":[]}' | j
echo '--- 暂扣（S-A2、S-B1 都应漏标 sesame，且路径追到 S-X）---'
curl -s "$B/holds" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for i in d["items"]:
    print(i["product_batch_no"], "missing=",i["missing_on_label"], "unknown=",i["unknown_refs"])
    for p in i["trace_paths"]:
        print("   ", " -> ".join(p))'

echo '== 5. 再补登原本缺档、但位于清洗前的原料：受影响成品仍按残留/回配判定 =='
post /operations/pack '{"seq":5,"line":"B","product_batch_no":"S-B2",
  "refs":[{"kind":"ingredient","ref_no":"S-Y"}],"label_allergens":[]}' >/dev/null
post /ingredients '{"batch_no":"S-Y","name":"花生","allergens":["peanut"]}' | j
curl -s "$B/operations" | python3 -c '
import json,sys
for o in json.load(sys.stdin):
    if o.get("product_batch_no")=="S-B2":
        print("S-B2", o["actual_allergens"], "on_hold=",o["on_hold"])
        for p in o["actual_paths"]:
            print("   ", " -> ".join(p))'
