# 健身教练绩效分润平台

面向健身工作室的排课、签到核销、转介绍、课包转让、退款坏账与月结分润的 Python 后端服务。

## 解决了什么

- **提成口径统一**：团课授课提成、销售拉新奖励按“服务真实发生时生效中的规则版本”计算，每条分录快照规则版本，新规则上线不会改写旧账。
- **杜绝重复计提**：同一节团课不会既算授课教练又触发重复拉新——拉新奖励每位被介绍会员至多一次，在首次核销时兑现。
- **替补归实际授课人**：教练请假登记替补后，提成发给替补教练，台账注明原教练、替补原因与关联排课。
- **签到必须有据可查**：签到（含月结后的补录）必须关联原预约记录；补录不能直接写进已锁账期，只能走带理由的更正批次。
- **课包转让不带走历史**：只移动未履约的剩余课节，已核销收入留在真实服务发生的门店。
- **退款/坏账是反向分录**：历史分录永不删除或修改，只追加带 `reversal_of` 链的负数分录，落在退款/坏账发生的当期。
- **月结锁定 + 更正批次**：账期锁定后普通业务不可写入；只有 `settlement_correction`（必须填写理由）可以追加更正批次，状态变为 `corrected`。
- **重跑稳定**：所有派生标识（entry_id、批次号 `JS{账期}-{校验和}`、更正批次 `-C{n}`）只依赖事件内容；财务从零重放事件流，批次编号和金额完全一致。

## 架构

事件溯源 + 确定性投影，仅依赖 Python 3.11 标准库：

```
HTTP(app.py)
  └─ service.Platform        命令 -> 事件；命令重试按 event_id 幂等
       └─ repository         只追加事件库（内存 / JSON 文件，原子落盘）
            └─ engine        materialize(events) -> 状态 + 不可变分录 + 批次
                 └─ rules    按生效日切换的规则版本
       └─ reports            个人台账（逐笔来源/扣减）、门店留存与人效对比
```

代码位于 `src/profitshare/`，设计细节见 `docs/design.md`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rules` | 发布规则版本（生效日、授课提成基点、拉新奖励） |
| POST | `/packages` | 课包成交（总额、课节数、归属门店与销售） |
| POST | `/sessions` | 排团课 |
| POST | `/substitutes` | 教练请假/替补登记（须在开课前、带原因） |
| POST | `/bookings` | 会员预约（占用课包课节） |
| POST | `/checkins` | 签到核销，必须带 `booking_id`；补录给 `service_time` |
| POST | `/referrals` | 转介绍登记 |
| POST | `/refunds` | 退款（可指定要冲回的签到核销，带原因） |
| POST | `/bad-debts` | 坏账（反向冲回该课包链路，带原因） |
| POST | `/transfers` | 课包转让（受让会员/门店） |
| POST | `/settlements` | 月结锁定账期 |
| POST | `/corrections` | 带理由的更正批次（手工调整/补录签到/补建预约） |
| GET | `/people/{id}/statement` | 教练/销售逐笔收入与扣减 |
| GET | `/stores/comparison` | 门店留存、留存率、人效对比 |
| GET | `/periods/{YYYY-MM}/batches` | 账期批次列表 |
| GET | `/batches/{no}` / `/batches/{no}/verify` | 批次详情 / 重放核验 |
| GET | `/events` | 原始事件流（审计） |

业务冲突返回 422，事件标识冲突 409，参数错误 400。

## 运行

```bash
python3 src/index.py            # 默认 0.0.0.0:8000
PROFITSHARE_DB=.runtime/events.json python3 src/index.py
```

## 测试

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。
