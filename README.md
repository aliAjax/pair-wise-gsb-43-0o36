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
- `POST /api/tenders/announce`：监督员冻结当前排名并发布结果公示，写明异议截止时间
- `POST /api/objections`：供应商在公示期内提出异议
- `POST /api/objections/resolve`：监督员处理异议，受理后公示失效并进入重新评审
- `POST /api/tenders/award`：公示期满且无未处理异议后，按公示排名快照授标

## 结果公示与异议

评标完成后、授标之前必须经过结果公示：

1. 监督员调用 `/api/tenders/announce` 把当前评审轮次的排名冻结成公示，并指定异议截止时间；同一评审轮次只保留一条有效公示（重复发布返回 409）。发布后项目进入 `announcing`，评分锁定、暂停授标。
2. 公示期内供应商可通过 `/api/objections` 提出异议；截止后不再受理。
3. 监督员 `/api/objections/resolve` 处理异议：驳回不影响公示；受理（accepted）后当前公示标记为 `invalid`，项目进入 `reevaluation` 且评审轮次 +1，需要重新评分并重新公示。
4. 只有异议截止时间已过、没有未处理异议（及投诉）时，监督员才能按**公示冻结的排名快照**授标；之后即使评分数据被改动也不会改写授标结果。
5. 公开页（`public`）在公示期只展示候选名次、供应商/投标编号和异议截止时间，不展示评分与报价；监督员等内部角色可看到完整快照与异议列表。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
