# 健身教练绩效分润平台

健身工作室排课、签到核销、转介绍、退款、教练替补与月结分润的 Python 后端服务。
仅依赖标准库（Python 3.11+），持久化为 `.runtime/events.jsonl` 仅追加事件台账。

## 核心原则

| 诉求 | 实现 |
| --- | --- |
| 同一团课不能被授课提成与拉新奖励重复计算 | 拉新奖励只在**会员首单成交时**确认一次（`sales_referral`）；授课提成只在**每次核销时**确认（`coach_commission`），两类分录互不重叠；续费、重复购课不能标记拉新 |
| 按生效中的规则版本计算 | 规则带 `effective_from`，授课按**服务发生日**取版本，拉新按成交日取版本 |
| 签到补录必须关联原记录 | 签到必须基于已存在的预约（booking），补录 `source=backfill` 且记录服务日与录入日；冲销必须引用原 `checkin_id`，反向分录链接原 `entry_id` |
| 课包转让，收入责任留在真实服务发生处 | 转让只改课包属主与后续服务门店；转让前已核销的收入留在原门店快照，不回改 |
| 退款 / 坏账不抹历史 | 一律新增负向分录（冲减授课、门店、按比例追回拉新，不足部分冲递延），原分录保留并被 `linked_entry_id` 引用；坏账仅限赊销 |
| 教练请假替补 | 替补事件只影响此后核销，已发生服务的教练/门店归属不变 |
| 月结锁定后只能追加带理由的更正批次 | 锁定后对该期间的任何补录/冲销/退款必须开 `COR-…` 更正批次并填理由；锁定快照（台账序号 `head_seq`）永不变 |
| 教练端看到每笔来源与扣减 | `GET /people/{id}/earnings` 返回逐笔分录、正负方向、来源与文字说明 |
| 店长比较门店留存与人效 | `GET /stores/comparison?period=YYYY-MM` |
| 财务重跑稳定 | 分录 ID 由事件序号确定性派生（`E000006-01`），批次编号为 `STL-YYYY-MM-CCY` / `COR-YYYY-MM-CCY-NN`；纯函数重放，金额与编号不变 |

金额内部一律为整数「分」，比率用 Decimal 计算、四舍五入到分。

## 运行

```bash
python3 src/index.py            # 默认 0.0.0.0:8000
RUNTIME_DIR=/data python3 src/index.py
python3 -m unittest discover -s tests
docker compose up --build
```

## API

所有请求/响应均为 JSON。错误响应：`{"error": "...", "message": "..."}`，
状态码 400（业务校验）/ 404（对象不存在）/ 409（状态冲突）。

基础数据与规则：

- `POST /stores` `{store_id?, name}`
- `POST /people` `{person_id?, name, role: coach|sales|both, store_id?}`
- `POST /rules` `{version_id?, effective_from, commission_rate, referral_rate, referral_qualifying_min_cents?}`

业务流程：

- `POST /sales` 售课包（赊销传 `on_credit:true`；拉新传 `referred:true` + `salesperson_id`）
- `POST /sessions` 排课
- `POST /substitutions` `{session_id, new_coach_id, reason}` 请假替补
- `POST /bookings` `{booking_id?, session_id, member_id, sale_id}`
- `POST /checkins` `{booking_id, source?: normal|backfill, reason?, correction_batch_id?}`
- `POST /checkin-reversals` `{checkin_id, reason, correction_batch_id?}` 冲销
- `POST /transfers` `{sale_id, new_member_id, to_store_id?}` 课包转让
- `POST /refunds` / `POST /bad-debts` `{sale_id, amount_cents, reason, date, correction_batch_id?}`

月结：

- `POST /settlements/lock` `{period: "YYYY-MM", currency?: "CNY"}`
- `POST /corrections/open` `{period, reason}` → 返回 `correction_batch_id`
- 锁定期间的补录/冲销/退款携带该批次号
- `POST /corrections/post` `{correction_batch_id, adjustments?: [{target_type: person|store, target_id, amount_cents, reason}], request_id?}`

查询：

- `GET /people/{person_id}/earnings?period=YYYY-MM&include_corrections=1`
- `GET /stores/comparison?period=YYYY-MM`（留存率、人效/人均留存）
- `GET /settlements/YYYY-MM`（锁定批次快照）
- `GET /corrections/{batch_id}`（更正批次明细）
- `GET /health`

## 事件类型

`store_registered`、`person_registered`、`rule_version_published`、`package_sold`、
`class_session_created`、`coach_reassigned`、`booking_registered`、
`check_in_recorded`、`check_in_reversed`、`package_transferred`、
`refund_recorded`、`bad_debt_written_off`、`settlement_locked`、
`correction_opened`、`correction_posted`。

分润分录（entries）由 `engine.compute_entries` 从事件流纯函数推导，不单独落库；
结算锁定时把台账 `head_seq` 写入快照边界，保证历史批次可重放且永不被后续事件改写。
