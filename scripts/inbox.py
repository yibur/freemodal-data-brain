#!/usr/bin/env python3
"""inbox.py - 线索状态机：unverified -> verifying -> promoted / rejected。

只做状态流转与一致性校验，不写入、不生成任何额度数值：
- 线索条目禁止出现 quota / amount / unit / period 等额度字段；
- promoted 必须对应 sources/offers.json 里一条真实已核验记录（provider + _verified_at 匹配）；
- 本工具不会把线索搬进 sources/offers.json，转正后的公开数据仍来自人工核验的真实记录。

用法：
  python scripts/inbox.py list
  python scripts/inbox.py add --provider 平台名 --claim "听说的内容" --heard-from "来源"
  python scripts/inbox.py status <id> verifying
  python scripts/inbox.py promote <id> --provider 平台名 [--verified-at ISO]
  python scripts/inbox.py check

退出码：0 成功 / 1 校验失败 / 2 用法错误。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INBOX = REPO_ROOT / "sources" / "inbox.json"
DEFAULT_OFFERS = REPO_ROOT / "sources" / "offers.json"

STATUSES = ("unverified", "verifying", "promoted", "rejected")
ENTRY_KEYS = ("id", "provider", "claim", "heard_from", "heard_at", "status", "promoted_to", "note")
VALUE_KEYS = ("quota", "amount", "unit", "period", "price", "unit_price_cny", "money_basis", "credit")
MAX_CLAIM_LEN = 500

ALLOWED_TRANSITIONS = {
    "unverified": {"verifying", "rejected"},
    "verifying": {"unverified", "rejected"},
    "promoted": set(),
    "rejected": {"unverified"},
}


class InboxError(Exception):
    pass


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def load_doc(path):
    p = Path(path)
    if not p.is_file():
        raise InboxError("线索文件不存在: " + str(p))
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InboxError("线索文件不可读或不是合法 JSON: " + type(exc).__name__)
    if not isinstance(doc, dict) or not isinstance(doc.get("inbox"), list):
        raise InboxError("线索文件顶层须为对象且 inbox 须为数组")
    return doc


def save_doc(path, doc):
    p = Path(path)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(p))


def load_offers(path):
    p = Path(path)
    if not p.is_file():
        raise InboxError("正式清单不存在: " + str(p))
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InboxError("正式清单不可读或不是合法 JSON: " + type(exc).__name__)
    if not isinstance(doc, dict) or not isinstance(doc.get("verified_offers"), list):
        raise InboxError("正式清单顶层须为对象且 verified_offers 须为数组")
    return doc


def build_verified_index(offers_doc):
    """provider -> [(原始 _verified_at 字符串, 解析后的 datetime)]"""
    index = {}
    for offer in offers_doc.get("verified_offers", []):
        if not isinstance(offer, dict):
            continue
        provider = offer.get("provider")
        verified_at = offer.get("_verified_at")
        parsed = parse_iso(verified_at)
        if not isinstance(provider, str) or parsed is None:
            continue
        index.setdefault(provider, []).append((verified_at, parsed))
    return index


def validate_entry(entry, idx, verified_index):
    problems = []
    prefix = "inbox[" + str(idx) + "]"
    if not isinstance(entry, dict):
        return [prefix + " 不是对象"]

    for key in entry:
        if key in VALUE_KEYS:
            problems.append(prefix + " 出现额度数值字段 " + key + "（线索区禁止携带额度数值）")
        elif key not in ENTRY_KEYS:
            problems.append(prefix + " 出现未知字段 " + key)

    for field in ("id", "provider", "claim", "heard_from", "heard_at", "status"):
        if field not in entry:
            problems.append(prefix + " 缺少必填字段 " + field)

    for field in ("id", "provider", "claim", "heard_from"):
        value = entry.get(field)
        if field in entry and (not isinstance(value, str) or not value.strip()):
            problems.append(prefix + "." + field + " 须为非空字符串")

    claim = entry.get("claim")
    if isinstance(claim, str) and len(claim) > MAX_CLAIM_LEN:
        problems.append(prefix + ".claim 超过 " + str(MAX_CLAIM_LEN) + " 字符")

    if "heard_at" in entry and parse_date(entry.get("heard_at")) is None:
        problems.append(prefix + ".heard_at 须为合法的 YYYY-MM-DD 日期")

    status = entry.get("status")
    if "status" in entry and status not in STATUSES:
        problems.append(prefix + ".status 取值非法: " + str(status))

    if "note" in entry and not isinstance(entry.get("note"), str):
        problems.append(prefix + ".note 须为字符串")

    promoted_to = entry.get("promoted_to")
    if "promoted_to" in entry and status != "promoted":
        problems.append(prefix + " 只有 status=promoted 才能带 promoted_to")

    if status == "promoted":
        if not isinstance(promoted_to, dict):
            problems.append(prefix + " status=promoted 但缺少 promoted_to")
            return problems
        target_provider = promoted_to.get("provider")
        target_verified = parse_iso(promoted_to.get("verified_at"))
        if not isinstance(target_provider, str) or not target_provider.strip():
            problems.append(prefix + ".promoted_to.provider 须为非空字符串")
            return problems
        if target_verified is None:
            problems.append(prefix + ".promoted_to.verified_at 须为带时区的 ISO-8601 时间")
            return problems
        candidates = verified_index.get(target_provider.strip(), [])
        if not candidates:
            problems.append(
                prefix
                + " status=promoted，但 sources/offers.json 里没有 provider="
                + target_provider.strip()
                + " 的已核验记录。请先人工核验并写入正式清单；本工具不会生成额度数值。"
            )
        elif not any(parsed == target_verified for _, parsed in candidates):
            available = ", ".join(raw for raw, _ in candidates)
            problems.append(
                prefix
                + ".promoted_to.verified_at 对不上正式清单（可用值："
                + available
                + "）"
            )
    return problems


def validate_doc(doc, offers_doc):
    verified_index = build_verified_index(offers_doc)
    entries = doc.get("inbox", [])
    problems = []
    seen = set()
    counts = {status: 0 for status in STATUSES}
    for idx, entry in enumerate(entries):
        problems.extend(validate_entry(entry, idx, verified_index))
        if isinstance(entry, dict):
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id.strip():
                if entry_id in seen:
                    problems.append("inbox[" + str(idx) + "] id 重复: " + entry_id)
                seen.add(entry_id)
            status = entry.get("status")
            if status in counts:
                counts[status] += 1
    summary = {
        "total": len(entries),
        "counts": counts,
        "promoted_ok": counts["promoted"] if not problems else 0,
        "verified_providers": len(verified_index),
    }
    return problems, summary


def find_entry(doc, entry_id):
    for entry in doc.get("inbox", []):
        if isinstance(entry, dict) and entry.get("id") == entry_id:
            return entry
    return None


def cmd_add(args, doc, offers_doc):
    now = datetime.now(timezone.utc)
    entry = {
        "id": "L" + now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2),
        "provider": args.provider.strip(),
        "claim": args.claim.strip(),
        "heard_from": args.heard_from.strip(),
        "heard_at": args.heard_at or now.date().isoformat(),
        "status": "unverified",
    }
    if args.note:
        entry["note"] = args.note
    doc["inbox"].append(entry)
    problems, _ = validate_doc(doc, offers_doc)
    if problems:
        doc["inbox"].pop()
        raise InboxError("新增线索未通过校验：" + "；".join(problems))
    save_doc(args.inbox, doc)
    print("已记录线索 " + entry["id"] + "（status=unverified）。它不会进入公开 offers.json。")
    return 0


def cmd_status(args, doc, offers_doc):
    entry = find_entry(doc, args.id)
    if entry is None:
        raise InboxError("找不到线索 " + args.id)
    current = entry.get("status")
    target = args.new_status
    if target == "promoted":
        raise InboxError("转正请用 promote 子命令；它要求正式清单里已有对应真实记录")
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        available = ", ".join(sorted(ALLOWED_TRANSITIONS.get(current, set()))) or "无"
        raise InboxError("不允许从 " + str(current) + " 转到 " + target + "；可用：" + available)
    entry["status"] = target
    problems, _ = validate_doc(doc, offers_doc)
    if problems:
        entry["status"] = current
        raise InboxError("状态变更未通过校验：" + "；".join(problems))
    save_doc(args.inbox, doc)
    print("线索 " + args.id + "：" + str(current) + " -> " + target)
    return 0


def cmd_promote(args, doc, offers_doc):
    entry = find_entry(doc, args.id)
    if entry is None:
        raise InboxError("找不到线索 " + args.id)
    if entry.get("status") not in ("unverified", "verifying"):
        raise InboxError("只有 unverified / verifying 的线索可以转正，当前为 " + str(entry.get("status")))

    provider = args.provider.strip()
    candidates = [
        offer
        for offer in offers_doc.get("verified_offers", [])
        if isinstance(offer, dict) and offer.get("provider") == provider and parse_iso(offer.get("_verified_at")) is not None
    ]
    if not candidates:
        raise InboxError(
            "sources/offers.json 里没有 provider="
            + provider
            + " 的已核验记录。请先人工核验并写入正式清单（含 _verified_at）；本工具不会生成任何额度数值。"
        )
    if args.verified_at:
        wanted = parse_iso(args.verified_at)
        if wanted is None:
            raise InboxError("--verified-at 须为带时区的 ISO-8601 时间")
        candidates = [offer for offer in candidates if parse_iso(offer["_verified_at"]) == wanted]
        if not candidates:
            raise InboxError("provider=" + provider + " 没有 _verified_at=" + args.verified_at + " 的记录")
    elif len(candidates) > 1:
        values = ", ".join(str(offer["_verified_at"]) for offer in candidates)
        raise InboxError("provider=" + provider + " 有多条记录，请用 --verified-at 指定其中一条：" + values)

    chosen = candidates[0]
    entry["status"] = "promoted"
    entry["promoted_to"] = {"provider": provider, "verified_at": chosen["_verified_at"]}
    problems, _ = validate_doc(doc, offers_doc)
    if problems:
        entry["status"] = "unverified"
        entry.pop("promoted_to", None)
        raise InboxError("转正未通过校验：" + "；".join(problems))
    save_doc(args.inbox, doc)
    print(
        "线索 "
        + args.id
        + " 已置为 promoted，指向正式清单中的 "
        + provider
        + "（_verified_at="
        + str(chosen["_verified_at"])
        + "）。公开数据仍来自该真实记录，本次未写入任何额度数值。"
    )
    return 0


def cmd_list(args, doc, offers_doc):
    problems, summary = validate_doc(doc, offers_doc)
    entries = doc.get("inbox", [])
    if not entries:
        print("线索区为空。")
    for entry in entries:
        if not isinstance(entry, dict):
            print("  <非法条目>")
            continue
        claim = str(entry.get("claim", ""))
        if len(claim) > 40:
            claim = claim[:40] + "…"
        print(
            "  "
            + str(entry.get("id", "?"))
            + "  "
            + str(entry.get("status", "?"))
            + "  "
            + str(entry.get("provider", "?"))
            + "  heard_at="
            + str(entry.get("heard_at", "?"))
            + "  "
            + claim
        )
    counts = summary["counts"]
    print(
        "合计 "
        + str(summary["total"])
        + " 条：unverified="
        + str(counts["unverified"])
        + " verifying="
        + str(counts["verifying"])
        + " promoted="
        + str(counts["promoted"])
        + " rejected="
        + str(counts["rejected"])
    )
    if problems:
        for problem in problems:
            print("  PROBLEM " + problem)
        return 1
    return 0


def cmd_check(args, doc, offers_doc):
    problems, summary = validate_doc(doc, offers_doc)
    counts = summary["counts"]
    if problems:
        print("INBOX FAIL (" + str(len(problems)) + " 个问题)")
    else:
        print(
            "INBOX OK total="
            + str(summary["total"])
            + " unverified="
            + str(counts["unverified"])
            + " verifying="
            + str(counts["verifying"])
            + " promoted="
            + str(counts["promoted"])
            + " rejected="
            + str(counts["rejected"])
        )
    for problem in problems:
        print("  PROBLEM " + problem)
    return 0 if not problems else 1


def build_parser():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--inbox", default=str(DEFAULT_INBOX), help="线索文件路径，默认 sources/inbox.json")
    parent.add_argument("--offers", default=str(DEFAULT_OFFERS), help="正式清单路径，默认 sources/offers.json")

    parser = argparse.ArgumentParser(
        prog="inbox.py",
        description="线索状态机（只做状态流转与校验，不生成额度数值）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", parents=[parent], help="列出线索与状态统计")
    sub.add_parser("check", parents=[parent], help="只校验，不修改")

    add = sub.add_parser("add", parents=[parent], help="新增一条 unverified 线索")
    add.add_argument("--provider", required=True)
    add.add_argument("--claim", required=True)
    add.add_argument("--heard-from", required=True, dest="heard_from")
    add.add_argument("--heard-at", default=None, dest="heard_at")
    add.add_argument("--note", default=None)

    status = sub.add_parser("status", parents=[parent], help="流转到 verifying / rejected / unverified")
    status.add_argument("id")
    status.add_argument("new_status", choices=("verifying", "rejected", "unverified"))

    promote = sub.add_parser("promote", parents=[parent], help="转正：要求正式清单已有对应真实记录")
    promote.add_argument("id")
    promote.add_argument("--provider", required=True)
    promote.add_argument("--verified-at", default=None, dest="verified_at")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        doc = load_doc(args.inbox)
        offers_doc = load_offers(args.offers)
        handlers = {
            "list": cmd_list,
            "check": cmd_check,
            "add": cmd_add,
            "status": cmd_status,
            "promote": cmd_promote,
        }
        return handlers[args.cmd](args, doc, offers_doc)
    except InboxError as exc:
        sys.stderr.write("INBOX ERROR: " + str(exc) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
