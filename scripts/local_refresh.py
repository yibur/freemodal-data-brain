#!/usr/bin/env python3
"""local_refresh.py - 本机刷新链路：拉线上 -> 校验 -> 编译 -> 输出 ->（可选）发布。

为什么存在：GitHub Actions 的 schedule 当前不触发，github.com:443 又间歇不通，
直连 git push 不可靠。本脚本用 api.github.com 提交数据，因此不依赖 443 上的 git 传输。

做三件事，全程不采集、不生成任何额度数值：
1. 拉取线上 offers.json（Pages -> jsDelivr gcore -> jsDelivr fastly -> raw 四源回退），
   校验 schema 与 generated_at 老化程度；
2. 用 build_feed 的契约编译 sources/offers.json，写 out/offers.json 与 out/refresh-report.json；
3. --publish 时经 GitHub API（Git Data API）提交 sources/offers.json（有变化时）与 offers.json。

线索区 sources/inbox.json 只做状态一致性校验（promoted 必须对得上真实已核验记录），
不会从线索生成任何额度数值。每次刷新还会顺带跑一次 scripts/discover_leads.py 做候选
线索发现（--no-discover 可关闭）；发现失败只记警告，不改变退出码。

退出码：
  0 成功
  1 校验或编译失败（含自检失败、发布被拒）
  2 用法错误
  3 数据老化告警（线上数据超过 --stale-days 天未更新，且本次未发布修复）
  4 缺少可用凭证（仅 --publish）
  5 网络不可达（线上四源与发布接口都不可用）
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import base64
import contextlib
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_feed as bf  # noqa: E402
import inbox as inbox_mod  # noqa: E402

CONTRACT_PATH = REPO_ROOT / "schema" / "contract.json"
SOURCES_PATH = REPO_ROOT / "sources" / "offers.json"
INBOX_PATH = REPO_ROOT / "sources" / "inbox.json"
LOCAL_PUBLISHED = REPO_ROOT / "offers.json"
DEFAULT_OUT_DIR = REPO_ROOT / "out"
DEFAULT_LOG = REPO_ROOT / "logs" / "refresh.log"
DISCOVER_SCRIPT = SCRIPTS_DIR / "discover_leads.py"
DEFAULT_DISCOVER_SECONDS = 120.0

DEFAULT_SLUG = "yibur/freemodal-data-brain"
DEFAULT_STALE_DAYS = 3.0
LIVE_TIMEOUT = 4.0
LIVE_BUDGET = 11.0
API_TIMEOUT = 25.0

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_STALE, EXIT_NO_CRED, EXIT_NET = 0, 1, 2, 3, 4, 5

STATUS_BY_EXIT = {
    EXIT_OK: "ok",
    EXIT_FAIL: "error",
    EXIT_USAGE: "usage",
    EXIT_STALE: "stale",
    EXIT_NO_CRED: "no_credentials",
    EXIT_NET: "network_unreachable",
}


class RefreshError(Exception):
    def __init__(self, message, exit_code=EXIT_FAIL):
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------- 线上拉取

def live_sources(slug):
    owner, repo = slug.split("/", 1)
    return [
        ("pages", "https://" + owner + ".github.io/" + repo + "/offers.json"),
        ("jsdelivr-gcore", "https://gcore.jsdelivr.net/gh/" + slug + "@main/offers.json"),
        ("jsdelivr-fastly", "https://fastly.jsdelivr.net/gh/" + slug + "@main/offers.json"),
        ("raw", "https://raw.githubusercontent.com/" + slug + "/main/offers.json"),
    ]


def fetch_remote(sources, timeout_per=LIVE_TIMEOUT, budget=LIVE_BUDGET):
    started = time.monotonic()
    attempts = []
    for name, url in sources:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0.5:
            attempts.append({"source": name, "url": url, "error": "整轮预算用尽，未尝试"})
            continue
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "freemodal-local-refresh/1.0",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=min(timeout_per, remaining)) as resp:
                raw = resp.read(4 * 1024 * 1024)
            return {"source": name, "url": url, "text": raw.decode("utf-8"), "attempts": attempts}
        except Exception as exc:  # noqa: BLE001 - 逐源失败要继续回退
            attempts.append({"source": name, "url": url, "error": type(exc).__name__ + ": " + str(exc)[:160]})
    return {"source": None, "url": None, "text": None, "attempts": attempts}


def read_file_source(path, label):
    p = Path(path)
    if not p.is_file():
        return {
            "source": None,
            "url": None,
            "text": None,
            "attempts": [{"source": label, "error": "文件不存在: " + str(p)}],
        }
    try:
        return {"source": label + "(" + str(p) + ")", "url": None, "text": p.read_text(encoding="utf-8"), "attempts": []}
    except OSError as exc:
        return {
            "source": None,
            "url": None,
            "text": None,
            "attempts": [{"source": label, "error": type(exc).__name__ + ": " + str(exc)[:160]}],
        }


def load_live(args, slug):
    if args.live_source == "auto":
        return dict(fetch_remote(live_sources(slug)), mode="remote")
    if args.live_source == "offline":
        return dict(read_file_source(LOCAL_PUBLISHED, "offline-local"), mode="offline")
    return dict(read_file_source(Path(args.live_source), "file"), mode="file")


def inspect_live(fetched, contract, now):
    info = {
        "source": fetched.get("source"),
        "mode": fetched.get("mode"),
        "ok": False,
        "attempts": fetched.get("attempts", []),
    }
    text = fetched.get("text")
    if not text:
        info["error"] = "线上数据不可达：四源都失败"
        return info
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        info["error"] = "线上 JSON 不合法: " + str(exc)
        return info
    try:
        result = bf.verify_payload(payload, contract, now)
    except bf.FeedError as exc:
        info["error"] = "线上产物校验失败: " + str(exc)
        return info
    generated_at = bf.parse_utc(payload.get("generated_at"))
    age_hours = (now - generated_at).total_seconds() / 3600.0
    info.update(
        {
            "ok": True,
            "generated_at": payload.get("generated_at"),
            "age_hours": round(age_hours, 2),
            "age_days": round(age_hours / 24.0, 2),
            "offers_count": result["offers_count"],
            "consumer_stale": age_hours > float(contract["stale_threshold_hours"]),
        }
    )
    return info


def measure_aging(live_info, now, stale_days):
    """优先用线上 generated_at；线上不可达时回退到本地已发布文件，并标明来源。"""
    if live_info.get("ok"):
        age_days = live_info["age_days"]
        return {
            "measured": True,
            "measured_from": "live:" + str(live_info.get("source")),
            "age_days": age_days,
            "age_hours": live_info.get("age_hours"),
            "threshold_days": stale_days,
            "alarm": age_days > stale_days,
        }
    if LOCAL_PUBLISHED.is_file():
        try:
            doc = json.loads(LOCAL_PUBLISHED.read_text(encoding="utf-8"))
            generated_at = bf.parse_utc(doc.get("generated_at"))
            if generated_at is not None:
                age_days = (now - generated_at).total_seconds() / 86400.0
                return {
                    "measured": True,
                    "measured_from": "local:" + str(LOCAL_PUBLISHED) + "（线上不可达，回退）",
                    "age_days": round(age_days, 2),
                    "age_hours": round(age_days * 24.0, 2),
                    "threshold_days": stale_days,
                    "alarm": age_days > stale_days,
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "measured": False,
        "measured_from": None,
        "age_days": None,
        "age_hours": None,
        "threshold_days": stale_days,
        "alarm": False,
    }


# --------------------------------------------------------------------------- 编译与校验

def check_inbox(offers_doc):
    doc = inbox_mod.load_doc(INBOX_PATH)
    problems, summary = inbox_mod.validate_doc(doc, offers_doc)
    return problems, summary


def compile_feed(contract, now):
    try:
        sources_doc = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshError("sources/offers.json 不可读或不是合法 JSON: " + type(exc).__name__)
    payload, dropped = bf.build(SOURCES_PATH, contract, now)
    result = bf.verify_payload(payload, contract, now)
    source_count = len(sources_doc.get("verified_offers", []))
    if source_count and not payload["offers"]:
        raise RefreshError(
            "源清单有 "
            + str(source_count)
            + " 条，但编译后 0 条：全部超出 "
            + str(contract["max_verified_age_days"])
            + " 天复核期或 _verified_at 异常。已拒绝输出空数据，需要人工复核 sources/offers.json。"
        )
    return sources_doc, payload, result, dropped, source_count


def run_publish_gate(contract):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_selftest = bf.run_selftest(contract)
        rc_fixtures = bf.run_fixtures(contract)
    text = buf.getvalue()
    if rc_selftest != 0 or rc_fixtures != 0:
        fails = [line.strip() for line in text.splitlines() if "FAIL" in line][:6]
        raise RefreshError("发布前自检未通过，已中止：" + "；".join(fails))
    return {
        "selftest": "PASS" if rc_selftest == 0 else "FAIL",
        "fixtures": "PASS" if rc_fixtures == 0 else "FAIL",
    }


# --------------------------------------------------------------------------- 凭证与 GitHub API

def repo_slug():
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        url = proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        url = ""
    match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", url)
    if match:
        return match.group(1) + "/" + match.group(2)
    return DEFAULT_SLUG


def credential_token():
    """按 环境变量 -> gh 登录态 -> git credential helper 的顺序找 token。不打印 token 本身。"""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, "环境变量 " + name

    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        token = proc.stdout.strip()
        if proc.returncode == 0 and token:
            return token, "gh auth token"
    except (OSError, subprocess.TimeoutExpired):
        pass

    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "git credential fill 不可用: " + type(exc).__name__
    if proc.returncode != 0:
        return None, "git credential fill 退出码 " + str(proc.returncode)
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            token = line[len("password="):].strip()
            if token:
                return token, "git credential（Windows 凭据管理器）"
    return None, "git credential 中没有 password"


def api(method, path, token, body=None, timeout=API_TIMEOUT):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "freemodal-local-refresh/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        if exc.code in (401, 403):
            raise RefreshError(
                "GitHub API 返回 "
                + str(exc.code)
                + "（凭证无效或权限不足），已中止。请更新 Windows 凭据管理器里的 GitHub 凭据后重试，不要用无效凭证反复试。详情："
                + detail
            )
        raise RefreshError("GitHub API " + method + " " + path + " 返回 " + str(exc.code) + "：" + detail)
    except urllib.error.URLError as exc:
        raise RefreshError("GitHub API 不可达：" + str(exc.reason) + "（api.github.com 也不通时无法发布）", EXIT_NET)
    except TimeoutError:
        raise RefreshError("GitHub API 请求超时", EXIT_NET)


def git_show(spec):
    try:
        proc = subprocess.run(
            ["git", "show", spec],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def publish_data(payload, token, slug, now):
    """用 Git Data API 基于远端最新 HEAD 提交；不覆盖别人已推的 sources。"""
    info = {"ok": False}
    head = api("GET", "/repos/" + slug + "/git/ref/heads/main", token)
    head_sha = head["object"]["sha"]
    info["head_before"] = head_sha
    commit = api("GET", "/repos/" + slug + "/git/commits/" + head_sha, token)
    base_tree = commit["tree"]["sha"]

    files = []
    local_sources = SOURCES_PATH.read_bytes()
    remote_contents = api("GET", "/repos/" + slug + "/contents/sources/offers.json?ref=main", token)
    remote_sources = base64.b64decode(remote_contents.get("content", ""))
    if remote_sources != local_sources:
        head_version = git_show("HEAD:sources/offers.json")
        if head_version is None or head_version != remote_sources:
            raise RefreshError(
                "远端 sources/offers.json 与本地基线不一致（可能有人在你之后推过）。"
                "为避免覆盖，已中止发布；请先 git pull 对齐后重试。"
            )
        files.append(("sources/offers.json", local_sources))

    offers_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    files.append(("offers.json", offers_text.encode("utf-8")))

    tree_entries = []
    for path, content in files:
        blob = api(
            "POST",
            "/repos/" + slug + "/git/blobs",
            token,
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree = api("POST", "/repos/" + slug + "/git/trees", token, {"base_tree": base_tree, "tree": tree_entries})
    message = (
        "chore: local refresh offers feed " + now.strftime("%Y-%m-%dT%H:%MZ") + " [skip ci]\n\n"
        "由本机刷新脚本编译并发布；只刷新 generated_at，未改动任何额度数值。"
    )
    created = api(
        "POST",
        "/repos/" + slug + "/git/commits",
        token,
        {"message": message, "tree": tree["sha"], "parents": [head_sha]},
    )
    api("PATCH", "/repos/" + slug + "/git/refs/heads/main", token, {"sha": created["sha"], "force": False})

    check = api("GET", "/repos/" + slug + "/contents/offers.json?ref=main", token)
    published = json.loads(base64.b64decode(check.get("content", "")).decode("utf-8"))
    if published.get("generated_at") != payload.get("generated_at"):
        raise RefreshError("发布后回读校验失败：远端 generated_at 与本次编译结果不一致")

    info.update(
        {
            "ok": True,
            "commit": created["sha"],
            "files": [path for path, _ in files],
            "verified_generated_at": published.get("generated_at"),
            "verified_offers_count": len(published.get("offers", [])),
        }
    )
    return info


# --------------------------------------------------------------------------- 线索发现

def run_discovery(max_seconds):
    """顺带跑一次候选线索发现。任何失败都只记入报告，不改变刷新退出码。"""
    info = {"enabled": True, "ok": False, "max_seconds": max_seconds}
    started = time.monotonic()
    if not DISCOVER_SCRIPT.is_file():
        info.update(error="脚本不存在: " + str(DISCOVER_SCRIPT))
        info["duration_seconds"] = round(time.monotonic() - started, 2)
        return info
    cmd = [sys.executable, str(DISCOVER_SCRIPT), "--max-seconds", str(max_seconds), "--json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(max_seconds) + 20.0,
        )
    except subprocess.TimeoutExpired:
        info.update(error="发现脚本超过 " + str(int(float(max_seconds) + 20)) + "s 未返回，已跳过")
        info["duration_seconds"] = round(time.monotonic() - started, 2)
        return info
    except Exception as exc:  # noqa: BLE001
        info.update(error=type(exc).__name__ + ": " + str(exc)[:160])
        info["duration_seconds"] = round(time.monotonic() - started, 2)
        return info

    info["exit_code"] = proc.returncode
    info["duration_seconds"] = round(time.monotonic() - started, 2)
    parsed = None
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("{"):
            try:
                parsed = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
    if parsed is None:
        info.update(error="发现脚本输出无法解析（exit=" + str(proc.returncode) + "）")
        return info
    info["ok"] = bool(parsed.get("ok")) and proc.returncode == 0
    for key in (
        "seed_total",
        "seed_domestic",
        "seed_overseas",
        "scanned",
        "reachable",
        "keyword_hit",
        "candidates",
        "duplicates",
        "listed",
        "deadline_hit",
    ):
        if key in parsed:
            info[key] = parsed[key]
    info["error"] = str(parsed.get("error") or "")
    return info


# --------------------------------------------------------------------------- 主流程

def summarize_report(report):
    live = report.get("live", {})
    build = report.get("build", {})
    inbox = report.get("inbox", {}).get("counts", {})
    aging = report.get("aging", {})
    lines = []
    if live.get("ok"):
        lines.append(
            "  线上      : "
            + str(live.get("source"))
            + "（age "
            + str(live.get("age_hours"))
            + "h，"
            + str(live.get("offers_count"))
            + " 条）"
        )
    else:
        lines.append("  线上      : 不可达（" + str(live.get("error")) + "）")
    lines.append(
        "  编译      : "
        + str(build.get("offers"))
        + " 条，剔除 "
        + str(build.get("dropped_count"))
        + " 条 -> "
        + str(build.get("output"))
    )
    lines.append(
        "  线索      : unverified="
        + str(inbox.get("unverified", 0))
        + " verifying="
        + str(inbox.get("verifying", 0))
        + " promoted="
        + str(inbox.get("promoted", 0))
        + " rejected="
        + str(inbox.get("rejected", 0))
    )
    discovery = report.get("discovery")
    if discovery and discovery.get("enabled"):
        if discovery.get("ok"):
            lines.append(
                "  发现      : 新增线索 "
                + str(discovery.get("candidates"))
                + " 条（命中 "
                + str(discovery.get("keyword_hit"))
                + "，重复 "
                + str(discovery.get("duplicates"))
                + "，覆盖 "
                + str(discovery.get("reachable"))
                + "/"
                + str(discovery.get("seed_total"))
                + " 家，耗时 "
                + str(discovery.get("duration_seconds"))
                + "s）"
            )
        else:
            lines.append("  发现      : 未完成（" + str(discovery.get("error") or discovery.get("exit_code")) + "），已跳过")
    if aging.get("measured"):
        lines.append(
            "  老化      : "
            + str(aging.get("age_days"))
            + " 天 / 阈值 "
            + str(aging.get("threshold_days"))
            + " 天"
            + ("（告警）" if aging.get("alarm") else "（正常）")
            + "，来源 "
            + str(aging.get("measured_from"))
        )
    else:
        lines.append("  老化      : 无法测量（线上与本地都没有可读的 generated_at）")
    publish = report.get("publish")
    if publish:
        lines.append("  发布      : " + ("commit " + str(publish.get("commit")) + " " + str(publish.get("files")) if publish.get("ok") else "失败"))
    lines.append("  结果      : " + str(report.get("status")))
    return "\n".join(lines)


def run(args, report):
    now = bf.current_now(args.now)
    contract = bf.load_contract()
    slug = repo_slug()

    report["run_at"] = now.isoformat(timespec="seconds")
    report["repo"] = slug
    report["options"] = {
        "publish": bool(args.publish),
        "live_source": args.live_source,
        "stale_days": args.stale_days,
        "out_dir": str(Path(args.out_dir).resolve()),
    }

    warnings = report.setdefault("warnings", [])

    # 1. 线索区一致性（只校验状态，不生成额度）
    sources_doc = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    inbox_problems, inbox_summary = check_inbox(sources_doc)
    report["inbox"] = inbox_summary
    if inbox_problems:
        raise RefreshError("sources/inbox.json 校验失败：" + "；".join(inbox_problems))

    # 2. 拉线上并校验
    fetched = load_live(args, slug)
    live_info = inspect_live(fetched, contract, now)
    report["live"] = live_info
    if not live_info.get("ok"):
        warnings.append("线上数据不可达，本次只完成本地编译；线上状态未确认。")

    # 3. 编译
    _, payload, result, dropped, source_count = compile_feed(contract, now)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "offers.json"
    bf.write_payload(payload, output_path)
    report["build"] = {
        "source_count": source_count,
        "offers": result["offers_count"],
        "dropped_count": len(dropped),
        "dropped": [{"index": index, "reason": reason} for index, reason in dropped],
        "generated_at": payload["generated_at"],
        "output": str(output_path.resolve()),
    }

    # 4. 老化告警
    aging = measure_aging(live_info, now, args.stale_days)
    report["aging"] = aging
    if aging.get("measured") and aging.get("alarm"):
        warnings.append(
            "数据老化告警：线上数据已 "
            + str(aging["age_days"])
            + " 天未更新（阈值 "
            + str(args.stale_days)
            + " 天）。本机计划任务可能没有运行、发布失败，或本机长时间关机。"
        )

    # 5. 候选线索发现（顺带跑一次；失败只记警告，不影响刷新主流程）
    if not args.no_discover:
        discovery = run_discovery(args.discover_max_seconds)
        report["discovery"] = discovery
        if not discovery.get("ok"):
            warnings.append(
                "候选线索发现未完成："
                + str(discovery.get("error") or discovery.get("exit_code") or "未知原因")
                + "（不影响本次刷新结果）"
            )

    # 6. 发布
    if args.publish:
        report["gate"] = run_publish_gate(contract)
        token, token_source = credential_token()
        if not token:
            raise RefreshError(
                "缺少可用凭证，无法自动发布。需要：一个对 "
                + slug
                + " 有 contents:write 权限的 GitHub token。"
                + "获取方式：GitHub -> Settings -> Developer settings -> Personal access tokens；"
                + "放到任意一处：环境变量 GITHUB_TOKEN / GH_TOKEN，或 git credential 里 host=github.com 的 password。"
                + "当前探测结果："
                + token_source,
                EXIT_NO_CRED,
            )
        report["credential_source"] = token_source
        publish_info = publish_data(payload, token, slug, now)
        if aging.get("measured") and aging.get("alarm"):
            publish_info["pre_publish_age_days"] = aging["age_days"]
            warnings.append("发布前线上数据已老化，本次发布已把 generated_at 刷新到最新。")
        report["publish"] = publish_info
        return EXIT_OK, "发布成功 commit=" + publish_info["commit"]

    if aging.get("measured") and aging.get("alarm"):
        return EXIT_STALE, "数据老化告警：线上数据已 " + str(aging["age_days"]) + " 天未更新（阈值 " + str(args.stale_days) + " 天）"

    if not live_info.get("ok"):
        return EXIT_NET, "线上四源都不可达，只完成本地编译（out/offers.json），线上状态未确认"

    return EXIT_OK, "本地刷新完成，未发布（如需发布加 --publish）"


def append_log(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        sys.stderr.write("WARN: 写日志失败: " + type(exc).__name__ + "\n")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="local_refresh.py",
        description="本机刷新链路：拉线上 -> 校验 -> 编译 -> 输出 ->（可选）发布",
    )
    parser.add_argument("--publish", action="store_true", help="经 GitHub API 提交 sources/offers.json（有变化时）与 offers.json")
    parser.add_argument("--live-source", default="auto", help="auto（默认，四源回退）/ offline（读本地 offers.json）/ 指定文件路径")
    parser.add_argument("--stale-days", type=float, default=DEFAULT_STALE_DAYS, help="数据老化告警阈值，默认 3 天")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="输出目录，默认 out/")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="追加日志路径，默认 logs/refresh.log")
    parser.add_argument("--now", default=None, help="注入带时区的 ISO-8601 时间，便于复现测试")
    parser.add_argument("--no-discover", action="store_true", help="本次不跑候选线索发现（默认每次刷新顺带跑一次）")
    parser.add_argument("--discover-max-seconds", type=float, default=DEFAULT_DISCOVER_SECONDS, help="单次线索发现的时长上限，默认 60 秒")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = {
        "status": "error",
        "exit_code": EXIT_FAIL,
        "message": "",
        "warnings": [],
    }
    exit_code, message = EXIT_FAIL, ""
    started = time.monotonic()
    try:
        exit_code, message = run(args, report)
    except RefreshError as exc:
        exit_code, message = exc.exit_code, str(exc)
    except bf.FeedError as exc:
        exit_code, message = exc.exit_code, str(exc)
    except (OSError, json.JSONDecodeError) as exc:
        exit_code, message = EXIT_FAIL, type(exc).__name__ + ": " + str(exc)

    report["exit_code"] = exit_code
    report["message"] = message
    report["status"] = STATUS_BY_EXIT.get(exit_code, "error")
    report["duration_seconds"] = round(time.monotonic() - started, 2)

    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        bf.write_payload(report, out_dir / "refresh-report.json")
    except OSError as exc:
        sys.stderr.write("WARN: 写报告失败: " + type(exc).__name__ + "\n")

    append_log(
        Path(args.log),
        {
            "ts": report.get("run_at"),
            "status": report["status"],
            "exit_code": exit_code,
            "message": message,
            "live": {
                "source": report.get("live", {}).get("source"),
                "age_hours": report.get("live", {}).get("age_hours"),
                "ok": report.get("live", {}).get("ok"),
            },
            "build": {
                "offers": report.get("build", {}).get("offers"),
                "dropped": report.get("build", {}).get("dropped_count"),
            },
            "aging": report.get("aging", {}),
            "discovery": {
                "ok": report.get("discovery", {}).get("ok"),
                "candidates": report.get("discovery", {}).get("candidates"),
                "duplicates": report.get("discovery", {}).get("duplicates"),
                "reachable": report.get("discovery", {}).get("reachable"),
                "seed_total": report.get("discovery", {}).get("seed_total"),
                "error": report.get("discovery", {}).get("error"),
            },
            "publish": report.get("publish"),
            "credential_source": report.get("credential_source"),
        },
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("[local-refresh] " + str(report.get("run_at")))
        print(summarize_report(report))
        for warning in report.get("warnings", []):
            print("  WARN      : " + warning)
        if message:
            print("  说明      : " + message)
        if exit_code != EXIT_OK:
            sys.stderr.write("LOCAL REFRESH EXIT " + str(exit_code) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
