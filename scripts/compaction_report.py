#!/usr/bin/env python3
"""DSH セッションのコンパクション統計を出す.

usage: compaction_report.py <session-id | session-dir | session.v4.jsonl.zstd>

圧縮の回数・所要時間に加えて、圧縮が発火したトークン数と、圧縮後の着地点を出す。
「圧縮前 ≈ 圧縮後 ≈ 閾値」ならドゥームループ。
"""
import json
import statistics
import subprocess
import sys
from pathlib import Path


def find_session(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        for name in ("session.v4.jsonl.zstd", "session.v4.jsonl"):
            if (p / name).is_file():
                return p / name
    root = Path.home() / ".dsh" / "sessions"
    for pattern in (f"*/session-{arg}*/session.v4.jsonl.zstd", f"*/session-{arg}*/session.v4.jsonl"):
        for cand in root.glob(pattern):
            return cand
    raise SystemExit(f"session not found: {arg}")


def read_events(path: Path):
    if path.suffix == ".zstd":
        text = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    else:
        text = path.read_bytes()
    for line in text.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def main() -> None:
    path = find_session(sys.argv[1] if len(sys.argv) > 1 else "")
    starts, ends = {}, []
    events = []  # (seq, kind, value)
    summaries = 0
    steps = 0
    first = last = None
    for ev in read_events(path):
        t = ev.get("type")
        data = ev.get("data") or {}
        ts = ev.get("time")
        seq = ev.get("seq", 0)
        if ts:
            first = first or ts
            last = ts
        if t == "step/start":
            steps += 1
        elif t == "compaction/start":
            starts[data.get("compactionId")] = ts
            events.append((seq, "START", None))
        elif t == "compaction/end":
            dur = (ts - starts.get(data.get("compactionId"), ts)) / 1000
            ends.append((dur, data.get("error")))
            events.append((seq, "END", None))
        elif t == "compaction/summary":
            summaries += 1
        elif t == "assistant/message":
            tokens = (data.get("usage") or {}).get("totalTokens")
            if isinstance(tokens, int):
                events.append((seq, "TOKENS", tokens))

    if not ends:
        print(f"{path}\nsession has no compaction: steps={steps}")
        return

    total = sum(d for d, _ in ends)
    span = (last - first) / 1000 / 60 if first and last else 0
    errors = [e for _, e in ends if e]
    print(f"session : {path}")
    print(f"span    : {span:.0f} 分  steps={steps}")
    print(f"圧縮    : {len(ends)} 回  合計 {total/60:.1f} 分  平均 {total/len(ends):.1f} 秒")
    if span:
        print(f"        : 稼働の {total/60/span*100:.0f}%  間隔 {steps/len(ends):.1f} step ごと")
    print(f"要約    : {summaries} 件")
    if errors:
        print(f"失敗    : {len(errors)} 回 ({errors[0]})")

    pre, after = [], []
    last_tokens = None
    want_post = False
    for _seq, kind, value in events:
        if kind == "TOKENS":
            if want_post:
                after.append(value)
                want_post = False
            last_tokens = value
        elif kind == "START":
            if last_tokens:
                pre.append(last_tokens)
            last_tokens = None
        elif kind == "END":
            want_post = True

    if pre:
        print(f"\n圧縮前  : med={statistics.median(pre):,.0f}  min={min(pre):,}  max={max(pre):,}")
    if after:
        print(f"圧縮後  : med={statistics.median(after):,.0f}  min={min(after):,}  max={max(after):,}")
    if pre and after:
        room = min(pre) - statistics.median(after)
        print(f"余裕    : 圧縮直後の空き 約 {room:,.0f} tokens")
        if room < statistics.median(pre) * 0.25:
            print("          → 空きが閾値の 25% 未満。ドゥームループの疑い。")

    if len(ends) >= 5 and steps / len(ends) < 15:
        print("\n判定: 圧縮が多すぎる。SKILL.md の「3. 直す」へ。")


if __name__ == "__main__":
    main()
