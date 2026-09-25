# 科研样品全生命周期管理服务

这是一个面向科研机构样品库、实验室和课题组的模块化后端，集中管理样品接收、分装、借用、归还、消耗、销毁、库存盘点、谱系事件、保管位置、异常记录、登录权限、审计以及可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 已有能力

- 身份与权限：支持引导管理员、登录、会话、用户、角色和细粒度权限。
- 批次与二维码：接收批次保存项目、数量和稳定二维码载荷。
- 样品档案：登记样品、数量、单位、保管位置和生命周期状态。
- 分装谱系：一次事务内校验直接守恒（子样合计+损耗=换算后分装数量±容差）后扣减母样、创建子样、记录事件链；每个操作固化处理前后单位、换算规则编码与版本、换算系数快照、可解释损耗原因、容差快照和操作版本，支持分装、冻干、研磨等操作类型。
- 换算规则：按规则编码版本化管理，系数不可改写，修正只产生新版本并废止旧版本；历史操作始终使用写入时的系数快照，不受后续规则升级影响。
- 谱系追溯与汇总：任意节点可向上追溯来源链路（含每步操作快照），向下汇总存量、消耗、销毁与损耗，并沿各步快照系数归一化到节点单位判定整条谱系是否守恒。
- 跨层守恒报告：标出断链、循环、重复编码和超出容差的步骤；摘要与问题排序确定性生成并附 SHA-256 摘要值，相同数据结果稳定一致。
- 借用归还：保存借用数量、到期时间、部分归还和最终归还状态。
- 实验消耗：使用幂等键登记消耗，防止重复请求二次扣减。
- 位置脱敏：普通权限只能看到受限位置的替代码，授权人员可查看精确位置。
- 双人审批：高风险操作要求申请人与审批人分离，并累计不同审批人的决定。
- 异常追踪：异常可以关联样品或接收批次，保存严重度和处理状态。
- 审计与任务：关键身份及业务操作留痕，后台任务支持去重、领取与完成。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/samples.db`，可用 `SAMPLE_DATABASE_PATH` 指定其他路径。

## 初始化与完整性检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 谱系接口一览

- `POST /api/sample-operations/conversion-rules`：注册换算规则（同参数幂等重放，系数变化产生新版本）。
- `GET /api/sample-operations/conversion-rules`：按编码与版本查看换算规则历史。
- `POST /api/samples/{id}/aliquots`：提交分装/冻干/研磨操作，事务内校验直接守恒，支持 `operation_kind`、`output_unit`、`conversion_rule_code`、`loss_reason`、`tolerance_ratio` 与幂等 `operation_code`。
- `GET /api/sample-operations/{id}/lineage/ancestors`：自根到该节点的来源链路与每步操作快照。
- `GET /api/sample-operations/{id}/lineage/rollup`：向下汇总存量与累计消耗并归一化判定守恒。
- `GET /api/sample-operations/{id}/lineage/report`：跨层守恒报告，列出断链、循环、重复编码与超容差步骤。

## 测试

```bash
python -m pytest
```

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```
