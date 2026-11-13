#!/usr/bin/env python3
"""build_feed.py - 方案 B 云端数据脑：把已核验清单编译成技能可消费的 offers.json。

只做确定性工作：读 sources/offers.json -> 按契约逐条校验 -> 剥离私有核验字段
-> 写带 UTC 时间戳的公开 JSON。不采集、不抓取、不编造任何额度与金额。

路径基准：REPO_ROOT = Path(__file__).resolve().parent.parent，禁止使用 cwd。
退出码：0 成功 / 1 业务失败 / 2 用法错误。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "sources" / "offers.json"
DEFAULT_OUTPUT = REPO_ROOT / "offers.json"
CONTRACT_PATH = REPO_ROOT / "schema" / "contract.json"

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class FeedError(Exception):
    def __init__(self, message, exit_code=EXIT_FAIL):
        super().__init__(message)
        self.exit_code = exit_code


def load_contract(path=None):
    p = Path(path) if path else CONTRACT_PATH
    if not p.is_file():
        raise FeedError("缺少契约文件 schema/contract.json，已中止")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FeedError("契约文件不可读: " + type(e).__name__)


def parse_utc(value):
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def current_now(injected=None):
    if injected:
        dt = parse_utc(injected)
        if dt is None:
            raise FeedError("--now 不是带时区的 ISO-8601 时间", EXIT_USAGE)
        return dt
    return datetime.now(timezone.utc)


def parse_deadline(value):
    """解析 YYYY-MM-DD 截止日期；格式错或非法日期返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def effective_status(offer, contract, now):
    """缺省 status 按 verified 读取；verified / pending_official 超过 deadline 自动置 expired。"""
    status = offer.get("status", contract.get("default_offer_status", "verified"))
    deadline = parse_deadline(offer.get("deadline"))
    if deadline is not None and status in ("verified", "pending_official") and now.date() > deadline:
        return "expired"
    return status


def scan_rejections(offer, contract):
    """返回命中的拒绝规则列表 [(rule, keyword)]；空列表表示未命中。"""
    text = json.dumps(offer, ensure_ascii=False, sort_keys=True)
    hits = []
    for rule in contract.get("rejection_rules", []):
        for keyword in rule.get("keywords", []):
            if keyword and keyword in text:
                hits.append((rule, keyword))
                break
    return hits


def validate_offer(offer, idx, contract):
    """返回问题列表；空列表表示该条合法。对齐技能端 validate_payload 的核心校验。"""
    problems = []
    prefix = "verified_offers[" + str(idx) + "]"
    if not isinstance(offer, dict):
        return [prefix + " 不是对象"]
    missing = [f for f in contract["required_offer_fields"] if f not in offer]
    if missing:
        problems.append(prefix + " 缺少必填字段 " + ", ".join(missing))
    enums = contract["enums"]
    provider = offer.get("provider")
    if "provider" in offer and (not isinstance(provider, str) or not provider.strip()):
        problems.append(prefix + ".provider 须为非空字符串")
    if "modality" in offer and offer["modality"] not in enums["modality"]:
        problems.append(prefix + ".modality 取值非法: " + str(offer["modality"]))
    if "quota" in offer:
        quota = offer["quota"]
        if not isinstance(quota, dict):
            problems.append(prefix + ".quota 须为对象")
        else:
            qmissing = [f for f in ("unit", "amount") if f not in quota]
            if qmissing:
                problems.append(prefix + ".quota 缺少必填字段 " + ", ".join(qmissing))
            if "unit" in quota and quota["unit"] not in enums["quota_unit"]:
                problems.append(prefix + ".quota.unit 取值非法: " + str(quota["unit"]))
            if "period" in quota and quota["period"] not in enums.get("quota_period", []):
                problems.append(prefix + ".quota.period 取值非法: " + str(quota["period"]))
            if "amount" in quota:
                amt = quota["amount"]
                if isinstance(amt, bool) or not isinstance(amt, (int, float)):
                    problems.append(prefix + ".quota.amount 须为数字")
                elif not math.isfinite(amt):
                    problems.append(prefix + ".quota.amount 须为有限数字（不接受 NaN / Infinity）")
                elif Decimal(str(amt)) < 0:
                    problems.append(prefix + ".quota.amount 不得为负")
            if "unit_price_cny" in quota:
                up = quota["unit_price_cny"]
                if isinstance(up, bool) or not isinstance(up, (int, float)):
                    problems.append(prefix + ".quota.unit_price_cny 须为数字")
                elif not math.isfinite(up) or Decimal(str(up)) <= 0:
                    problems.append(prefix + ".quota.unit_price_cny 须为有限正数")
    if "status" in offer:
        status = offer["status"]
        if not isinstance(status, str) or status not in enums["offer_status"]:
            problems.append(prefix + ".status 取值非法: " + str(status))
    if "deadline" in offer:
        if parse_deadline(offer["deadline"]) is None:
            problems.append(prefix + ".deadline 须为合法的 YYYY-MM-DD 日期: " + str(offer["deadline"]))
    if "money_basis" in offer:
        basis = offer["money_basis"]
        if not isinstance(basis, str) or basis not in enums["money_basis"]:
            problems.append(prefix + ".money_basis 取值非法: " + str(basis))
        elif basis == "face_value":
            quota = offer.get("quota")
            if not isinstance(quota, dict) or quota.get("unit") != "credit":
                problems.append(prefix + ".money_basis=face_value 仅适用于 quota.unit=credit（按面值 1:1 计人民币）")
    for field in ("watermark", "commercial_rights", "queue_speed", "resolution"):
        if field in offer and offer[field] not in enums[field]:
            problems.append(prefix + "." + field + " 取值非法: " + str(offer[field]))
    return problems


def strip_private(offer, contract):
    """剥离下划线开头的私有核验字段，只留公开字段；并阻止外链/引流/禁词进入公开产物。"""
    public = {k: v for k, v in offer.items() if not k.startswith("_")}
    text = json.dumps(public, ensure_ascii=False, sort_keys=True)
    for token in contract["forbidden_substrings"]:
        if token in text:
            raise FeedError("公开产物出现禁用内容 " + token + "，已中止（provider=" + str(public.get("provider")) + "）")
    return public


def build(input_path, contract, now):
    try:
        raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FeedError("清单不可读或不是合法 JSON: " + type(e).__name__)
    if not isinstance(raw, dict) or not isinstance(raw.get("verified_offers"), list):
        raise FeedError("清单顶层须为对象且 verified_offers 须为数组，已中止")
    entries = raw["verified_offers"]
    problems = []
    for i, offer in enumerate(entries):
        problems.extend(validate_offer(offer, i, contract))
        if isinstance(offer, dict):
            for rule, keyword in scan_rejections(offer, contract):
                problems.append(
                    "verified_offers[" + str(i) + "] 命中拒绝规则「" + str(rule.get("label", rule.get("id", "")))
                    + "」（关键词：" + keyword + "）：" + str(rule.get("reason", "")) + "，不予收录"
                )
    if problems:
        raise FeedError("清单校验失败：" + "；".join(problems) + "；已中止，不发布任何数据")

    max_days = Decimal(str(contract["max_verified_age_days"]))
    kept, dropped = [], []
    for i, offer in enumerate(entries):
        verified_at = parse_utc(offer.get("_verified_at"))
        if verified_at is None:
            dropped.append((i, "缺少或非法 _verified_at"))
            continue
        status = effective_status(offer, contract, now)
        age_days = Decimal(str((now - verified_at).total_seconds())) / Decimal(86400)
        # 已过期/已停止的条目保留在公开 JSON 里并标状态，不受「7 天内复核」约束；
        # 其余状态仍按 7 天阈值强制复核。
        if status not in ("expired", "discontinued") and age_days > max_days:
            dropped.append((i, "核验时间超过 " + str(max_days) + " 天"))
            continue
        if verified_at > now:
            dropped.append((i, "_verified_at 晚于当前时间"))
            continue
        public = strip_private(offer, contract)
        public["status"] = status
        if "deadline" in offer:
            public["deadline"] = offer["deadline"]
        kept.append(public)

    payload = {
        "schema_version": contract["schema_version"],
        "generated_at": now.isoformat(timespec="seconds"),
        "source_note": contract["source_note"],
        "offers": kept,
    }
    return payload, dropped


def verify_payload(payload, contract, now):
    """模拟技能端 fetch_validate.validate_payload 的核心判定，做发布前预检。"""
    if not isinstance(payload, dict):
        raise FeedError("顶层须为对象")
    sv = payload.get("schema_version")
    if not isinstance(sv, str) or not sv.strip():
        raise FeedError("缺少 schema_version")
    if sv.strip().split(".")[0] != contract["schema_major_supported"]:
        raise FeedError("未知 schema 主版本 " + sv.strip().split(".")[0])
    ga = parse_utc(payload.get("generated_at"))
    if ga is None:
        raise FeedError("generated_at 缺失或不是带时区的 ISO-8601 UTC 时间")
    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise FeedError("offers 须为数组")
    problems = []
    for i, offer in enumerate(offers):
        problems.extend(
            p.replace("verified_offers[", "offers[") for p in validate_offer(offer, i, contract)
        )
        for k in offer:
            if k.startswith("_"):
                problems.append("offers[" + str(i) + "] 泄漏私有字段 " + k)
    if problems:
        raise FeedError("产物校验失败：" + "；".join(problems))
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for token in contract["forbidden_substrings"]:
        if token in text:
            raise FeedError("产物出现禁用内容 " + token)
    delta = Decimal(str((now - ga).total_seconds()))
    stale = delta > Decimal(str(contract["stale_threshold_hours"])) * Decimal(3600)
    return {"status": "stale" if stale else "ok", "offers_count": len(offers)}


def write_payload(payload, output_path):
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(p))
    return text


def run_selftest(contract):
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    good = {
        "provider": "示例平台",
        "modality": "image",
        "quota": {"unit": "count", "amount": 10},
        "watermark": "none",
        "commercial_rights": "allowed",
        "queue_speed": "instant",
        "resolution": "2k",
        "_verified_at": "2026-11-13T00:00:00+00:00",
        "_official_homepage_confirmed": True,
    }
    check("合法条目校验通过", validate_offer(good, 0, contract) == [])
    check("私有字段被剥离", "_verified_at" not in strip_private(good, contract))
    for label, bad in (
        ("缺四字段", {k: v for k, v in good.items() if k != "watermark"}),
        ("枚举非法", dict(good, watermark="maybe")),
        ("额度为负", dict(good, quota={"unit": "count", "amount": -1})),
        ("单位非法", dict(good, quota={"unit": "hour", "amount": 1})),
        ("quota 非对象", dict(good, quota="10")),
        ("provider 非字符串", dict(good, provider=123)),
        ("modality 非法", dict(good, modality="audio")),
        ("额度周期非法", dict(good, quota={"unit": "count", "amount": 10, "period": "weekly"})),
        ("额度过 NaN", dict(good, quota={"unit": "count", "amount": float("nan")})),
        ("额度过 Infinity", dict(good, quota={"unit": "count", "amount": float("inf")})),
    ):
        check(label + " 被拒", len(validate_offer(bad, 0, contract)) > 0)

    # 生命周期与折算口径字段
    check("缺 status 默认合法", validate_offer(good, 0, contract) == [])
    check("status=pending_official 合法", validate_offer(dict(good, status="pending_official"), 0, contract) == [])
    check("非法 status 被拒", len(validate_offer(dict(good, status="rumored"), 0, contract)) > 0)
    check("deadline 合法", validate_offer(dict(good, deadline="2026-12-31"), 0, contract) == [])
    check("非法 deadline 被拒", len(validate_offer(dict(good, deadline="2026/12/31"), 0, contract)) > 0)
    check("非法 deadline（不存在的日期）被拒", len(validate_offer(dict(good, deadline="2026-02-30"), 0, contract)) > 0)
    check("money_basis=none 合法", validate_offer(dict(good, money_basis="none"), 0, contract) == [])
    check("非法 money_basis 被拒", len(validate_offer(dict(good, money_basis="market"), 0, contract)) > 0)
    check("face_value 非 credit 被拒", len(validate_offer(dict(good, money_basis="face_value"), 0, contract)) > 0)
    check("credit 单价合法", validate_offer(dict(good, quota={"unit": "credit", "amount": 100, "unit_price_cny": 0.1}), 0, contract) == [])
    check("credit 单价为 0 被拒", len(validate_offer(dict(good, quota={"unit": "credit", "amount": 100, "unit_price_cny": 0}), 0, contract)) > 0)
    for rule in contract.get("rejection_rules", []):
        hit = dict(good, note="说明：" + rule["keywords"][0])
        check("拒绝规则「" + str(rule.get("label")) + "」命中", len(scan_rejections(hit, contract)) > 0)

    now = current_now("2026-11-13T01:00:00+00:00")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        empty = base / "empty.json"
        empty.write_text('{"verified_offers": []}', encoding="utf-8")
        payload, dropped = build(empty, contract, now)
        check("空表合法且 offers 为空", payload["offers"] == [] and dropped == [])
        res = verify_payload(payload, contract, now)
        check("空表通过消费端预检", res["status"] == "ok" and res["offers_count"] == 0)
        t1 = write_payload(payload, base / "a.json")
        t2 = write_payload(payload, base / "b.json")
        check("同输入幂等逐字节一致", t1 == t2)

        one = base / "one.json"
        one.write_text(json.dumps({"verified_offers": [good]}, ensure_ascii=False), encoding="utf-8")
        p2, dropped2 = build(one, contract, now)
        check("合法条目被收录", len(p2["offers"]) == 1 and not dropped2)
        check("公开产物不含私有字段", "_verified_at" not in p2["offers"][0])

        stale_entry = dict(good, _verified_at="2026-01-01T00:00:00+00:00")
        stale_file = base / "stale.json"
        stale_file.write_text(json.dumps({"verified_offers": [stale_entry]}, ensure_ascii=False), encoding="utf-8")
        p3, dropped3 = build(stale_file, contract, now)
        check("过期核验条目被剔除", len(p3["offers"]) == 0 and len(dropped3) == 1)

        legacy_file = base / "legacy.json"
        legacy_file.write_text(json.dumps({"verified_offers": [good]}, ensure_ascii=False), encoding="utf-8")
        p_legacy, _ = build(legacy_file, contract, now)
        check("缺 status 的旧数据按 verified 输出", p_legacy["offers"][0]["status"] == "verified")

        pending_file = base / "pending.json"
        pending_file.write_text(
            json.dumps({"verified_offers": [dict(good, status="pending_official")]}, ensure_ascii=False),
            encoding="utf-8",
        )
        p_pending, dropped_pending = build(pending_file, contract, now)
        check("pending_official 保留并标注", p_pending["offers"][0]["status"] == "pending_official" and not dropped_pending)

        expired_file = base / "expired.json"
        expired_file.write_text(
            json.dumps({"verified_offers": [dict(good, _verified_at="2026-01-01T00:00:00+00:00", deadline="2026-11-01")]}, ensure_ascii=False),
            encoding="utf-8",
        )
        p_expired, dropped_expired = build(expired_file, contract, now)
        check(
            "deadline 过期自动置 expired 且不因 7 天剔除",
            len(p_expired["offers"]) == 1
            and p_expired["offers"][0]["status"] == "expired"
            and p_expired["offers"][0]["deadline"] == "2026-11-01"
            and not dropped_expired,
        )

        rebate_file = base / "rebate.json"
        rebate_file.write_text(
            json.dumps({"verified_offers": [dict(good, note="邀请好友返利，最高 20 元")]}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            build(rebate_file, contract, now)
            check("拒绝规则在编译期生效", False)
        except FeedError:
            check("拒绝规则在编译期生效", True)

        bad_file = base / "bad.json"
        bad_file.write_text(json.dumps({"verified_offers": [dict(good, watermark="maybe")]}, ensure_ascii=False), encoding="utf-8")
        try:
            build(bad_file, contract, now)
            check("非法清单整体中止", False)
        except FeedError:
            check("非法清单整体中止", True)

        leak = base / "leak.json"
        leak.write_text(json.dumps({"verified_offers": [dict(good, note="https://example.invalid/x")]}, ensure_ascii=False), encoding="utf-8")
        try:
            build(leak, contract, now)
            check("外链/引流内容被拦", False)
        except FeedError:
            check("外链/引流内容被拦", True)

        old = dict(payload, generated_at="2026-11-12T00:00:00+00:00")
        check("过期数据被判定 stale", verify_payload(old, contract, now)["status"] == "stale")
        try:
            verify_payload(dict(payload, schema_version="2.0"), contract, now)
            check("未知主版本被拒", False)
        except FeedError:
            check("未知主版本被拒", True)

    failed = [n for n, ok in checks if not ok]
    for name, ok in checks:
        print(("  OK  " if ok else " FAIL ") + name)
    print("SELFTEST " + ("PASS" if not failed else "FAIL") + " (" + str(len(checks) - len(failed)) + "/" + str(len(checks)) + ")")
    return EXIT_OK if not failed else EXIT_FAIL


def run_fixtures(contract):
    now = current_now("2026-11-13T01:00:00+00:00")
    good = {
        "provider": "示例平台",
        "modality": "video",
        "quota": {"unit": "second", "amount": 25},
        "watermark": "visible",
        "commercial_rights": "restricted",
        "queue_speed": "normal",
        "resolution": "720p",
        "_verified_at": "2026-11-13T00:30:00+00:00",
    }
    cases = [
        ("good_empty", {"verified_offers": []}, True),
        ("good_one", {"verified_offers": [good]}, True),
        ("good_unknown_enum", {"verified_offers": [dict(good, commercial_rights="unknown")]}, True),
        ("bad_missing_caveat", {"verified_offers": [dict(good, queue_speed=None)]}, False),
        ("bad_negative", {"verified_offers": [dict(good, quota={"unit": "count", "amount": -5})]}, False),
        ("bad_version", {"verified_offers": [dict(good, modality="text")]}, False),
        ("bad_link", {"verified_offers": [dict(good, note="https://example.invalid")]}, False),
        ("good_period", {"verified_offers": [dict(good, quota={"unit": "count", "amount": 5, "period": "once"})]}, True),
        ("bad_period", {"verified_offers": [dict(good, quota={"unit": "count", "amount": 5, "period": "weekly"})]}, False),
        ("bad_nan", {"verified_offers": [dict(good, quota={"unit": "count", "amount": float("nan")})]}, False),
        ("bad_infinity", {"verified_offers": [dict(good, quota={"unit": "count", "amount": float("inf")})]}, False),
        # 生命周期 / 截止日期
        ("good_default_status", {"verified_offers": [good]}, True),
        ("good_pending_official", {"verified_offers": [dict(good, status="pending_official")]}, True),
        ("good_expired_deadline", {"verified_offers": [dict(good, deadline="2026-11-01")]}, True),
        ("bad_status", {"verified_offers": [dict(good, status="rumored")]}, False),
        ("bad_deadline_format", {"verified_offers": [dict(good, deadline="2026/11/13")]}, False),
        # 折算口径
        ("good_money_basis_none", {"verified_offers": [dict(good, money_basis="none")]}, True),
        ("good_credit_unit_price", {
            "verified_offers": [dict(good, quota={"unit": "credit", "amount": 100, "unit_price_cny": 0.1}, money_basis="unit_price")],
        }, True),
        ("good_credit_no_price", {
            "verified_offers": [dict(good, quota={"unit": "credit", "amount": 100}, money_basis="unit_price")],
        }, True),
        ("bad_money_basis", {"verified_offers": [dict(good, money_basis="market")]}, False),
        ("bad_face_value_unit", {"verified_offers": [dict(good, money_basis="face_value")]}, False),
        # 拒绝规则：每类一条
        ("bad_rebate_invite", {"verified_offers": [dict(good, note="邀请码注册返现 10 元")]}, False),
        ("bad_relay_proxy", {"verified_offers": [dict(good, note="API 中转，价格是官方三折")]}, False),
        ("bad_secondhand_credit", {"verified_offers": [dict(good, note="二手积分转让，量大优惠")]}, False),
        ("bad_sms_multi_account", {"verified_offers": [dict(good, note="支持接码注册，可多开")]}, False),
        ("bad_requires_topup", {"verified_offers": [dict(good, note="需先充值 100 元才送额度")]}, False),
        ("bad_stacking_rule", {"verified_offers": [dict(good, note="新用户券可与活动同享，可叠加")]}, False),
    ]
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for name, doc, expect_ok in cases:
            src = base / (name + ".json")
            src.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            try:
                payload, _ = build(src, contract, now)
                verify_payload(payload, contract, now)
                ok = True
            except FeedError:
                ok = False
            if ok != expect_ok:
                failures.append(name)
            print(("  OK  " if ok == expect_ok else " FAIL ") + name + " expected=" + ("pass" if expect_ok else "reject"))
    print("FIXTURES " + ("PASS" if not failures else "FAIL") + " (" + str(len(cases) - len(failures)) + "/" + str(len(cases)) + ")")
    return EXIT_OK if not failures else EXIT_FAIL


def build_parser():
    p = argparse.ArgumentParser(prog="build_feed.py", description="编译已核验清单为公开 offers.json")
    p.add_argument("--input", default=str(DEFAULT_INPUT))
    p.add_argument("--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--now", default=None, help="注入带时区的 ISO-8601 UTC 时钟，保证可复现")
    p.add_argument("--verify", action="store_true", help="只校验已有产物，不写文件")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--fixtures", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract()
        if args.selftest:
            return run_selftest(contract)
        if args.fixtures:
            return run_fixtures(contract)
        now = current_now(args.now)
        if args.verify:
            try:
                payload = json.loads(Path(args.output).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise FeedError("产物不可读: " + type(e).__name__)
            result = verify_payload(payload, contract, now)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else "VERIFY OK " + json.dumps(result, ensure_ascii=False, sort_keys=True))
            return EXIT_OK
        payload, dropped = build(args.input, contract, now)
        result = verify_payload(payload, contract, now)
        write_payload(payload, args.output)
        out = dict(result, dropped_count=len(dropped), dropped_reasons=[r for _, r in dropped], output=str(Path(args.output).resolve()))
        if args.json:
            print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        else:
            print("BUILD OK offers=" + str(result["offers_count"]) + " dropped=" + str(len(dropped)) + " generated_at=" + payload["generated_at"])
            for i, reason in dropped:
                print("  DROPPED verified_offers[" + str(i) + "] " + reason)
        return EXIT_OK
    except FeedError as e:
        sys.stderr.write("FEED ERROR: " + str(e) + "\n")
        if args.json:
            print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False, sort_keys=True))
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
