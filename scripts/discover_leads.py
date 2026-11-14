"""discover_leads.py - 候选线索发现：官方入口页关键词扫描 -> sources/inbox.json（unverified）。

为什么存在：sources/inbox.json 的三态流转与白名单校验已经就绪，但从来没有任何东西往里写。
本脚本补上「产出端」：读根目录 `额度核验清单.md` 里的官方入口，逐个抓取可公开访问的页面，
按免费/额度/赠送/试用/free/trial/credit 等关键词做命中扫描，把命中项写成候选线索。

硬边界（与 inbox.py 的校验保持一致，不绕过）：
- 只写 sources/inbox.json，状态一律 unverified；绝不写 sources/offers.json；
- 条目不生成任何额度数值，键名只用 inbox 白名单里的 8 个字段；
- 官方入口 URL 写进 claim，来源与抓取时间写进 note；
- 只抓 https 官方入口；401/403（登录墙）与短链域名直接跳过；
- 无头浏览器是可选依赖：装了 playwright 且浏览器可用才补抓 JS 渲染页，
  否则自动退回纯静态抓取，行为与之前完全一致；
- 单次扫描有总时限；网络失败只记入报告，不抛出。

退出码：0 成功 / 1 校验失败 / 2 用法错误。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import concurrent.futures
import hashlib
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import inbox as inbox_mod  # noqa: E402

DEFAULT_INBOX = REPO_ROOT / "sources" / "inbox.json"
DEFAULT_OFFERS = REPO_ROOT / "sources" / "offers.json"
DEFAULT_SEED = REPO_ROOT / "额度核验清单.md"
DEFAULT_OUT_DIR = REPO_ROOT / "out"
DEFAULT_LOG = REPO_ROOT / "logs" / "discovery.log"

DEFAULT_MAX_SECONDS = 120.0
DEFAULT_PER_URL_TIMEOUT = 6.0
DEFAULT_WORKERS = 8
DEFAULT_BROWSER_TIMEOUT_MS = 12000
DEFAULT_BROWSER_WAIT_MS = 5000
MAX_BYTES = 1536 * 1024

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 判定「页面正文里确实有额度线索」用的证据规则（浏览器抓取专用）
QUOTA_UNIT = re.compile(
    r"\d[\d,\.]*\s*(?:次|张|积分|点|token|tokens|万|亿|GB|MB|秒|分钟|小时|天|"
    r"requests?|credits?|images?|generations?|元|美元|\$|%)",
    re.I,
)
FREE_PHRASES = (
    "免费额度",
    "免费模型",
    "免费api",
    "免费试用",
    "免费体验",
    "免费赠送",
    "长期免费",
    "永久免费",
    "完全免费",
    "free tier",
    "free credits",
    "free trial",
    "free quota",
)
FREE_WORDS = ("免费", "赠送", "试用", "白嫖", "free", "trial", "credit", "quota", "complimentary", "giveaway", "额度")
# 出现这些词的句子往往是被误读的付费/私有化报价，不能当免费额度
QUOTA_NEGATIVE = ("私有化", "套餐", "单价", "报价", "年费", "付费", "充值", "续费", "企业版", "按量计费价格")
# 这类句式是「计费示例」，只能当原文摘录，不能当免费额度说明
BILLING_EXAMPLE = (
    "计费示例",
    "费用计算如下",
    "费用计算",
    "费用估算",
    "价格示例",
    "示例价格",
    "举例说明",
    "假设您",
    "计算公式",
    "计费公式",
)
LOGIN_WORDS = (
    "需登录",
    "请登录",
    "登录后",
    "登录以",
    "登录即可",
    "需实名",
    "实名认证",
    "未登录",
    "sign in to",
    "log in to",
    "please sign in",
    "access denied",
    "拒绝访问",
)

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

# 关键词组合：中文按原样匹配，英文按小写匹配。命中的短词会被更长的命中词吸收。
KEYWORDS = (
    "免费额度",
    "免费",
    "白嫖",
    "赠送",
    "试用",
    "体验",
    "限时",
    "新用户",
    "额度",
    "free tier",
    "free trial",
    "free credits",
    "free",
    "trial",
    "credit",
    "quota",
    "complimentary",
    "giveaway",
)

# 社区转帖常用的短链/跳转域名：一律不抓
SHORTENER_HOSTS = (
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "dwz.cn",
    "url.cn",
    "sourl.cn",
    "suo.im",
    "cutt.ly",
    "is.gd",
    "t.cn",
    "rebrand.ly",
)

URL_IN_CLAIM = re.compile(r"官方入口\s+(https?://[^\s；;，,。）)]+)")
ANY_URL = re.compile(r"https?://[^\s；;，,。]+")


class DiscoverError(Exception):
    pass


def utc_now(injected=None):
    if injected:
        text = str(injected).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- 种子清单

def parse_seed_table(path):
    """从 `额度核验清单.md` 第 3 节解析 平台 / 官方入口 / 在页面上看什么 / 形态。"""
    text = Path(path).read_text(encoding="utf-8")
    seeds = []
    in_section = False
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_section = line.startswith("## 3.")
            continue
        if not in_section:
            continue
        heading = re.match(r"^###\s+3\.\d+.*?(国内|国外)", line)
        if heading:
            group = heading.group(1)
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0] in ("#", "") or set(cells[0]) <= set(":- "):
            continue
        found = re.search(r"https://[^\s`|)]+", cells[2])
        if not found or not group:
            continue
        seeds.append(
            {
                "group": group,
                "name": cells[1],
                "url": found.group(0).rstrip(".,;，。；"),
                "look_for": cells[3],
                "shape": cells[4],
            }
        )
    return seeds


def is_allowed_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return False, "非 https"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "缺少主机名"
    if host in SHORTENER_HOSTS:
        return False, "短链/跳转域名"
    return True, ""


# --------------------------------------------------------------------------- 抓取与扫描

def fetch_page(url, timeout):
    info = {"url": url, "ok": False, "status": None, "final_url": url, "bytes": 0, "text": "", "error": ""}
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "freemodal-lead-discovery/1.0 (+https://github.com/yibur/freemodal-data-brain)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            info["status"] = getattr(resp, "status", 200)
            info["final_url"] = resp.geturl()
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                decoded = raw.decode(charset, errors="replace")
            except LookupError:
                decoded = raw.decode("utf-8", errors="replace")
            info.update(ok=True, text=decoded, bytes=len(raw))
    except urllib.error.HTTPError as exc:
        info["status"] = exc.code
        info["error"] = "HTTP " + str(exc.code)
    except Exception as exc:  # noqa: BLE001 - 单源失败要继续跑完其余种子
        info["error"] = type(exc).__name__ + ": " + str(exc)[:120]
    return info


def scan_keywords(text):
    lowered = text.lower()
    hits = []
    for word in KEYWORDS:
        if word.isascii():
            if word in lowered:
                hits.append(word)
        elif word in text:
            hits.append(word)
    kept = [hit for hit in hits if not any(hit != other and hit.lower() in other.lower() for other in hits)]
    return kept[:6]


def extract_title(page):
    found = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    if not found:
        return ""
    title = html_lib.unescape(found.group(1))
    return " ".join(title.split())[:120]


def split_sentences(text):
    parts = re.split(r"[。；;\n\r]+", text)
    return [re.sub(r"\s+", " ", part).strip() for part in parts if 4 <= len(part.strip()) <= 240]


def phrase_in(phrase, text, lowered):
    return phrase.lower() in lowered if any(ch.isascii() and ch.isalpha() for ch in phrase) else phrase in text


def extract_evidence(text):
    """浏览器渲染后的正文证据：既要有免费相关词，也要有额度数字或明确的免费模型表述。"""
    lowered = text.lower()
    excerpts = []
    excerpt_is_billing = []
    for sentence in split_sentences(text):
        if any(word in sentence for word in QUOTA_NEGATIVE):
            continue
        if not any(word in sentence.lower() for word in FREE_WORDS):
            continue
        if not QUOTA_UNIT.search(sentence):
            continue
        if sentence not in excerpts:
            excerpts.append(sentence[:160])
            excerpt_is_billing.append(any(word in sentence for word in BILLING_EXAMPLE))
        if len(excerpts) >= 3:
            break
    free_phrases = [p for p in FREE_PHRASES if phrase_in(p, text, lowered)]
    if not excerpts and free_phrases:
        # 没有额度数字，但存在明确的免费模型/免费额度表述时，摘一句原文供人工判断
        for sentence in split_sentences(text):
            if len(sentence) >= 12 and any(phrase_in(p, sentence, sentence.lower()) for p in free_phrases):
                excerpts.append(sentence[:160])
                excerpt_is_billing.append(any(word in sentence for word in BILLING_EXAMPLE))
                break
    login = [w for w in LOGIN_WORDS if w.lower() in lowered]
    return {
        "hits": scan_keywords(text),
        "free_phrases": free_phrases[:4],
        "excerpts": excerpts,
        "billing_examples": [flag for flag in excerpt_is_billing],
        "login": login[:2],
        "has_lead": bool(excerpts or free_phrases),
    }


# --------------------------------------------------------------------------- 无头浏览器（可选依赖）

class BrowserFetcher:
    """可选的无头浏览器抓取。Playwright 或浏览器不可用时 start() 返回 False，调用方保持静态结果。"""

    def __init__(self, spec, page_timeout_ms, wait_ms):
        self.spec = (spec or "auto").strip()
        self.page_timeout_ms = max(2000, int(page_timeout_ms))
        self.wait_ms = max(1000, int(wait_ms))
        self.available = False
        self.backend = ""
        self.reason = ""
        self._pw = None
        self._browser = None
        self._context = None

    def _launch_args(self):
        return [
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--mute-audio",
        ]

    def _launch(self):
        args = self._launch_args()
        if self.spec not in ("auto", "off"):
            self.backend = "custom:" + str(Path(self.spec).name)
            return self._pw.chromium.launch(headless=True, executable_path=self.spec, args=args)
        last_error = None
        try:
            self.backend = "playwright-bundled-chromium"
            return self._pw.chromium.launch(headless=True, args=args)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        for channel in ("msedge", "chrome"):
            try:
                browser = self._pw.chromium.launch(headless=True, channel=channel, args=args)
                self.backend = "system-" + channel
                return browser
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise last_error

    def start(self):
        if self.spec == "off":
            self.reason = "已显式关闭（--browser off）"
            return False
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001 - 没装就是可选依赖缺失，退回静态抓取
            self.reason = "未安装 playwright（" + type(exc).__name__ + "），退回静态抓取"
            return False
        try:
            self._pw = sync_playwright().start()
        except Exception as exc:  # noqa: BLE001
            self.reason = "playwright 启动失败：" + type(exc).__name__
            self._pw = None
            return False
        try:
            self._browser = self._launch()
        except Exception as exc:  # noqa: BLE001
            self.reason = "浏览器不可用：" + str(exc)[:160]
            self.close()
            return False
        try:
            self._context = self._browser.new_context(
                locale="zh-CN",
                user_agent=BROWSER_UA,
                viewport={"width": 1366, "height": 900},
            )
        except Exception as exc:  # noqa: BLE001
            self.reason = "浏览器上下文创建失败：" + str(exc)[:160]
            self.close()
            return False
        self.available = True
        return True

    def fetch(self, url, remaining_seconds):
        budget = int(max(3.0, min(self.page_timeout_ms / 1000.0, remaining_seconds)) * 1000)
        out = {
            "url": url,
            "ok": False,
            "status": None,
            "final_url": url,
            "text": "",
            "error": "",
            "step": "goto",
            "backend": self.backend,
        }
        started = time.monotonic()
        page_deadline = started + budget / 1000.0

        def left_ms():
            return int(max(500.0, (page_deadline - time.monotonic()) * 1000.0))

        page = None
        try:
            page = self._context.new_page()
            page.set_default_timeout(left_ms())
            out["step"] = "goto"
            response = page.goto(url, wait_until="domcontentloaded", timeout=left_ms())
            out["status"] = response.status if response else None
            out["final_url"] = page.url
            # 等网络空闲 + 正文就绪，而不是固定 sleep；每次等待都受本页总预算约束
            out["step"] = "wait"
            try:
                page.wait_for_load_state("networkidle", timeout=min(self.wait_ms, left_ms()))
            except Exception:  # noqa: BLE001 - 长连接页面永远不 idle，继续等正文
                pass
            try:
                page.wait_for_function(
                    "() => document.body && document.body.innerText.length > 200",
                    timeout=min(self.wait_ms, left_ms()),
                )
            except Exception:  # noqa: BLE001
                pass
            out["step"] = "extract"
            out["text"] = page.inner_text("body") or ""
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 单页失败要能继续下一家
            message = str(exc)
            if "ERR_NAME_NOT_RESOLVED" in message:
                out["error"] = "DNS 解析失败"
            elif "ERR_CONNECTION" in message:
                out["error"] = "连接失败"
            elif "Timeout" in type(exc).__name__ or "Timeout" in message:
                out["error"] = "超时（" + out["step"] + "）"
            else:
                out["error"] = type(exc).__name__ + ": " + message[:100]
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
        out["elapsed"] = round(time.monotonic() - started, 2)
        return out

    def close(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
            self._context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None


# --------------------------------------------------------------------------- 去重与「已收录」判定

def normalize_name(name):
    text = str(name).lower()
    return re.sub(r"[\s\u3000·・,，。.;；:：/\\|\-_()（）\[\]【】<>《》\"'“”‘’]+", "", text)


def has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def common_substring_len(left, right):
    if not left or not right:
        return 0
    best = 0
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best = current[j]
        previous = current
    return best


def match_listed(name, offers_doc):
    """在 sources/offers.json 里找同名平台；中文名允许长公共子串，英文名要求包含关系。"""
    mine = normalize_name(name)
    if not mine:
        return None
    for offer in offers_doc.get("verified_offers", []):
        if not isinstance(offer, dict):
            continue
        other = normalize_name(offer.get("provider", ""))
        if not other:
            continue
        if mine == other or mine in other or other in mine:
            return offer
        if has_cjk(mine) and has_cjk(other) and min(len(mine), len(other)) >= 4:
            if common_substring_len(mine, other) >= 4:
                return offer
    return None


def dedupe_key(provider, url):
    return normalize_name(provider), str(url).rstrip("/").lower()


def existing_keys(doc):
    keys = set()
    for entry in doc.get("inbox", []):
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        if not isinstance(provider, str):
            continue
        for text in (entry.get("claim") or "", entry.get("note") or ""):
            found = URL_IN_CLAIM.search(str(text)) or ANY_URL.search(str(text))
            if found:
                keys.add(dedupe_key(provider, found.group(1) if URL_IN_CLAIM.search(str(text)) else found.group(0)))
                break
    return keys


# --------------------------------------------------------------------------- 候选条目

def build_entry(seed, fetched, hits, listed_offer, now, mode="static", evidence=None):
    evidence = evidence or {}
    listed = listed_offer is not None
    prefix = "[已收录] " if listed else "[候选] "
    claim = (
        prefix
        + "官方入口 "
        + seed["url"]
        + "；页面命中关键词："
        + "、".join(hits)
        + "；疑似形态："
        + seed["shape"]
        + "（来自人工核验清单的形态提示，不是核验结论）"
    )
    excerpts = evidence.get("excerpts") or []
    billing_flags = evidence.get("billing_examples") or []
    first_is_billing = bool(billing_flags and billing_flags[0])
    if excerpts:
        if first_is_billing:
            claim += "；页面原文摘录（计费示例，不可当额度）：" + excerpts[0]
        else:
            claim += "；页面原文摘录：" + excerpts[0]
    if len(claim) > inbox_mod.MAX_CLAIM_LEN:
        claim = claim[: inbox_mod.MAX_CLAIM_LEN - 1] + "…"

    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    title = extract_title(fetched.get("text", ""))
    method = (
        "无头浏览器（" + str(fetched.get("backend") or "playwright") + "，等待 networkidle + 正文就绪）"
        if mode == "browser"
        else "静态 HTTP"
    )
    parts = [
        "自动发现于 " + stamp + "（UTC），抓取方式：" + method + "，页面 HTTP " + str(fetched.get("status")),
        "命中关键词：" + "、".join(hits),
        "疑似形态提示：" + seed["shape"] + "（人工清单给的提示，不是核验结论；本条目不含任何结构化额度字段）",
        "在页面上看什么：" + seed["look_for"],
    ]
    if evidence.get("free_phrases"):
        parts.append("页面出现免费表述：" + "、".join(evidence["free_phrases"]))
    if excerpts:
        label = "页面原文摘录（仅文本，未转成额度字段）"
        if first_is_billing:
            label = "页面原文摘录（仅文本，未转成额度字段；计费示例，不可当额度）"
        parts.append(label + "：" + " / ".join(excerpts))
    if first_is_billing:
        parts.append("注意：上面的摘录是计费示例（不是「新用户赠送 / 免费额度」说明），不可当额度填写")
    if evidence.get("login"):
        parts.append("页面出现「" + "、".join(evidence["login"]) + "」字样，不能当作可用额度，需人工登录后核实")
    if title:
        parts.append("页面标题：" + title)
    if fetched.get("final_url") and fetched["final_url"] != seed["url"]:
        parts.append("落点：" + fetched["final_url"])
    if listed:
        verified_at = listed_offer.get("_verified_at") or "未知"
        parts.append(
            "sources/offers.json 已有该平台记录（provider="
            + str(listed_offer.get("provider"))
            + "，最近 _verified_at="
            + str(verified_at)
            + "），本条可能只是同一额度的重复发现，请人工判断是「新额度」还是「已收录」。"
        )
    else:
        parts.append("sources/offers.json 尚无该平台记录。")

    digest = hashlib.sha1((normalize_name(seed["name"]) + "|" + seed["url"]).encode("utf-8")).hexdigest()[:6]
    return {
        "id": "L" + now.strftime("%Y%m%d-%H%M%S") + "-" + digest,
        "provider": seed["name"],
        "claim": claim,
        "heard_from": "自动发现 discover_leads.py：官方页面关键词扫描",
        "heard_at": now.date().isoformat(),
        "status": "unverified",
        "note": "；".join(parts),
    }


# --------------------------------------------------------------------------- 主流程

def discover(
    seeds,
    offers_doc,
    doc,
    inbox_path,
    max_seconds,
    per_url_timeout,
    workers,
    dry_run,
    now,
    browser_spec="auto",
    browser_timeout_ms=DEFAULT_BROWSER_TIMEOUT_MS,
    browser_wait_ms=DEFAULT_BROWSER_WAIT_MS,
):
    started = time.monotonic()
    deadline = started + max(5.0, float(max_seconds))

    allowed, blocked = [], []
    for seed in seeds:
        ok, reason = is_allowed_url(seed["url"])
        if ok:
            allowed.append(seed)
        else:
            blocked.append({"provider": seed["name"], "url": seed["url"], "reason": reason})

    fetched_by_seed = {}
    timed_out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {}
        for seed in allowed:
            if time.monotonic() >= deadline:
                timed_out.append(seed)
                continue
            futures[pool.submit(fetch_page, seed["url"], per_url_timeout)] = seed
        try:
            limit = max(0.5, deadline - time.monotonic())
            for future in concurrent.futures.as_completed(futures, timeout=limit):
                seed = futures[future]
                try:
                    fetched_by_seed[id(seed)] = future.result()
                except Exception as exc:  # noqa: BLE001
                    fetched_by_seed[id(seed)] = {
                        "url": seed["url"],
                        "ok": False,
                        "status": None,
                        "final_url": seed["url"],
                        "bytes": 0,
                        "text": "",
                        "error": type(exc).__name__ + ": " + str(exc)[:120],
                    }
        except concurrent.futures.TimeoutError:
            for future, seed in futures.items():
                if not future.done():
                    timed_out.append(seed)

    reachable, static_hit, browser_hit, skipped, candidates = 0, 0, 0, [], []
    seen = existing_keys(doc)
    duplicates, listed_count = 0, 0
    outcomes = []

    for seed in allowed:
        fetched = fetched_by_seed.get(id(seed))
        if fetched is None:
            skipped.append({"provider": seed["name"], "url": seed["url"], "reason": "超出单次扫描时限，未抓取"})
            outcomes.append(None)
            continue
        status = fetched.get("status")
        if not fetched.get("ok"):
            if status in (401, 403):
                reason = "需登录或拒绝匿名访问（HTTP " + str(status) + "）"
            elif status == 404:
                reason = "页面不存在（HTTP 404）"
            else:
                reason = "不可达：" + str(fetched.get("error") or "未知")
            skipped.append({"provider": seed["name"], "url": seed["url"], "reason": reason})
            outcomes.append(None)
            continue
        reachable += 1
        hits = scan_keywords(fetched.get("text", ""))
        if hits:
            static_hit += 1
        outcomes.append(
            {
                "seed": seed,
                "fetched": fetched,
                "hits": hits,
                "mode": "static",
                "evidence": None,
                "browser_failure": "",
            }
        )

    # 静态正文没命中的页面，再用无头浏览器补抓。浏览器不可用就保持静态结果，只记一条原因。
    browser_report = {
        "enabled": browser_spec != "off",
        "available": False,
        "backend": "",
        "attempted": 0,
        "hits": 0,
        "pages": [],
        "reason": "",
    }
    pending = [item for item in outcomes if item and not item["hits"]]
    if browser_report["enabled"] and pending:
        fetcher = BrowserFetcher(browser_spec, browser_timeout_ms, browser_wait_ms)
        if fetcher.start():
            browser_report.update(available=True, backend=fetcher.backend)
            try:
                for item in pending:
                    remaining = deadline - time.monotonic()
                    if remaining < 3:
                        browser_report["reason"] = "总时长上限已到，剩余页面未用浏览器补抓"
                        break
                    browser_report["attempted"] += 1
                    result = fetcher.fetch(item["seed"]["url"], remaining)
                    record = {
                        "provider": item["seed"]["name"],
                        "url": item["seed"]["url"],
                        "status": result.get("status"),
                        "ok": bool(result.get("ok")),
                        "chars": len(result.get("text") or ""),
                        "elapsed": result.get("elapsed"),
                        "step": result.get("step"),
                        "error": result.get("error"),
                    }
                    if not result.get("ok"):
                        record["result"] = "失败：" + str(result.get("error") or "未知")
                        item["browser_failure"] = record["result"]
                    elif result.get("status") in (401, 403):
                        record["result"] = "需登录/被拒绝（HTTP " + str(result.get("status")) + "）"
                        item["browser_failure"] = record["result"]
                    else:
                        evidence = extract_evidence(result.get("text") or "")
                        item["fetched"] = result
                        item["hits"] = evidence["hits"]
                        item["mode"] = "browser"
                        item["evidence"] = evidence
                        if evidence["has_lead"]:
                            browser_hit += 1
                            browser_report["hits"] += 1
                            record["result"] = (
                                "命中：摘录 "
                                + str(len(evidence["excerpts"]))
                                + " 条，免费表述 "
                                + str(len(evidence["free_phrases"]))
                                + " 个"
                            )
                        else:
                            record["result"] = "正文 " + str(len(result.get("text") or "")) + " 字符，无额度证据"
                    browser_report["pages"].append(record)
            finally:
                fetcher.close()
        else:
            browser_report["reason"] = fetcher.reason
            fetcher.close()

    for item in outcomes:
        if not item:
            continue
        seed = item["seed"]
        if item["mode"] == "browser":
            evidence = item["evidence"]
            if not evidence["has_lead"]:
                text = item["fetched"].get("text") or ""
                if item.get("browser_failure"):
                    reason = "无头浏览器补抓未拿到可用正文：" + str(item["browser_failure"])
                elif len(text) < 200:
                    reason = "无头浏览器渲染后正文仅 " + str(len(text)) + " 字符，疑似登录墙/地区限制"
                elif item["hits"]:
                    reason = "无头浏览器渲染后只有泛化关键词（" + "、".join(item["hits"]) + "），没有额度数字或免费模型表述"
                else:
                    reason = "无头浏览器渲染后正文 " + str(len(text)) + " 字符，未出现免费/额度相关词"
                skipped.append({"provider": seed["name"], "url": seed["url"], "reason": reason})
                continue
        elif not item["hits"]:
            reason = "页面可访问但未命中关键词（静态）"
            if item.get("browser_failure"):
                reason += "；浏览器补抓：" + item["browser_failure"]
            elif browser_report["reason"]:
                reason += "；浏览器补抓未执行：" + browser_report["reason"]
            skipped.append({"provider": seed["name"], "url": seed["url"], "reason": reason})
            continue

        hits = item["hits"] or (item["evidence"]["free_phrases"] if item["evidence"] else [])
        key = dedupe_key(seed["name"], seed["url"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        listed_offer = match_listed(seed["name"], offers_doc)
        if listed_offer is not None:
            listed_count += 1
        candidates.append(
            build_entry(seed, item["fetched"], hits, listed_offer, now, item["mode"], item["evidence"])
        )

    if candidates and not dry_run:
        doc.setdefault("inbox", []).extend(candidates)
        problems, _ = inbox_mod.validate_doc(doc, offers_doc)
        if problems:
            raise DiscoverError("候选写入前未通过 inbox 校验：" + "；".join(problems[:5]))
        inbox_mod.save_doc(inbox_path, doc)

    return {
        "ok": True,
        "run_at": now.isoformat(timespec="seconds"),
        "dry_run": bool(dry_run),
        "seed_total": len(seeds),
        "seed_domestic": sum(1 for s in seeds if s["group"] == "国内"),
        "seed_overseas": sum(1 for s in seeds if s["group"] == "国外"),
        "blocked": blocked,
        "scanned": len(allowed),
        "reachable": reachable,
        "keyword_hit": static_hit + browser_hit,
        "static_hit": static_hit,
        "browser_hit": browser_hit,
        "browser": browser_report,
        "candidates": len(candidates),
        "new_written": 0 if dry_run else len(candidates),
        "duplicates": duplicates,
        "listed": listed_count,
        "timed_out": [{"provider": s["name"], "url": s["url"]} for s in timed_out],
        "skipped": skipped,
        "entries": [
            {
                "id": entry["id"],
                "provider": entry["provider"],
                "listed": entry["claim"].startswith("[已收录]"),
                "mode": "浏览器" if "抓取方式：无头浏览器" in entry["note"] else "静态",
                "hits": (re.search(r"命中关键词：([^；]*)", entry["note"]) or [None, ""])[1],
            }
            for entry in candidates
        ],
        "duration_seconds": round(time.monotonic() - started, 2),
        "deadline_hit": bool(timed_out),
    }


def summarize(report):
    lines = []
    lines.append(
        "  种子      : "
        + str(report.get("seed_total"))
        + " 家（国内 "
        + str(report.get("seed_domestic"))
        + " / 国外 "
        + str(report.get("seed_overseas"))
        + "）"
    )
    lines.append(
        "  抓取      : 成功 "
        + str(report.get("reachable"))
        + " / "
        + str(report.get("scanned"))
        + "，跳过 "
        + str(len(report.get("skipped", [])))
        + "，超时 "
        + str(len(report.get("timed_out", [])))
    )
    lines.append("  关键词命中: " + str(report.get("keyword_hit")) + " 家（静态 " + str(report.get("static_hit")) + " / 浏览器补抓 " + str(report.get("browser_hit")) + "）")
    browser = report.get("browser") or {}
    if browser.get("enabled"):
        if browser.get("available"):
            lines.append(
                "  浏览器    : "
                + str(browser.get("backend"))
                + "，补抓 "
                + str(browser.get("attempted"))
                + " 页，命中 "
                + str(browser.get("hits"))
                + " 家"
            )
        else:
            lines.append("  浏览器    : 不可用，已退回静态抓取（" + str(browser.get("reason")) + "）")
    lines.append(
        "  候选      : 命中 "
        + str(report.get("keyword_hit"))
        + "，去重后新增 "
        + str(report.get("candidates"))
        + " 条，重复 "
        + str(report.get("duplicates"))
        + " 条"
    )
    lines.append("  已收录标记: " + str(report.get("listed")) + " 条")
    lines.append(
        "  写入      : "
        + ("未写入（dry-run）" if report.get("dry_run") else "sources/inbox.json（unverified，未生成任何额度数值）")
    )
    lines.append("  耗时      : " + str(report.get("duration_seconds")) + "s")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 自检

def run_selftest():
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    sample = "\n".join(
        [
            "## 3. 候选新额度清单",
            "### 3.1 国内（2 家）",
            "| # | 平台 | 官方入口 | 在页面上看什么 | 形态 |",
            "| :-- | :--- | :--- | :--- | :--- |",
            "| 1 | 腾讯云 · 混元生图 | `https://cloud.tencent.com/document/product/1729/105925` | 「计费概述」 | count / once |",
            "| 2 | 假平台 | `https://example.com/pricing` | 定价页 | credit / daily |",
            "## 4. 判定规则速查",
            "| 档位 | 条件 | 落到哪里 |",
            "| :--- | :--- | :--- |",
            "| 已核验 | ① | offers |",
        ]
    )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        seed_file = Path(td) / "seed.md"
        seed_file.write_text(sample, encoding="utf-8")
        seeds = parse_seed_table(seed_file)
        check("种子表解析出 2 行", len(seeds) == 2)
        check("种子分组为国内", seeds and seeds[0]["group"] == "国内")
        check("种子 URL 去掉反引号", seeds and seeds[0]["url"].startswith("https://cloud.tencent.com/"))
        check("种子形态列保留", seeds and seeds[0]["shape"] == "count / once")
        check("第 4 节表格不被误采", all("已核验" not in s["name"] for s in seeds))

    hits = scan_keywords("本页说明：新用户可获免费额度，Free Tier 每月重置。")
    check("中文关键词命中", "免费额度" in hits)
    check("短词被长词吸收", "免费" not in hits)
    check("英文关键词命中", "free tier" in hits)
    check("无关键词时返回空", scan_keywords("纯说明文字，不含营销词") == [])

    check("https 官方页放行", is_allowed_url("https://example.com/a")[0])
    check("http 被拒", not is_allowed_url("http://example.com/a")[0])
    check("短链被拒", not is_allowed_url("https://bit.ly/abc")[0])
    check("t.co 被拒", not is_allowed_url("https://t.co/abc")[0])

    offers = {
        "verified_offers": [
            {"provider": "腾讯混元生图", "_verified_at": "2026-11-13T08:57:23+00:00"},
            {"provider": "Recraft", "_verified_at": "2026-11-13T08:57:23+00:00"},
        ]
    }
    check("中文平台名模糊匹配已收录", match_listed("腾讯云 · 混元生图", offers) is not None)
    check("英文平台名精确匹配已收录", match_listed("Recraft", offers) is not None)
    check("无关平台不误判", match_listed("Runway", offers) is None)

    now = utc_now("2026-11-14T03:00:00+00:00")
    seed = {"group": "国内", "name": "腾讯云 · 混元生图", "url": "https://cloud.tencent.com/x", "look_for": "计费页", "shape": "count / once"}
    fetched = {"ok": True, "status": 200, "final_url": "https://cloud.tencent.com/x", "text": "<title>计费概述</title>免费额度"}
    entry = build_entry(seed, fetched, ["免费额度"], offers["verified_offers"][0], now)
    check("条目只用 inbox 白名单键", all(k in inbox_mod.ENTRY_KEYS for k in entry))
    check("条目不含额度数值键", not any(k in inbox_mod.VALUE_KEYS for k in entry))
    check("条目状态为 unverified", entry["status"] == "unverified")
    check("claim 含官方入口 URL", "官方入口 https://cloud.tencent.com/x" in entry["claim"])
    check("claim 不超过长度上限", len(entry["claim"]) <= inbox_mod.MAX_CLAIM_LEN)
    check("已收录平台被标记", entry["claim"].startswith("[已收录]"))
    check("note 含抓取时间", "2026-11-14T03:00:00Z" in entry["note"])

    doc = {"inbox": []}
    doc["inbox"].append(entry)
    problems, _ = inbox_mod.validate_doc(doc, offers)
    check("条目通过 inbox 校验", problems == [])
    check("去重键可复现", dedupe_key("腾讯云 · 混元生图", "https://cloud.tencent.com/x/") in existing_keys(doc))

    # 浏览器渲染后的证据判定
    evidence = extract_evidence("接口名称 免费额度 混元生图 50次 文生图轻量版 50次。混元生图价格说明")
    check("证据：抓到额度数字", bool(evidence["excerpts"]))
    check("证据：摘录含 50次", "50次" in " ".join(evidence["excerpts"]))
    check("证据：has_lead 为真", evidence["has_lead"])

    nav_only = extract_evidence("首页 免费 开始使用 免费 登录 控制台")
    check("仅导航栏「免费」不算命中", not nav_only["has_lead"] and not nav_only["excerpts"])

    free_model = extract_evidence("智谱开放平台提供以下免费API模型：GLM-4.7-Flash（200K上下文）：输入免费、输出免费、缓存免费。")
    check("免费模型表述算命中", free_model["has_lead"])

    private_plan = extract_evidence("云端私有化套餐 10亿tokens训练语料额度 单价50万元/年")
    check("私有化套餐报价不当成免费额度", not private_plan["excerpts"])

    login_wall = extract_evidence("请登录后查看免费额度 100次")
    check("登录墙被标出", bool(login_wall["login"]))

    billing = extract_evidence(
        "用户当月累计调用混元生图接口生成1万张图，混元生图的免费额度还剩余10张，所需支付的费用计算如下："
    )
    check("计费示例被识别", bool(billing["billing_examples"]) and billing["billing_examples"][0])
    billing_entry = build_entry(
        seed,
        {"ok": True, "status": 200, "final_url": "https://cloud.tencent.com/x", "text": "", "backend": "playwright-bundled-chromium"},
        ["免费额度"],
        None,
        now,
        "browser",
        billing,
    )
    check("计费示例在 note 标注不可当额度", "计费示例，不可当额度" in billing_entry["note"])
    check("计费示例在 claim 标注不可当额度", "计费示例，不可当额度" in billing_entry["claim"])

    browser_entry = build_entry(
        seed,
        {"ok": True, "status": 200, "final_url": "https://cloud.tencent.com/x", "text": "", "backend": "playwright-bundled-chromium"},
        ["免费额度"],
        None,
        now,
        "browser",
        evidence,
    )
    check("浏览器条目只用白名单键", all(k in inbox_mod.ENTRY_KEYS for k in browser_entry))
    check("浏览器条目 note 标出抓取方式", "无头浏览器" in browser_entry["note"])
    check("浏览器条目 claim 含原文摘录", "页面原文摘录" in browser_entry["claim"])
    check("浏览器条目 claim 不超长", len(browser_entry["claim"]) <= inbox_mod.MAX_CLAIM_LEN)

    browser_doc = {"inbox": [browser_entry]}
    browser_problems, _ = inbox_mod.validate_doc(browser_doc, offers)
    check("浏览器条目通过 inbox 校验", browser_problems == [])

    off_fetcher = BrowserFetcher("off", 3000, 1000)
    check("--browser off 能关闭浏览器", off_fetcher.start() is False and "关闭" in off_fetcher.reason)
    off_fetcher.close()

    failed = [name for name, ok in checks if not ok]
    for name in failed:
        print("FAIL " + name)
    print("SELFTEST " + ("PASS" if not failed else "FAIL") + " (" + str(len(checks) - len(failed)) + "/" + str(len(checks)) + ")")
    return not failed


# --------------------------------------------------------------------------- CLI

def write_report(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def append_log(path, payload):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        sys.stderr.write("WARN: 写日志失败: " + type(exc).__name__ + "\n")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="discover_leads.py",
        description="候选线索发现：官方入口页关键词扫描 -> sources/inbox.json（unverified）",
    )
    parser.add_argument("--seed", default=str(DEFAULT_SEED), help="种子清单，默认根目录 额度核验清单.md")
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX), help="线索文件，默认 sources/inbox.json")
    parser.add_argument("--offers", default=str(DEFAULT_OFFERS), help="正式清单（只读，用于判断已收录）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="报告输出目录，默认 out/")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="追加日志路径，默认 logs/discovery.log")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="单次扫描总时限，默认 120 秒")
    parser.add_argument("--per-url-timeout", type=float, default=DEFAULT_PER_URL_TIMEOUT, help="单个 URL 超时，默认 6 秒")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发抓取数，默认 8")
    parser.add_argument(
        "--browser",
        default="auto",
        help="无头浏览器：auto（默认）/ off / 浏览器可执行文件路径；不可用时自动退回静态抓取",
    )
    parser.add_argument("--browser-timeout-ms", type=int, default=DEFAULT_BROWSER_TIMEOUT_MS, help="单页浏览器超时，默认 12000ms")
    parser.add_argument("--browser-wait-ms", type=int, default=DEFAULT_BROWSER_WAIT_MS, help="等网络空闲/正文就绪的上限，默认 5000ms")
    parser.add_argument("--dry-run", action="store_true", help="只扫描并输出报告，不写 inbox")
    parser.add_argument("--now", default=None, help="注入带时区的 ISO-8601 时间，便于复现测试")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument("--selftest", action="store_true", help="跑离线自检（不联网、不写文件）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.selftest:
        return EXIT_OK if run_selftest() else EXIT_FAIL

    report = {"ok": False, "error": "", "run_at": utc_now(args.now).isoformat(timespec="seconds")}
    try:
        seed_path = Path(args.seed)
        if not seed_path.is_file():
            raise DiscoverError("种子清单不存在: " + str(seed_path))
        seeds = parse_seed_table(seed_path)
        if not seeds:
            raise DiscoverError("种子清单里没有解析到任何官方入口: " + str(seed_path))
        offers_doc = inbox_mod.load_offers(args.offers)
        doc = inbox_mod.load_doc(args.inbox)
        now = utc_now(args.now)
        report = discover(
            seeds,
            offers_doc,
            doc,
            args.inbox,
            args.max_seconds,
            args.per_url_timeout,
            args.workers,
            args.dry_run,
            now,
            browser_spec=args.browser,
            browser_timeout_ms=args.browser_timeout_ms,
            browser_wait_ms=args.browser_wait_ms,
        )
    except DiscoverError as exc:
        report = dict(report, ok=False, error=str(exc))
    except inbox_mod.InboxError as exc:
        report = dict(report, ok=False, error="线索或正式清单不可读：" + str(exc))
    except Exception as exc:  # noqa: BLE001 - 发现失败不能打断调用方
        report = dict(report, ok=False, error=type(exc).__name__ + ": " + str(exc)[:200])

    report["duration_seconds"] = report.get("duration_seconds") or 0.0
    out_dir = Path(args.out_dir)
    try:
        write_report(out_dir / "discovery-report.json", report)
    except OSError as exc:
        sys.stderr.write("WARN: 写报告失败: " + type(exc).__name__ + "\n")
    append_log(
        Path(args.log),
        {
            "ts": report.get("run_at"),
            "ok": report.get("ok"),
            "seed_total": report.get("seed_total"),
            "reachable": report.get("reachable"),
            "keyword_hit": report.get("keyword_hit"),
            "candidates": report.get("candidates"),
            "duplicates": report.get("duplicates"),
            "listed": report.get("listed"),
            "browser_available": (report.get("browser") or {}).get("available"),
            "browser_hit": report.get("browser_hit"),
            "error": report.get("error"),
        },
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("[discover-leads] " + str(report.get("run_at")))
        if report.get("ok"):
            print(summarize(report))
        else:
            print("  失败      : " + str(report.get("error")))
    return EXIT_OK if report.get("ok") else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
