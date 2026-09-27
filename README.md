# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/award-notices`：监督员冻结当前排名形成结果公示并写明异议截止时间（同一评审轮次仅一条有效公示）
- `POST /api/objections`、`POST /api/objections/resolve`：公示期供应商提异议；受理后公示失效并重新评审
- `POST /api/tenders/award`：公示截止且无未处理异议后，按冻结的公示排名授标，之后评分变化不改写结果

## 结果公示与授标

评审完成后监督员先发布结果公示（`POST /api/award-notices`），系统把当前轮次排名冻结为快照并进入 `notice` 状态：

- 同一评审轮次只保留一条 `active` 公示（数据库部分唯一索引保证），重复发布返回 409。
- 公示期暂停授标，且不能再评分或废标；供应商可通过 `POST /api/objections` 提出异议，截止后不能再提。
- 监督员受理异议（`accepted`）后公示置为 `void`、项目回到 `reevaluation` 且评审轮次 +1；驳回（`rejected`）则公示继续有效。
- 公示截止且没有未处理异议时，监督员才能 `POST /api/tenders/award`，赢家直接取自公示冻结快照——之后即便修改评分也不能改写结果。
- 公开页（`public`/`vendor`）公示期只展示候选顺序（供应商名称）和截止时间，评分与报价不展示；内部角色（procurement/supervisor/auditor）可见完整排名。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标—公示—异议—授标流程、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、公示冻结排名不可改写和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
