#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OrangeConnex / 递一物流 —— 美国（US）订单时效抓取
=================================================

口径（已用真实数据核对）
------------------------
  订单创建时间   orderCreatedTimeWithZone
  A-SCAN 时间    firstScanTime
  RDC 出库时间   JV Event Code == DFDLUS002
                 = “Package ready for dispatch”
                   描述形如 “At LMS facility and sorted into carrier bins” /
                           “Driver pickup from PUDOpoint” / “shipment.injected/made_ready”
                 → 货已从 PUDO INC / LMS 的 RDC 出库（标准 warehouse out 节点）
                   事件自带的 vendorName 区分是 PUDO 还是 LMS
  SPX 提取时间   JV Event Code == SCAUS002
                 = “Picked up by delivery carrier”（vendor = SPX）
  核心指标       RDC 出库 → SPX 提取 的耗时（小时）

接口（实测可用）
----------------
  BASE   = 环境变量 OC_HOST_US（各区域各一台主机，见下方 REGIONS / region_cfg）
  PREFIX = /aquarius-order
  GET {BASE}{PREFIX}/order/tracking-number/v1/tracking-number-list
      ?page=1&limit=100&startDate=2026/10/19 00:00:00&endDate=2026/11/03 23:59:59
  GET {BASE}{PREFIX}/order/tracking-number/v1/tracking-event-list/{trackingNumber}
  鉴权：Cookie hk-sit=...

时区口径（已实证，见 probe_time.py）
------------------------------------
  事件 oprTimeWithZone 恒为 +04:00（不是 UTC-4，就是 UTC+4），订单头恒为 +08:00。
  两者只是"渲染时区"不同：同一时刻在事件流写 05:31+04:00、在订单头写 09:31+08:00，
  都指向 01:31 UTC。按各自标注解析即可对齐，绝对不能把 +04:00 当 -04:00 算。

  oprTime 毫秒戳才是脏数据：同一个字符串 2026-11-03T03:30:20+04:00 会对应
  两个相差 6 小时的毫秒值（实测 7h/12h/13h/16h 四种漂移）。因此以
  oprTimeWithZone 为主，oprTime 仅作兜底。订单头同理优先 *Timestamp。

用法
----
  python scraper.py --real                近 14 天
  python scraper.py --real --from 2026/10/19 --to 2026/11/03
  python scraper.py --real --days 7 --max 30
  python scraper.py --probe               探一次接口
  python scraper.py --demo                内置示例数据（无网络时看效果）
"""

from __future__ import annotations

import argparse
import csv
import re
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

TZ_CN = timezone(timedelta(hours=8))


def _load_dotenv(path: Path):
    """读取同目录 .env（不覆盖已存在的环境变量），避免额外依赖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


_load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# 区域预设
# ---------------------------------------------------------------------------
# 后台右上角三个 tab：UK | All | US。
# 每个 tab 对应一台 API 主机（同一套接口、同一个 hk-sit cookie，但数据各自独立）。
# 接口主机**刻意不写在这里**：从 OC_HOST_<区域> 读取（见 region_cfg），
# 这样仓库里不含任何公司内部域名，公开镜像/PR 都不会泄露后台地址。
REGIONS = {
    "us": {"label": "US 美国", "host": "",
           "rdc": "DFDLUS002", "spx": "SCAUS002", "dest": "US"},
    "uk": {"label": "UK 英国", "host": "",
           "rdc": "DFDLUK002", "spx": "SCAUK002", "dest": ""},
    "au": {"label": "AU 澳洲", "host": "",
           "rdc": "DFDLAU002", "spx": "SCAAU002", "dest": ""},
}

REGION = (os.getenv("OC_REGION") or "us").lower()   # us / uk / au / all（all = UK + US）
PORTAL = (os.getenv("OC_PORTAL") or "").strip().rstrip("/")
API_PREFIX = os.getenv("OC_API_PREFIX", "/aquarius-order").rstrip("/")
COOKIE = os.getenv("OC_COOKIE", "")
DEST_OVERRIDE = (os.getenv("OC_DEST_COUNTRY") or "").strip()
SEND_COUNTRY = os.getenv("OC_SEND_COUNTRY", "")

API = {
    "list": "/order/tracking-number/v1/tracking-number-list",
    "events": "/order/tracking-number/v1/tracking-event-list/{tn}",
}

# 单次查询返回超过这个数就认定「日期条件被后台忽略、退化成全库」，直接报错。
# 正常 US 单月量级是几百单，全库约 2.5 万单，中间差两个数量级，阈值不敏感。
SANITY_MAX_ORDERS = 5000


def active_regions() -> list[str]:
    """要抓哪些区域。all = 后台 All tab（UK + US 合并）。"""
    if REGION == "all":
        return [k for k in ("uk", "us") if k in REGIONS]
    if REGION in REGIONS:
        return [REGION]
    print(f"[warn] 未知区域 {REGION}，回退 US")
    return ["us"]


def region_cfg(key: str) -> dict:
    cfg = dict(REGIONS[key])
    cfg["key"] = key
    cfg["code"] = key.upper()
    if DEST_OVERRIDE:
        cfg["dest"] = DEST_OVERRIDE
    # 接口主机从环境变量取：OC_HOST_<区域>（US/UK/AU）。
    # OC_BASE_URL 只是单区域调试用的临时覆盖，避免把两个 tab 打到同一台主机。
    host = ""
    if REGION != "all" and os.getenv("OC_BASE_URL"):
        host = os.environ["OC_BASE_URL"]
    else:
        host = os.getenv(f"OC_HOST_{key.upper()}") or ""
    cfg["host"] = host.strip().rstrip("/")
    cfg["rdc"] = os.getenv("OC_RDC_EVENT", cfg["rdc"])
    cfg["spx"] = os.getenv("OC_SPX_EVENT", cfg["spx"])
    return cfg


FIRST_CFG = region_cfg(active_regions()[0])
# 兼容旧引用的全局默认
BASE_URL = FIRST_CFG["host"]
DEST_COUNTRY = FIRST_CFG["dest"]
RDC_EVENT_CODE = FIRST_CFG["rdc"]
SPX_EVENT_CODE = FIRST_CFG["spx"]

# 面单生成（label generated）
# jvEventCode = OCRETURNLBDE002
#   oprCnDes = 下单 - 卖家已经下单创建物流面单Label
#   oprEnDes = The sender has placed an order and the shipping label has been generated.
# 每个订单恰好上报一次（2026-10-20~11-03 的 333 单实测 333/333），
# 因此「有这个事件」等价于「这个单的面单已经生成」。
# 订单来源：按接口的 serviceProductCode（看板里叫「serviceID」）区分渠道。
#   USDOMS2D → eBay ； UDDOMS2D → Depop
# 实测 US 接口没有 serviceId 字段，能区分渠道的就是 serviceProductCode，
# 且与 serviceProductName 同值；channel 两边都是 GPLT、ebayOrderId 在 Depop 单上是
# 占位值 '00-00000-00000'，都不能用来分流。
SOURCE_MAP = {
    "USDOMS2D": "eBay",
    "UDDOMS2D": "Depop",
}


def source_of(code: str) -> str:
    """serviceProductCode → 订单来源标签；未登记的码原样返回，空值给 —。"""
    c = (code or "").strip().upper()
    if not c:
        return "—"
    return SOURCE_MAP.get(c, c)


LABEL_EVENT_CODE = (os.getenv("OC_LABEL_EVENT") or "OCRETURNLBDE002").strip().upper()

# 「SPX 提取 / 接手」除区域主码之外还认可的通用事件码。
# SCN005 = Processing at Origin Facility：PUDO 链路出库后往往不报 SCAUS002，
# 最早出现的就是它（实测出库 4 单，SCAUS002 命中 0，但 3 单出库后 ~2.7h 有 SCN005）。
# 用户口径：SCAUS002 或 SCN005，满足任意一个就算已提取。
# Depop 渠道（单号 UDP* 前缀）上游用的是 **UD 后缀** 事件码，语义与 US 完全一致：
#   DFDLUD002 出库 / AFDLUD002 到仓 / COLUD002 门店取件 / SHIPFCUD002 舱单
#   SCAUD002  提取 / OFDUD006 派送 / DELUD006 妥投 / SHIPCCUD001 取消
# 只认 US/UK 后缀会让 Depop 单的物流节点**整段丢失**（2026-11-09 实测 17 单：
# 2 单已妥投却仍显示「已A-SCAN」，出库/提取/妥投三列全空）。
# 所以把 UD 变体并进各码表，不给 Depop 单单独配区域 —— 它们和 US 走同一条链路。
SPX_PICKUP_EXTRA = tuple(
    c.strip().upper()
    for c in (os.getenv("OC_SPX_EVENT_EXTRA", "SCN005,SCAUD002") or "").split(",")
    if c.strip()
)

# RDC 出库码：US=DFDLUS002 / Depop=DFDLUD002，靠 vendorEventCode 区分谁出的库。
RDC_OUT_EXTRA = tuple(
    c.strip().upper()
    for c in (os.getenv("OC_RDC_EVENT_EXTRA", "DFDLUD002") or "").split(",")
    if c.strip()
)

# 妥投（delivered）
# jvEventCode 按区域不同：US = DELUS006，UK = DEL005，两者语义一致：
#   isEventCode = DELIVERED / jvEventCnRemark = Delivered / oprEnDes = Delivered
# 每单最多一条（10/20~11/03 的 333 单 US 里只有 17 单有，其余尚未派送完）。
# 匹配时**两个码都认**，不按区域写死：区域主码只用于页面展示，
# 免得哪天上游给 UK 也换成带区域后缀的码时整列变空。
DELIVERED_EVENT_CODES = tuple(
    c.strip().upper()
    for c in (os.getenv("OC_DELIVERED_EVENT") or "DELUS006,DEL005,DELUD006").split(",")
    if c.strip()
)
# RDC 到仓（arrived at warehouse）。出库=DFDLUS002，到仓=AFDLUS002，成对出现。
# 用途：算「到仓→出库」这段等待。PUDO 单到仓后要等一班车（中位 24.5h），
# LMS 单基本到仓即出（中位 0.2h），所以压库判断必须分投递方看，不能混算。
ARRIVE_EVENT_CODES = tuple(
    c.strip().upper()
    for c in (os.getenv("OC_ARRIVE_EVENT") or "AFDLUS002,AFDLUK002,AFDLAU002,AFDLCA002,AFDLUD002").split(",")
    if c.strip()
)

# 各区域在页面上展示哪个码（实际匹配两个都认）
DELIVERED_PRIMARY = {"UK": "DEL005", "US": "DELUS006"}
# 兜底展示码（区域不在表里时用）
DELIVERED_FALLBACK = DELIVERED_EVENT_CODES[0] if DELIVERED_EVENT_CODES else "DELUS006"

VENDOR_PUDO = "PUDO INC"
VENDOR_LMS = "LMS"

# RDC 出库（warehouse out）节点登记表
# ------------------------------------
# 依据业务方《供应商 eventCode & jveventcode》登记表（2026-11-03 核对）：
# 两家 drop-off vendor 都上报 jvEventCode = DFDLUS002（isEventCode = ML_FACILITY_OUT，
# isEventCnRemark = Package departed from the facility），靠**同一条事件的 vendorEventCode**
# 区分是谁出的库：
#
#   vendor      vendorCode      vendorEventCode                  oprEnDes
#   PUDO INC    PUP / PU        PUP                              Driver pickup from PUDOpoint
#   LMS         LMS / LM        shipment.injected/made_ready     Package ready for dispatch
#
# 判定顺序：jvEventCode 命中 → 再看 vendorEventCode 定归属。
# vendorEventCode 是登记表里的一等字段，比 vendorCode 更贴近业务口径。
OUT_NODES_US = [
    {"vendor": VENDOR_PUDO, "vendorCode": "PUP", "vendorEventCode": "PUP",
     "desc": "Driver pickup from PUDOpoint",
     "note": "接口 vendorCode 常返回短码 PU（vendorName=PUDO），PU / PUP 均归入 PUDO INC"},
    {"vendor": VENDOR_LMS, "vendorCode": "LMS", "vendorEventCode": "shipment.injected/made_ready",
     "desc": "Package ready for dispatch",
     "note": "接口 vendorCode 常返回短码 LM；vendorEventCode 见登记表"},
]

# 替代码：不是登记表主码，但实测在跑，且 isEventCode 同为 ML_FACILITY_OUT。
# 2026-11-01 起 LMS 改用 shipment.sorted 上报同一个 DFDLUS002（10/30 之前只用 injected/made_ready，
# 11/01 起 injected/made_ready 完全消失）。归入 LMS，但在订单上打 alias 标记，
# 让「口径被上游悄悄换了」这件事在看板上一眼可见，而不是静默变成「未出库」。
VENDOR_EVENT_ALIASES = {
    "shipment.sorted": {
        "vendor": VENDOR_LMS,
        "note": "2026-11-01 起 LMS 改用此码上报，语义同为 Package departed from the facility",
    },
}

# ---------------------------------------------------------------------------
# 投递（drop off）节点登记
# ---------------------------------------------------------------------------
# 「客户把包裹交给谁」和「谁出的库」是两件事，必须分开记：
#   投递在 PUDO INC（vendorCode=PU）→ 客户投在 PUDO 点
#   投递在 LMS（vendorCode=LM）      → 客户交到 LMS 门店 / 站点
# 存在「投在 PUDO INC、却从 LMS 的 RDC 出库」的单，按投递方统计才不会归错。
#
# 事件码跨区域不统一：
#   US → DOUS002（isEventCode=FIRST_SCAN，收件扫描）
#   UK → SHRSERPTESDE002 / SHRPCKSTESDE002（DHL Service Point / Packstation，vendor=DR）
# 所以判定以 **isEventCode = FIRST_SCAN** 为主、DO* 事件码为辅，跨区域都能认。
DROP_EVENT_CODES = ("DOUS002", "DOUD002")
DROP_FIRST_SCAN = "FIRST_SCAN"
DROP_NODES = [
    {"vendor": VENDOR_PUDO, "vendorCode": "PU",
     "desc": "Customer drop-off at PUDOpoint",
     "note": "客户把包裹投在 PUDO 网点"},
    {"vendor": VENDOR_LMS, "vendorCode": "LM",
     "desc": "At store, awaiting LMS pickup",
     "note": "客户把包裹交到 LMS 门店 / 站点"},
]

# 登记表主码集合：vendorEventCode -> vendor
VENDOR_EVENT_REGISTERED = {n["vendorEventCode"]: n["vendor"] for n in OUT_NODES_US}
# 全量可识别码（主码 + 替代码）：vendorEventCode -> vendor
VENDOR_EVENT_MAP = {**VENDOR_EVENT_REGISTERED,
                    **{k: v["vendor"] for k, v in VENDOR_EVENT_ALIASES.items()}}

# vendorCode -> 统一展示名。显式表优先，猜不到才回退到子串匹配。
# 后台展示名是 PUP / LMS，接口实际返回短码 PU / LM，两套都要认，
# 否则同一家 vendor 会被拆成两个，或掉到「未知」。
VENDOR_ALIASES = {
    "PUP": VENDOR_PUDO, "PU": VENDOR_PUDO, "PUDO": VENDOR_PUDO,
    "PUDOINC": VENDOR_PUDO, "PUDO INC": VENDOR_PUDO, "PUDOINCUS": VENDOR_PUDO,
    "LMS": VENDOR_LMS, "LM": VENDOR_LMS, "LMSUS": VENDOR_LMS, "LMSUK": VENDOR_LMS,
    "SPX": "SPX", "JV": "JV",
}

# 反向表：归一后的 vendor -> 后台展示码。页面徽标统一显示 PUP / LMS，
# 接口原值（PU / LM）另存 vendorCodeRaw 供排查，两者不混用。
VENDOR_DISPLAY_CODE = {n["vendor"]: n["vendorCode"] for n in OUT_NODES_US}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _pick(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for k in keys:
        if k in obj and obj[k] not in (None, "", "-"):
            return obj[k]
    return default


def parse_time(value):
    """仅用于订单头（orderCreatedTimeWithZone / firstScanTime）。"""
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=TZ_CN)
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(TZ_CN)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ_CN)
        except ValueError:
            continue
    return None


def event_time(ev):
    """事件时间：优先 oprTimeWithZone。

    +04:00 按其字面语义（UTC+4）解析即得正确绝对时刻，已与订单头 firstScanTimeStamp
    交叉校验 41/41 一致；oprTime 毫秒戳存在 7~16h 漂移，仅作兜底。
    """
    s = _pick(ev, "oprTimeWithZone")
    if s:
        dt = parse_time(s)
        if dt:
            return dt
    for key in ("oprTime", "oprTimestamp"):
        v = ev.get(key)
        if not v:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 1e11:
            n /= 1000
        return datetime.fromtimestamp(n, tz=TZ_CN)
    return None


def fmt_time(dt):
    return dt.astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def hours_between(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600.0, 2)


def map_vendor_event(vendor_event_code):
    """按登记表的 vendorEventCode 定 RDC 归属；不是可识别码则返回 None。

    `PUP` → PUDO INC；`shipment.injected/made_ready`（主码）、`shipment.sorted`（11/01 起的替代码）→ LMS。
    这是首选判定：vendorEventCode 是登记表里的一等字段，vendorCode 只是它的上游短码。
    """
    return VENDOR_EVENT_MAP.get((vendor_event_code or "").strip())


def out_code_kind(vendor_event_code):
    """出库事件码的身份：registered（登记表主码）/ alias（替代码）/ ''（未登记）。"""
    v = (vendor_event_code or "").strip()
    if v in VENDOR_EVENT_REGISTERED:
        return "registered"
    if v in VENDOR_EVENT_ALIASES:
        return "alias"
    return ""


def map_vendor(raw_code, raw_name):
    """RDC 归属：PUDO INC / LMS。优先按 vendorCode 精确匹配（PUP → PUDO INC）。"""
    code = (raw_code or "").strip().upper()
    if code in VENDOR_ALIASES:
        return VENDOR_ALIASES[code]
    s = f"{code} {(raw_name or '').upper()}"
    if "PUDO" in s or code.startswith("PU"):
        return VENDOR_PUDO
    if "LMS" in s or code.startswith("LM"):
        return VENDOR_LMS
    return (raw_name or raw_code or "未知").strip() or "未知"


# 生产环境里混有测试单：先看收件人名，再看地址里是否带 DUMMY / TEST 字样。
# 刻意不做「短名包含 TEST」这类宽松匹配，避免 `Testa`、`MyTest Inc` 这类真实收件人被误杀。
TEST_NAME_RE = re.compile(r"^(TEST|TESTS|TESTING|TESTER|DUMMY|DEMO|SAMPLE|测试|样例|演示)\d*$")
# 任一词命中：`Testing Liuz` / `Tester Liuz` / `Test ToAdd` / `ALANA DRESNER PUDO TEST` 这类
# 带测试词的收件人。要求该词**整体**等于测试词（\d* 允许 TEST01），所以 Testa / TESTIMONIAL CORP 不受影响。
TEST_LEAD_RE = re.compile(r"^(TEST|TESTS|TESTING|TESTER|TESTNAME|DUMMY|DEMO|SAMPLE|测试|样例|演示)\d*$", re.I)
TEST_ADDR_RE = re.compile(r"(DUMMY|TEST\s*ADDR|\bTEST\b|SANDBOX|NO\s*ADDRESS)", re.I)


def looks_like_test(consignee: str, addr: str = "") -> bool:
    """占位单判定，三级从严到松，全部要求「整词」命中：

    1. 整体 —— 归一化后整个收件人是 `TEST` / `TEST01` / `DUMMY` …；
    2. 任一词 —— `Testing Liuz`、`Tester Liuz`、`testName`、`Test ToAdd`、
       `ALANA DRESNER PUDO TEST`（真名后面挂 PUDO TEST 后缀）；
    3. 地址 —— 明写 Dummy / Test Addr / Sandbox。
    """
    raw = (consignee or "").strip()
    n = re.sub(r"[\s._\-&,]+", "", raw).upper()
    if n and TEST_NAME_RE.match(n):
        return True
    toks = [t for t in re.split(r"[\s._\-&,]+", raw) if t]
    # 任意一个词完整等于测试词即命中：`ALANA DRESNER PUDO TEST`、`Test ToAdd`、`testName`
    for t in toks:
        if TEST_LEAD_RE.match(t):
            # 单 token 时只认 TESTNAME 这种明确占位词；单独一个 Tester 可能是真姓，交给第 1 级判断
            if len(toks) >= 2 or t.rstrip("0123456789").upper() == "TESTNAME":
                return True
    return bool(TEST_ADDR_RE.search(addr or ""))


def addr_key(addr: str) -> str:
    """地址归一化：只留字母数字并大写，用于判断「同一个收货地址」。"""
    return re.sub(r"[^0-9A-Za-z]+", "", (addr or "")).upper()


def test_flags(rows: list[tuple[str, str, str]], min_hits: int = 2) -> list[bool]:
    """返回每单是否测试单。rows = [(region, consignee, addr)]。

    两级判定：
    1. 直接命中 —— 姓名是占位词，或地址含 Dummy/Test；
    2. **同地址推定** —— 同一区域、同一地址上已有 >= min_hits 单直接命中，
       则该地址其余单也标为测试。用于兜住 `Test ToAdd` 这类没预料到的变体：
       真实客户地址上不会出现多单收件人叫 Test，所以这条不会误杀。
    """
    direct = [looks_like_test(c, a) for _r, c, a in rows]
    groups: dict[tuple[str, str], list[int]] = {}
    for i, (region, _c, addr) in enumerate(rows):
        k = (region, addr_key(addr))
        if k[1]:
            groups.setdefault(k, []).append(i)

    flags = list(direct)
    for idxs in groups.values():
        if sum(1 for i in idxs if direct[i]) >= min_hits:
            for i in idxs:
                flags[i] = True
    return flags




# ---------------------------------------------------------------------------
# 标准化
# ---------------------------------------------------------------------------
def extract_spx_pickup(events, codes, sem=None, prefixes=("SCA", "SCN")):
    """最早一条「SPX 提取 / 交接」事件，返回 (time, ev)。

    口径（2026-11-06 用户确认）：**SCAUS002 或 SCN005，任意一个命中即算 SPX 提取**。
      SCAUS002  Picked up by delivery carrier    司机提取（只有部分链路报）
      SCN005    Processing at Origin Facility    始发中心处理（PUDO 链路最早出现）
    Depop 的对应码是 SCAUD002（Service ID 不同），靠语义 IN_TRANSIT + SCA/SCN 前缀认。
    两个码可能都报，取最早的一条 —— 那才是交接真正发生的时刻。
    """
    return extract_event_multi(events, codes, sem=sem, prefixes=prefixes)


def spx_pickup_codes(cfg) -> tuple:
    """当前区域认可的 SPX 提取事件码：区域主码（SCAUS002 / SCAUK002）+ 通用码 SCN005。"""
    codes = [str(cfg.get("spx") or "").strip().upper()]
    codes += [c.strip().upper() for c in SPX_PICKUP_EXTRA]
    return tuple(c for c in codes if c)


def extract_event(events, code):
    """找指定 JV Event Code 的最早一条（同码可能重复上报）。"""
    hits = []
    for ev in events or []:
        if str(ev.get("jvEventCode", "")).strip().upper() != code.upper():
            continue
        t = event_time(ev)
        if t:
            hits.append((t, ev))
    return min(hits, key=lambda x: x[0]) if hits else (None, None)


# ---- 语义匹配：跨 Service ID 的稳定判据 -------------------------------------
# eBay（Service ID = USDOMS2D）和 Depop（UDDOMS2D）走的是**同一套流程**，
# 上报的 jvEventCode 只是后缀不同（US / UD）：DFDLUS002 ↔ DFDLUD002、
# DELUS006 ↔ DELUD006 …… 但 isEventCode（语义）完全一致：
#   出库 ML_FACILITY_OUT / 到仓 ML_FACILITY_IN / 妥投 DELIVERED / 投递 FIRST_SCAN
# 所以关键节点**按语义匹配**，以后再来一个新 Service ID（UV/UB…）也不用改代码。
# 显式码表保留作兜底：个别事件 isEventCode 缺失时仍能靠码认出来。
SEM_DELIVERED = "DELIVERED"
SEM_ARRIVE = "ML_FACILITY_IN"    # 到仓
SEM_OUT = "ML_FACILITY_OUT"      # 出库
SEM_DROP = "FIRST_SCAN"          # 客户投递
SEM_CANCELLED = "CANCELLED"


def sem_of(ev) -> str:
    """isEventCode 归一化：上游同一语义有 'IN_TRANSIT' / 'IN TRANSIT' 两种写法。"""
    return str(ev.get("isEventCode") or "").strip().upper().replace(" ", "_").replace("-", "_")


def extract_event_multi(events, codes=(), sem=None, prefixes=()):
    """多码 / 多语义取最早的一条事件，返回 (time, ev)。

    匹配规则（三者取并集）：
      1. jvEventCode ∈ codes          —— 显式码表（兜底）
      2. isEventCode == sem           —— 语义（跨 Service ID 稳定）
    若给了 prefixes，则事件码必须以其中之一开头（用于把 IN_TRANSIT 收窄到
    提取类事件 SCA*/SCN*，排除同样报 IN_TRANSIT 的 ARR005 / INT005）。
    """
    wanted = {str(c or "").strip().upper() for c in codes or () if str(c or "").strip()}
    pres = tuple(str(p or "").strip().upper() for p in prefixes or () if str(p or "").strip())
    hits = []
    for ev in events or []:
        code = str(ev.get("jvEventCode") or "").strip().upper()
        ok = code in wanted
        if not ok and sem and sem_of(ev) == sem:
            ok = True
        if ok and pres and not code.startswith(pres):
            ok = False
        if ok:
            t = event_time(ev)
            if t:
                hits.append((t, ev))
    return min(hits, key=lambda x: x[0]) if hits else (None, None)


def is_drop_event(ev) -> bool:
    """这条事件是不是「客户投递（drop off）」。

    主判据是 isEventCode = FIRST_SCAN（收件扫描）——US 的 DOUS002、UK 的
    SHRSERPTESDE002 / SHRPCKSTESDE002 都是它，跨区域通用；
    DO* 事件码作为兜底，防止哪天 isEventCode 字段缺失。
    """
    if str(ev.get("isEventCode") or "").strip().upper() == DROP_FIRST_SCAN:
        return True
    return str(ev.get("jvEventCode") or "").strip().upper() in DROP_EVENT_CODES


# specialOprDesc 里跟「投递点位」有关的键。上游把一段 JSON 塞在这个字符串字段里，
# **并且会硬截断在 500 字符**（尾部引号都不闭合），所以 json.loads 必失败，只能正则逐取。
LOC_DESC_KEYS = ("locationId", "locationAlias", "locationName", "address", "city", "state", "zipCode")


def parse_location_desc(ev):
    """从投递事件的 specialOprDesc 抠点位信息。返回 dict，取不到的键不出现。

    实测（2026-11-11，US）：
      LMS   → specialOprDesc 存在，含 locationId（如 13516 / AB4122 / AU006152）、
              locationAlias、locationName、address、zipCode
      PUDO  → FIRST_SCAN 这条事件**根本没有 specialOprDesc**，只能退回 oprDistrict / zipCode
    覆盖率也不是 100%（同一家 vendor 有的单就是没这段），所以调用方必须允许空值。
    """
    raw = str((ev or {}).get("specialOprDesc") or "")
    if not raw:
        return {}
    out = {}
    for key in LOC_DESC_KEYS:
        m = re.search(r'"' + key + r'"\s*:\s*"([^"]*)"', raw)
        if m:
            v = m.group(1).strip()
            if v:
                out[key] = v
    return out


def extract_drop(events):
    """抽出最早的投递事件。返回 (time, ev, vendor, raw_vendor_code)。

    同一单可能重复上报投递（换点位重投），取最早一条——那才是真正的首次交寄。
    vendor 取事件的 vendorCode（PU → PUDO INC，LM → LMS），
    认不出来的（UK 的 DHL Service Point 等）回退到后台的 vendorName。
    """
    hits = []
    for ev in events or []:
        if not is_drop_event(ev):
            continue
        t = event_time(ev)
        if t:
            hits.append((t, ev))
    if not hits:
        return None, None, "", ""
    t, ev = min(hits, key=lambda x: x[0])
    raw = str(ev.get("vendorCode") or "").strip().upper()
    vendor = map_vendor(raw, ev.get("vendorName"))
    return t, ev, vendor, raw


def out_codes(cfg) -> tuple:
    """当前区域认可的「RDC 出库」事件码：区域主码 + UD 后缀变体（Depop）。"""
    codes = [str(cfg.get("rdc") or "").strip().upper()]
    codes += [c.strip().upper() for c in RDC_OUT_EXTRA]
    return tuple(c for c in codes if c)


def extract_out(events, codes):
    """抽出全部「出库」事件（同码重复上报 + 两家 RDC 共用同一码）。

    返回 [(time, ev, vendor, vendor_event_code, kind)]，按时间升序。kind 见 out_code_kind()。
    调用方取第一条作为「RDC 出库时间」，但保留全量是为了两件事：
      1. 同一单被 PUDO 和 LMS 各报一次时能发现（outMixed）；
      2. 页面/排查时能说明「这条单到底是谁出的库」。

    归属判定以 **vendorEventCode** 为准（登记表一等字段）；登记表里没有的码才回退
    vendorCode 猜，并让 kind 为空串，好在看板上暴露「上游又换了码」。

    codes 可以是单个码或码元组（US 主码 + Depop 的 UD 变体）。
    另外按语义 ML_FACILITY_OUT 兜底 —— Service ID 换了、码后缀变了也能认。
    """
    if isinstance(codes, str):
        codes = (codes,)
    wanted = {str(c or "").strip().upper() for c in codes or () if str(c or "").strip()}
    hits = []
    for ev in events or []:
        code = str(ev.get("jvEventCode", "")).strip().upper()
        if code not in wanted and sem_of(ev) != SEM_OUT:
            continue
        t = event_time(ev)
        if not t:
            continue
        vec = str(ev.get("vendorEventCode") or "").strip()
        kind = out_code_kind(vec)
        vendor = map_vendor_event(vec) or map_vendor(ev.get("vendorCode"), ev.get("vendorName"))
        hits.append((t, ev, vendor, vec, kind))
    hits.sort(key=lambda x: x[0])
    return hits


def normalize_order(record: dict, events: list | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or FIRST_CFG
    tn = _pick(record, "trackingNumber", default="")
    # 订单头优先毫秒戳（*Timestamp 与 *WithZone 已实证一致；orderCreatedTime 会偏 +3h）
    created = parse_time(_pick(record, "orderCreatedTimestamp", "orderCreatedTime"))
    a_scan = parse_time(_pick(record, "firstScanTime"))

    # 出库事件全量：两家 vendor 共用 DFDLUS002（Depop 为 DFDLUD002），靠 vendorEventCode 分流
    out_hits = extract_out(events, out_codes(cfg))
    if out_hits:
        rdc, rdc_ev, vendor, vendor_ev_code, code_kind = out_hits[0]
    else:
        rdc, rdc_ev, vendor, vendor_ev_code, code_kind = None, None, "—", "", ""

    # SPX 提取：SCAUS002 / SCN005（Depop 为 SCAUD002），语义 IN_TRANSIT + SCA/SCN 前缀兜底
    spx, spx_ev = extract_spx_pickup(events, spx_pickup_codes(cfg), sem="IN_TRANSIT")
    # 其中「只有区域主码」的严格口径，留档用，不参与主指标
    spx_strict_t, spx_strict_ev = extract_event(events, cfg["spx"])
    # 面单生成：每单一次，缺失说明面单还没出 / 接口没回
    label_t, label_ev = extract_event(events, LABEL_EVENT_CODE)
    # 妥投：按语义 DELIVERED 匹配（DELUS006 / DELUD006 / DEL005 都认）
    delivered_t, delivered_ev = extract_event_multi(events, DELIVERED_EVENT_CODES, sem=SEM_DELIVERED)
    # RDC 到仓：用于算「到仓→出库」压库时长（PUDO 单会等一班车）
    arrive_t, arrive_ev = extract_event_multi(events, ARRIVE_EVENT_CODES, sem=SEM_ARRIVE)
    # 投递（drop off）：客户把包裹交给谁 —— 与「谁出的库」分开记
    drop_t, drop_ev, drop_vendor, drop_raw = extract_drop(events)
    # 投递点位：LMS 走 specialOprDesc.locationId，PUDO 只有地址行（见 parse_location_desc）
    loc = parse_location_desc(drop_ev)
    loc_id = loc.get("locationId", "")
    loc_addr = (loc.get("address")
                or str((drop_ev or {}).get("oprDistrict") or "").strip())
    loc_zip = (loc.get("zipCode")
               or str((drop_ev or {}).get("zipCode") or "").strip())
    # 城市/州：specialOprDesc 里有点位自己的 city/state 时优先，取不到再退回事件上的
    # oprCity / oprState（PUDO 的 FIRST_SCAN 没有 specialOprDesc，只能走这两项）
    loc_city = (loc.get("city")
                or str((drop_ev or {}).get("oprCity") or "").strip())
    loc_state = (loc.get("state")
                 or str((drop_ev or {}).get("oprState") or "").strip())
    # 统一 key：有官方 ID 就用 ID，没有就退到「地址|邮编」。
    # 两种 key 不能混着聚合，dropSiteKind 明确标注来源，看板上分开呈现。
    if loc_id:
        site_key, site_kind = loc_id, "id"
    elif loc_addr:
        site_key, site_kind = f"{loc_addr}|{loc_zip}", "addr"
    else:
        site_key, site_kind = "", ""

    # vendorCode 只作留档与展示：接口原值（PU / LM）存 vendorCodeRaw，
    # 页面徽标统一显示后台口径（PUP / LMS）。
    vendor_code_raw = str((rdc_ev or {}).get("vendorCode") or "").strip().upper()
    vendor_code = VENDOR_DISPLAY_CODE.get(vendor, vendor_code_raw)

    out_vendors, _seen = [], set()
    for _t, _ev, _v, _vec, _kind in out_hits:
        if _v not in _seen:
            _seen.add(_v)
            out_vendors.append(_v)

    # 最新节点
    latest_t, latest_ev = None, None
    for ev in events or []:
        t = event_time(ev)
        if t and (latest_t is None or t > latest_t):
            latest_t, latest_ev = t, ev

    if delivered_t:
        status = "已妥投"
    elif spx:
        status = "已交SPX"
    elif rdc:
        status = "已出库待SPX"
    elif a_scan:
        status = "已A-SCAN"
    else:
        status = "待A-SCAN"

    def gap(a, b):
        """只返回有意义的正区间，负值视为数据异常（返回 None）。"""
        h = hours_between(a, b)
        return h if (h is not None and h >= 0) else None

    raw = [hours_between(created, a_scan), hours_between(a_scan, rdc),
           hours_between(rdc, spx)]
    anomaly = any(h is not None and h < 0 for h in raw)

    consignee = _pick(record, "consigneeFullName", default="").strip()
    addr = _pick(record, "consigneeAddr1", "consigneeAddr", default="").strip()

    return {
        "ocOrderNo": tn,
        "region": cfg["code"],
        "vendor": vendor,
        "vendorCode": vendor_code,
        "vendorCodeRaw": vendor_code_raw,
        # 登记表里的一等字段：PUP = PUDO INC 出库；shipment.injected/made_ready = LMS 出库
        "vendorEventCode": vendor_ev_code,
        # registered=登记表主码 / alias=替代码（上游换码）/ ''=未登记码
        "outCodeKind": code_kind,
        "outNodeCode": str((rdc_ev or {}).get("jvEventCode") or (cfg["rdc"] if rdc else "")),
        "outEventCount": len(out_hits),
        "outVendors": "/".join(out_vendors),
        "outMixed": len(out_vendors) > 1,
        # 投递方：客户把包裹交给谁（PUDO INC / LMS / 其他 vendor）
        "dropVendor": drop_vendor,
        "dropVendorRaw": drop_raw,
        "dropEventCode": str((drop_ev or {}).get("jvEventCode") or ""),
        # 这条投递事件的 isEventCode 原值：正常是 FIRST_SCAN；
        # 空 / DO* 说明是靠事件码兜底认出来的，明细里标「首扫」时要能区分
        "dropIsEventCode": str((drop_ev or {}).get("isEventCode") or ""),
        "dropIsFirstScan": bool(drop_ev)
                           and str((drop_ev or {}).get("isEventCode") or "").strip().upper() == DROP_FIRST_SCAN,
        "dropTime": fmt_time(drop_t),
        # ---- 投递点位（location）----
        # dropLocationId 是上游点位 ID（LMS 常见 13516 / AB4122 / AU006152）；
        # PUDO 的 FIRST_SCAN 不带 specialOprDesc，这个字段就是空，靠下面的地址兜底。
        "dropLocationId": loc_id,
        "dropLocationAlias": loc.get("locationAlias", ""),
        "dropLocationName": loc.get("locationName", ""),
        "dropLocationAddr": loc_addr,
        "dropLocationCity": loc_city,
        "dropLocationState": loc_state,
        "dropLocationZip": loc_zip,
        # 聚合用的点位键 + 来源：id=官方 ID；addr=地址兜底（PUDO 为主）
        "dropSiteKey": site_key,
        "dropSiteKind": site_kind,
        # 投递方 ≠ 出库方时置位：「投在 PUDO 却从 LMS 出库」这类串场一眼可见
        "dropVendorDiffOut": bool(drop_vendor and vendor != "—"
                                  and drop_vendor != vendor),
        "referenceNumber": _pick(record, "referenceNumber", default=""),
        "lastMileWaybill": _pick(record, "lastMileTrackingNumber", default=""),
        "destCountry": _pick(record, "consigneeCountry", default=""),
        "destState": _pick(record, "consigneeState", default=""),
        "consignee": consignee,
        "consigneeAddr": addr,
        "isTest": looks_like_test(consignee, addr),
        "product": _pick(record, "serviceProductCode", default=""),
        # 订单来源：serviceProductCode → eBay / Depop（见 SOURCE_MAP）
        "source": source_of(_pick(record, "serviceProductCode", default="")),
        "orderCreateTime": fmt_time(created),
        # 面单生成时间：jvEventCode=OCRETURNLBDE002，空串 = 这份快照里没拿到该事件
        "labelTime": fmt_time(label_t),
        "aScanTime": fmt_time(a_scan),
        # 到仓时间：jvEventCode=AFDLUS002（arrived at warehouse），空串 = 没拿到
        "rdcArriveTime": fmt_time(arrive_t),
        "rdcOutTime": fmt_time(rdc),
        "spxPickupTime": fmt_time(spx),
        # 命中的是哪个码（SCAUS002 / SCN005），便于核对
        "spxPickupCode": str((spx_ev or {}).get("jvEventCode") or ""),
        # 严格口径：只看 SCAUS002（司机提取），仅留档对比，不参与看板主指标
        "spxStrictTime": fmt_time(spx_strict_t),
        "spxStrictCode": str((spx_strict_ev or {}).get("jvEventCode") or ""),
        # 妥投时间：jvEventCode=DELUS006 / DEL005，空串 = 还没妥投
        "deliveredTime": fmt_time(delivered_t),
        "deliveredCode": str((delivered_ev or {}).get("jvEventCode") or ""),
        "latestCode": (latest_ev or {}).get("jvEventCode", ""),
        "latestTime": fmt_time(latest_t),
        "hCreateToAScan": gap(created, a_scan),
        "hCreateToLabel": gap(created, label_t),
        # 下单 → 投递（交寄）
        "hCreateToDrop": gap(created, drop_t),
        # 端到端：下单 → 妥投
        "hCreateToDelivered": gap(created, delivered_t),
        "hAScanToRdc": gap(a_scan, rdc),
        # 到仓 → 出库：压库时长。PUDO 中位 24.5h、LMS 中位 0.2h，判压库必须分投递方
        "hArriveToRdc": gap(arrive_t, rdc),
        "hRdcToSpx": gap(rdc, spx),
        # 严格口径（仅 SCAUS002）的耗时，留档对比
        "hRdcToSpxStrict": gap(rdc, spx_strict_t),
        "hAScanToSpx": gap(a_scan, spx),
        "hCreateToLatest": gap(created, latest_t),
        "anomaly": anomaly,
        "status": status,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _session():
    import requests

    s = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # Referer / Origin 跟着后台地址走；OC_PORTAL 没配就不带（空头反而可能被网关拒）
    if PORTAL:
        headers["Referer"] = f"{PORTAL}/orderManage/OrderComplexQuery"
        headers["Origin"] = PORTAL
    s.headers.update(headers)
    if COOKIE:
        s.headers["Cookie"] = COOKIE
    return s


def _host_of(cfg: dict) -> str:
    """取接口根地址；没配就明确报错，别拼出 '/aquarius-order/...' 这种相对 URL 去猜。"""
    host = (cfg.get("host") or "").strip().rstrip("/")
    if not host:
        key = (cfg.get("key") or "us").upper()
        raise SystemExit(
            f"缺少接口主机：请在 .env 里设置 OC_HOST_{key}=区域接口根地址"
            f"（例如 OC_HOST_{key}=https://<region>-api.<域名>），"
            f"或临时用 OC_BASE_URL 覆盖"
        )
    return host


def _url(path, cfg=None):
    cfg = cfg or FIRST_CFG
    return _host_of(cfg) + API_PREFIX + path


def fetch_list(s, start, end, page=1, limit=100, cfg=None):
    cfg = cfg or FIRST_CFG
    params = {"page": page, "limit": limit, "startDate": start, "endDate": end}
    if cfg["dest"]:
        params["destinationCountry"] = cfg["dest"]
    if SEND_COUNTRY:
        params["sendCountry"] = SEND_COUNTRY
    r = s.get(_url(API["list"], cfg), params=params, timeout=40)
    r.raise_for_status()
    d = r.json()
    # 登录态失效时后端返回 success=false + errorCode=302，且 result 是个字符串；
    # 不拦的话后面 .get 会抛 AttributeError，报错信息完全看不出是 Cookie 过期。
    if d.get("success") is False:
        code = d.get("errorCode")
        hint = "（登录态失效，请更新 Cookie）" if str(code) == "302" else ""
        raise RuntimeError(f"接口拒绝 errorCode={code} {d.get('errorMsg') or ''}{hint}".strip())
    res = d.get("result") or {}
    if not isinstance(res, dict):
        res = {}
    return res.get("list") or [], int(res.get("total") or 0)


def fetch_events(s, tn, cfg=None):
    if not tn:
        return []
    try:
        r = s.get(_url(API["events"].format(tn=tn), cfg), timeout=40)
        r.raise_for_status()
        res = r.json().get("result") or []
        return res if isinstance(res, list) else []
    except Exception:  # noqa: BLE001
        return []


def fetch_one_region(s, cfg, date_from, date_to, max_orders=0, workers=12) -> list[dict]:
    try:
        first, total = fetch_list(s, date_from, date_to, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"[real] {cfg['code']} 列表接口失败：{exc}")
        print(f"       地址 {_url(API['list'], cfg)}")
        return []

    # 日期守卫：后台对 startDate 有硬下限（实测 2026/10/19，约「今天往前 17 天」）。
    # 早于下限时它**不报错**，而是静默忽略时间条件、返回全库（US 约 2.5 万单）。
    # 不拦的话会白拉几万单、还算出一堆 10 月之前的脏口径。
    if total > SANITY_MAX_ORDERS:
        raise RuntimeError(
            f"{cfg['code']} 返回 {total} 单，明显是后台忽略了日期条件（startDate={date_from} "
            f"早于接口下限）。接口只放最近约 17 天的数据，请把 OC_START_DATE 调到下限之后，"
            f"否则会拉全库。"
        )

    print(f"[real] {cfg['code']}（{cfg['label']}）路向 {cfg['dest'] or '不限'}，"
          f"{date_from} ~ {date_to}，共 {total} 单")
    records = list(first)
    page = 2
    while len(records) < total and first:
        if max_orders and len(records) >= max_orders:
            break
        batch, _ = fetch_list(s, date_from, date_to, page=page, limit=100, cfg=cfg)
        if not batch:
            break
        records.extend(batch)
        page += 1
        if page > 300:
            break
    if max_orders:
        records = records[:max_orders]
    print(f"[real] {cfg['code']} 已取 {len(records)} 单，开始拉事件明细…")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        event_lists = list(pool.map(lambda r: fetch_events(s, r.get("trackingNumber", ""), cfg), records))

    return [normalize_order(rec, evs, cfg) for rec, evs in zip(records, event_lists)]


def fetch_real(date_from: str, date_to: str, max_orders: int = 0, workers: int = 12) -> list[dict]:
    if not COOKIE:
        print("[real] 缺少登录态：请在 .env 里填 OC_COOKIE（浏览器 Cookie 里的 hk-sit=...）")
        sys.exit(2)

    s = _session()
    orders: list[dict] = []
    for key in active_regions():
        orders.extend(fetch_one_region(s, region_cfg(key), date_from, date_to, max_orders, workers))

    orders.sort(key=lambda o: (o["region"], o["orderCreateTime"]))
    mark_tests(orders)
    return orders


def mark_tests(orders: list[dict]) -> int:
    """按姓名/地址 + 同地址推定重算 isTest，就地写回。返回因推定新增的条数。"""
    flags = test_flags([(o.get("region", ""), o.get("consignee", ""),
                         o.get("consigneeAddr", "")) for o in orders])
    added = 0
    for o, f in zip(orders, flags):
        if f and not o.get("isTest"):
            added += 1
        o["isTest"] = f
    if added:
        print(f"[real] 同地址推定追加 {added} 单测试单")
    return added


def probe():
    s = _session()
    now = datetime.now(TZ_CN)
    start = (now - timedelta(days=7)).strftime("%Y/%m/%d 00:00:00")
    end = now.strftime("%Y/%m/%d 23:59:59")
    print(f"Cookie   : {'已配置 ' + COOKIE[:18] + '...' if COOKIE else '未配置'}")
    for key in active_regions():
        cfg = region_cfg(key)
        print(f"\n=== {key.upper()} / {cfg['label']}  事件码 {cfg['rdc']} / {cfg['spx']}")
        print(f"    接口 : {_url(API['list'], cfg)}")
        try:
            lst, total = fetch_list(s, start, end, limit=5, cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"    请求失败 : {exc}")
            continue
        print(f"    近 7 天 : 共 {total} 单，本次返回 {len(lst)} 条")
        if not lst:
            continue
        o = lst[0]
        print(f"    样例 : {o.get('trackingNumber')} | {o.get('consigneeCountry')} | "
              f"创建 {o.get('orderCreatedTimeWithZone')} | A-SCAN {o.get('firstScanTime') or '-'}")
        evs = fetch_events(s, o.get("trackingNumber"), cfg)
        print(f"    事件数 : {len(evs)}")
        for e in evs[:6]:
            print(f"       {e.get('jvEventCode'):<18} {fmt_time(event_time(e))}  "
                  f"{e.get('vendorName') or ''}  {e.get('jvEventEnRemark') or ''}")


def audit_out(date_from: str, date_to: str, max_orders: int = 0):
    """核对「出库」节点按 vendorCode 的分布。

    用途：确认 PUDO INC（PUP）与 LMS 各自出了多少单、出库事件码是否都是 DFDLUS002。
    换了 Cookie 或怀疑口径变了，跑一次这个比翻页面快：
        python scraper.py --audit-out --from 2026/10/29
    """
    from collections import Counter, defaultdict

    if not COOKIE:
        print("[audit] 缺少登录态：请在 .env 里填 OC_COOKIE")
        return
    s = _session()
    for key in active_regions():
        cfg = region_cfg(key)
        recs, total, page = [], 0, 1
        while True:
            batch, total = fetch_list(s, date_from, date_to, page=page, limit=100, cfg=cfg)
            recs.extend(batch)
            if len(batch) < 100 or len(recs) >= total or page > 50:
                break
            page += 1
        if max_orders:
            recs = recs[:max_orders]
        print(f"\n=== {key.upper()} 订单 {len(recs)} / {total}")
        with ThreadPoolExecutor(max_workers=12) as pool:
            evs = list(pool.map(lambda r: fetch_events(s, r.get("trackingNumber") or "", cfg), recs))

        by_out = Counter()          # 出库事件：vendorCode -> 条数
        orders_by_out = Counter()   # 出库事件：vendorCode -> 单数（按首条归属）
        codes = Counter()           # 出库事件用到了哪些 jvEventCode
        by_ev = Counter()           # 出库事件：(vendorEventCode, vendor, kind) -> 条数
        mixed = 0
        for e in evs:
            hits = extract_out(e, out_codes(cfg))
            if not hits:
                continue
            vs = []
            for _t, ev, _v, _vec, _kind in hits:
                vc = str(ev.get("vendorCode") or "").strip().upper() or "(空)"
                by_out[vc] += 1
                codes[str(ev.get("jvEventCode") or "").strip()] += 1
                if vc not in vs:
                    vs.append(vc)
                by_ev[(_vec or "(空)", _v, _kind or "未登记")] += 1
            orders_by_out[vs[0]] += 1
            if len(vs) > 1:
                mixed += 1

        print(f"    出库事件码 : {dict(codes)}")
        print(f"    按 vendorCode 单数 : {dict(orders_by_out)}")
        print(f"    按 vendorCode 事件数: {dict(by_out)}")
        print("    按 vendorEventCode（登记表口径）:")
        for (vec, v, kind), n in by_ev.most_common():
            flag = {"registered": "主码", "alias": "替代码", "": "未登记"}.get(kind, kind)
            print(f"      {vec:<32} -> {v:<9} [{flag}]  {n} 条")
        if mixed:
            print(f"    同一单被两家各报一次 : {mixed} 单（按更早那条定归属）")
        for vc, n in orders_by_out.most_common():
            print(f"      {vc:<6} -> {map_vendor(vc, '')}  {n} 单")


# ---------------------------------------------------------------------------
# DEMO
# ---------------------------------------------------------------------------
def build_demo_orders(n=60, seed=20261103, cfg=None):
    cfg = cfg or region_cfg("us")
    rng = random.Random(seed)
    base = datetime(2026, 10, 19, 0, 0, 0, tzinfo=TZ_CN)
    orders = []
    for i in range(n):
        created = base + timedelta(days=rng.randint(0, 13), hours=rng.randint(8, 20), minutes=rng.randint(0, 59))
        a_scan = created + timedelta(hours=rng.uniform(2, 18))
        vendor = VENDOR_PUDO if i % 3 == 0 else VENDOR_LMS

        rdc = spx = None
        events = []
        if rng.random() >= 0.1:
            rdc = a_scan + timedelta(hours=rng.uniform(3, 20))
            events.append({"jvEventCode": cfg["rdc"],
                           "vendorCode": "PUP" if vendor == VENDOR_PUDO else "LMS",
                           "vendorName": "PUDO" if vendor == VENDOR_PUDO else "LMS",
                           "oprTime": int(rdc.timestamp() * 1000)})
            if rng.random() >= 0.15:
                spx = rdc + timedelta(hours=rng.uniform(5, 60))
                events.append({"jvEventCode": cfg["spx"], "vendorName": "SPX",
                               "oprTime": int(spx.timestamp() * 1000)})

        seq = 500000000 + i * 137 + rng.randint(0, 99)
        oc_no = f"{cfg['code']}PDPPS{seq:010d}N"
        record = {
            "trackingNumber": oc_no,
            "referenceNumber": oc_no[:13],
            "lastMileTrackingNumber": oc_no,
            "consigneeCountry": "UnitedStates",
            "consigneeState": rng.choice(["California", "Texas", "New York", "Florida", "Illinois"]),
            "serviceProductCode": "USDOMS2D",
            "orderCreatedTimestamp": int(created.timestamp() * 1000),
            "orderCreatedTimeWithZone": created.isoformat(),
            "firstScanTime": a_scan.isoformat(),
        }
        orders.append(normalize_order(record, events, cfg))

    orders.sort(key=lambda o: o["orderCreateTime"])
    return orders


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def save(orders, meta, name: str = "orders"):
    """写 data/{name}.json + .csv。demo 走 demo_orders，绝不覆盖真实快照。"""
    (DATA_DIR / f"{name}.json").write_text(
        json.dumps({"meta": meta, "orders": orders}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if orders:
        with (DATA_DIR / f"{name}.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(orders[0].keys()))
            w.writeheader()
            w.writerows(orders)
    print(f"[save] {len(orders)} 条 -> data/{name}.json + data/{name}.csv")


def _norm_date(s: str, end: bool = False) -> str:
    """接口要求 'yyyy/MM/dd HH:mm:ss'，只给日期时自动补 00:00:00 / 23:59:59。"""
    s = (s or "").strip().replace("-", "/")
    if not s:
        return s
    return s if ":" in s else f"{s} {'23:59:59' if end else '00:00:00'}"


def _range(days: int, date_from, date_to):
    now = datetime.now(TZ_CN)
    if date_from and date_to:
        return _norm_date(date_from), _norm_date(date_to, end=True)
    start = (now - timedelta(days=days or 14)).strftime("%Y/%m/%d 00:00:00")
    return start, now.strftime("%Y/%m/%d 23:59:59")


def build_meta(start: str, end: str, orders: list, demo: bool = False) -> dict:
    """构造看板 meta（供 CLI 与 serve.py 共用）。"""
    regions = active_regions()
    codes = {k: region_cfg(k) for k in regions}
    meta = {
        "regions": [k.upper() for k in regions],
        "regionsPresent": sorted({k.upper() for k in regions}),
        "regionLabels": {k.upper(): REGIONS[k]["label"] for k in regions},
        "region": REGIONS[regions[0]]["label"],
        "rdcEventCode": codes[regions[0]]["rdc"],
        "spxEventCode": codes[regions[0]]["spx"],
        # 面单生成节点：全区域共用同一码
        "labelEventCode": LABEL_EVENT_CODE,
        "eventCodes": {k.upper(): {"rdc": codes[k]["rdc"], "spx": codes[k]["spx"],
                                   "delivered": DELIVERED_PRIMARY.get(k.upper(), DELIVERED_FALLBACK)}
                       for k in regions},
        # 妥投：匹配时两个码都认，这里给出全量，便于页面在 hover 里说明
        "deliveredEventCodes": list(DELIVERED_EVENT_CODES),
        # 出库节点登记：两家 vendor 共用 jvEventCode=DFDLUS002，靠 vendorEventCode 分流
        # PUDO INC → PUP；LMS → shipment.injected/made_ready
        "outNodes": {
            k.upper(): [
                {"vendor": n["vendor"], "vendorCode": n["vendorCode"],
                 "vendorEventCode": n["vendorEventCode"],
                 "code": codes[k]["rdc"], "desc": n["desc"], "note": n.get("note", "")}
                for n in OUT_NODES_US
            ] if codes[k]["rdc"].upper().startswith("DFDLUS") else []
            for k in regions
        },
        "outNodeAliases": VENDOR_EVENT_ALIASES,
        "vendorAliases": VENDOR_ALIASES,
        # 投递（drop off）节点登记：客户把包裹交给谁，与「谁出的库」分开
        "dropNodes": {k.upper(): [dict(n) for n in DROP_NODES] for k in regions},
        "dropEventCodes": list(DROP_EVENT_CODES),
        "dropFirstScan": DROP_FIRST_SCAN,
        "timezone": "北京时间 UTC+8",
        "generatedAt": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M"),
        "demo": demo,
        "source": ("示例数据（未接真实后台）" if demo else
                   " / ".join(f"{(codes[k].get('host') or '(未配置主机)')}{API_PREFIX}"
                              for k in regions)),
        "dateFrom": start, "dateTo": end,
        "anomalies": sum(1 for o in orders if o.get("anomaly")),
    }
    return meta


def main():
    global REGION
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--audit-out", action="store_true",
                    help="核对「出库」事件按 vendorCode（PUP / LMS）的分布")
    ap.add_argument("--region", dest="region", default=None,
                    help="us / uk / au / all（all = 后台 All tab，即 UK + US）")
    ap.add_argument("--days", type=int, default=int(os.getenv("OC_DAYS", "14")))
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--max", dest="max_orders", type=int, default=0, help="最多取多少单（调试用）")
    args = ap.parse_args()

    if args.region:
        REGION = args.region.lower()
        globals()["FIRST_CFG"] = region_cfg(active_regions()[0])

    if args.probe:
        probe()
        return

    if args.audit_out:
        start, end = _range(args.days, args.date_from, args.date_to)
        audit_out(start, end, args.max_orders)
        return

    regions = active_regions()
    codes = {k: region_cfg(k) for k in regions}

    if args.real:
        start, end = _range(args.days, args.date_from, args.date_to)
        orders = fetch_real(start, end, args.max_orders)
        meta = build_meta(start, end, orders, demo=False)
        save(orders, meta)
    else:
        orders = []
        for k in regions:
            orders.extend(build_demo_orders(cfg=codes[k]))
        meta = build_meta("2026/10/19 00:00:00", "2026/11/02 23:59:59", orders, demo=True)
        # 演示数据单独落盘，避免把真实快照覆盖掉
        save(orders, meta, name="demo_orders")


if __name__ == "__main__":
    main()
