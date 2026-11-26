# 中央厨房装盒线过敏原暂扣系统

清洗复验未通过时，定位需要暂扣的盒饭：管理两条装盒线（A/B）的班次作业，
按全班统一工序号排列，自动传播过敏原（原料、回配、线体残留），支持清洗结论
更正与波及批次实查，并生成暂扣清单。

## 业务规则

1. **原料批次**：登记批号与过敏原清单。
2. **统一工序号 `seq`**：两条线的装盒与清洗共用一个递增序号，时间线按 `seq` 排列；
   序号不可重复。
3. **装盒**：录入成品批号、用料批号（原料或更早成品的回配）、标签过敏原。
   成品实际过敏原 = 直接原料 ∪ 回配成品 ∪ **本线残留**；本批的全部过敏原
   随后留在本线。回配可指向更早的任意成品（含跨线）。
4. **开班无残留**：两线开班时残留均为空。
5. **清洗**：合格 → 清空本线残留；不合格 → 残留保留。清洗不改变已产出的成品，
   清洗前的成品在之后被回配时仍可带入过敏原。
6. **暂扣判定**：
   - 漏标：实际含有的过敏原未印在标签上；
   - 缺项：未填用料批号或标签过敏原；
   - 每条漏标过敏原都附追到原料的路径，如
     `P-B2 → rework:P-A2 → residue:P-A1 → ingredient:I-EGG → egg`；
   - 引用了不存在的原料/成品批号也列入暂扣并标注。
7. **清洗更正**：只复核更正之后能经「本线残留」或「回配（可跨线、传递）」
   波及到的批次，响应中返回**实查批号**（`examined_batch_nos`）。新清洗结论与
   暂扣清单在**同一事务**生效；更正失败则回滚，沿用原结果。

## 启动（Docker Compose）

```bash
docker compose up --build
# API:  http://localhost:8000
# 文档: http://localhost:8000/docs   （可直接录入/调试）
```

SQLite 数据文件位于卷 `kitchen_data` 内的 `/data/kitchen.db`。

## 本地开发（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./dev.db uvicorn app.main:app --reload
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/ingredients` | 登记原料批次及过敏原 |
| GET  | `/ingredients` | 原料批次列表 |
| POST | `/operations/pack` | 装盒作业（成品批号、用料、标签过敏原） |
| POST | `/operations/clean` | 清洗作业及合格结论 |
| GET  | `/operations` | 按工序号列出全部作业及计算结果 |
| GET  | `/holds` | 暂扣清单（漏标、缺项、追原料路径、未知引用） |
| POST | `/operations/clean/{seq}/correct` | 更正清洗结论，返回实查批号与新清单 |
| GET  | `/status` | 班次与两线状态 |

### 示例

```bash
# 原料
curl -X POST localhost:8000/ingredients -H 'Content-Type: application/json' \
  -d '{"batch_no":"I-EGG","name":"蛋液","allergens":["egg"]}'

# 装盒（seq 为全班统一工序号）
curl -X POST localhost:8000/operations/pack -H 'Content-Type: application/json' \
  -d '{"seq":1,"line":"A","product_batch_no":"P-A1",
       "refs":[{"kind":"ingredient","ref_no":"I-EGG"}],
       "label_allergens":["egg"]}'

# 清洗（合格）
curl -X POST localhost:8000/operations/clean -H 'Content-Type: application/json' \
  -d '{"seq":2,"line":"A","passed":true}'

# 回配更早成品（kind=rework，可跨线）
curl -X POST localhost:8000/operations/pack -H 'Content-Type: application/json' \
  -d '{"seq":3,"line":"B","product_batch_no":"P-B2",
       "refs":[{"kind":"rework","ref_no":"P-A1"}],"label_allergens":[]}'

# 更正清洗结论：只复核波及批次，返回实查批号，清单一同生效
curl -X POST localhost:8000/operations/clean/2/correct \
  -H 'Content-Type: application/json' -d '{"passed":false}'
```

## 测试

`tests/test_api.py` 含 10 个端到端用例，覆盖：开班无残留、原料继承、漏标/缺项、
未知批号、工序号冲突、残留与合格清洗、清洗前成品经回配带入、**跨线回配的清洗
更正（实查批号闭包 + 追原料路径 + 再改回）**、更正失败沿用原结果等。

```bash
pytest -q
# 另见 scripts/smoke.sh：对真实 HTTP 服务的完整场景试跑
```

## 目录结构

```
app/
  main.py        FastAPI 入口（/docs）
  routers.py     路由与清洗更正事务
  schemas.py     请求/响应模型
  models.py      SQLite ORM（原料、作业、用料引用）
  engine.py      纯函数传播引擎（残留/回配/路径/波及闭包）
  crud.py        DB 与引擎桥接、全量重算与持久化
  database.py    SQLite engine/session
tests/           pytest 端到端测试
scripts/smoke.sh 真实服务 HTTP 试跑脚本
docker-compose.yml / Dockerfile
```
