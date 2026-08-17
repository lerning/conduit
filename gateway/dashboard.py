"""Operational dashboard rendered from the ledger.

Answers the questions you actually have about a gateway: where did the money go,
is one tenant crowding out the others, and did the protections fire?

Served live at GET /v1/dashboard (Conduit is private -- reach it with
`fly proxy 8200:8200 -a conduit-gateway`), or written to a static file with
`python -m gateway.dashboard --snapshot out.html`.

The ledger is metadata-only by construction, so nothing here can leak message
content no matter how it is aggregated. Client ids are already pseudonymous
(Inner Council passes an opaque invite-code user id, never a name); they are
truncated further for display.
"""
from __future__ import annotations

import html
import time
from collections import defaultdict
from datetime import datetime, timezone

from gateway.telemetry.ledger import (OUTCOME_CAP_APP,
                                      OUTCOME_CAP_GLOBAL, OUTCOME_CAP_USER,
                                      OUTCOME_FAIL_CLOSED, OUTCOME_OK,
                                      OUTCOME_PROVIDER_FAILED,
                                      OUTCOME_RATE_LIMITED, REJECTIONS)

REJECT_LABEL = {
    OUTCOME_RATE_LIMITED: "throttled (429)",
    OUTCOME_CAP_USER: "tenant cap (402)",
    OUTCOME_CAP_APP: "app cap (402)",
    OUTCOME_CAP_GLOBAL: "global cap (402)",
    OUTCOME_PROVIDER_FAILED: "providers exhausted (502)",
    OUTCOME_FAIL_CLOSED: "fail-closed (503)",
    "bad_request": "bad request (400)",
}


def _short(client_id: str) -> str:
    """`ic:u_a080f989a3` -> `ic:u_a080…`. Keeps the app prefix (the tenant
    boundary that matters for isolation), abbreviates the opaque user id."""
    app, _, user = client_id.partition(":")
    return f"{app}:{user[:6]}…" if len(user) > 7 else client_id


def _pct(n: float, d: float) -> float:
    return (100.0 * n / d) if d else 0.0


def _f(x: float, places: int = 4) -> str:
    return f"{x:.{places}f}".rstrip("0").rstrip(".") or "0"


def _bar_rows(items, total, color="var(--ok)", fmt=lambda v: f"{v:g}"):
    out = ""
    for name, val in items:
        out += (f'<div class="bar-row"><span class="lbl">{html.escape(name)}</span>'
                f'<span class="bar"><span class="fill" style="width:{_pct(val,total):.1f}%;'
                f'background:{color}"></span></span>'
                f'<span class="num">{fmt(val)}</span></div>')
    return out or '<p class="muted sm">nothing yet</p>'


def _timeline(rows, since: float, now: float, n: int = 32):
    """Returns (labels, index_of). Slots span [first row .. now].

    The axis follows the DATA, not the nominal window: a 24h window holding two
    minutes of traffic would otherwise render as one full-width bar with no shape
    in it at all."""
    if not rows:
        return [], (lambda ts: 0)
    t0 = max(since, min(float(r["ts"]) for r in rows))
    t1 = max(now, t0 + 60)
    width = (t1 - t0) / n
    fmt = "%H:%M" if (t1 - t0) < 3 * 3600 else "%H:00"
    labels = [datetime.fromtimestamp(t0 + i * width, tz=timezone.utc).strftime(fmt)
              for i in range(n)]
    return labels, (lambda ts: max(0, min(n - 1, int((float(ts) - t0) / width))))


def _series(rows, labels, index_of, keep, value) -> list[tuple[str, float]]:
    totals = [0.0] * len(labels)
    for r in rows:
        if keep(r):
            totals[index_of(r["ts"])] += value(r)
    return list(zip(labels, totals))


def _sparkline(buckets: list[tuple[str, float]], color: str) -> str:
    if not buckets:
        return '<p class="muted sm">no traffic yet</p>'
    W, H, PAD = 640, 110, 22
    peak = max(v for _, v in buckets) or 1.0
    n = len(buckets)
    bw = (W - PAD * 2) / max(1, n)
    bars = ""
    for i, (label, v) in enumerate(buckets):
        h = (v / peak) * (H - PAD * 2)
        x = PAD + i * bw
        bars += (f'<rect x="{x:.1f}" y="{H-PAD-h:.1f}" width="{max(1.0,bw-2):.1f}" '
                 f'height="{max(0.5,h):.1f}" fill="{color}" rx="1">'
                 f'<title>{html.escape(label)}: {v:g}</title></rect>')
    return (f'<svg viewBox="0 0 {W} {H}" class="chart">{bars}'
            f'<text x="{PAD}" y="{H-6}" class="ax">{html.escape(buckets[0][0])}</text>'
            f'<text x="{W-PAD}" y="{H-6}" class="ax" text-anchor="end">'
            f'{html.escape(buckets[-1][0])}</text></svg>')


def _controls(window_h: int, refresh_s: int) -> str:
    """Window + refresh links. Plain anchors, no JS: a link to the same URL IS
    a refresh, and pausing is just refresh=0."""
    def link(label, h, r, on):
        cls = " on" if on else ""
        return f'<a class="ctl{cls}" href="?hours={h}&amp;refresh={r}">{label}</a>'

    windows = "".join(link(lbl, h, refresh_s, h == window_h)
                      for lbl, h in (("1h", 1), ("24h", 24), ("7d", 168), ("30d", 720)))
    auto = (link("pause auto-refresh", window_h, 0, False) if refresh_s
            else link("auto-refresh 30s", window_h, 30, False))
    return (f'<div class="ctls"><span class="ctl-lbl">window</span>{windows}'
            f'<span class="ctl-gap"></span>'
            f'{link("refresh now", window_h, refresh_s, False)}{auto}</div>')


def build_html(ledger, config, cache, breakers, window_h: int = 24,
               banner: str = "", refresh_s: int = 0, controls: bool = False) -> str:
    """refresh_s > 0 makes the page reload itself and say so.

    Without it the page is a static render that looks identical whether it is
    two seconds or two days old -- which reads as "the dashboard is broken" the
    first time you generate traffic and the numbers don't move. The live route
    sets it; file snapshots leave it at 0 and are labelled as snapshots."""
    now = time.time()
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M:%S")
    if refresh_s:
        freshness = f"updated {stamp} UTC · refreshing every {refresh_s}s"
    elif controls:
        freshness = f"updated {stamp} UTC · auto-refresh off"
    else:
        freshness = f"static snapshot taken {stamp} UTC — it does not update"
    since = now - window_h * 3600
    rows = ledger.rows_since(since)
    served = [r for r in rows if str(r.get("outcome", OUTCOME_OK)) not in REJECTIONS]
    rejected = [r for r in rows if str(r.get("outcome", OUTCOME_OK)) in REJECTIONS]

    spend = sum(float(r.get("cost_usd", 0)) for r in served)
    tok_in = sum(int(r.get("input_tokens", 0)) for r in served)
    tok_out = sum(int(r.get("output_tokens", 0)) for r in served)
    hits = sum(1 for r in served if r.get("cache_hit"))
    # Why the non-hits didn't hit. "0% hit rate" alone can't distinguish
    # "cache broken" from "nothing repeated" from "callers opted out" -- and for
    # IC's traffic all three read differently: safety classifiers and dialogue
    # turns SET cache_bypass on purpose (decision #6), and every conversation
    # turn is unique, so a low hit rate here is the design working.
    bypassed = sum(1 for r in served if r.get("cache_skip") == "bypassed")
    streamed = sum(1 for r in served if r.get("cache_skip") == "streamed")
    missed = sum(1 for r in served if r.get("cache_skip") == "missed")
    # rows from before cache_skip existed have neither hit nor skip
    unknown = len(served) - hits - bypassed - streamed - missed
    cacheable = hits + missed          # requests the cache was allowed to serve
    cache_note = " · ".join(
        s for s in (
            f"{bypassed} bypassed by caller" if bypassed else "",
            f"{streamed} streamed (uncacheable in v1)" if streamed else "",
            f"{unknown} predate tracking" if unknown else "",
        ) if s) or "no bypass or stream traffic"
    cap = config.global_daily_cap_usd
    day = ledger.day_totals()

    # --- per-tenant (the bulkhead view) -------------------------------------
    by_client = defaultdict(lambda: {"req": 0, "tok": 0, "spend": 0.0, "rej": 0})
    for r in rows:
        c = by_client[str(r.get("client_id", "?"))]
        if str(r.get("outcome", OUTCOME_OK)) in REJECTIONS:
            c["rej"] += 1
            continue
        c["req"] += 1
        c["tok"] += int(r.get("input_tokens", 0)) + int(r.get("output_tokens", 0))
        c["spend"] += float(r.get("cost_usd", 0))
    ranked = sorted(by_client.items(), key=lambda kv: -kv[1]["spend"])
    concentration = (f"Concentration is the thing to watch — <strong>top tenant is "
                     f"{_pct(ranked[0][1]['spend'], spend):.0f}% of spend</strong>."
                     if ranked and spend else
                     "Concentration is the thing to watch; no paid spend in this "
                     "window yet." + (" (Drill/mock traffic below is $0 by design.)"
                                      if ranked else ""))

    tenant_rows = ""
    for cid, d in ranked[:12]:
        user_cap = config.user_daily_cap_usd
        head = (f"{_pct(d['spend'], user_cap):.0f}% of cap"
                if user_cap else "—")
        tenant_rows += (
            f'<tr><td><code>{html.escape(_short(cid))}</code></td>'
            f'<td class="n">{d["req"]}</td><td class="n">{d["tok"]:,}</td>'
            f'<td class="n">${_f(d["spend"])}</td>'
            f'<td class="n">{_pct(d["spend"], spend):.0f}%</td>'
            f'<td class="n">{head}</td>'
            f'<td class="n">{d["rej"] or "—"}</td></tr>')

    # --- time series ---------------------------------------------------------
    labels, slot = _timeline(rows, since, now)
    is_rejected = lambda r: str(r.get("outcome", OUTCOME_OK)) in REJECTIONS  # noqa: E731
    tok_series = _series(rows, labels, slot, lambda r: not is_rejected(r),
                         lambda r: int(r.get("input_tokens", 0)) + int(r.get("output_tokens", 0)))
    rej_series = _series(rows, labels, slot, is_rejected, lambda r: 1)

    # --- rejections by reason ------------------------------------------------
    by_reason = defaultdict(int)
    for r in rejected:
        by_reason[str(r.get("outcome"))] += 1
    reason_rows = _bar_rows(
        [(REJECT_LABEL.get(k, k), v) for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])],
        max(by_reason.values()) if by_reason else 1, "var(--warn)", lambda v: f"{int(v)}")

    # --- reliability ---------------------------------------------------------
    failovers = sum(1 for r in served if "failover_from" in str(r.get("routing_reason", "")))
    by_provider = defaultdict(int)
    for r in served:
        by_provider[str(r.get("provider", "?"))] += 1
    breaker_rows = "".join(
        f'<tr><td><code>{html.escape(p)}</code></td><td class="n">{n}</td>'
        f'<td class="n"><span class="pill {breakers.states().get(p,"closed")}">'
        f'{html.escape(breakers.states().get(p, "closed"))}</span></td></tr>'
        for p, n in sorted(by_provider.items(), key=lambda kv: -kv[1]))

    # --- latency -------------------------------------------------------------
    # `is not None`, not truthiness: a sub-millisecond call rounds to 0.0 and
    # would silently drop out of the percentiles.
    # Cache hits are excluded: they are recorded at 0.0 ms and would flatter the
    # numbers into meaninglessness. This is provider latency.
    lats = sorted(float(r["latency_ms"]) for r in served
                  if r.get("latency_ms") is not None and not r.get("cache_hit"))
    ttfts = sorted(float(r["ttft_ms"]) for r in served if r.get("ttft_ms") is not None)

    def pctl(xs, q):
        return f"{xs[min(len(xs)-1, int(len(xs)*q))]:.0f} ms" if xs else "—"

    tok_cost_note = (f"output tokens are {_pct(tok_out, tok_in + tok_out):.0f}% of volume "
                     f"but carry most of the cost") if tok_out else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
{f'<meta http-equiv="refresh" content="{refresh_s}">' if refresh_s else ''}
<title>Conduit — Gateway Operations</title>
<style>
:root{{--bg:#f7f7f9;--panel:#fff;--ink:#23262b;--muted:#6a7078;--line:#e3e5ea;
  --ok:#2f7d5d;--warn:#c2892a;--red:#c0483a;--blue:#3f7fa6;--violet:#7b6bc4}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--panel:#1e2126;--ink:#e6e8ec;
  --muted:#98a0aa;--line:#2f3440;--ok:#5fbf92;--warn:#e0b25c;--red:#e5796a;
  --blue:#6fb6d8;--violet:#a99aea}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:940px;margin:0 auto;padding:34px 22px 70px}}
h1{{font-size:23px;margin:0 0 3px}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);
  margin:30px 0 10px;font-weight:600}}
.lede{{color:var(--muted);margin:0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:15px 17px}}
.row{{display:flex;gap:11px;flex-wrap:wrap}}.row>.card{{flex:1;min-width:145px}}
.k{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.v{{font-size:26px;font-weight:700;line-height:1.2}}.v small{{font-size:14px;color:var(--muted);font-weight:400}}
.muted{{color:var(--muted)}}.sm{{font-size:12.5px}}
.chart{{width:100%;height:auto}}.ax{{fill:var(--muted);font-size:10px}}
.bar-row{{display:grid;grid-template-columns:190px 1fr 60px;gap:10px;align-items:center;
  padding:3px 0;font-size:13px}}
.bar{{background:color-mix(in srgb,var(--line) 70%,transparent);border-radius:99px;height:8px;display:block}}
.fill{{display:block;height:100%;border-radius:99px}}
.num,.n{{text-align:right;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
th.n,td.n{{text-align:right}}
code{{background:color-mix(in srgb,var(--line) 55%,transparent);padding:1px 5px;
  border-radius:4px;font-size:12.5px}}
.pill{{padding:1px 8px;border-radius:99px;font-size:11.5px;background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}}
.pill.open{{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}}
.pill.half_open{{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}}
.meter{{height:9px;border-radius:99px;background:color-mix(in srgb,var(--line) 70%,transparent);
  overflow:hidden;margin-top:7px}}
.meter i{{display:block;height:100%;border-radius:99px}}
.note{{border-left:3px solid var(--blue);padding-left:11px;margin-top:11px}}
.ctls{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:13px 0 0}}
.ctl-lbl{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  margin-right:3px}}
.ctl-gap{{flex:1;min-width:14px}}
a.ctl{{font-size:12.5px;text-decoration:none;color:var(--muted);padding:3px 10px;
  border:1px solid var(--line);border-radius:99px;background:var(--panel)}}
a.ctl:hover{{color:var(--ink);border-color:var(--muted)}}
a.ctl.on{{color:var(--ink);border-color:var(--blue);
  background:color-mix(in srgb,var(--blue) 14%,transparent)}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:99px;background:var(--ok);
  margin-right:7px;vertical-align:middle;animation:pulse 2s ease-in-out infinite}}
.dot.still{{background:var(--muted);animation:none}}
@keyframes pulse{{50%{{opacity:.25}}}}
@media (prefers-reduced-motion:reduce){{.dot{{animation:none}}}}
.banner{{margin:13px 0 0;padding:9px 13px;border-radius:8px;font-size:13px;
  background:color-mix(in srgb,var(--warn) 14%,transparent);
  border:1px solid color-mix(in srgb,var(--warn) 40%,transparent);color:var(--ink)}}
.foot{{margin-top:32px;color:var(--muted);font-size:12px}}
</style></head><body>
<div class="wrap">
  <h1>Conduit — Gateway Operations</h1>
  <p class="lede">Last {window_h}h · {len(rows)} requests · metadata only, never message content</p>
  <p class="lede sm"><span class="dot{'' if refresh_s else ' still'}"></span>{freshness}</p>
  {_controls(window_h, refresh_s) if controls else ''}
  {f'<p class="banner">{html.escape(banner)}</p>' if banner else ''}

  <h2>Spend &amp; budget</h2>
  <div class="row">
    <div class="card"><div class="k">spend today</div>
      <div class="v">${_f(day['spend_usd'])}<small> / ${_f(cap)}</small></div>
      <div class="meter"><i style="width:{min(100,_pct(day['spend_usd'],cap)):.1f}%;
        background:{'var(--red)' if _pct(day['spend_usd'],cap)>80 else 'var(--ok)'}"></i></div>
      <div class="muted sm">hard stop at 100% — requests get 402</div></div>
    <div class="card"><div class="k">served today</div><div class="v">{day['requests']}</div>
      <div class="muted sm">{day.get('rejected',0)} refused</div></div>
    <div class="card"><div class="k">tokens ({window_h}h)</div>
      <div class="v">{(tok_in+tok_out):,}</div>
      <div class="muted sm">{tok_in:,} in · {tok_out:,} out</div></div>
    <div class="card"><div class="k">cache</div>
      <div class="v">{_pct(hits, cacheable):.0f}%<small> of eligible</small></div>
      <div class="muted sm">{hits} hit · {missed} unique miss</div></div>
  </div>
  <p class="muted sm note">Cache: exact-match, so only a repeated identical request can hit.
  Of {len(served)} served: {hits} hit, {missed} unique misses, {cache_note}.
  Bypasses are deliberate — the calling app opts out for safety classifiers and live dialogue
  (a cached crisis verdict would defeat a drift check), so a low rate here is policy, not failure.</p>
  {f'<p class="muted sm note">{tok_cost_note}</p>' if tok_cost_note else ''}

  <h2>Token volume over time</h2>
  <div class="card">{_sparkline(tok_series, "var(--blue)")}</div>

  <h2>Tenant distribution — bulkhead view</h2>
  <div class="card">
    <p class="muted sm" style="margin-top:0">Rate limiting is keyed on <code>app:user</code>, not
    source IP: behind a private network every request shares one address, so an IP bucket would let
    a single noisy tenant throttle everyone. {concentration}</p>
    <table><tr><th>tenant</th><th class="n">served</th><th class="n">tokens</th>
      <th class="n">spend</th><th class="n">share</th><th class="n">of own cap</th>
      <th class="n">refused</th></tr>
      {tenant_rows or '<tr><td colspan="7" class="muted">no traffic in window</td></tr>'}
    </table></div>

  <h2>Throttles &amp; rejections</h2>
  <div class="row">
    <div class="card" style="flex:2">{reason_rows}</div>
    <div class="card"><div class="k">refused ({window_h}h)</div>
      <div class="v">{len(rejected)}</div>
      <div class="muted sm">{_pct(len(rejected), len(rows)):.0f}% of all requests</div></div>
  </div>
  <div class="card" style="margin-top:11px">{_sparkline(rej_series, "var(--warn)")}
    <p class="muted sm">A protection that never fires is untested. These are recorded refusals —
    the gateway saying no and the caller getting a clean 4xx instead of a surprise bill.</p></div>

  <h2>Reliability</h2>
  <div class="row">
    <div class="card" style="flex:2"><table>
      <tr><th>provider</th><th class="n">served</th><th class="n">breaker</th></tr>
      {breaker_rows or '<tr><td colspan="3" class="muted">no calls yet</td></tr>'}</table></div>
    <div class="card"><div class="k">failovers</div><div class="v">{failovers}</div>
      <div class="muted sm">requests that changed provider</div></div>
  </div>

  <h2>Latency</h2>
  <div class="row">
    <div class="card"><div class="k">p50</div><div class="v">{pctl(lats,.5)}</div></div>
    <div class="card"><div class="k">p95</div><div class="v">{pctl(lats,.95)}</div></div>
    <div class="card"><div class="k">TTFT p50</div><div class="v">{pctl(ttfts,.5)}</div>
      <div class="muted sm">streaming only</div></div>
  </div>

  <p class="foot">Generated {datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC ·
  tenant ids are opaque by construction (the calling app passes a pseudonymous user id) ·
  <code>fly proxy 8200:8200 -a conduit-gateway</code></p>
</div></body></html>"""


def _cli() -> None:
    import argparse
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gateway.app import create_app

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="dashboards/snapshot.html")
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()
    app = create_app()
    s = app.state
    out = Path(a.snapshot)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(s.ledger, s.config, s.cache, s.breakers, a.hours),
                   encoding="utf-8")
    print(f"snapshot -> {out.resolve()}")


if __name__ == "__main__":
    _cli()
