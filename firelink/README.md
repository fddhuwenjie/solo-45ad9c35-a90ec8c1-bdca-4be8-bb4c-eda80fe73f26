# 消防联动试验链复核 API

消防验收时，烟感、排烟机、电梯、楼层广播各自留日志，设备时钟相差几十秒。
本服务把多源日志统一到一条时间轴上，逐场景回放联动链，判定次序与时限是否合格。

- `wsgiref` 接 HTTP，`json` 解载荷，`sqlite3` 存修订（零第三方依赖）
- 校时脉冲拟合各设备时钟偏移，**残差越界** → 该设备时标不可信
- 逐场景回放：探测确认 → 阀门到位 → 风机启停 → 电梯迫降 → 广播切换
- 指出**首个超时**、**倒序**、**互斥输出**、**未复位旁路**，并给出上游链路
- **日志缺号 / 别名多解 / 校时残差越界 / 前置状态不明** → 相应环节保持 `unknown`
- 重绑设备或改时钟锚点必须附 `justification`，系统另起修订并保留旧演算
- 签结后冻结矩阵 / 事件 / 别名映射，拒绝新修订；提供修订差异与重放 JSON

## 运行

```bash
python3 app.py                 # 监听 :8073，库文件 firelink.db
python3 app.py --port 9000 --db /data/review.db
python3 simulate.py            # 模拟请求：时钟漂移 / 漏事件 / 合法旁路 / 修订 / 签结
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/projects` | 创建项目（载荷即请求体），存为 rev1 并立即复核 |
| GET | `/projects/{pid}` | 项目元信息 + 修订列表 |
| POST | `/projects/{pid}/revisions` | 新修订；`justification` 必填，`payload` 整体替换或 `patch` 顶层键补丁 |
| GET | `/projects/{pid}/revisions/{n}` | 第 n 版重放 JSON（旧演算永久保留） |
| GET | `/projects/{pid}/diff?from=a&to=b` | 两版载荷差异 + 场景判定变化 |
| POST | `/projects/{pid}/signoff` | 签结，冻结 matrix/events/aliases |

## 请求体约定

```json
{
  "project": "1#楼三层",
  "sync_tolerance_ms": 150,
  "devices":  {"D1": {"type": "smoke"}, "V1": {"type": "valve"}, "F1": {"type": "fan"}},
  "aliases":  {"走廊烟感": "D1"},
  "matrix":   [{"id": "S1",
                "trigger": {"device": "走廊烟感", "signal": "alarm"},
                "respond": [{"device": "V1", "signal": "open",  "within_ms": 30000},
                            {"device": "F1", "signal": "start", "within_ms": 60000,
                             "after": ["V1:open"]}]}],
  "mutex":    [["F1:start", "F2:start"]],
  "sync_pulses":    [{"device": "D1", "device_ts": 0, "master_ts": 50}],
  "bypass_permits": [{"device": "V1", "start": 0, "end": 200000, "reason": "年检"}],
  "events":         [{"device": "D1", "seq": 1, "signal": "alarm", "device_ts": 10000}]
}
```

- 时间统一为毫秒；`device_ts + 设备偏移 = 统一时轴`，偏移取该设备校时脉冲的中位数
- `after` 声明次序约束（阀门到位后才允许风机启动）；前置事件缺失记 `out_of_order`，
  但前置别名多解/无解或前置时钟不可信时保持 `unknown`，不判倒序
- 触发设备校时残差越界时，其时刻不可信，下游环节全部 `unknown`，不再计算超时
- 旁路以 `bypass_on` / `bypass_off` 事件表达；落在许可窗口内且已复位为合法，
  越窗记 `bypass_outside_permit`；无配对复位且日志无缺号才记 `bypass_not_reset`，
  缺号区间可能遗漏 `bypass_off` 时保持 `unknown`
- 所有 finding（含 mutex / bypass）均带 `upstream` 上游链路
- 判定：`pass` / `fail` / `unknown`；任何环节证据不足只标 `unknown`，不臆断

## 测试

```bash
python3 test_regression.py   # 16 项：边界组合 + 既有行为 + 修订/签结生命周期
```
