#!/usr/bin/env python3
"""
和歌山県の新型コロナ・インフルエンザ 定点当たり報告数を毎週取得して
RSS フィード（docs/feed.xml）と JSON（docs/data.json）を生成する。

データ元: 国立健康危機管理研究機構(JIHS) IDWR速報データ
  https://id-info.jihs.go.jp/surveillance/idwr/provisional/{年}/{週}/index.html
  CSV は Shift_JIS(cp932)、都道府県 × 疾患（報告数 / 定点当たり）の2段ヘッダ。

使い方:
    python build_feed.py                # 最新週を取得して追記
    python build_feed.py --backfill 10  # 直近10週分をまとめて取得
    python build_feed.py --week 2026 32 # 特定の週だけ取得
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from pathlib import Path

PREF = "和歌山県"
NATION = "総数"          # CSV の全国計の行ラベル
DISEASES = ["インフルエンザ", "COVID-19"]

BASE = "https://id-info.jihs.go.jp/surveillance/idwr/provisional"
WIDR_PAGE = "https://www.pref.wakayama.lg.jp/prefg/031801/idsw/khdc/d00153694.html"
MHLW_COVID = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000121431_00485.html"
MHLW_FLU = ("https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/kenkou/"
            "kekkaku-kansenshou01/houdou_00023.html")

OUT_DIR = Path(__file__).parent / "docs"
DATA_JSON = OUT_DIR / "data.json"
FEED_XML = OUT_DIR / "feed.xml"
EMBED_HTML = OUT_DIR / "index.html"

# フィードの公開URL。GitHub Pages を使う場合はここを自分のものに書き換える。
SITE_URL = os.environ.get("SITE_URL", "https://example.github.io/wakayama-feed")

UA = "wakayama-idwr-feed/1.0 (personal weekly digest)"


# --------------------------------------------------------------------------
# 取得
# --------------------------------------------------------------------------
def csv_url(year: int, week: int) -> str:
    return f"{BASE}/{year}/{week:02d}/{year}-{week:02d}-teiten.csv"


def page_url(year: int, week: int) -> str:
    return f"{BASE}/{year}/{week:02d}/index.html"


def fetch(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def iso_week_of(date: dt.date) -> tuple[int, int]:
    y, w, _ = date.isocalendar()
    return y, w


def week_candidates(back: int = 8) -> list[tuple[int, int]]:
    """今週から過去に向かって (年, 週) を列挙する。"""
    out = []
    d = dt.date.today()
    for _ in range(back):
        out.append(iso_week_of(d))
        d -= dt.timedelta(days=7)
    return out


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def parse_teiten(raw: bytes) -> dict:
    """teiten.csv をパースして {期間, 疾患: {都道府県: {報告数, 定当}}} を返す。"""
    text = raw.decode("cp932", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))

    period = ""
    header_i = None
    for i, row in enumerate(rows[:8]):
        joined = "".join(row)
        if "週(" in joined and not period:
            period = row[0].strip()
        # 疾患名の行 = 目的の疾患名を含む行
        if any(c.strip() in DISEASES for c in row):
            header_i = i
            break
    if header_i is None:
        raise ValueError("疾患名のヘッダ行が見つかりません（CSVの書式が変わった可能性）")

    # 疾患名 -> 「報告数」列のindex。「定点当たり」はその隣。
    col = {}
    for j, cell in enumerate(rows[header_i]):
        name = cell.strip()
        if name in DISEASES and name not in col:
            col[name] = j
    missing = [d for d in DISEASES if d not in col]
    if missing:
        raise ValueError(f"疾患が見つかりません: {missing}")

    def num(v: str):
        v = v.strip()
        if v in ("", "-", "－"):
            return None
        try:
            return float(v)
        except ValueError:
            return None

    data = {d: {} for d in DISEASES}
    for row in rows[header_i + 2:]:
        if not row or not row[0].strip():
            continue
        area = row[0].strip()
        for d, j in col.items():
            if j + 1 >= len(row):
                continue
            data[d][area] = {"cases": num(row[j]), "rate": num(row[j + 1])}
    return {"period": period, "data": data}


def build_record(year: int, week: int, raw: bytes) -> dict:
    parsed = parse_teiten(raw)
    rec = {
        "year": year,
        "week": week,
        "key": f"{year}-{week:02d}",
        "period": parsed["period"],
        "source": page_url(year, week),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "diseases": {},
    }
    for d in DISEASES:
        pref = parsed["data"][d].get(PREF, {})
        nat = parsed["data"][d].get(NATION, {})
        rec["diseases"][d] = {
            "pref_cases": pref.get("cases"),
            "pref_rate": pref.get("rate"),
            "national_rate": nat.get("rate"),
        }
    return rec


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------
def load_history() -> list[dict]:
    if DATA_JSON.exists():
        return json.loads(DATA_JSON.read_text(encoding="utf-8"))
    return []


def save_history(records: list[dict]) -> None:
    records.sort(key=lambda r: (r["year"], r["week"]))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def arrow(cur, prev) -> str:
    if cur is None or prev is None:
        return ""
    if prev == 0:
        return " ↑" if cur > 0 else " →"
    diff = (cur - prev) / prev * 100
    if diff >= 5:
        return f" ↑{diff:+.0f}%"
    if diff <= -5:
        return f" ↓{diff:+.0f}%"
    return " →ほぼ横ばい"


def fmt(v) -> str:
    return "－" if v is None else f"{v:.2f}"


def summarize(rec: dict, prev: dict | None) -> tuple[str, str]:
    flu = rec["diseases"]["インフルエンザ"]
    cov = rec["diseases"]["COVID-19"]
    title = (
        f"{PREF} {rec['year']}年第{rec['week']}週 — "
        f"インフル {fmt(flu['pref_rate'])} / コロナ {fmt(cov['pref_rate'])}"
    )

    lines = [f"【{PREF} 定点当たり報告数】{rec['period']}", ""]
    for name, cur in (("インフルエンザ", flu), ("新型コロナ (COVID-19)", cov)):
        key = "インフルエンザ" if name.startswith("インフル") else "COVID-19"
        p = prev["diseases"][key]["pref_rate"] if prev else None
        lines.append(
            f"■ {name}: {fmt(cur['pref_rate'])}"
            f"{arrow(cur['pref_rate'], p)}"
            f"（報告数 {int(cur['pref_cases']) if cur['pref_cases'] is not None else '－'}件"
            f" / 全国平均 {fmt(cur['national_rate'])}）"
        )
    lines += [
        "",
        "※インフルエンザの目安: 注意報 10.0 / 警報 30.0（定点当たり）",
        "※2025年第15週以降は定点数が変更されており、過去との単純比較には注意",
        f"※出典: 国立健康危機管理研究機構 IDWR速報 {rec['source']}",
        f"※保健所別の内訳は和歌山県 WIDR (PDF) を参照: {WIDR_PAGE}",
        f"※厚生労働省の報道発表（金曜・全国分）: {MHLW_COVID}",
    ]
    return title, "\n".join(lines)


def build_rss(records: list[dict], limit: int = 30) -> None:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"{PREF} コロナ・インフルエンザ 週次定点報告"
    ET.SubElement(ch, "link").text = SITE_URL
    ET.SubElement(ch, "description").text = (
        f"{PREF}の新型コロナウイルス感染症とインフルエンザの定点当たり報告数を毎週配信します。"
        "データ元: 国立健康危機管理研究機構 感染症発生動向調査(IDWR)速報。"
    )
    ET.SubElement(ch, "language").text = "ja"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(
        dt.datetime.now(dt.timezone.utc)
    )

    recent = sorted(records, key=lambda r: (r["year"], r["week"]), reverse=True)[:limit]
    for i, rec in enumerate(recent):
        prev = recent[i + 1] if i + 1 < len(recent) else None
        title, body = summarize(rec, prev)
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = rec["source"]
        ET.SubElement(item, "guid", isPermaLink="false").text = f"wakayama-{rec['key']}"
        ET.SubElement(item, "description").text = body
        try:
            pub = dt.datetime.fromisoformat(rec["fetched_at"])
        except ValueError:
            pub = dt.datetime.now(dt.timezone.utc)
        ET.SubElement(item, "pubDate").text = format_datetime(pub)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(FEED_XML, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------
# Notion 埋め込み用 HTML
# --------------------------------------------------------------------------
def sparkline(values: list, color: str, width: int = 150, height: int = 34) -> str:
    """直近の推移をSVGの折れ線で描く。"""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = width / (len(pts) - 1)
    coords = [
        (i * step, height - 3 - (v - lo) / span * (height - 6))
        for i, v in enumerate(pts)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,{height} {line} {width},{height}"
    lx, ly = coords[-1]
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polygon points="{area}" fill="{color}" opacity="0.12"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" '
        f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.6" fill="{color}"/></svg>'
    )


def level_color(rate) -> str:
    if rate is None:
        return "#888780"
    if rate >= 30:
        return "#E24B4A"
    if rate >= 10:
        return "#EF9F27"
    if rate >= 3:
        return "#BA7517"
    return "#1D9E75"


def build_embed(records: list[dict], span: int = 12) -> None:
    """Notion の /embed に貼るための1枚ページを生成する。"""
    ordered = sorted(records, key=lambda r: (r["year"], r["week"]))
    if not ordered:
        return
    latest, prev = ordered[-1], (ordered[-2] if len(ordered) > 1 else None)
    window = ordered[-span:]

    cards = []
    for label, key in (("インフルエンザ", "インフルエンザ"), ("新型コロナ", "COVID-19")):
        cur = latest["diseases"][key]
        p = prev["diseases"][key]["pref_rate"] if prev else None
        color = level_color(cur["pref_rate"])
        delta = arrow(cur["pref_rate"], p).strip()
        series = [r["diseases"][key]["pref_rate"] for r in window]
        cases = cur["pref_cases"]
        cards.append(
            f'<div class="card">'
            f'<div class="label"><span class="dot" style="background:{color}"></span>{label}</div>'
            f'<div class="value">{fmt(cur["pref_rate"])}'
            f'<span class="delta">{delta}</span></div>'
            f'<div class="spark">{sparkline(series, color)}</div>'
            f'<div class="meta">報告 {int(cases) if cases is not None else "－"}件'
            f' ／ 全国 {fmt(cur["national_rate"])}</div>'
            f"</div>"
        )

    updated = dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=9))
    )
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{PREF} コロナ・インフルエンザ 定点当たり報告数</title>
<style>
:root{{--fg:#1a1a18;--fg2:#5f5e5a;--fg3:#888780;--bg:#fff;--line:#e7e5e0}}
@media(prefers-color-scheme:dark){{:root{{--fg:#f0efec;--fg2:#a8a69f;--fg3:#7a7975;--bg:#1f1f1e;--line:#38383550}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:18px;background:var(--bg);color:var(--fg);
font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}}
h1{{font-size:15px;font-weight:600;margin:0 0 2px}}
.period{{font-size:12px;color:var(--fg2);margin:0 0 16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}}
.card{{border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.label{{font-size:13px;color:var(--fg2);display:flex;align-items:center;gap:6px}}
.dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.value{{font-size:30px;font-weight:600;margin:4px 0 2px;letter-spacing:-.01em}}
.delta{{font-size:13px;font-weight:400;color:var(--fg2);margin-left:8px}}
.spark{{margin:6px 0 4px;height:34px}}
.spark svg{{width:100%;height:34px;display:block}}
.meta{{font-size:12px;color:var(--fg3)}}
footer{{margin-top:14px;font-size:11px;color:var(--fg3)}}
footer a{{color:inherit}}
</style></head><body>
<h1>{PREF} 定点当たり報告数</h1>
<p class="period">{latest["period"]}（直近{len(window)}週の推移）</p>
<div class="grid">{"".join(cards)}</div>
<footer>出典 <a href="{latest['source']}" target="_blank" rel="noopener">国立健康危機管理研究機構 IDWR速報</a>
 ／ <a href="{WIDR_PAGE}" target="_blank" rel="noopener">和歌山県 WIDR</a>
 ／ 厚労省 <a href="{MHLW_COVID}" target="_blank" rel="noopener">コロナ</a>・<a href="{MHLW_FLU}" target="_blank" rel="noopener">インフル</a>
 ・ 最終更新 {updated:%Y-%m-%d %H:%M} JST</footer>
</body></html>"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    EMBED_HTML.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# 通知（任意）
# --------------------------------------------------------------------------
def level_emoji(rate) -> str:
    """定点当たり報告数のおおまかな水準を絵文字で示す。"""
    if rate is None:
        return "⚪"
    if rate >= 30:
        return "🔴"
    if rate >= 10:
        return "🟠"
    if rate >= 3:
        return "🟡"
    return "🟢"


def slack_blocks(rec: dict, prev: dict | None) -> list[dict]:
    flu = rec["diseases"]["インフルエンザ"]
    cov = rec["diseases"]["COVID-19"]
    p_flu = prev["diseases"]["インフルエンザ"]["pref_rate"] if prev else None
    p_cov = prev["diseases"]["COVID-19"]["pref_rate"] if prev else None

    def field(label: str, cur: dict, prev_rate) -> dict:
        cases = cur["pref_cases"]
        return {
            "type": "mrkdwn",
            "text": (
                f"{level_emoji(cur['pref_rate'])} *{label}*\n"
                f"*{fmt(cur['pref_rate'])}*{arrow(cur['pref_rate'], prev_rate)}\n"
                f"報告 {int(cases) if cases is not None else '－'}件 ／ "
                f"全国 {fmt(cur['national_rate'])}"
            ),
        }

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{PREF} 第{rec['week']}週の感染状況",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"{rec['period']}｜定点当たり報告数"}
            ],
        },
        {
            "type": "section",
            "fields": [
                field("インフルエンザ", flu, p_flu),
                field("新型コロナ", cov, p_cov),
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"<{rec['source']}|JIHS 速報データ>"
                        f" ・ <{WIDR_PAGE}|和歌山県 WIDR（保健所別 PDF）>"
                        "｜🟡3以上 🟠10以上 🔴30以上"
                    ),
                }
            ],
        },
    ]


def notify(rec: dict, prev: dict | None) -> None:
    """SLACK_WEBHOOK_URL / DISCORD_WEBHOOK_URL が設定されていれば投稿する。"""
    title, body = summarize(rec, prev)

    payloads = {
        # text は通知バナー・検索用のフォールバック
        "SLACK_WEBHOOK_URL": {"text": title, "blocks": slack_blocks(rec, prev)},
        "DISCORD_WEBHOOK_URL": {"content": f"**{title}**\n```{body}```"[:1900]},
    }
    for env, payload in payloads.items():
        url = os.environ.get(env)
        if not url:
            continue
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
            print(f"notified via {env}")
        except Exception as e:  # 通知失敗でジョブは落とさない
            print(f"notify failed ({env}): {e}", file=sys.stderr)


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=1, help="さかのぼって取得する週数")
    ap.add_argument("--week", nargs=2, type=int, metavar=("YEAR", "WEEK"))
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument(
        "--print-payload",
        action="store_true",
        help="送信せずSlackのJSONを表示する（Block Kit Builderに貼って確認できる）",
    )
    args = ap.parse_args()

    history = load_history()
    known = {r["key"] for r in history}

    if args.week:
        targets = [tuple(args.week)]
    else:
        targets = week_candidates(back=max(args.backfill, 1) + 3)

    added: list[dict] = []
    for year, week in targets:
        key = f"{year}-{week:02d}"
        if key in known:
            continue
        raw = fetch(csv_url(year, week))
        if raw is None:
            continue  # まだ公開されていない週
        rec = build_record(year, week, raw)
        history.append(rec)
        known.add(key)
        added.append(rec)
        print(f"取得: {key} {rec['period']}")
        if len(added) >= args.backfill:
            break

    if not added:
        print("新しい週のデータはありませんでした。")

    save_history(history)
    build_rss(history)
    build_embed(history)
    print(f"書き出し: {FEED_XML} / {DATA_JSON} / {EMBED_HTML}（全{len(history)}週）")

    ordered = sorted(history, key=lambda r: (r["year"], r["week"]))
    latest = ordered[-1] if ordered else None
    prev = ordered[-2] if len(ordered) > 1 else None

    if args.print_payload and latest:
        payload = {"text": summarize(latest, prev)[0], "blocks": slack_blocks(latest, prev)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif added and not args.no_notify:
        notify(latest, prev)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
