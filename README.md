# dynamic-config-flags-python

本地可运行的动态配置与功能开关服务（FastAPI）。配置分层合并、开关按主体判定、
运行时版本化更新与回滚，在不同调用路径下给出一致、可解释、可复现的结果。

## 运行

```bash
uv run python main.py          # 启动服务，默认 0.0.0.0:8000
uv run pytest -q               # 运行全部测试
uv run pytest -q --log-cli-level=INFO   # 测试中打印输入配置与判定依据
```

## 配置分层与优先级

优先级确定且可解释（低 → 高），高层覆盖低层，合并结果只取决于层级、与加载顺序无关：

| 层级 | 来源 | 说明 |
|---|---|---|
| 0 | DEFAULTS | 内置默认值（`app/sources.py: load_defaults`） |
| 10 | FILE | JSON 配置文件（`create_store(config_path)`） |
| 20 | ENV | 环境变量，前缀 `APP_`，如 `APP_SERVICE_PORT=9000` |
| 30 | RUNTIME | 运行时写入（`POST /config`），版本化管理 |

### 已知配置项（`app/schema.py`）

| 键 | 类型 | 约束 | 默认 |
|---|---|---|---|
| `service.name` | str | — | `dynamic-config-flags` |
| `service.port` | int | 1..65535 | 8080 |
| `service.log_level` | str | DEBUG/INFO/WARNING/ERROR | INFO |
| `limits.max_requests` | int | 1..1000000 | 1000 |
| `limits.timeout_ms` | int | 1..60000 | 3000 |
| `features.enable_metrics` | bool | — | false |

### 非法配置的拒绝原因（可区分，不静默忽略）

- `unknown_key`：键不在模式中；
- `type_mismatch`：类型不符（`true` 不会被当作 int 接受）；
- `out_of_range`：数值越界或枚举值非法；
- `stale_version`：基于过期版本写入（HTTP 409）；
- `unknown_version`：回滚目标版本不存在（HTTP 404）。

任何一层校验失败即整体拒绝，当前生效配置保持不变。

## 功能开关语义

开关规则：`enabled`（总开关）、`rollout`（0..100 灰度百分比）、`allowlist`、`denylist`。
内置开关：`new_checkout`、`dark_mode`、`beta_api`。

判定顺序固定：`denylist` > `allowlist` > `enabled` > `rollout`，判定依据通过
`reason` 返回（`denylist_hit` / `allowlist_hit` / `flag_disabled` / `rollout_hit` / `rollout_miss`）。

灰度分桶是确定性的：`bucket = sha256(f"{flag}:{subject}") mod 100`，
`bucket < rollout` 即命中。同一主体在配置未变时结论恒定；`rollout=0` 全关、`100` 全开。
未知开关会被拒绝（`unknown_key`），不会静默按关闭处理。

## 运行时变更与版本管理

- 每次生效配置是一个不可变快照，携带单调递增的版本号；
- 写入必须携带 `expected_version`，过期版本被拒绝（乐观并发）；
- 更新先校验后发布，失败不影响当前配置；发布在锁内原子完成，
  并发读取与热更新期间不会观察到部分生效的中间状态；
- 回滚把历史版本内容发布为新版本，回滚后的判定结果与该历史版本一致；
- 历史保留最近 50 个版本。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/config` | 当前生效快照（version/config/flags） |
| POST | `/config` | 增量更新，body: `{config?, flags?, expected_version}` |
| POST | `/config/rollback` | 回滚，body: `{version}` |
| GET | `/config/history` | 可回滚版本列表 |
| GET | `/flags/{flag}?subject=...` | 开关判定，返回结论与判定依据 |

示例：

```bash
curl localhost:8000/config
curl -X POST localhost:8000/config -H 'content-type: application/json' \
  -d '{"config": {"service.port": 9000}, "expected_version": 1}'
curl 'localhost:8000/flags/dark_mode?subject=user-1'
curl -X POST localhost:8000/config/rollback -H 'content-type: application/json' -d '{"version": 1}'
```

## 验证方法

```bash
uv run pytest -q --log-cli-level=INFO
```

- `tests/test_merge.py`：层级优先级、非法配置的可区分拒绝原因；
- `tests/test_flags.py`：判定稳定性、名单优先级、灰度边界与分桶一致性；
- `tests/test_store.py`：过期版本拒绝、失败保护、回滚后判定与历史版本一致；
- `tests/test_concurrency.py`：并发读写无中间状态、并发写同一版本只有一个成功；
- `tests/test_api.py`：HTTP 层的优先级、更新、回滚与判定。

测试日志会打印输入配置与判定依据（`--log-cli-level=INFO`）。
