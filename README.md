# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 学时明细留存与处置

不同明细类型按 `app/retention` 中的留存策略设置不同保留年限，学生删除请求经预览、批准、执行三段处理，处置分三类：

- **删除（delete）**：过保留期且未被已签发证明引用，物理删除明细行；
- **保留指纹（retain_fingerprint）**：被冻结快照引用（已签发证明需可核验）或策略要求留痕时，脱敏原始内容并保留 `sha256` 内容指纹，冻结快照是独立 JSON 不受影响；
- **法律冻结（legal_hold）**：学生名下有未解除冻结令时原样保留，执行时实时复核，优先级最高。

API：

- `POST /api/retention/policies` / `GET /api/retention/policies/{version}`：发布不可变策略版本（按明细类型设保留天数与到期动作，支持 `*` 缺省规则）；
- `POST /api/legal-holds/{id}` / `POST /api/legal-holds/{id}/release` / `GET /api/legal-holds`：法律冻结令管理；
- `POST /api/disposal-tasks/{id}/preview`：生成预览任务（任务固定创建时的策略版本与内容校验值）；
- `GET /api/disposal-tasks/{id}`：查询任务与逐条状态；
- `POST /api/disposal-tasks/{id}/approve`：批准（记录批准人，`dry_run` 置为假）；
- `POST /api/disposal-tasks/{id}/execute`：执行，支持 `{"limit": N}` 分批、`/pause` 暂停、`/cancel` 取消；失败项标记 `failed` 可重复调用重试，崩溃后用新会话重新调用即可从已提交位置续跑；
- `GET /api/disposal-tasks/{id}/manifest`：哈希链式处置清单；
- `GET /api/disposal-tasks/{id}/verify`：重算哈希链并逐条核对数据库当前状态（删除是否消失、脱敏/冻结是否相符、指纹是否一致）。

每条明细独立事务提交；清单为哈希追加链，链尖存任务行并以比较交换推进，多执行器并发不会重复处置或产生分叉。相关测试见 `tests/test_retention_disposal.py`，覆盖三分类、批准门控、部分失败重试、法律冻结复核、暂停分批、重启续跑、并发执行与查询、篡改检测。
