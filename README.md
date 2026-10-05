# 动态配置与功能开关服务（Python）

一套本地可运行的动态配置 + 功能开关能力，保证：配置优先级确定可解释、非法配置被明确拒绝
且不影响当前生效版本、开关判定对同一主体稳定可复现、热更新无中间状态、版本可乐观并发控制
与回滚；并提供**来源追溯**（每个生效值来自哪一层）、**变更审计**（成功变更可查询、有容量
上限）与**只读预演**（提交前判断候选变更是否可接受、影响哪些键）。

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

### 运行时来源追溯

`provenance` 不是启动时的静态快照，而是和生效值绑定在同一份不可变快照里，热更新与回滚后
立即同步，**不会出现值已经变了、来源还停在旧层的情况**：

- 启动期各键来源为 `DEFAULT/FILE/ENV`（沿用分层合并结果）；
- 运行时整份写入成功后，该版本配置中的每个键（含 `flags` 整表以及每个开关
  `flags.<名称>`）来源都标记为 `RUNTIME`；
- 回滚会把配置和来源**一起**恢复到目标版本（例如回滚到 v0 后，键来源重新显示为
  `FILE`，而非停留在 `RUNTIME`）。

查询方式：

- `GET /config`：`config` 与 `provenance` 取自同一版本快照，严格配套；
- `store.get_effective()`：一次性返回 `(version, config, provenance)`，避免分两次查询
  跨过一次热更新；`get_config()` / `get_provenance()` 单独使用时各自返回独立深拷贝。
- 开关配置同样可追溯：`provenance["flags"]` 是整表来源，`provenance["flags.new-ui"]`
  是单个开关的来源。

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

## 变更审计

每一次**成功的运行时变更**（热更新或回滚）都会留下一条审计记录，可通过
`GET /config/audit` 或 `store.audit_records()` 查询。每条记录包含：

| 字段 | 含义 |
| --- | --- |
| `sequence` | 单调递增序号，标识先后次序（淘汰不回卷、不复用） |
| `timestamp` | UTC ISO-8601 发生时间 |
| `kind` | `update`（热更新）或 `rollback`（回滚） |
| `from_version` / `version` | 变更前版本 / 本次产生的新版本 |
| `target_version` | 仅回滚记录有，指回滚目标版本 |
| `changes` | 结构化差异：普通键的新增/移除/修改（含 old/new），开关的新增/移除/逐字段变化 |

规则：

- **被拒绝或失败的写入不产生审计记录**（非法配置、过期版本、回滚失败均如此），也不占用
  版本号，当前生效配置不受影响。
- 审计记录有容量上限（默认保留最近 **100** 条，构造 `ConfigStore(audit_capacity=N)`
  可调整），超限按时间**淘汰最旧**记录。
- 历史版本另有独立容量上限（默认保留最近 **64** 个版本，
  `ConfigStore(history_capacity=N)`），与审计容量互不影响。
- 回滚目标若**曾经存在但已被历史容量淘汰**，返回可区分的原因 `version_evicted`
  （HTTP 410 Gone），并明确说明保留窗口；**绝不会静默回滚到其他版本**。
  目标版本号从未存在则是 `version_not_found`（HTTP 404）。

## 只读预演（dry-run）

`POST /config/preview`（或 `store.preview(config, expected_version=None)`）允许先提交候选
变更做只读校验：

- **不改变**当前生效配置、**不占用**版本号、**不产生**审计记录、不写历史；
- 可接受时返回 `accepted=true`，并给出：
  - `base_version` / `would_be_version`：基于哪个版本预演、若真提交会成为哪个版本；
  - `changes`：键级与开关级差异（哪些新增/移除/修改，old/new 分别是什么）；
  - `provenance_after`：变更生效后每个键的来源预测（整份写入后均为 `RUNTIME`）；
- 不能接受时返回 `accepted=false` 与可区分的 `reason`
  （`unknown_key` / `type_mismatch` / `out_of_range` / `stale_version`）及当前基准版本，
  方便调用方修正后重试。

隔离性保证：预演在锁内读取某一完整不可变快照后对其深拷贝做计算，多个调用方并发预演、
同时又有真实热更新时，预演不会看到半新半旧的中间状态，预演之间、预演与真实更新之间互不
串扰。所有查询/审计/预演返回的数据都是独立深拷贝，调用方修改返回值不会影响后续的配置
读取与开关判定。

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

# 只读预演：不改配置、不占版本、不留审计，返回 accepted/changes/provenance_after
curl -s -X POST localhost:8000/config/preview \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "config": {"max_connections": 128}}'

# 查询审计记录（只含成功变更，按时间正序）
curl -s localhost:8000/config/audit
```

## 代码结构

```
app/schema.py    字段约束、整份校验、可区分拒绝原因（含 version_evicted）
app/layers.py    分层来源与优先级合并（带来源记录）
app/loader.py    启动加载：默认值 < 文件 < 环境变量
app/flags.py     开关规则、稳定哈希桶位、判定与依据
app/diffing.py   键级/开关级结构化差异（审计与预演共用）
app/audit.py     审计日志：容量上限、按时间淘汰最旧、返回值深拷贝隔离
app/store.py     不可变快照(含来源)、乐观锁热更新、历史容量淘汰、审计登记、只读预演
main.py          FastAPI 接口（含 /config/preview、/config/audit、410/404 区分）
tests/           优先级 / 非法配置 / 灰度确定性 / 并发读写 / 乐观锁 / 回滚 / HTTP
                 / 来源追溯 / 审计淘汰与回滚失败 / 预演只读与并发隔离
```

## 兼容性说明

- 原有接口行为保持不变：`GET /config`、`POST /config`、`POST /config/rollback`、
  `GET /flags/{flag}` 的请求与原有字段均不变；`provenance` 以前是启动期静态值，现在改为
  随当前版本动态返回（修复了热更新后来源不更新的问题），并新增 `flags.<名称>` 粒度。
- 回滚目标版本不存在仍返回 `version_not_found`；新增的 `version_evicted` 只在目标曾存在
  但已被淘汰时出现，是互斥、可区分的新原因，不影响既有失败分支。
- `ConfigStore(initial)` 旧构造方式仍然可用；新增参数 `initial_provenance`、
  `history_capacity`、`audit_capacity`、`clock` 均为可选关键字参数（容量默认 64/100，
  即小规模使用时不会触发淘汰）。
- 预演与审计是只读/只增的旁路能力，不参与版本号推进，也不影响灰度、名单与开关判定。

## 验证方法

`uv run pytest`（共 48 个用例，`log_cli` 会打印输入配置、预演/审计结论与判定依据），覆盖：

- **优先级**：三层来源冲突时高优先级覆盖，来源记录正确，合并顺序无关可复现；
- **非法配置**：未知键 / 类型不匹配 / 越界分别给出不同 `reason`，失败后仍是上一份配置；
- **开关判定**：关闭、白名单、0%/100% 边界、同主体确定性、50% 灰度的分布近似；
- **并发读写**：8 个读线程与 200 轮热更新（穿插非法写入）期间，无非法/部分中间态，
  同一快照内判定自洽；6 个写线程竞争同一版本时恰有一个成功，其余均为 `stale_version`；
- **回滚**：回滚后判定结果与目标历史版本完全一致，未知版本被拒绝；
- **来源追溯**（`tests/test_provenance.py`）：初始分层逐键/逐开关来源正确；热更新后
  值与来源同步变为 `RUNTIME`；回滚后来源随值一起恢复；跨多次更新开关来源与判定始终一致；
- **变更审计**（`tests/test_audit.py`）：成功 update/rollback 含版本、时间、结构化差异；
  非法/过期/回滚失败均不留痕且不影响生效配置；审计容量超限淘汰最旧；历史容量淘汰后回滚
  返回 `version_evicted`，与 `version_not_found` 可区分，且不静默回滚；审计返回值被篡改
  不影响内部状态；
- **只读预演**（`tests/test_preview.py`）：接受时给出键/开关变化与来源预测，拒绝原因可
  区分；多次预演不占版本、不留审计、不改生效配置；篡改预演/查询/审计返回值不影响后续读取
  与开关判定；6 个预演线程与 300 轮真实热更新并发时，无半新半旧状态、无相互串扰。

也可启动服务后按「HTTP 接口」小节手动验证：热更新后 `GET /config` 的 `provenance` 立即
变为 `RUNTIME`；`POST /config/preview` 连续调用后版本不变；`GET /config/audit` 只在真实
变更后增加记录。
