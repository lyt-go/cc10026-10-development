# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 订阅费用归属（用量计量 × 订阅管理）

用量计量在「按调用方、API、月份累计」之外，新增调用明细与有效订阅的关联计算，
生成可用于出账的订阅费用归属明细；订阅管理按已核定金额形成各自账期明细。

- `usage_billing.record_call_detail`：记录调用明细并生成归属明细。只有调用发生时
  有效且匹配调用方和 API 的订阅可以承接费用；恰好一个候选订阅即时核定，否则金额
  挂待核。任何时点：归属金额 + 待核余额 ≡ 原调用金额。
- `usage_billing.submit_evidence`：补录订阅凭据并核定归属。按调用编号加 flock
  互斥锁，并发/重复补录只确认一次；账期已封账时由订阅管理在后续账期补记。
- `subscription.close_billing_period`：封账，按已核定金额形成各订阅账期明细，
  待核调用保留待核余额并留下封账快照（封账依据）；封账只能进行一次。
- `usage_billing.call_trail` / `attribution_report`：沿调用编号回溯事件流
  （封账依据、待核余额变化）与补记/冲正记录，重新出具归属明细。
- 归属状态持久化在 `data/attribution/`（JSON 文件，原子替换写入），进程重启后
  仍可追查。

场景测试（同一团队两份重叠订阅，8 元先出账 + 12 元待核，封账前/后补凭据，
合计始终 20 元）：

```bash
cd backend && python3 tests/test_usage_attribution.py
```

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
