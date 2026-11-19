# 订单时效看板（UK / All / US · PUDO / LMS RDC → SPX）

抓取订单管理后台的订单数据，计算 **RDC 出库 → SPX 提取** 的耗时，输出自包含 HTML 看板。
看板右上角的 `UK | All | US` 三个 tab 直接对应后台 `Order Comprehensive Query` 右上角那个橙色开关。

## 区域 tab ↔ 数据源

后台那个 `UK | All | US` 不是前端过滤，而是**三份独立数据**，各自一台 API 主机（同一个 `hk-sit` cookie 通用）：

| tab | API 主机（由环境变量提供） | RDC 出库码 | SPX 提取码 | 当前单量（10/29 起） |
|---|---|---|---|---|
| `UK` | `OC_HOST_UK` | `DFDLUK002`（占位） | `SCAUK002`（占位） | 367 |
| `All` | 上面两台合并 | — | — | 468 |
| `US` | `OC_HOST_US` | **`DFDLUS002`** | **`SCAUS002`** | 101 |

> **后台地址刻意不进本仓库。** 代码里没有任何内部域名，主机/门户/SSO 全部从环境变量读，
> 见「配置」一节的 `OC_HOST_US` / `OC_HOST_UK` / `OC_PORTAL` / `OC_SSO_URL`。
> 本文件里出现的 `<US-API-HOST>`、`<PORTAL-HOST>` 等都是占位符，实际值填在 `.env` 或 GitHub Secret 里。
> 好处是仓库可以公开镜像/分享而不泄露内网系统地址；代价是**少了配置就会直接报错**，
> 报错信息会告诉你缺哪个变量。

`All` 在脚本里就是 UK + US 两次抓取后合并。看板默认落在 `US` tab，`?region=UK` / `?region=ALL` 可直连某个 tab。

### 取数路径（与人工操作完全同一条）

```
<SSO-HOST>/login                            # LDAP 账号 + 4 位验证码
  → 门户 /main 点「智慧物流管理系统」
  → <PORTAL-HOST>/orderManage/OrderComplexQuery
  → 右上角选 US → 点「搜 索」
```

页面右上角切 US 后发出的请求，正是脚本用的那条（已用浏览器抓包核对）：

```
GET https://<US-API-HOST>/aquarius-order/order/tracking-number/v1/tracking-number-list
    ?startDate=...&endDate=...&page=1&limit=10
```

所以**抓数和人工查询是同一个接口、同一台主机**，不存在"脚本看到的数据和页面不一样"。
区别只有两处，都在脚本侧：

1. `limit` 用 100（页面默认 10），翻页拉全量；
2. 带 `destinationCountry=US`（页面不带）。实测两者 total 完全相同（10/29–11/05 都是 161 单），
   脚本仍保留它作为兜底，随后**再在本地按看板口径过滤**（时间窗、测试单、取消、串场等）。

> **UK tab 的实情**：这个账号的 UK 数据全是跨境首程单（产品 `ESDE`/`EDAE`/`ESEPAE`，德/欧起运），
> 事件码是 `OKDLFD-EDAE`、`DLFD-JPFULL`、`ITFD-EDAE` 这类 FedEx / 分拨码，**既没有 DFDL 出库也没有 SCA 提取**。
> 所以 UK tab 只能看单量与 A-SCAN 时效，看板会显式给出橙色提示，不做假数。

## 一句话口径（US）

| 节点 | 取值 | 说明 |
|---|---|---|
| 订单创建 | `orderCreatedTimestamp`（毫秒戳） | 订单头 |
| **面单生成** | JV Event Code = **`OCRETURNLBDE002`** | `The sender has placed an order and the shipping label has been generated`，即 label 生成 |
| A-SCAN | `firstScanTime` | 订单头 |
| **RDC 出库** | JV Event Code = **`DFDLUS002`** | `Package ready for dispatch`，标准 warehouse out |
| **SPX 提取** | JV Event Code = **`SCAUS002` 或 `SCN005`**（任一命中即算，取最早一条） | `SCAUS002` = `Picked up by delivery carrier`（司机提取）；`SCN005` = `Processing at Origin Facility`（始发中心处理）。PUDO 链路出库后不报 `SCAUS002`，只认前者会把已交接误判为未交接 |
| **妥投** | JV Event Code = **`DELUS006`**（US）/ **`DEL005`**（UK） | isEventCode = `DELIVERED`，`派送完成 / Delivered` |
| 核心指标 | `hRdcToSpx` | 出库 → 被 SPX 拿走 的小时数 |
| 端到端 | `hCreateToDelivered` | 下单 → 妥投 的小时数 |
| PUDO / LMS 区分 | **同一条 `DFDLUS002` 事件自带的 `vendorCode`** | `PUP` → PUDO INC；`LMS` → LMS |
| **订单来源** | 接口 `serviceProductCode`（即 serviceID） | **`USDOMS2D` → eBay**、**`UDDOMS2D` → Depop**；`channel` 两边同为 `GPLT`、`ebayOrderId` 在 Depop 单上是占位值 `00-00000-00000`，都不能用来分流 |

### 面单生成：`OCRETURNLBDE002`

`jvEventCode = OCRETURNLBDE002` → `The sender has placed an order and the shipping label has been generated`
（中文后台：`下单 - 卖家已经下单创建物流面单Label`），即**卖家已建单、面单已生成**。

- **每单恰好一条**（2026-10-20 ~ 11-03 的 333 单实测 333/333），所以「有这个事件」= 面单已生成；
- 取最早一条，落在订单字段 `labelTime`，派生 `hCreateToLabel` = 下单 → 面单生成的小时数；
- KPI 卡片「面单生成」显示**有该事件的单数**，副标题写覆盖率。

覆盖率按区域差别很大，属接口行为、不是丢单：

| 区域 | 覆盖 | 原因 |
|---|---|---|
| US | 100%（105/105） | 全量上报，下单后几秒即出 |
| UK | 38%（146/380） | 只有本地派送单（`ESDE` 等）上报；**跨境首程单**（`EDAE` / `ESEPAE` / `EXAE` / `ESAYAE` / `EXFE*`）事件里只有 0~1 条 `OCFD-*` / `ITFD-*`，接口不报此码 |

看板据此把缺失单分成两种文案：缺失单 ≥80% 是 `E` 开头跨境产品时写
「N 单为跨境首程单，接口不报此码」，否则写「N 单未上报」——后者才是真的面单缺失，要查。

### 妥投：`DELUS006`（US）/ `DEL005`（UK）

`isEventCode = DELIVERED`、`jvEventCnRemark = Delivered`、中文后台「派送完成」，
`vendorCode = SX`（SPX）。**jvEventCode 按区域不同**：

| 区域 | 妥投码 |
|---|---|
| US | `DELUS006` |
| UK | `DEL005` |

取数时**两个码都认**（`scraper.DELIVERED_EVENT_CODES`，可用 `OC_DELIVERED_EVENT` 覆盖，逗号分隔），
区域码只决定页面上展示哪一个——上游哪天给 UK 也换成带区域后缀的码时不会整列变空。
每单最多一条，取最早一条，落在 `deliveredTime`；派生 `hCreateToDelivered` = 下单 → 妥投的小时数。
有妥投事件的单状态记为 **`已妥投`**（优先级高于「已交SPX」）。

**为什么窗口内常常是 0 单**：妥投事件由 SPX 回传、延迟较大，实测端到端 **4~6 天（约 100 小时）**，
所以按「下单时间」取数的看板里，只有窗口头部那几天下的单才会显示妥投。
2026-11-03 用 10/29 起的窗口抓 486 单是 **0 单妥投**；同期用 10/20 起的窗口能看到
**7 单真实单妥投**（`EVA MYRBACK`，下单 10/20–10/22、妥投 10/26–10/27）。
想看妥投数据就把 `OC_START_DATE` 提前到 `2026/10/19`（接口可回溯的最早日期，见下）。

> **接口的日期硬下限**：后台对 `startDate` 有下限，实测 **2026/10/19**（约「今天往前 17 天」）。
> 写更早的日期时它**不报错**，而是静默忽略时间条件、返回全库（US 约 2.5 万单）。
> `scraper.SANITY_MAX_ORDERS = 5000` 是守门员：超过就抛错，不会把全库写进快照；
> `refresh_all.py` 捕获这个错后会自动收缩到「今天往前 16 天」重试一次。
> 所以 **2026 年 10 月 1–18 日的数据，用这个接口拿不到**，需要后台导出或另开权限。

### 出库节点登记：PUDO INC 与 LMS 两家 vendor 共用 `DFDLUS002`

业务口径（2026-11-03 依《供应商 eventCode & jveventcode》登记表确认）：
`DFDLUS002` 是 RDC 出库（warehouse out）的唯一事件码，
**PUDO INC 和 LMS 是两家 drop off vendor，各自上报 `DFDLUS002`**，
靠**同一条事件的 `vendorEventCode`** 区分是谁出的库：

| vendor | vendorEventCode（登记表主码） | vendorCode（后台展示） | 接口常见值 | jvEventCode |
|---|---|---|---|---|
| PUDO INC | `PUP` | `PUP` | `PU` | `DFDLUS002` |
| LMS | `shipment.injected/made_ready` | `LMS` | `LM` | `DFDLUS002` |

三层事件码的层级关系（实测，同一单只会出现其中一种）：

```
isEventCode      = ML_FACILITY_OUT            ← 三家共用
isEventCnRemark  = Package departed from the facility
jvEventCode      = DFDLUS002                  ← 三家共用，判断「是否出库」只看它
vendorEventCode  = PUP | shipment.injected/made_ready | shipment.sorted  ← 判断「谁出的库」看它
vendorCode       = PU | LM                    ← 上游短码，仅用于展示与排查
```

判读规则：

- **判断「是否已出库」→ 只看 `jvEventCode`**（`DFDLUS002`）；
- **判断「哪家 RDC 出的库」→ 看同一条事件的 `vendorEventCode`**，不靠 `vendorCode` 猜；
- 登记表没收录的码不丢弃，标 `outCodeKind=""`（未登记）并在看板打红色 `?`，
  这样上游改码能立刻发现，而不是静默变成「未出库」。

#### 替代码：`shipment.sorted`

`shipment.sorted` 不是登记表主码，但**实测在跑**，且 `isEventCode` 同为 `ML_FACILITY_OUT`、
`isEventCnRemark` 同为 `Package departed from the facility`，语义等价。归入 LMS，
在 `scraper.VENDOR_EVENT_ALIASES` 里登记，订单上标 `outCodeKind="alias"`，看板打黄色「替」。

实测分布（10/20–11/03，共 333 单）：

| vendorEventCode | 单数 | 收件人 | 时间 | 说明 |
|---|---|---|---|---|
| `shipment.injected/made_ready` | 22 | 全部 `Test` | 10/20 – 10/30 | LMS 链路，窗口内只出现在测试单上 |
| `PUP` | 8 | 全部 `Test` | 10/20 – 10/30 | PUDO INC 链路，同上 |
| `shipment.sorted` | 14 | 全部真实收件人 | 11/01 – 11/03 | **真实生产单实际只用这个码** |

即 **11/01 起 `shipment.injected/made_ready` 在数据中完全消失**，LMS 只报 `shipment.sorted`。
若严格只认登记表主码，11/01 之后的真实单会全部变成「未出库」，所以替代码必须保留。
两套都在 `--audit-out` 的输出里分列，方便随时核对。

#### 其他实施细节

1. **接口 `vendorCode` 常返回短码 `PU` / `LM`**，后台界面显示 `PUP` / `LMS`。
   代码两套都认（`scraper.VENDOR_ALIASES`），页面徽标统一 `PUP` / `LMS`，
   接口原值另存 `vendorCodeRaw` 备查。
2. **`COLUS002`（司机从 PUDO 点取件）不作为出库判据**。真实 PUDO 单目前观察到
   `COLUS002` → `AFDLUS002` 这条链，与 `DFDLUS002` 是两回事，不要混用。

若同一单被两家各报一次出库（少见，通常是交接），按**更早那条**定归属，单元格标「两家」，
`outMixed=true` 同时写进 `orders.json`。

想核对当前窗口里各 vendorEventCode 的分布、有没有未登记码：

```bash
python scraper.py --audit-out --from 2026/10/29
```

输出会按 `vendorEventCode` 分列，并标 `[主码]` / `[替代码]` / `[未登记]`。

同一单可能重复上报同一个码（实测 SCAUS002 常出现 3 条），一律**取最早一条**。

**时间精度**：所有时间统一为北京时间（UTC+8），**精确到秒**（`YYYY-MM-DD HH:mm:ss`），
明细表、事件时间轴、CSV 导出、清单页一致。秒级时间也是排查「同分钟上报」（如 PUDO 的
DFDLUS002 与 SCAUS002 落在同一分钟）时的必要信息。

### 投递方（drop off）：客户把包裹交给了谁

「客户把包裹交给谁」和「谁出的库」是**两件事**，必须分开记：

| 维度 | 取数 | 例子 |
|---|---|---|
| **投递方（drop off）** | 收件扫描事件的 vendor | PUDO INC（投在 PUDO 点）、LMS（交到 LMS 门店/站点） |
| 出库方（RDC） | `DFDLUS002` 事件的 `vendorEventCode` | PUDO INC（`PUP`）、LMS（`shipment.injected/made_ready`） |

存在「投递在 PUDO INC、却从 LMS 的 RDC 出库」的单，按 RDC 统计会把它算进 LMS，
按投递方统计才看得出货到底在谁手上。

**投递事件的判定**（`scraper.is_drop_event` / `extract_drop`）：

- 主判据：`isEventCode = FIRST_SCAN`（收件扫描）—— 跨区域通用；
- 兜底：事件码 `DOUS002` / `DOUD002`（`DOUD002` 实测出现过 1 条，像是上游拼写漂移）；
- 同一单可能重复上报投递（换点位重投），一律**取最早一条**，那才是首次交寄；
- 事件码跨区域不统一：US 是 `DOUS002`（vendor `PU`/`LM`），
  UK 是 `SHRSERPTESDE002` / `SHRPCKSTESDE002`（DHL Service Point / Packstation，vendor `DR`），
  所以 UK 的投递方会显示成后台的 vendorName。

取到的字段：`dropVendor`（展示名）/ `dropVendorRaw`（接口原值）/ `dropEventCode` /
`dropTime` / `hCreateToDrop` / `dropVendorDiffOut`（投递方 ≠ 出库方时为真）。

### 串场：PUDO 点投递 → LMS 司机取走

运营上要单独盯的一类异常：**客户把包裹投在 PUDO INC 的点位上，最后却是 LMS 的司机来取走的**。

判定要两条同时成立：

1. 单内存在 `DOUS002`（Customer drop-off at PUDOpoint）且该事件 `vendorCode = PU`
   —— 说明投递点在 PUDO INC 那边，包裹确实进了 PUDO；
2. 该投递时间**之后**，出现取件/离场类事件且 `vendorCode = LM`：
   `COLUS002`（司机取件）、`DFDLUS002`（场地分拣进笼车）、`AFDLUS002`（到仓）、
   `SHIPFCUS002`（可提货）。

刻意的两条排除，不能省：

- **`SHIPCCUS001`（取消）不算**。它同样带 `vendorCode=LM`，但语义是撤销，不是取件；
  窗口内「投递后被取消」的单有 17 单，混进来会把结果放大十几倍。
- **投递之前的事件不看**。只认严格晚于投递的取件，否则「先被 LMS 取走、之后再补登
  PUDO 投递」这种顺序倒挂的单会被误收进来。

跑法：

```bash
python cross_vendor.py --from 2026/10/20 --to 2026/11/03
```

产出 `data/cross_vendor.json` 与 `data/cross_vendor.csv`；看板里该单的单号单元格会挂红色
**串场** 标记，口径说明区顶部有一条汇总条。`refresh_all.py` 已把它接进定时流程，
CronJob 每轮会自动重算。

实测（10/20–11/03，333 单，PUDO 点投递 34 次）：

| 运单号 | 收件人 | PUDO 投递 | LMS 取件 | 间隔 | 佐证 |
|---|---|---|---|---|---|
| `USPDPPS4000057524N` | LEX BYRNE | 10-30 04:49 | 10-31 00:43 `COLUS002` | +19.9h | 10-31 01:53 `MISUS001` Package not found at pick-up |

只有 1 单，占 PUDO 投递量的 3%（1/34）。这单在看板 RDC 列显示「—」，
因为它的取件事件是 `COLUS002`(LM) 而不是 `DFDLUS002`，**这个空值本身就是串场的表现**，
不要当成数据缺失。`MISUS001`（PUDO 去取件时包裹未找到）是同一件事的旁证：
包裹已被 LMS 拿走，PUDO 的司机自然扑空。

### LMS 同供：建单与投递都是 LMS

运营上想单独看的一类：**从建单到投递全程由 LMS 承接、不经 PUDO INC** 的单。
判定看的是**事件自身的 `vendorCode`**，不是订单上的出库方：

1. 单内存在 `OCUS001`（Shipment created，建单）且该事件 `vendorCode = LM`；
2. 同一单存在 `DOUS002` / `DOUD002`（客户投递）且该事件 `vendorCode = LM`。

`OCUS001` 有两种语义，靠 `oprCnDes` 区分，这是判定的关键：

| vendor | oprCnDes | 含义 |
|---|---|---|
| `LM` | `shipment.created` | 由 LMS 建单 |
| `PU` | `Pending drop-off at PUDOpoint, shipment registered with PUDO` | 登记为「待投递到 PUDO 点」 |

跑法：

```bash
python lms_same_vendor.py              # 读 data/orders.json，事件有缓存，只补新单
python lms_same_vendor.py --no-cache   # 全部重拉
```

产出 `data/lms_same_vendor.json`；明细表单号后挂蓝色 **LMS 同供** 标记，工具栏勾「只看 LMS 同供」
可单独筛出。`refresh_all.py` 已接入，每轮自动重算。

### FIRST_SCAN（首扫）字段回填

`scraper.py` 从本轮起给每张单多写两个字段：

- `dropIsEventCode`：投递事件（最早一条）的 `isEventCode` 原值，正常是 `FIRST_SCAN`
- `dropIsFirstScan`：是否真的由 `FIRST_SCAN` 判定出来（false 说明是靠 `DO*` 事件码兜底的）

老快照没有这两个字段，不想为这点改动重抓一遍（要等 Cookie + 十几分钟）时，用事件缓存回填：

```bash
python backfill_firstscan.py --dry-run   # 先看统计
python backfill_firstscan.py             # 写回 orders.json / orders.csv
```

判定直接调 `scraper.extract_drop`，和抓数时同一份逻辑，不会出现两套口径。
实测 399 单里有投递事件 140 单，`isEventCode` 全部是 `FIRST_SCAN`，兜底 0 单。

**实测（10/19–11/05，399 单）：命中 16 单，全部是测试单，真实单 0 单。**
建单方 × 投递方交叉：

| 建单 | 投递 | 真实单 | 测试单 |
|---|---|---|---|
| 无事件（还没进链路） | — | 83 | 159 |
| PU 建单 | LM 投递 | **53** | 20 |
| PU 建单 | PU 投递 | **36** | 15 |
| **LM 建单（另有一条 PU 建单）** | **LM 投递** | **0** | **16** |
| PU 建单 | 未投递 | 0 | 17 |

两个结论要记住：

- **真实单里没有任何一张是 LM 建单**，`OCUS001` 全部是 PU（「待投递到 PUDO 点」）。
  所以这张标记在默认「排除测试单」下是 0 单，不是功能失效。
- 命中的 16 张测试单**同时带两条 `OCUS001`**：先 `LM shipment.created`，2 秒后 `DOUS002`（LM 投递），
  约 12 小时后又补一条 `PU` 的登记事件。建单即投递、间隔 2 秒，是压测脚本的产物。

反方向「PU 建单 → LM 投递」有 **53 张真实单**，即登记时指向 PUDO 点、客户实际投到了 LMS 门店，
量级比串场大得多。这张不是异常清单，只是登记点与投递点不一致，需要时可按同样方式加标记。

### 窗口下限：早于 10/20 的日期查不了

`startDate/endDate` 在**早于 10/20** 的窗口上会失效——接口直接忽略日期条件返回全库
（10/13–10/19 返回 `total=24837`，10/06–10/19 返回 24925），数据不可用。
所以串场、取消清单这类基于事件明细的统计，实际可回溯范围就是 **10/20 起**。
更早的数据只能换别的方式取（按单号逐个查，或让后台导出）。

## 接口（实测可用）

```
PREFIX = /aquarius-order

GET https://<US-API-HOST>{PREFIX}/order/tracking-number/v1/tracking-number-list
    ?page=1&limit=100
    &startDate=2026/10/20 00:00:00
    &endDate=2026/11/03 23:59:59
    &destinationCountry=US            # US 区；UK 区不带这个参数
GET {同上}{PREFIX}/order/tracking-number/v1/tracking-event-list/{trackingNumber}
```

鉴权：Cookie `hk-sit=...`（`<PORTAL-HOST>` 登录后拿到，跨区通用）。

### 踩过的坑

1. **网关前缀必须是 `/aquarius-order`**，缺了它一律 404（单靠猜 `api` / `gateway` 猜不到）。
2. **区域由右上角 tab 决定**：US 数据只在 US 主机（`OC_HOST_US`），UK 主机（`OC_HOST_UK`）上是另一批跨境单。
3. **日期必须带时间**：`2026/10/20 00:00:00`，只给日期会返回 0 单。`limit` 上限 100。
4. **必须 GET**，POST 返回 405。
5. **时区：`+04:00` 就是 UTC+4，不是 UTC-4**，别把符号想反。事件 `oprTimeWithZone` 恒标 `+04:00`，
   订单头 `firstScanTime` 恒标 `+08:00`，这是两套渲染时区，同一时刻写法不同（事件写 `05:31+04:00`、
   订单头写 `09:31+08:00`，都指向 `01:31 UTC`），按各自标注解析即可对齐。
   **真正不可信的是 `oprTime` 毫秒戳**：同一个字符串会对应两个相差 6 小时的毫秒值，实测存在
   +7h/+12h/+13h/+16h 四种漂移。因此解析顺序为 `oprTimeWithZone` 优先、毫秒戳兜底；订单头反之
   （`orderCreatedTimestamp` 与 `*WithZone` 一致，`orderCreatedTime` 会偏 +3h）。
   交叉验证：取件事件的 `parse(oprTimeWithZone)` 与订单头 `firstScanTimeStamp` 41/41 完全一致。
   诊断脚本：`probe_time.py`（字段自洽性统计）、`probe_diff.py`（新旧口径差异）。
6. **后台存在时间倒挂**：批量导入会回填旧事件时间，出现「事件早于建单」。此类区间直接置空不参与均值，
   数量记在 `meta.anomalies`，看板副标题会显示（14 天 UK+US 共 142 单）。

## 用法

```bash
python scraper.py --probe                        # 探接口，逐个区域打印单量与样例事件
python scraper.py --real                         # 按 .env 的 OC_REGION 抓（默认 all = UK + US）
python scraper.py --real --region us             # 只抓美国
python scraper.py --real --region all --days 30
python scraper.py --real --from 2026/10/29 --to 2026/11/03
python run.py --real --from 2026/10/29           # 抓取 + 出看板，一条命令
python scraper.py --demo                         # 示例数据，无网络也能看效果
```

> **当前口径**：`.env` 里 `OC_START_DATE=2026/10/19`（接口下限），`OC_KPI_START=2026/10/29`
> （KPI 与图表），`OC_DETAIL_START=2026/10/19`（底部明细）。服务端「刷新数据」也读这几个值。
> 要改窗口只改 `.env` 并重跑 `refresh_all.py`。

产物：
- `dashboard.html` —— 静态看板（**完全自包含**，0 个外部 js/css/fetch，双击即开，离线也能看）
- `data/orders.json` / `data/orders.csv` —— 明细（CSV 可直接进 Excel，含 `region` 列）
- `data/dropoff_cancel.html`、`data/stuck_shipcc.html` —— 两张异常清单，同样是单文件

### 本地双击看（不需要起服务、不需要隧道）

`python refresh_all.py` 跑完会自动重新生成 `dashboard.html`，直接双击就用浏览器打开。
标题右上角有三个按钮：

```
[刷新数据]  [drop off 取消异常 8]  [SHIPCCUS001 取消清单]
                 ↓                        ↓
        data/dropoff_cancel.html   data/stuck_shipcc.html
```

**「刷新数据」在三个入口是同一套逻辑**（`dashboard.py` 的 `REFRESH_CORE_JS`）。
点下去先判断「这次的数据该从哪儿来」，再走对应那条路；按钮文案、进度显示、失败提示三处一致：

| 你在哪个页面点 | 怎么判断 | 走了哪条路 |
|---|---|---|
| 书签弹出的 `?live=1` 窗口 | `window.__OC_LIVE` | 数据在你浏览器内存里，只能回内部系统页面重新点一次书签 → 弹说明 |
| 本地 `dashboard.html` / `127.0.0.1:8000`，服务在跑 | 探测 `/healthz` 通 | POST `/api/refresh-all` → 轮询进度 → 自动重载 |
| 本地 `dashboard.html`，服务没起 | 探测失败 + `file://` | 弹「本机服务没在跑」，指 `启动看板.bat` |
| 线上托管版（输口令进的那个） | 探测失败 + `http(s)` | 弹「数据来源是云端定时任务」，可「检查有没有新数据」 |

本机抓数那条路的具体动作：

```
点按钮 → POST http://127.0.0.1:8000/api/refresh-all   （**不用输口令**）
        → 服务端起后台线程跑 refresh_all.py（抓数 → 三张清单 → 重写 dashboard.html）
        → 前端每 2 秒轮询 /api/refresh-status 显示「抓取中 32s…」
        → 成功后自动 location.reload()，页面就是新数据
```

线上托管版那条路，「检查有没有新数据」是 `HEAD data.bin` 比对 `ETag` / `Last-Modified`：
跟当前页面加载的那份一致，就告诉你「云端每 2 小时抓一次，下次约 12:23」；
不一致才重新加载（口令存在 `sessionStorage` 里，不用再输一遍）。

两个前提：

1. **本机 serve.py 得在跑**（`python serve.py --port 8000 --no-fetch --token 你的口令`）。
   没跑时点按钮会弹提示并给出这行命令，不会静默失败。
2. **仅本机可触发**：服务端校验来源 IP 是回环、没有 `CF-Connecting-IP` / `X-Forwarded-For`
   这类反代头、`Origin` 是本机（或 `file://` 的 `null`）。通过隧道或内网访问的人点了会收到 403。

**刷新不需要口令**：口令是给「看」用的（页面访问 / 登录），刷新是给「坐在本机的人」用的。
门禁完全靠上面那条来源校验 —— 外人拿不到 127.0.0.1 回环 + 干净头这个组合，
所以不必再让他输一遍口令，本机点一下就开始抓。顺带也挡掉了「浏览器里开着的外部网页
偷偷 POST 过来」这种 CSRF（它的 Origin 不是本机，会被 403）。

服务不在 8000 端口时，用 `dashboard.html?api=http://127.0.0.1:9000` 打开即可
（这个参数三处入口通用，线上版也能用它指向自己机器上的服务）。

**点单号看轨迹（右侧抽屉）**：抽屉里显示的是**接口原始事件**，字段原样、不换算、不合并：

| 情况 | 抽屉里是什么 |
|---|---|
| 本机 serve.py 在跑 | **接口实时返回的原始事件**（`/api/track` 的 `raw` 字段，含 26 个原始字段） |
| 没起服务 / 换台机器打开 | **构建时嵌进 HTML 的那份原始事件**（来自最新的 `data/_ev_cache_*.json`，快照时间写在小黄条里） |
| 该单不在缓存里（极少） | 才退回里程碑视图（用归一化字段拼的，会明确标注「仅供参考」） |

每条事件显示：`oprTimeWithZone` 原串（如 `2026-11-05T23:02:34+04:00`）+ 北京换算、
`jvEventCode` 与 `jvEventCnRemark`、`isEventCode`、`vendorCode / vendorName / vendorEventCode`、
`oprCnDes`、`oprUser / oprDistrict / oprCountry`；展开「原始 JSON」能看到这条事件的全部字段。

这样 `file://` 双击也能看到真正的中转扫描明细，不再只给 6 个里程碑节点。

内嵌范围默认是**最近 7 天建单的单 + 所有还没妥投的真实单**（`OC_RAW_DAYS` 可调，0 = 全量）：
这样 `dashboard.html` 约 1.5MB（不裁是 2.5MB），而正在盯的待提取 / 异常单无论建单多早都有原始事件。
被裁掉的单点开时会明确说明「建单早于内嵌窗口」，而不是悄悄退回里程碑。

`/api/track` 和刷新按钮用同一套门禁：本机回环 + 无反代头 + 本机/`null` Origin 才免口令，
隧道与内网访问仍然要口令。

和动态版（`serve.py`）的其余差别：

| | 静态 `dashboard.html` | 动态 `serve.py` |
|---|---|---|
| 单号追踪抽屉 | 有：原始事件（本机有服务→实时拉；没有→内嵌快照） | 有（原始事件，实时拉 OC） |
| 刷新数据 | 三分支统一逻辑（书签 / 本机 / 云端，见上） | 页面内直接调 |
| 更新 Cookie | 无 | 有 |
| 清单入口 | 指向 `data/*.html` | 指向 `/dropoff-cancel`、`/stuck` 路由 |

数据、KPI 六卡、图表、明细表的计算逻辑两份完全一样，同一个 `render()` 出的。

## 内网动态看板（给同事 trace 用）

静态版只能看已抓下来的那批；要让同事按运单号自己查，跑服务版：

```bash
python serve.py                      # 0.0.0.0:8000，同事访问 http://<你的内网IP>:8000
python serve.py --port 9000 --open   # 换端口并自动开浏览器
python serve.py --no-fetch           # 只用磁盘快照，不自动抓
```

启动后终端会打印可直接发给同事的地址。页面能力：

- **点明细表任意一行** → 右侧抽屉拉取该运单的**实时全链路轨迹**：每条事件的事件码、时间、描述、
  vendor，以及距建单的累计小时数；`DFDLUS002`（出库）与 `SCAUS002`（SPX）在时间轴上高亮
- 抽屉顶部给出四段时效（创建→A-SCAN、A-SCAN→出库、出库→SPX、A-SCAN→SPX）
- **刷新数据** 按钮：重新抓一轮（约 3–5 分钟），完成后自动重载
- **更新 Cookie** 按钮：会话过期时在页面里粘贴新的 `hk-sit` 即可恢复，不用改文件重启

### 访问口令（只读模式）

在 `.env` 里设 `OC_DASHBOARD_TOKEN=你的口令`（或启动时 `--token xxx`），服务立即切换为只读模式：

- 未登录访问任何页面 → 跳 `/login`；未登录调任何 `/api/*` → `401`
- 登录页可「显示 / 隐藏」口令，避免粘贴出错；登录后 cookie 有效 12 小时（HttpOnly + SameSite=Lax），`/logout` 清除
- **刷新数据** 与 **更新 Cookie** 按钮隐藏，对应接口返回 `403`
- 同一 IP 连续输错 5 次锁 5 分钟
- 不设口令则回到原来的无鉴权模式（只建议在纯内网用）

**支持多个口令**，逗号分隔，一人一个；谁离职或泄露就删掉那一个再重启：

```
OC_DASHBOARD_TOKEN=key-zhang,key-li,key-wang
```

#### 外发口令（脱敏视图）

看板里有 195 张真实订单的**收件人真名**，`/api/orders` 还会返回 `consigneeAddr`（完整门牌，
如 `200 Bethel Loop, Apt 17A`）。把链接发给公司同事时，用**外发口令**：

```
OC_DASHBOARD_TOKEN_EXT=team-a,team-b      # 或启动时 --token-ext xxx
```

| | 内部口令 `--token` | 外发口令 `--token-ext` |
|---|---|---|
| 页面 | 全量，收件人真名 | 顶栏挂「外发视图 · 收件人已脱敏」徽标，收件人显示 `E***A M***K` |
| `/api/orders` | 全部字段 | `consigneeAddr` **整体剔除**，`consignee` 打码 |
| `/api/track` | 事件详情 + 真名 | 事件详情照旧，`consignee` 与测试单说明里的姓名打码 |
| 刷新数据 / 更新 Cookie | 关（只读） | 关（只读） |

脱敏在**服务端注入数据前**完成，不是靠 CSS 遮挡 —— 前端拿到的 JSON 里就没有明文。
两组口令互不影响：撤销同事的只删 `OC_DASHBOARD_TOKEN_EXT` 那一行再重启，
你自己的 `OC_DASHBOARD_TOKEN` 照用，已登录的 cookie 也不会失效（cookie 是 `HMAC(口令)`，与进程无关）。

### 对外访问（公网）——已停用，仅存档

> **2026-11-13：用户明确不再需要公网地址，公网隧道已下线且不再维护。**
> 不要为它起 `publish.py` / `tunnel_keeper.py`，也不要再发 trycloudflare 链接。
> 日常就是本机 `serve.py` + 双击 `dashboard.html`；将来对外走 K8S Ingress（见文末）。
> 下面这段仅作历史参考。

用 Cloudflare 快速隧道，不用注册账号、不用改路由：

```bash
python publish.py                 # 起服务 + 隧道，打印公网地址
python publish.py --token a,b     # 临时换口令
python publish.py --no-tunnel     # 只起本地服务
```

`publish.py` 做三件事：起 `serve.py`（绑 `0.0.0.0`）、起 `bin/cloudflared.exe`、
从隧道日志抓出 `https://xxx.trycloudflare.com` 打印出来。Ctrl+C 一起关。

- **地址是临时的**：每次重启换新域名，得重新发链接
- 快速隧道不承诺可用性，适合内部临时分发，不适合当正式系统
- 要固定域名得用 named tunnel（需 Cloudflare 账号 + 自己的域名）
- 出站只需 TCP 443：脚本已强制 `--protocol http2`（这台机器 UDP 7844 不通，QUIC 必失败）
- 隧道一通，**口令就是唯一的门**。链接和 Key 别发在同一个群里

### 常驻保活 + 数据守护（tunnel_keeper.py）

`publish.py` 只管把服务推出去就退出；要**一直在线**用 `tunnel_keeper.py`：

```bash
python tunnel_keeper.py            # 常驻：服务 + 隧道 + cookie/数据守护
python tunnel_keeper.py --once     # 只体检一次（不弹登录窗、不刷数据）
python tunnel_keeper.py --no-data  # 只管通道
python tunnel_keeper.py --data-once  # 只看 cookie 与数据年龄
```

它每 20 秒一轮心跳，做四件事：

1. 本地 `serve.py` 不在就跑起来（`--no-fetch`，不自己抓数）；
2. `cloudflared` 不在就起；判死**只认** cloudflared 自己的 `/ready`，连续 15 次（约 5 分钟）
   不就绪才重建（重建=换域名，能等就别杀）；
3. 当前域名写进 `logs/public_url.txt`，历史在 `logs/public_url_history.log`；
4. **数据守护**（默认开，`OC_KEEPER_DATA=0` 关）：
   - 启动第一轮立刻验一次 `hk-sit`（`login_browser.verify_cookie`，固定 US 主机）；
   - 每 `OC_COOKIE_TICK`（默认 1800 秒）验一次；
   - **失效** → 自动弹浏览器续期，人只输 4 位验证码；成功后再刷一轮数据；
     失败则 `OC_LOGIN_COOLDOWN`（默认 30 分钟）内不重复弹窗，免得没人值守时窗口刷屏；
   - **有效但数据旧于** `OC_REFRESH_MAX_AGE`（默认 3600 秒）→ 异步跑 `refresh_all.py`；
     已有别的 `refresh_all.py` 在跑就跳过，避免两份刷新互相覆盖。

子进程都是异步的（日志 `logs/data.log`），不阻塞隧道心跳；单次刷新超过
`OC_REFRESH_TIMEOUT`（20 分钟）、登录窗超过 `OC_LOGIN_TIMEOUT`（15 分钟）就终止。
最近一次体检结果写在 `logs/data_status.json`（`cookieOk` / `action` / `dataAgeMin`）。

| 路由 | 说明 |
|---|---|
| `GET /` | 看板页面（动态模式，含追踪抽屉） |
| `GET /healthz` | 探针专用，免鉴权：`{"ok":true,"orders":476,...}`，无数据返回 503 |
| `GET /api/track?tn=<运单号>&region=US` | 单个运单的完整轨迹 + 各段时长 |
| `GET /api/orders?region=US` | 当前缓存明细 |
| `GET /api/status` | 会话有效性、缓存时间、是否在抓取 |
| `GET /stuck` | SHIPCCUS001 未完成清单页（见下节） |
| `GET /api/stuck` | 该清单的 JSON |
| `GET /dropoff-cancel` | drop off 取消异常清单页（已投递却被取消） |
| `GET /api/dropoff-cancel` | 该清单的 JSON |
| `POST /api/refresh` | 强制重抓 |
| `POST /api/refresh-all` | 一键全量刷新（抓数 + 三张清单 + 重写 `dashboard.html`），**仅本机回环**可调 |
| `POST /api/refresh-status` | 上面那个任务的进度（`running` / `elapsed` / `ok` / `tail`） |
| `POST /api/cookie` | 更新 Cookie（body: `{"cookie":"hk-sit=..."}`，会先校验再落盘） |

行为细节：
- 启动优先读 `data/orders.json` 快照，页面秒开；随后按需后台抓取
- 缓存 TTL 默认 600s（`OC_CACHE_TTL` 可调）；`/api/track` 每次实时拉，不吃缓存
- 缓存里查不到的运单也能 trace（只靠事件流现算），但「创建→A-SCAN」段需要订单头时间，此时留空
- **不配口令就没有任何鉴权**，此时只能绑内网；要对外必须先设 `OC_DASHBOARD_TOKEN`

## 公网加密看板（GitHub Pages）

给外部同事看，但又不能把客户信息裸奔。做法是**模板公开、数据加密**：

```
build_site.py  →  site/index.html   （0.11 MB，纯模板 + 锁屏，不含任何订单数据）
                  site/data.bin     （约 210 KB，gzip 后再 AES-256-GCM）
```

浏览器打开先看到口令框，输对才解密出数据并渲染。口令错就只得到「口令不正确」，
拿到的 `data.bin` 在没有口令的情况下是密文。

### 为什么不整页加密
整页加密的密文不可压缩：3.8 MB 的 `dashboard.html` 会**原样下载**。拆开之后数据是
「先 gzip 再加密」，实测 210 KB；模板还能被浏览器缓存，改模板不必重下数据。

### 构建

```bash
python build_site.py                    # 口令取 .env 的 OC_SITE_PASSWORD
python build_site.py --password 临时口令  # 临时换一个
python build_site.py --out dist         # 换输出目录
```

加密参数：`PBKDF2-SHA256` 200,000 次派生 → `AES-256-GCM`，AAD 固定为 `OCDB1`。
文件头 `OCDB1 | iter(4B) | salt(16B) | iv(12B) | ciphertext`。改口令重跑即生效，旧口令立刻失效。

### 自动更新（GitHub Actions）

`.github/workflows/refresh.yml`：每 2 小时抓一次数 → 验产物 → 发布 Pages；也能手动触发。

一次性配置：

1. 建仓库并 push（公开仓实测可用：`site/` 里只有模板与密文，`.env`、`data/`、`site/` 都不入库）
2. `Settings → Secrets and variables → Actions` 加 5 条：
   - `OC_COOKIE`：`.env` 里那一整行 `hk-sit=xxxx-...`（**要带 `hk-sit=` 前缀**，共 43 字符）
   - `OC_SITE_PASSWORD`：看板口令
   - `OC_HOST_US` / `OC_PORTAL` / `OC_SSO_URL`：接口地址、后台门户、登录入口
3. `Settings → Pages → Source` 选 **GitHub Actions**
4. `Actions → refresh-dashboard → Run workflow` 跑第一次

> Secret 值务必用 `gh secret set` 或 stdin 写入。GitHub 网页的值是多行输入框，
> 粘贴时很容易带进行尾空白，症状是 `Host '***' contains invalid character ' '`。

几点要知道：

- **cookie 约 4~5 天失效**。失效后 job 会在「先探一次接口」这步失败并给你发邮件，
  这时在本机跑 `python renew_token.py` 一条命令即可：先验当前 cookie（有效就跳过登录），
  失效才弹 Chrome（人只输 4 位验证码），然后自动写回 `.env`、推 GitHub Secret、
  触发部署、解密线上 `data.bin` 核对单量与生成时间。
  `--check` 只看状态、`--no-run` 只同步不部署、`--force` 强制重登。
  纯静态托管没法在前端续 cookie——续期最后一步是人工输 4 位验证码，只能在你本机做。
- 工作流不往仓库里提交数据，只上传 Pages artifact，所以仓库不会随时间膨胀。
- 免费额度：私有仓库 2000 分钟/月，每次完整刷新约 15 分钟 × 12 次/天 ≈ 超额度；
  **公开仓库的 Actions 不计分钟**，所以想保持 2 小时频率就把仓库设为公开（内容已经是密文）。

## 本地取数（书签，token 不进服务器）

上面那套是**中央抓数**：cookie 存在 GitHub Secret 里，到期要人换。
这套是**本地取数**：使用方在自己已登录的内部页面上点一下书签，用**浏览器自己的登录态**取数，
token 既不进服务器也不需要维护。

### 为什么可行（实测，不是推测）

| 探测项 | 结果 |
|---|---|
| 从内部系统页面（`oc-uk`）里 `fetch` 内部接口 | `success=true`，CORS 放行，带 `allow-credentials: true` |
| 换 `evil.example.com` / `null`（`file://` 的 origin） | **403**，无 CORS 头 |
| `hk-sit` 能否被页面 JS 读到 | **不能**（HttpOnly），但浏览器发请求时自动带上 |
| 内部页面 CSP | 无（只有 HSTS），脚本不会被拦 |
| jsdelivr 可达性 / Pyodide 启动 | 3.4 MB/s，加载运行时 5.4 秒 |

也就是说：跨域白名单是按域名配的，只有跑在内部系统页面里的脚本能读数据；而这个脚本
**碰不到 token**（HttpOnly），token 也不会离开浏览器。

### 四步使用

1. 打开 `https://<站点>/install.html`，填接口地址（`…-api.` 开头那个域名）
2. 把「取当期订单」按钮**拖到书签栏**
3. 登录内部系统 → 进**订单综合查询**页面（必须是 `oc-` 开头那个系统，别的域不在白名单里）
4. 点书签，抓完自动弹出看板窗口

接口地址也可以不填：脚本会依次尝试 `localStorage` 里上次用过的地址、页面网络记录里的
接口请求（即使用者先点过一次「搜 索」的情况）。

### 口径为什么不漂移

浏览器里跑 Python（Pyodide），**直接 import 现有的 `scraper.py` 与三个异常脚本**，
调用 `build_meta()` / `analyze()`，一行业务逻辑都没有重写：

```
live/agent.js    在内部页面里抓 list + events，postMessage 交给看板窗口
live/live.js     看板窗口：握手 → 载入 Pyodide → pipeline.compute() → 渲染
live/pipeline.py 把网络层换成内存里已抓到的数据，其余原样调用
```

回归测试（`test_live_pipeline.py`）用同一份真实数据分别跑「本地 collect」和
「实时 payload」，逐字段比对：**156 单全字段一致**，零漂移。

### 实测速度

2 天窗口 / 558 单：抓数 45 秒（列表并发 8、事件并发 12），本地计算 4.2 秒。
17 天窗口约 1100 单，抓数 4–6 分钟，计算 8–10 秒。

### 与中央抓数的取舍

| | 中央抓数（Actions） | 本地取数（书签） |
|---|---|---|
| token | GitHub Secret | 不存在（HttpOnly，浏览器自持） |
| 数据落地 | Pages 上的密文 | 使用方浏览器内存 |
| 更新 | 每 2 小时自动 | 每次手动点一下 |
| 失效 | cookie 约 4 天要换 | 只要还能登录内部系统就一直能用 |

两套并存，互不影响：`/` 是加密成品（给看不了内网的人），`/?live=1` 是实时模式（给内部同事）。

## 私有 K8S 部署

`trycloudflare` 是临时方案。要长期跑、要固定域名、要进内网，用这套清单。

**这套方案为什么能让地址"永久存活"**：访问地址来自 Ingress 的 `host` 字段，
它是集群里的一条声明式配置，和任何进程无关。所以 Pod 重启、节点漂移、
换镜像版本、本机关机、隧道进程被杀 —— 统统不影响这个地址。

对比一下两种通道的存活条件：

| | 快速隧道（trycloudflare） | K8S Ingress |
|---|---|---|
| 地址来源 | 每次启动随机分配 | 集群配置里写死的域名 |
| 进程死了 | 域名**永久**回收，无法找回 | 无影响 |
| 电脑关机/重启 | 换一个新地址 | 无影响 |
| 谁在跑 | 这台 Windows 电脑 | 集群 |

**架构**：一个 Deployment 提供只读服务 + 一个 CronJob 定时抓数，两者共用一块 PVC。

```
CronJob（每 30 分钟）                   Deployment（常驻 1 副本）
  refresh_all.py                          serve.py --no-fetch
   ├─ 抓订单 → data/orders.json  ──┐        └─ 读 data/*.json / stuck_shipcc.html
   └─ 重算取消清单 → data/*.json ──┤              ↑
                                    └── PVC（RWO, 1Gi）──┘
                                              ↑
                                    Ingress → 域名 + 口令
```

服务端靠**文件 mtime 变化自动热重载**，CronJob 写完下一轮请求就是新数据，不用重启 Pod。

### 1. 构建镜像

```bash
docker build -t harbor.example.com/infra/oc-timeliness:1.0.0 .
docker push harbor.example.com/infra/oc-timeliness:1.0.0
```

镜像基于 `python:3.13-slim`，依赖只有 `requests`。拉不到 Docker Hub 就把 `Dockerfile`
第一行换成内网源（`harbor.xxx/library/python:3.13-slim` 或阿里云镜像）。容器以 uid `10001`
非 root 运行，根文件系统只读。

### 2. 先建 Secret

```bash
cp k8s/secret.example.yaml k8s/secret.yaml     # 填 OC_COOKIE 与 OC_DASHBOARD_TOKEN
kubectl apply -f k8s/secret.yaml
```

或者不落文件：

```bash
kubectl -n oc-timeliness create secret generic oc-timeliness-secret \
  --from-literal=OC_COOKIE='hk-sit=xxxx; language=en-US' \
  --from-literal=OC_DASHBOARD_TOKEN='key-a,key-b' \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 3. 填参数并渲染清单

清单里只有镜像地址和对外域名是随环境变的，抽到 `k8s/deploy.env` 里，不用手改 240 行 YAML：

```bash
cp k8s/deploy.env.example k8s/deploy.env
vi k8s/deploy.env          # 至少填 IMAGE 和 HOST
python k8s/render.py       # 生成 k8s/rendered/oc-timeliness.yaml
```

`deploy.env` 的字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `IMAGE` | 是 | 第 1 步推上去的完整镜像地址（含 tag） |
| `HOST` | 是 | **这就是以后的固定访问地址**，如 `oc.corp.local`；带 `https://` 或结尾 `/` 会自动去掉 |
| `INGRESS_CLASS` | 否 | 默认 `nginx`，按集群实际改（traefik / higress / apisix） |
| `STORAGE_CLASS` | 否 | 留空走集群默认；多节点建议填 RWX 存储类 |
| `TLS_SECRET` | 否 | 已有证书 secret 就填名字；留空 = 不加 tls 段（纯 HTTP） |
| `CLUSTER_ISSUER` | 否 | 装了 cert-manager 就填 issuer，自动签发并续期 |

`render.py` 会做三件事：替换占位符（Deployment 与 CronJob 两份镜像一起换）、
空值删行（K8S 里 `storageClassName: ""` 不等于"走默认"）、用 PyYAML 回读校验
七类资源齐全且两份镜像一致。填了示例域名会直接报错退出，防止误 apply。

### 4. 部署

```bash
kubectl apply -f k8s/rendered/oc-timeliness.yaml   # 命名空间 / 配置 / PVC / 服务 / 定时任务 / Ingress
kubectl -n oc-timeliness get pods -w               # Deployment 会先 Running 但 NotReady（没数据）
kubectl -n oc-timeliness create job --from=cronjob/oc-timeliness-refresh bootstrap   # 手动跑首轮
kubectl -n oc-timeliness logs -f job/bootstrap
```

首次必须先跑一轮抓数，否则 Pod 一直是 `NotReady`（readiness 探针要求内存里有快照）。

### 5. 验证

```bash
kubectl -n oc-timeliness port-forward svc/oc-timeliness 8080:80
curl -s localhost:8080/healthz          # {"ok":true,"orders":476,...}
```

`/healthz` 免鉴权、只暴露单量与数据时间，专门给探针用。浏览器打开要过口令页。

### 6. 之后维护

| 场景 | 做法 |
|---|---|
| Cookie 过期 | 改 Secret 后 `apply`，CronJob 下一轮自动用新值；服务不用重启 |
| 换访问口令 | 同上。口令变了旧 cookie 立即失效，用户重登 |
| 改统计起点 | 改 ConfigMap 的 `OC_START_DATE`，等下一轮抓数 |
| 立即刷新一次 | `kubectl -n oc-timeliness create job --from=cronjob/oc-timeliness-refresh manual-$(date +%s)` |
| 看抓数日志 | `kubectl -n oc-timeliness logs job/<name>` |

`refresh_all.py` 有三道保护：没 Cookie 直接退出、**抓到 0 单不覆盖旧快照**、
订单抓成功才重算取消清单。Cookie 失效时 CronJob 会 Failed 但线上数据继续可用，
建议给这个 CronJob 配一条失败告警。

### 7. 集群只有内网时，怎么给外网入口

先分清两件事：

- **内网地址**：上面的 Ingress `HOST` 就是。连上 VPN 就能开，Pod 怎么重启都不变。
- **外网地址**：集群 Ingress 只在内网解析，外网必须另开一条出网隧道。

外网入口用 `k8s/cloudflared-tunnel.yaml`（可选组件，不用就别 apply）：

```bash
# 1. Cloudflare → Zero Trust → Networks → Tunnels → 新建 tunnel，拿到 token
# 2. 在该 tunnel 加 Public Hostname：
#      oc.你的域名  →  http://oc-timeliness.oc-timeliness.svc.cluster.local:80
# 3. 填 token 并部署
vi k8s/cloudflared-tunnel.yaml            # 换掉 TUNNEL_TOKEN
kubectl apply -f k8s/cloudflared-tunnel.yaml
kubectl -n oc-timeliness logs -f deploy/cloudflared   # 出现 Registered tunnel connection 即成功
```

这条通道**地址永久固定**的原因和快速隧道完全不同：域名和 DNS 记录由 Cloudflare 托管，
cloudflared 只是个连出去的客户端。Pod 重启、换节点、换镜像，都用同一个域名重连。
（快速隧道是域名跟着进程走，进程一死域名就永久回收。）

需要自有域名（NS 托管在 Cloudflare，.com 约 ¥70/年）。不想买域名就别开外网，
或改用 Tailscale Funnel（`*.ts.net` 固定子域，免费但要装客户端）。

安全建议：外网这条建议只开给特定人 —— Cloudflare Access 里加一条 Email/SSO 策略，
比只靠看板口令更稳。看板本身的口令（`OC_DASHBOARD_TOKEN`）继续保留。

### 几点限制

- **只能 1 副本**：内存里有快照缓存，PVC 是 `ReadWriteOnce`，`strategy: Recreate`
- **Deployment 与 CronJob 必须同节点**：RWO 卷不能同时挂两个节点。多节点集群换成
  RWX 存储（NFS / CephFS / NAS），或把抓数改成服务内置定时器（`serve.py` 不设只读时可用）
- **必须能出网**：容器要访问 `<US-API-HOST>` / `<UK-API-HOST>`:443，
  私有集群记得配出口或 NAT
- **接口有跨窗口限制**：`startDate` 跨度过大时后端会返回全库数（`total≈25000`），
  抓取会变慢。当前默认从 `OC_START_DATE` 起，跨度建议不超 14 天

## SHIPCCUS001 未完成清单

`SHIPCCUS001` 是 LMS 上报的「面单过期 / 订单取消」节点，后台文案有三种：
`shipment.expired`、`shipment.cancelled`、`Label expired, never dropped off`。

```bash
python stuck_shipcc.py --region us --days 14   # 抓取 + 筛 + 出页
python stuck_shipcc.py --render-only           # 只改页面样式时用，不重抓
```

产物：`stuck_shipcc.html`、`data/stuck_shipcc.json` / `.csv`，
以及「取消已恢复」留档 `data/stuck_recovered.json` / `.csv`。

### 口径：18 单的 2×2 拆法（页面顶部矩阵就是这张表）

第一层按**测试 / 生产**分，第二层按**取消之后有没有新节点**（已更新 / 没更新）分：

| | 已更新<br><sub>取消后又有新节点</sub> | 没更新<br><sub>取消后再没动过</sub> | 小计 |
|---|---:|---:|---:|
| 测试单 | 0 | 5 | **5** |
| 生产单 | 3 | **10** ← 要跟进 | **13** |
| 合计 | **3** | **15** | **18** |

- **测试 5 单全部「没更新」**，0 单更新 —— 测试数据，不用管；
- **生产 13 单 = 已更新 3 + 没更新 10**；
- 唯一行动项：**生产 × 没更新 = 10 单**。

> 早前页面上把 15、5、10、3、10 五个数字平铺成五张卡，其中两个 10 含义不同（B 组单数 / 剔除测试后单数，凑巧相等），
> 层级读不出来，已废弃；现改为矩阵，每个格子就是一个可点击的筛选。

### 判定口径（业务确认，唯一标准）

**命中 `SHIPCCUS001`，且按时间排序后「取消节点之后再没有时间更晚的新节点」→ 没更新。**

反过来：只要取消之后还存在**一条时间更晚的节点**，就算**已更新**，不进要跟进清单、单独留档。

关键点：判据是**时间先后**，不是「事件链里有没有出现过 A-SCAN」。
取消发生在 A-SCAN 之后、之后又零动作的单，**依然算没更新** —— 这正是之前分组的错处：
旧逻辑把「有 A-SCAN」直接当成「已更新」，把 10 单已经收寄后被撤销、实际卡住的单
误标成了"货已发出"。

同一单号可能有多次 `SHIPCCUS001`（重复上报），一律以**最后一次**取消为基准比较。

### A / B 分组（只作归因，和上面的 2×2 是两个维度）

明细里的「分组」列按「取消**之前**是否已交寄」拆两组，**不改变上面的判定**：

| 组 | 条件 | 含义 |
|---|---|---|
| A · 从未交寄 | 取消前只有建单 / 信息收到，无 A-SCAN、无 `DFDLUS002`、无 `SCAUS002` | 面单做了又撤，包裹从未进入作业 |
| B · 交寄后被撤销 | 取消前已有 A-SCAN / `DFDLUS002` / `SCAUS002` | 货已经收进来了又被撤单，之后彻底不动 |

B 组典型形态是「A-SCAN 后 4–12 分钟被 LMS 撤单」，是真正需要跟进的一批。

**当前窗口里两组恰好各自对齐一类**：5 个 A 组全是测试单（= ①），10 个 B 组全是真实单（= ②）。
这是本批数据凑巧，逻辑上 A 组也可能是真实单（真实单从未交寄就被撤），届时 2×2 与 A/B 会分叉，不要混用。

### 页面结构

顶部矩阵的每个数字都可点，点了就是一次筛选：

| 点哪里 | 得到 | tab 高亮 |
|---|---|---|
| 测试单 · 已更新 | 0 单 | — |
| 测试单 · 没更新 | 5 单 | `测试单` |
| 生产单 · 已更新 | 3 单 | — |
| **生产单 · 没更新** | **10 单** | `要跟进` |
| 小计 / 合计 | 该行或全部 | `测试单` / `生产单` / `全部` |

tab 栏固定 4 个：`全部 18 / 测试单 5 / 生产单 13 / 要跟进 10`，与矩阵对应。

「已更新」的单在明细里列不同（专门看「取消 → 恢复」的过程）：

`运单号 / 取消时间 / 恢复节点时间 / 恢复首节点 / 取消→恢复(h) / 后续节点数 / 后续链路 / 州 / 收件人`

点行展开能看到判定说明和完整后续链路。全部视图里这类单带 `取消已恢复` 的绿色标签，排在没更新的单之后。

### 注意：同号重发会污染判定

再发时如果**复用同一个单号**（实测 `USPDPPS1000057787N`：11-03 06:41 取消 →
07:31 以同号重建面单 → 09:31 投递），取消节点会永久留在事件链开头。
任何形式的「事件流里出现过 `SHIPCCUS001` 就算未恢复」都会误杀这类单。
必须用「取消是否为排序后最后一个节点」来筛。

## drop off 取消异常清单

`SHIPCCUS001` 清单只统计「取消后没再动过」，**不区分包裹是不是已经交出去了**。
但运营上更痛的是另一类：**货已经交到点位/门店了，系统却把这一单取消了** ——
货在，人在系统里却查不到，要人工决定取回、补录还是重发。

```bash
python dropoff_cancel.py --region us --days 14     # 抓取 + 判定 + 出页
python dropoff_cancel.py --cache data/_ev_cache_xxx.json   # 复用事件缓存
python dropoff_cancel.py --render-only              # 只改页面样式时用，不重抓
```

产物：`data/dropoff_cancel.html`（独立清单页）、`data/dropoff_cancel.json` / `.csv`。
看板右上角有红色的「**drop off 取消异常（N）**」按钮直达该页（N = 要跟进的单数），
明细表里命中的单挂红色「取消异常」标记；`refresh_all.py` 已接进定时流程。

**判定口径**（同一单同时满足「有投递事件」和「有取消事件」才进清单）：

| 档位 | 判据 | 要不要跟进 |
|---|---|---|
| **时序倒挂** | 取消时间 **早于** 投递时间 | ✅ 最优先：面单已失效，客户却还是把包裹投了 |
| **取消后无后续** | 投递之后被取消，且取消之后**没有任何更晚的节点** | ✅ 货留在点位上 |
| **取消后已恢复** | 投递之后被取消，但取消之后**还有更晚的节点** | 留档，不算问题单 |

测试单（收件人占位）单独归类，不计入「要跟进」。

实测（10/20–11/03，US 333 单，其中 97 单有投递事件）：

| 档位 | 单数 | 说明 |
|---|---|---|
| 时序倒挂 | 1 | `USPDPPS1000057787N`（同号重建面单后又投递） |
| 取消后无后续 | 16 | 其中 4 单是测试单 |
| 取消后已恢复 | 3 | 其中 1 单是测试单 |

**要跟进（生产 · 未恢复）13 单**，全部是 PUDO INC 投递、**LMS 上报取消**
（`Shipment cancelled`），且取消都发生在投递之后 **4~12 分钟**、取消后无任何节点 ——
像是 PUDO 点或后台在投递后不久做的一次批量取消，建议找业务确认这批单的来龙去脉。

## 看板内容

- **顶部 tab**：`UK | All | US`，默认 US；每切换一次 KPI / 图表 / 明细全部重算
- **时区 tab（北京 | 纽约）**：所有「按日」的东西都跟着它走 —— 每日单量、每日 drop off 的日期分桶、
  点位卡的日期下拉、明细表的「按建单日/投递日/妥投日」筛选、明细里的时间列、抽屉轨迹时间轴。
  选择记在 `localStorage.oc_tz`，默认北京。
  数据落盘时所有 `*Time` 都是**北京时间墙钟**（`scraper` 用 `astimezone(TZ_CN)`），换纽约走
  `Intl.DateTimeFormat` + 时区库，**不能固定减 13 小时** —— 10/29–10/31 纽约还是 EDT，只差 12 小时，
  11/01 起才是 EST（-13h）。时长类指标（各段小时数、`hXxx` 字段）是绝对时刻之差，不受时区影响。
- **KPI**（3 列 × 2 行）：订单总数 / 面单生成（`OCRETURNLBDE002`）/ 妥投（`DELUS006`·`DEL005`）｜ RDC 出库 → SPX 提取 / A-SCAN → RDC 出库 / SPX 交接率
- **每日单量**：按**下单日**统计、按**投递方（drop off）**堆叠 —— PUDO INC / LMS / 其他 / 未投递（灰）。
  分母是「这段时间下了多少单」，所以含还没投递的单
- **每日 drop off 量级**：按**投递发生日**（不是下单日）统计 `isEventCode=FIRST_SCAN`
  （US 即 `DOUS002`/`DOUD002`）的包裹数，同样按投递方堆叠；右侧面板给合计 / 日均（两种摊平口径）/
  峰值日 / 两家占比 / `LMS : PUDO` 比例。**范围同样是 10/29 起**，但卡的是投递日：
  分母是「这段时间有多少包裹真的交寄了」，没投递的单不出现。
  两卡可以互相校验：`152 单（10/29 后下单） = 77 已投递 + 75 未投递`，
  `78 单（10/29 后投递） = 77 + 1 单（10/23 下单、11/03 才投递）`
- **各段时效均值**：创建→A-SCAN、A-SCAN→出库、出库→SPX、A-SCAN→SPX、创建→最新节点
- **PUDO vs LMS**：同一口径下的出库→SPX 对比
- **出库→SPX 分布**：≤24h / 24–48h / 48–72h / >72h
- **明细表**：可排序、按 RDC / **来源（eBay·Depop）** / **投递方** / 状态筛选、单号/收件人搜索；出库→SPX 超 48h 标红；
  含「来源」「收件人」「面单生成」「**投递（drop off）**」「妥投」「投递方」列；投递方 ≠ 出库方标「异」，
  投递后被取消的单挂红色「**取消异常**」标记；有 `isEventCode = FIRST_SCAN` 投递事件的单，
  投递方后面带一枚灰蓝 **首扫** 徽标（悬停显示 isEventCode / 事件码 / 时间），没有徽标就是还没被扫到
- **日期筛选**：工具栏可选**按建单日 / 按投递日 / 按妥投日**再选具体日期，只为定位
  「某天投递了多少单、分别是哪几张」。以前只能按建单日排序，而投递一般比建单晚 1–3 天，
  所以拿投递量去明细里对日期永远对不上（不是单丢了）。也可以直接点「每日 drop off 量级」的柱条跳转
- **搜索范围**：单号 / 参考号 / 面单号 / **收件人** / 目的州 / 产品
- **顶部高亮按钮**：红色的「drop off 取消异常（N）」直达异常清单（N = 要跟进的单数）
- **排除测试单**：工具栏勾选框（默认勾选），切换后 KPI / 图表 / 明细全部按真实单重算

## Cookie 失效与自动续登录

`hk-sit` 大约只能撑一天（实测：11-04 10:00 刷新正常，11-05 09:55 已是 302）。失效后接口返回
`success=false, errorCode=302`，看板数据就停在最后一次成功的时间，看起来像"数据没更新"。

```bash
python login_browser.py --check     # 只查现在还有效没（不开浏览器）
python login_browser.py             # 需要时才弹浏览器续期
python login_browser.py --refresh   # 续期后顺便跑一轮 refresh_all.py
python login_browser.py --enter     # 用已保存登录态走一遍真实页面（核对人能用）
```

它按三步递进，能不开浏览器就不开：

1. 拿 `.env` 里的 `OC_COOKIE` 打一次列表接口 —— 有效就直接结束
2. 无效则读 `data/_auth_state.json`（上次保存的浏览器登录态，含 session cookie）—— 能恢复就写回 `.env`
3. 都不行才弹 Chrome：自动填好账号密码（`.env` 里有 `OC_USER` / `OC_PASS` 时），
   你只需看一眼 4 位验证码点「登 录」

**登录成功后会顺手走一遍真实路径**（这一步是核对，不是取数必需）：

```
门户 /main → 点「智慧物流管理系统」 → 订单综合查询 → 切 US → 点「搜 索」
```

走通了会打印 `✓ US 区查询正常，当前页 20 行`，说明**人和机器看到的是同一份数据**；
走不通只打印 `!`，不阻断（cookie 已经用接口验过）。急着直接拿 cookie 就加 `--no-enter`。

**为什么验证码不能自动过**：SSO 是 LDAP 账号 + 4 位图形验证码（另可选飞书扫码）。
本机试过三条路线都不行 ——
`ddddocr`（PyPI 没有 Python 3.13 可用的包）、Windows 自带 OCR（5 张全错，返回空或乱码）、
自建模板匹配（数字字体带扭曲，孔洞特征分不开 2/3/5/7）。所以保留"人看一眼验证码"这一步。

**关于账号密码：不写进 `.env`，改用 Chrome 自己记（2026-11-18 起）。**
`.env` 里没有 `OC_USER` / `OC_PASS`，是主动决定，不是漏配。理由：域账号密码一旦落盘就是长期
有效的明文凭证，而它换来的只是每次续期少输一次账号密码（约 20 秒）。

替代方案：`login_browser.py` 现在用 **`launch_persistent_context` + 固定配置目录 `.pw-profile/`**
打开 Chrome（以前是 `new_context`，一次性临时 profile，密码存不下来）。所以只要你在弹窗登录时
点一次 Chrome 的「保存密码」，之后账号密码就会自动填充，**人只需要输 4 位验证码**，密码也不进
`.env`。`.pw-profile/` 已在 `.gitignore` 里。

`login_browser.py` 仍支持 `OC_USER` / `OC_PASS` 两个变量（优先级高于 Chrome 自动填充），想用就用，
默认留空。

所以一次续期的完整动作是：**输 4 位验证码 → 点「登 录」**（首次或密码库被清空时还要输账号密码）。

登录态存在 `data/_auth_state.json`，能跨浏览器重启复用；`.env` 只存 `OC_COOKIE`。
K8S 那边没有浏览器，CronJob 用的是 Secret 里的 cookie，过期后 Job 会 Failed，
需要人工按上面的流程续一次再更新 Secret。

## 配置（`.env`）

```
# ---- 后台地址：必填，代码里没有默认值（不写死是为了仓库能公开分享）----
OC_HOST_US=https://...     # US 区域接口主机根地址
OC_HOST_UK=https://...     # UK 区域接口主机根地址（OC_REGION=us 时用不到）
OC_HOST_AU=https://...     # AU 区域接口主机根地址（用不到可留空）
OC_PORTAL=https://...      # 后台门户地址：Referer / Origin + login_browser 走查
OC_SSO_URL=https://...     # SSO 登录入口：login_browser 打开的那个页面
OC_DEPOP_TRACK=https://... # Depop 对外轨迹页域名；不填则明细表不显示 ↗ 外链

OC_REGION=us           # us / uk / au / all（当前固定 us，只看美国；all = 后台 All tab）
OC_COOKIE=hk-sit=...
OC_DAYS=14
OC_START_DATE=2026/10/19   # 抓数窗口起点 = 接口下限（10/18 及更早会被忽略并返回全库）
OC_KPI_START=2026/10/29    # 汇总口径起点：KPI 与所有图表只算这天起的单
OC_DETAIL_START=2026/10/19 # 明细口径起点：订单明细表能查到这天起的单
OC_SITE_PASSWORD=口令      # 加密部署版：build_site.py 用它加密 data.bin
# OC_USER=域账号      # 默认不启用，见上文「账号密码刻意不保存」
# OC_PASS=密码
```

### 为什么地址不进代码

`OC_HOST_*` / `OC_PORTAL` / `OC_SSO_URL` 在代码里**没有默认值**，全部运行时从 `.env`
（或 GitHub Secret）读。原因只有一个：仓库要能公开镜像，而内部系统地址一旦提交就会
进 git 历史和搜索引擎索引，删不干净。

代价是**少了配置会直接报错**，不是静默失败：

| 缺什么 | 表现 |
|---|---|
| `OC_HOST_US` | `SystemExit: 缺少接口主机：请在 .env 里设置 OC_HOST_US=...` |
| `OC_PORTAL` | 请求不带 `Referer`/`Origin`（一般不致命，但网关可能拒） |
| `OC_SSO_URL` | 续期时直接提示「缺少 OC_SSO_URL」，不会开空浏览器 |
| `OC_DEPOP_TRACK` | 明细表的 Depop `↗` 外链整体不渲染（不报错） |

抓数入口 `_url()` 在拼 URL 前会检查主机，绝不会拼出 `/aquarius-order/...` 这种相对路径去撞墙。

### 凭证相关文件

| 文件 | 内容 | 有效期 |
|---|---|---|
| `.env` | `OC_COOKIE`（会话票据）、`OC_DASHBOARD_TOKEN`（内部口令）、`OC_DASHBOARD_TOKEN_EXT`（外发口令） | 票据随登录态，约 24h |
| `data/_auth_state.json` | 浏览器登录态，4 个 cookie，含 `hk-sit` | 约 24h |

- **不含长期密码**：任何文件里都没有域账号密码。
- **镜像不带出**：`.dockerignore` 已排除 `.env`、`data/`，构建上下文里没有凭证。
- **不外泄到公网**：隧道只暴露看板 HTML；`/api/track`、`/api/refresh-all` 对本机以外来源
  分别返回 401 / 403，凭证不会随接口出去。
- **文件权限**：继承 `C:\Users\182251` 的 ACL，实际可读的是 `OC\182251`、`Administrators`、
  `SYSTEM` 三者，同机其他标准用户读不到，无需额外收紧。
- **入库风险**：项目当前不是 git 仓库；已放 `.gitignore` 屏蔽上述文件，若日后 `git init`
  不会误提交。

交接给别人部署时，传代码 + `.env.example`，**不要传 `.env` 和 `data/`**，对方用自己的账号登一次。

`OC_DEST_COUNTRY` 可强制指定目的国（留空则用各区域默认：US 区带 `US`，UK 区不筛）。
`OC_BASE_URL` 只用于单区域调试，`--region all` 时会忽略，避免两个 tab 打到同一台主机。
`OC_LABEL_EVENT` 可改面单生成的事件码，默认 `OCRETURNLBDE002`。
`OC_DELIVERED_EVENT` 可改妥投事件码，默认 `DELUS006,DEL005`（逗号分隔，两个都认）。

## 已知情况（口径：2026/10/19 起）

- **10/19–10/28 是压测期**，US 区几乎全是测试单（10/21 17/17、10/22 82/81、10/26 51/51、
  10/28 11/11），10/29 起恢复常态（每天 0–3 单测试）。所以看 10/19 起的量必须勾着「排除测试单」，
  否则会把压测流量当成真实单量。这 10 天里 234 单有 214 单是测试，真实单只有 20 张。
- **当前只看 US**（`OC_REGION=us`）。全窗口 10/19–11/05：后台 **398 单 = 测试 226 + 真实 172**。
- **两条日期口径**（刻意分开，需求是「总数/图表看 10/29 到现在，明细能看到 10 月」）：

  | 口径 | 配置 | 用在哪儿 | 当前单量 |
  |---|---|---|---|
  | 汇总 | `OC_KPI_START=2026/10/29` | 顶部 KPI、每日单量、时效分布、投递方占比 | 152 单（已排除 12 单测试） |
  | 明细 | `OC_DETAIL_START=2026/10/19` | 底部订单明细表（可搜索/筛选） | 172 单 |
  | 抓数 | `OC_START_DATE=2026/10/19` | 接口取数起点 = 接口硬下限 | 398 单 |

  明细起点本来是「10 月 1 日」，但接口只放最近约 17 天（10/18 及更早会静默返回全库 2.5 万单），
  所以 **实际最早只能到 10/19**，10/01–10/18 需要后台导出才能补。

  **「每日 drop off 量级」的时间范围同样是 10/29 起**，但分母与横轴不同：它按**投递发生日**
  归集、只统计真的投递了的包裹，所以和「每日单量」不是同一张图（详见上文「看板内容」）。
  10/29 之前投递的 11 单会在图例里单列「不计入」，不会被静默丢掉。

  页面副标题会同时写出这三段，不会出现「总数和明细对不上」的误会。
  注意：时效类 KPI（妥投、RDC→SPX）跟着汇总口径走，10/29 后下单的单还很新、链路没走完，
  所以这两项数字天然偏小，不是接口缺数据。
- 看板默认**勾选「排除测试单」**：KPI 会带 `真实单` 标签，副标题写明
  `后台 N 单，汇总自 10/29 起 M 单（已排除 K 单测试）`，避免把 M 误当成后台总数。
- **测试单判定规则**（`scraper.test_flags`，三级 + 同地址推定，全部要求「整词」命中）：
  1. **整体命中** —— 收件人归一化后整体为 `TEST / TEST01 / DUMMY / DEMO / SAMPLE / 测试 / 样例 / 演示`
     （可带数字后缀），或地址含 `Dummy` / `Test Addr`。
  2. **任一词命中** —— `Testing Liuz` / `Tester Liuz` / `testName` / `Test ToAdd` /
     `ALANA DRESNER PUDO TEST`（真名后面挂 `PUDO TEST` 后缀）。**任一词完整等于**测试词
     （可带数字后缀）即命中，所以 `Testa`、`TESTIMONIAL CORP`、`MyTest Inc` 依然不命中
     —— 用的是「整词」而不是子串，误杀面没扩大。
  3. **同地址推定** —— 同一区域、同一收货地址上已有 ≥2 单直接命中，则该地址所有单也标为测试。
     用于兜住未预见的变体（实测 200 Bethel Loop 5 单里有 1 单写成 `Test ToAdd`）。
     真实客户地址不会出现多单收件人叫 `Test`，所以这条不会扩大误杀面。

  命中结果落在 CSV 的 `isTest` 列，明细表「收件人」列可直接核对；
  清单页（`stuck_shipcc.py`）也在全量订单上算这个标记，避免只对命中清单的十几单做判定而丢掉上下文。
- **核心指标「出库→SPX」样本仍然极少**：US 真实单 98 单里只有 **1 单**同时具备 `DFDLUS002` 与
  `SCAUS002`（50.85h，LMS；PUDO INC 为 0 单）。原因是窗口太短，多数单还没走到取件：

  | 状态 | 单量 | 含义 |
  |---|---|---|
  | 待 A-SCAN | 55 | 尚未扫描 |
  | 已 A-SCAN | 28 | 已扫描，未出库 |
  | 已出库待 SPX | 13 | 已出库，等 SPX 取件 |
  | 已交 SPX | 2 | 完整链路 |

- **「已出库待 SPX」的 13 单更值得看**（替代指标：出库 → 现在已等多久）：10 单是 11/03 02:46–02:48
  批量出库（约 8.4h，正常在途），**3 单是 11/01 00:21 出库、已等 58.8h**（`USPDPPS8000057568N` CO、
  `USPDPPS0000057566N` FL、`USPDPPS2000057625N` CA），超 48h 需跟。全部为 LMS。
- 存在有 `SCAUS002` 但无 `DFDLUS002` 的单，说明没走 RDC 出库节点（直发或漏扫），
  这类单不计入「出库→SPX」均值。
- 个别单时间落在 2025 年，为历史脏数据，看板按时间倒挂剔除并计入 `meta.anomalies`（本窗口 42 单）。
