# 动态配置与功能开关服务（Python）

一套本地可运行的动态配置 + 功能开关能力，保证：配置优先级确定可解释、非法配置被明确拒绝
且不影响当前生效版本、开关判定对同一主体稳定可复现、热更新无中间状态、版本可乐观并发控制
与回滚。

## 快速开始

```bash
uv sync
# 启动（可选：指定分层配置文件）
DYNAMIC_CONFIG_FILE=examples/config.json uv run fastapi dev main.py
# 运行测试（单测日志会打印输入配置与判定依据）
uv run pytest
```

## 配置优先级

启动期分层加载，后者覆盖前者，优先级**从低到高**：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 0（最低） | DEFAULT | `CONFIG_SCHEMA` 中各字段的内置默认值 |
| 1 | FILE | 环境变量 `DYNAMIC_CONFIG_FILE` 指向的 JSON 文件 |
| 2（最高） | ENV | `DYNCFG_` 前缀环境变量，值按 JSON 解析，如 `DYNCFG_MAX_CONNECTIONS=200` |

`GET /config` 的 `provenance` 字段给出每个键最终来自哪一层，优先级可解释；
合并结果只与输入有关，可复现。

## 来源追溯

`GET /config` 返回的 `provenance` 是**与当前生效值同步**的键级来源说明：

- 启动期：来自分层合并结果（`DEFAULT` / `FILE` / `ENV`）；
- 热更新后：生效键立即标记为 `RUNTIME`，不再生效的键（含旧开关）的来源标记同步消失；
- 回滚后：来源与值一起还原到目标版本的状态，不会停留在旧位置。

开关逐项可追溯：`flags` 整表一个来源，同时展开为 `flags.<名称>` 逐项来源
（flags 以整表覆盖，故逐项来源与整表一致）。代码侧对应 `store.get_provenance()`，
返回副本，调用方改动不影响内部状态。

## 变更审计

每一次**成功的**运行时变更（`update` 与 `rollback`）都会追加一条审计记录，
代码侧 `store.get_audit_log()`，HTTP 侧 `GET /config/audit`：

```json
{"version": 3, "timestamp": 1791159737.0, "kind": "update",
 "changes": {"max_connections": {"old": 100, "new": 256, "old_missing": false, "new_missing": false},
             "flags.beta": {"old": null, "new": {"enabled": true, "percentage": 30},
                            "old_missing": true, "new_missing": false}},
 "target_version": null}
```

- `kind` 为 `update` 或 `rollback`；回滚记录额外带 `target_version`；
- `changes` 逐项列出变更明细，开关展开为 `flags.<名称>`，新增/删除的键以
  `old_missing` / `new_missing` 标记；
- **被拒绝或失败的写入不留审计、不影响生效配置**（与校验失败的语义一致）；
- 容量上限：`ConfigStore(audit_capacity=N)`（默认 100）。超过 N 条后按时间淘汰最旧记录；
  历史版本窗口与审计容量联动，最多保留 N+1 个版本（当前版本 + 每次成功变更一条）。
  回滚目标若已被淘汰，返回**可区分的** `version_evicted`（从未存在过的版本仍是
  `version_not_found`），不会悄悄回滚到别处；
- 查询返回深拷贝，调用方改动返回值不影响后续读取与判定。

## 只读预演

`POST /config/dry-run`（请求体与 `POST /config` 相同）或代码侧
`store.dry_run(config, expected_version)`：在**不改动生效配置、不占用版本号、
不产生审计记录**的前提下，告知候选变更能否被接受：

```json
{"accepted": true, "based_on_version": 2, "reason": null,
 "changes": {"max_connections": {"old": 100, "new": 256, ...}, "flags.beta": {...}},
 "provenance_changes": {"max_connections": {"old": "DEFAULT", "new": "RUNTIME"}}}
```

- 可接受：返回 `changes`（哪些键/开关会变）与 `provenance_changes`（来源怎么变）；
- 不可接受：`accepted=false` 且 `reason` 可区分
  （`unknown_key` / `type_mismatch` / `out_of_range` / `stale_version`）；
- 并发隔离：预演全程基于同一份不可变快照，并发真实更新不会让预演看到半新半旧的
  中间态，预演也不干扰生效配置；预演不消费版本号，之后用同一 `expected_version`
  真实提交仍然可行（前提是期间没有别的写入成功）。

## 兼容范围

- 既有行为不变：优先级合并、非法配置整份拒绝、灰度/名单判定、乐观锁、
  回滚语义（生成新版本号）全部保持；既有 32 个用例无回归。
- `ConfigStore(initial)` 的旧用法仍可用：未传 `provenance` 时初始键按 `DEFAULT` 标记，
  未传 `audit_capacity` 时默认 100。
- 回滚新增一种可区分失败原因 `version_evicted`：只有在变更次数超过审计容量、
  目标版本被淘汰后才会出现；默认容量下需 100 次以上变更才触发。

## 配置项与校验

| 配置项 | 类型 | 取值范围 | 默认值 |
| --- | --- | --- | --- |
| `service_name` | string | — | `dynamic-config-service` |
| `max_connections` | int | 1 ~ 65535 | 100 |
| `request_timeout_ms` | int | 1 ~ 600000 | 3000 |
| `debug` | bool | — | false |
| `flags` | object | 见下 | `{}` |

`flags.<名称>` 规则字段：

| 字段 | 类型 | 范围 | 默认 |
| --- | --- | --- | --- |
| `enabled` | bool | — | true |
| `percentage` | number | 0 ~ 100（越界拒绝） | 0 |
| `allowlist` | string[] | — | `[]` |

非法配置**不会被静默忽略**，整份拒绝并返回可区分的原因 `reason`：

- `unknown_key`：出现模式之外的未知键；
- `type_mismatch`：类型不匹配（`true/false` 不会被当作数值接受）；
- `out_of_range`：数值超出上下界。

校验在写入锁外对整份配置完成，任一项失败即整体拒绝，**当前生效配置保持不变**
（失败时继续沿用上一份可用配置）。

## 灰度语义与判定顺序

对开关 `flag` 与主体 `subject`，按以下顺序判定，第一条命中即为结论：

1. `enabled = false` → 拒绝（依据：开关已关闭）；
2. `subject ∈ allowlist` → 放行（依据：命中白名单）；
3. 计算稳定桶位 `bucket`：`SHA-256("flag:subject")` 十六进制前 8 位对 10000 取模再除以
   100，映射到 **[0, 100)**，粒度 0.01%；`bucket < percentage` → 放行，否则拒绝。

哈希仅取决于 `(flag, subject)`，与时间、进程、随机数无关，因此**配置不变时，同一主体
永远得到同一结论**。`percentage=0` 全关、`=100` 全开；未知开关 fail-closed（拒绝）。
接口返回 `basis`（判定依据）与 `bucket`（桶位），结论可解释、可复现。

## 运行时更新与版本管理

- 生效状态是不可变快照 `Snapshot(version, config, rules)`；热更新在锁内执行
  「构建新快照 → 整体替换引用」，并发读者只会看到某一完整版本，**不会观察到部分生效**。
- 写入必须携带 `expected_version`（乐观锁）。与当前版本不一致时拒绝：
  `reason=stale_version`。
- 每次成功写入产生递增的新版本号；历史版本内容不可变。
- 回滚 `POST /config/rollback {"target_version": N}` 用历史版本内容生成**新版本号**生效，
  判定结果与目标版本一致；目标不存在时返回 `version_not_found`，当前配置不受影响。

## HTTP 接口

```bash
# 查看当前配置、版本、键级来源与历史版本
curl -s localhost:8000/config

# 按主体判定（返回 allowed/basis/bucket）
curl -s "localhost:8000/flags/new-checkout?subject=vip-001"

# 基于版本 0 提交整份配置
curl -s -X POST localhost:8000/config \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "config": {"max_connections": 128,
       "flags": {"new-checkout": {"enabled": true, "percentage": 10, "allowlist": ["vip-001"]}}}}'

# 过期版本 / 非法配置 -> 409，detail.reason 可区分（stale_version/unknown_key/type_mismatch/out_of_range）

# 回滚到历史版本
curl -s -X POST localhost:8000/config/rollback \
  -H 'Content-Type: application/json' -d '{"target_version": 0}'

# 查询变更审计（版本/时间/变更明细，按时间从旧到新）
curl -s localhost:8000/config/audit

# 只读预演：不生效、不占版本、不写审计，返回能否接受与变更摘要
curl -s -X POST localhost:8000/config/dry-run \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "config": {"max_connections": 256}}'
```

## 代码结构

```
app/schema.py    字段约束、整份校验、可区分拒绝原因
app/layers.py    分层来源与优先级合并（带来源记录）
app/loader.py    启动加载：默认值 < 文件 < 环境变量
app/flags.py     开关规则、稳定哈希桶位、判定与依据
app/store.py     不可变快照、乐观锁热更新、历史版本与回滚、来源追溯、审计、只读预演
main.py          FastAPI 接口
tests/           优先级 / 非法配置 / 灰度确定性 / 并发读写 / 乐观锁 / 回滚 / 来源追溯 / 审计 / 预演 / HTTP
```

## 验证方法

`uv run pytest`（共 47 个用例），覆盖：

- **优先级**：三层来源冲突时高优先级覆盖，来源记录正确，合并顺序无关可复现；
- **非法配置**：未知键 / 类型不匹配 / 越界分别给出不同 `reason`，失败后仍是上一份配置；
- **开关判定**：关闭、白名单、0%/100% 边界、同主体确定性、50% 灰度的分布近似；
- **并发读写**：8 个读线程与 200 轮热更新（穿插非法写入）期间，无非法/部分中间态，
  同一快照内判定自洽；6 个写线程竞争同一版本时恰有一个成功，其余均为 `stale_version`；
- **回滚**：回滚后判定结果与目标历史版本完全一致，未知版本被拒绝；
- **来源追溯**：热更新后来源同步切换为 `RUNTIME`、回滚后来源与值一起还原、
  开关逐项来源可查，返回值被篡改不影响存储；
- **变更审计**：成功才记录、拒绝/失败不留痕，容量淘汰最旧记录，回滚已淘汰版本得到
  `version_evicted`（与 `version_not_found` 可区分），窗口内版本仍可回滚；
- **只读预演**：接受时给出键/开关/来源变更摘要，拒绝时原因可区分；不生效、不占版本、
  不写审计；4 个预演线程与 100 次真实更新并发时结果自洽、互不串扰；
- **返回值独立性**：查询/审计/预演返回的数据被调用方改动后，后续配置读取与开关判定
  不受影响。

单测日志（`logging`）会打印输入数据与判定依据，`uv run pytest -o log_cli=true` 可见。
