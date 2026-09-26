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

## 数据保留与处置

针对学员删除请求，服务在 `/api/retention` 下提供留存合规能力：

- **保留策略**：按明细类别（如 `checkin:regular`、`checkin:internship`、`leave_correction`、通配 `*`）配置保留年限与处置方式（`erase` 删除 / `fingerprint` 留指纹）。策略按版本管理，内容创建后不可变，全程只有一个激活版本。
- **处置任务**：创建任务时把当前策略版本与规则快照固定到任务；任务需批准后执行，支持分片执行、暂停、恢复与失败重试，每条明细独立提交，进程中断后可用新会话续跑。
- **分类处置**：法律冻结的学员明细一律跳过；已过保留期且被已签发证明（冻结快照）引用的明细删除原文但保留 SHA-256 指纹；未被引用的明细彻底删除。冻结与证明引用在执行时重新核验，批准之后落下的冻结同样生效。
- **不可篡改清单**：每次处置（含失败与冻结跳过）都追加哈希链清单条目，篡改、缺页、乱序均可被核验接口发现。
- **核验接口**：`GET /api/retention/tasks/{id}/verify` 校验策略固定、清单链与处置结果；`GET /api/retention/certificates/{plan}/{freeze}/verify` 校验已签发证明引用的明细要么仍在、要么留有指纹；`POST /api/retention/fingerprints/verify` 供外部出示原始明细比对留存指纹。

主要接口：`POST/GET /api/retention/policies`、`POST /api/retention/policies/{id}/{version}/activate`、`POST/GET /api/retention/holds`、`POST /api/retention/holds/{id}/release`、`POST /api/retention/preview`、`POST/GET /api/retention/tasks`、`POST /api/retention/tasks/{id}/approve|execute|pause|resume|retry`、`GET /api/retention/tasks/{id}/manifest|verify`。
