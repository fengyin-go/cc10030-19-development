# 航次编排协作台

航运调度员、船长和港口代理共同编排航线、分配船舶、登记货物并跟踪每个航次的装载与到港状态。

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
| 船舶档案 | `vessel` | 船舶 | 船名、总吨位、载重吨 |
| 航线编排 | `route` | 航线 | 起运港、目的港、经停港口 |
| 航次编排 | `voyage` | 航次 | 船舶编号、航线编号、计划离港 |
| 货物登记 | `cargo` | 货物 | 货物品名、托运方、件数 |
| 船员排班 | `crew_shift` | 排班单 | 船员姓名、岗位、上船日期 |
| 燃料记录 | `fuel_log` | 燃料记录 | 船舶编号、加油港口、燃料类型 |
| 港口挂靠 | `port_call` | 挂靠记录 | 航次编号、挂靠港口、预计到港 |
| 箱号登记 | `container` | 集装箱 | 箱号、箱型尺寸、封条编号 |
| 气象预警 | `weather_alert` | 预警通知 | 预警海域、预警类型、预警等级 |
| 事故记录 | `accident_log` | 事故记录 | 发生时间、发生海域、涉事船舶 |
| 船舶检验 | `inspection` | 检验记录 | 船舶编号、检验类型、检验机构 |
| 通关申报 | `clearance` | 申报单 | 航次编号、申报类型、申报港口 |
| 租船合约 | `charter` | 合约单 | 租用船舶、承租方、日租金 |
| 泊位安排 | `berth` | 泊位安排 | 港口名称、泊位编号、靠泊船舶 |
| 舱单编制 | `manifest` | 舱单 | 航次编号、货物清单、托运方汇总 |
| 航海日志 | `voyage_log` | 日志条目 | 航次编号、记录时刻、船位经纬度 |
| 船舶保养 | `maintenance` | 保养记录 | 船舶编号、保养项目、保养周期 |
| 船员证书 | `crew_cert` | 证书记录 | 船员姓名、证书类型、证书编号 |
| 调度指令 | `dispatch_note` | 指令单 | 接收船舶、指令内容、发出时间 |
| 费率核算 | `tariff` | 费率单 | 服务项目、计费单位、标准费率 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：应用开发
子方向：web前端/全栈开发

## 配载批次回执

货物登记不再只支持逐票确认，新增「按舱位批量核对装载」：

- `POST /api/stowage/preview`：按船舶档案 + 货票（可带舱位）预检余量，只读、不落库；
- `POST /api/stowage/batches`：多选货票提交配载批次，生成回执并关联船舶档案；
- `POST /api/stowage/batches/<id>/confirm`：整批核对装载，按每张货票当前的实际吨位扣减船舶占用；
- `POST /api/stowage/batches/<id>/return`：退回协调，未装载货票保持待装载、可回原批重办；
- `POST /api/stowage/cargo/<票号>/confirm`：单票确认也生成回执；
- `POST /api/stowage/cargo/<票号>/reweigh`：补报称重，已装载的按差额修正船舶占用；
- `GET /api/stowage/cargo/<票号>`：货代视角的配载资格（可单独确认 / 需原批协调 / 余量不足）；
- `GET /api/stowage/vessels/<船名>`：船舶载重吨、已占用、剩余与相关回执。

一致性规则：

1. 同一货票在任一时刻只能出现在一份有效回执（已确认 / 部分确认）里；重复选入的批次核对为「待原批协调」，不重复扣吨位；
2. 确认按货票当前吨位（含复磅吨数）计算，每次有效确认后船舶占用都不超过载重吨；
3. 批次按舱位顺序累计余量，先确认能装下的货票，装不下的保持「待装载」，回执标记为「部分确认」；
4. 整批核对未通过时货票状态和船舶吨位都不变，未确认货票仍留在原批回执里。

前端 `货物登记（/cargo）` 提供多选货票、按舱位核对、提交配载批次与单票确认；
`配载批次（/stowage_batch）` 展示回执明细、需协调/余量不足货票与船舶剩余吨位。
