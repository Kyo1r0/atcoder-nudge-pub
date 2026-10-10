#!/usr/bin/env python3
"""因幡めぐるが AtCoder の毎日精進をナッジしてくれるスクリプト。

使い方:
    nudge.py morning   朝のお題（未ACのA/B/Cから最新のもの）を投稿
    nudge.py check     今日ACしていて、まだ褒めていなければ褒める（定期実行用）
    nudge.py remind    今日まだACしていなければ催促（夜）
    nudge.py late      今日まだACしていなければ強めに催促（夜遅く）

オプション:
    --dry-run          Discord に送らず標準出力に表示（画像は data/preview.png）
    --test-card N      N日連続のカード画像だけ作って data/preview.png に保存
"""
import argparse
import io
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
IMAGES = BASE / "images"
JST = ZoneInfo("Asia/Tokyo")
API = "https://kenkoooo.com/atcoder"
HEADERS = {"User-Agent": "atcoder-nudge (daily practice reminder)"}
ONE_DAY = timedelta(days=1)
PINK = (255, 122, 162)
GOLD = (249, 200, 80)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# ---------------------------------------------------------------- 設定・状態

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_config():
    cfg = load_json(BASE / "config.json", None)
    if cfg is None:
        sys.exit("config.json がありません。config.example.json をコピーして作ってください。")
    return cfg


def load_messages():
    """messages.txt を [セクション] ごとのリストにする。"""
    sections, cur = {}, None
    for raw in (BASE / "messages.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            cur = sections.setdefault(m.group(1), [])
        elif cur is not None:
            cur.append(line.replace("\\n", "\n"))
    return sections


def say(msgs, key, **kw):
    candidates = msgs.get(key) or ["……センパイ？"]
    return random.choice(candidates).format(**kw)


# ---------------------------------------------------------------- AtCoder Problems API

def get_json(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def cached_resource(name, max_age_hours=20):
    """問題一覧などの大きいデータは1日1回だけ取りに行く。"""
    path = DATA / name
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return load_json(path, [])
    try:
        data = get_json(f"{API}/resources/{name}")
    except requests.RequestException:
        if path.exists():  # 取れなければ古いキャッシュで我慢
            return load_json(path, [])
        raise
    save_json(path, data)
    time.sleep(1.5)
    return data


def fetch_submissions(user):
    """提出をキャッシュしつつ差分だけ取得する。"""
    path = DATA / "submissions.json"
    subs = {s["id"]: s for s in load_json(path, [])}
    # ジャッジ待ちの取りこぼし対策で1時間ぶん重ねて取る
    from_sec = max(0, max((s["epoch_second"] for s in subs.values()), default=0) - 3600)
    while True:
        batch = get_json(f"{API}/atcoder-api/v3/user/submissions",
                         {"user": user, "from_second": from_sec})
        for s in batch:
            subs[s["id"]] = {k: s[k] for k in
                             ("id", "epoch_second", "problem_id", "contest_id", "result")}
        if len(batch) < 500:
            break
        from_sec = max(s["epoch_second"] for s in batch) + 1
        time.sleep(1.5)
    save_json(path, list(subs.values()))
    return list(subs.values())


# ---------------------------------------------------------------- 連続日数

def jst_date(epoch):
    return datetime.fromtimestamp(epoch, JST).date()


def ac_dates(subs):
    return {jst_date(s["epoch_second"]) for s in subs if s["result"] == "AC"}


def run_ending(days, d):
    """d を最終日とする連続日数。"""
    n = 0
    while d in days:
        n += 1
        d -= ONE_DAY
    return n


def streak_info(days, today):
    # 今日まだでも昨日まで続いていれば「継続中」として数える
    current = run_ending(days, today if today in days else today - ONE_DAY)
    best, run, prev = 0, 0, None
    for d in sorted(days):
        run = run + 1 if prev is not None and d - prev == ONE_DAY else 1
        best = max(best, run)
        prev = d
    return current, best


def is_milestone(n):
    return n in {3, 7, 14, 30, 50, 77} or (n >= 100 and n % 100 == 0) or (n > 0 and n % 365 == 0)


# ---------------------------------------------------------------- お題選び

def pick_problems(subs):
    """ABC の A/B/C それぞれについて、未ACのうち最新のコンテストのものを選ぶ。"""
    solved = {s["problem_id"] for s in subs if s["result"] == "AC"}
    contests = cached_resource("contests.json")
    contest_problems = cached_resource("contest-problem.json")
    problems = cached_resource("problems.json")

    info = {p["id"]: p for p in problems}
    now_sec = time.time()
    abc_start = {c["id"]: c["start_epoch_second"] for c in contests
                 if re.fullmatch(r"abc\d+", c["id"])
                 and c["start_epoch_second"] + c["duration_second"] < now_sec}

    picks = {}
    for idx in "ABC":
        best = None
        for cp in contest_problems:
            cid, pid = cp["contest_id"], cp["problem_id"]
            if cid not in abc_start or pid in solved:
                continue
            p_idx = cp.get("problem_index") or info.get(pid, {}).get("problem_index")
            if p_idx != idx:
                continue
            if best is None or abc_start[cid] > abc_start[best["contest_id"]]:
                best = cp
        if best:
            p = info.get(best["problem_id"], {})
            picks[idx] = {
                "contest_id": best["contest_id"],
                "title": p.get("title") or p.get("name") or best["problem_id"],
                "url": f"https://atcoder.jp/contests/{best['contest_id']}/tasks/{best['problem_id']}",
            }
    return picks


# ---------------------------------------------------------------- カード画像

def find_font(cfg):
    candidates = [cfg.get("font_path")]
    candidates += sorted(str(p) for p in (BASE / "fonts").glob("*") if p.suffix.lower() in {".ttf", ".otf", ".ttc"})
    candidates += [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def load_font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size, index=0)
        except OSError:
            pass
    return ImageFont.load_default(size)


def pick_image(milestone):
    dirs = [IMAGES / "milestone", IMAGES / "normal"] if milestone else [IMAGES / "normal"]
    for d in dirs:
        files = [p for p in d.glob("*") if p.suffix.lower() in IMAGE_EXTS] if d.exists() else []
        if files:
            return random.choice(files)
    return None


def make_card(days, best, milestone, cfg):
    W, H, PAD = 760, 300, 24
    accent = GOLD if milestone else PINK
    img = Image.new("RGB", (W, H), (30, 30, 46))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, W - 5, H - 5], radius=24, outline=accent, width=4)

    x = PAD + 16
    pic_path = pick_image(milestone)
    if pic_path:
        size = H - PAD * 2
        pic = ImageOps.fit(Image.open(pic_path).convert("RGB"), (size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=18, fill=255)
        img.paste(pic, (PAD, PAD), mask)
        x = PAD + size + 32

    font_path = find_font(cfg)
    small = load_font(font_path, 30)
    unit = load_font(font_path, 48)
    area = W - x - PAD - 10

    head = f"★ {days}日達成！" if milestone else "AtCoder 毎日精進"
    draw.text((x, 40), head, font=small, fill=accent if milestone else (205, 214, 244))

    # 数字は横幅に収まるまで縮める
    num, unit_text = str(days), "日連続"
    unit_w = draw.textlength(unit_text, font=unit)
    size = 140
    while size > 40:
        big = load_font(font_path, size)
        if draw.textlength(num, font=big) + unit_w + 12 <= area:
            break
        size -= 8
    baseline = 210
    draw.text((x, baseline), num, font=big, fill=accent, anchor="ls")
    draw.text((x + draw.textlength(num, font=big) + 12, baseline), unit_text,
              font=unit, fill=(255, 255, 255), anchor="ls")

    draw.text((x, 245), f"最長記録 {best}日", font=small, fill=(166, 173, 200))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- Discord

def post(cfg, content, embeds=None, image=None, dry_run=False):
    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    if image:
        payload.setdefault("embeds", []).append({"image": {"url": "attachment://streak.png"},
                                                 "color": 0xFF7AA2})
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if image:
            (DATA / "preview.png").write_bytes(image)
            print(f"[画像] {DATA / 'preview.png'}")
        return
    if image:
        r = requests.post(cfg["webhook_url"], data={"payload_json": json.dumps(payload)},
                          files={"files[0]": ("streak.png", image, "image/png")}, timeout=30)
    else:
        r = requests.post(cfg["webhook_url"], json=payload, timeout=30)
    r.raise_for_status()


# ---------------------------------------------------------------- 各モード

def do_morning(cfg, msgs, subs, days, today, current, best, dry_run):
    parts = []
    yesterday = today - ONE_DAY
    lost = run_ending(days, yesterday - ONE_DAY)
    if yesterday not in days and lost > 0:
        parts.append(say(msgs, "broken", days=lost, best=best))
    parts.append(say(msgs, "morning", days=current, next=current + 1, best=best))

    picks = pick_problems(subs)
    labels = {"A": "A問題", "B": "B問題", "C": "C問題（余裕があれば！）"}
    lines = [f"**{labels[i]}**　[{p['title']}]({p['url']})　`{p['contest_id']}`"
             for i, p in picks.items()]
    embed = {"title": "📝 今日のお題",
             "description": "\n".join(lines) or "未ACの問題が見つかりませんでした……すごいです、センパイ！",
             "color": 0xFF7AA2}
    post(cfg, "\n".join(parts), embeds=[embed], dry_run=dry_run)


def do_celebrate(cfg, msgs, days, current, best, dry_run):
    milestone = is_milestone(current)
    if milestone:
        key = "milestone"
    elif current == 1 and len(days) > 1:
        key = "restart"
    else:
        key = "done"
    text = say(msgs, key, days=current, best=best)
    post(cfg, text, image=make_card(current, best, milestone, cfg), dry_run=dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["morning", "check", "remind", "late"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-card", type=int, metavar="N")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    cfg = load_config()

    if args.test_card is not None:
        n = args.test_card
        (DATA / "preview.png").write_bytes(make_card(n, max(n, 42), is_milestone(n), cfg))
        print(f"[画像] {DATA / 'preview.png'}")
        return
    if not args.mode:
        ap.error("mode を指定してください")

    msgs = load_messages()
    subs = fetch_submissions(cfg["atcoder_user"])
    days = ac_dates(subs)
    today = datetime.now(JST).date()
    current, best = streak_info(days, today)
    state_path = DATA / "state.json"
    state = load_json(state_path, {})

    if args.mode == "morning":
        do_morning(cfg, msgs, subs, days, today, current, best, args.dry_run)
    elif today in days:
        if state.get("celebrated") != str(today):
            do_celebrate(cfg, msgs, days, current, best, args.dry_run)
            if not args.dry_run:
                state["celebrated"] = str(today)
    elif args.mode in ("remind", "late"):
        post(cfg, say(msgs, args.mode, days=current, next=current + 1, best=best),
             dry_run=args.dry_run)

    save_json(state_path, state)


if __name__ == "__main__":
    main()
