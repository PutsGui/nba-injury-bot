#!/usr/bin/env python3
"""Bot de lesões da NBA para Discord.

Fontes:
  - ESPN (injuries): lista geral de lesões, comentário do analista, data de regresso
  - Relatório oficial da NBA (PDF): Questionable / Doubtful / Probable / Available
  - ESPN (scoreboard): jogos do dia e horas (mostradas na hora de Lisboa)

Modos (--mode):
  changes  posta só o que MUDOU desde a última execução (por omissão)
  digest   posta o resumo do dia, organizado por jogo
  summary  posta a lista completa por estado
  check    diagnóstico: mostra o que cada fonte devolveu, não posta nada

Sem DISCORD_WEBHOOK_URL só imprime no ecrã (modo de teste).
Se o relatório oficial ou o scoreboard falharem, o bot continua só com a ESPN.
"""
import argparse
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LISBON = ZoneInfo("Europe/Lisbon")

ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"
NBA_PDF = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{d}_{t}.pdf"

DEFAULT_STATE = Path(__file__).with_name("state.json")
STATE_VERSION = 2
STICKY_HOURS = 36  # quanto tempo o estado oficial se mantém se o PDF deixar de o listar

DISCORD_UA = "DiscordBot (https://github.com/PutsGui/nba-injury-bot, 1.0)"
BROWSER_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# Do mais grave para o menos grave
EMOJI = {
    "out": "🔴",
    "doubtful": "🟠",
    "questionable": "🟡",
    "day-to-day": "🟡",
    "probable": "🟢",
    "available": "✅",
}
LABELS = {
    "out": "Out",
    "doubtful": "Doubtful",
    "questionable": "Questionable",
    "day-to-day": "Day-To-Day",
    "probable": "Probable",
    "available": "Available",
}
ORDER = list(EMOJI.keys())
OFFICIAL_STATUSES = {"out", "doubtful", "questionable", "probable", "available"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


# ----------------------------------------------------------------- utilitários
def log(msg):
    print(msg, flush=True)


def warn(msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", flush=True)
    else:
        print(f"AVISO: {msg}", flush=True)


def status_key(status):
    s = (status or "").lower().strip()
    for key in ORDER:
        if s.startswith(key):
            return key
    return ""


def emoji(status):
    return EMOJI.get(status_key(status), "⚪")


def severity(status):
    k = status_key(status)
    return ORDER.index(k) if k else len(ORDER)


def label(status):
    return LABELS.get(status_key(status), status or "?")


def nick(team):
    words = (team or "?").split()
    return words[-1] if words else "?"


def team_key(team):
    return nick(team).lower()


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("-", " "))
    toks = s.split()
    while len(toks) > 1 and toks[-1] in NAME_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def name_key(team, name):
    return (team_key(team), norm_name(name))


def short_key(team, name):
    n = norm_name(name)
    toks = n.split()
    return (team_key(team), toks[-1] if toks else "", n[:1])


def clean_text(s, limit=200):
    s = re.sub(r"[*_`~|>]", "", str(s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def clean_reason(reason):
    r = re.sub(r"^(Injury/Illness|Injury Management)\s*-\s*", "", str(reason or "").strip())
    return r.replace(";", "").strip()


def fmt_date_short(iso):
    try:
        d = datetime.fromisoformat(str(iso)[:10])
        return f"{d.strftime('%b')} {d.day}"
    except ValueError:
        return ""


def http_get(url, accept="application/json, text/plain, */*", referer="https://www.espn.com/"):
    """GET com cabeçalhos de browser. 404 sobe logo; outros erros tentam o 2.º UA."""
    last = None
    for ua in BROWSER_UAS:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": ua, "Accept": accept, "Accept-Language": "en-US,en;q=0.9", "Referer": referer},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            log(f"HTTP {e.code} em {url[:80]} (UA {ua[:30]}...)")
            last = e
        except urllib.error.URLError as e:
            log(f"Erro de rede em {url[:80]}: {e.reason}")
            last = e
    raise last


# ------------------------------------------------------------------------ ESPN
def parse_espn(data):
    out = {}
    for team in data.get("injuries", []):
        team_name = team.get("displayName", "?")
        for inj in team.get("injuries", []):
            ath = inj.get("athlete") or {}
            name = ath.get("displayName") or "?"
            pid = str(ath.get("id") or f"{norm_name(name)}|{team_key(team_name)}")
            details = inj.get("details") or {}
            bits = []
            for key in ("side", "type", "detail"):
                v = str(details.get(key) or "").strip()
                if v and v not in ("Not Specified", "None") and v not in bits:
                    bits.append(v)
            out[pid] = {
                "name": name,
                "team": team_name,
                "status": inj.get("status") or "?",
                "detail": " ".join(bits),
                "comment": clean_text(inj.get("shortComment")),
                "return": str(details.get("returnDate") or "")[:10],
            }
    return out


def parse_scoreboard(data):
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        iso = ev.get("date") or comp.get("date")
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            continue
        sides = {}
        for c in comp.get("competitors", []):
            t = c.get("team") or {}
            name = t.get("displayName") or t.get("name") or "?"
            sides[c.get("homeAway")] = {"name": name, "key": team_key(name)}
        if "home" in sides and "away" in sides:
            games.append(
                {"dt": dt, "time": dt.astimezone(LISBON).strftime("%H:%M"), "ts": f"<t:{int(dt.timestamp())}:t>",
                 "home": sides["home"], "away": sides["away"]}
            )
    games.sort(key=lambda g: g["dt"])
    return games


def games_by_team(games):
    out = {}
    for g in games:
        out[g["home"]["key"]] = {"time": g["ts"], "opp": nick(g["away"]["name"]), "home": True}
        out[g["away"]["key"]] = {"time": g["ts"], "opp": nick(g["home"]["name"]), "home": False}
    return out


# ------------------------------------------------------------- relatório oficial
def official_candidates(now):
    """URLs prováveis do PDF mais recente (hora de Nova Iorque)."""
    n = now.astimezone(ET).replace(second=0, microsecond=0)
    q = n.replace(minute=(n.minute // 15) * 15)
    for i in range(16):  # formato novo, de 15 em 15 min, até 4 h atrás
        t = q - timedelta(minutes=15 * i)
        yield NBA_PDF.format(d=t.strftime("%Y-%m-%d"), t=t.strftime("%I_%M%p")), t
    h = n.replace(minute=0)
    for i in range(5):  # formato antigo, hora a hora
        t = h - timedelta(hours=i)
        yield NBA_PDF.format(d=t.strftime("%Y-%m-%d"), t=t.strftime("%I%p")), t


def fetch_official_pdf(now, tried):
    for url, t in official_candidates(now):
        try:
            data = http_get(url, accept="application/pdf,*/*", referer="https://official.nba.com/")
        except urllib.error.HTTPError as e:
            tried.append(f"{e.code} {url.rsplit('/', 1)[-1]}")
            if e.code == 404:
                continue
            raise
        if data[:4] == b"%PDF":
            tried.append(f"200 {url.rsplit('/', 1)[-1]}")
            return data, t.strftime("%d/%m %H:%M ET")
        tried.append(f"não-PDF {url.rsplit('/', 1)[-1]}")
    raise RuntimeError("nenhum PDF encontrado nas últimas horas")


def parse_official_pdf(pdf):
    from nba_injury_report.report import _process_pdf  # dependência opcional

    return [e.model_dump() for e in _process_pdf(pdf)]


def official_fullname(raw):
    raw = (raw or "").strip()
    if "," in raw:
        last, first = raw.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return raw


def load_official(args, now):
    meta = {"ok": False, "label": "", "count": 0, "error": "", "tried": []}
    try:
        if args.official_file:
            entries = json.loads(Path(args.official_file).read_text(encoding="utf-8"))
            meta["label"] = "ficheiro de teste"
        else:
            pdf, meta["label"] = fetch_official_pdf(now, meta["tried"])
            entries = parse_official_pdf(pdf)
        out = []
        for e in entries:
            # A biblioteca devolve as colunas do PDF ("Player Name"); aceita também snake_case
            e = {str(k).lower().replace(" ", "_"): v for k, v in e.items()}
            st = str(e.get("current_status") or "").strip()
            if st.lower() not in OFFICIAL_STATUSES or not e.get("player_name") or not e.get("team"):
                continue
            out.append(
                {
                    "team": str(e["team"]).strip(),
                    "name": official_fullname(e["player_name"]),
                    "status": st.title(),
                    "reason": clean_reason(e.get("reason")),
                }
            )
        if not out:
            raise RuntimeError(f"PDF lido ({len(entries)} linhas) mas sem entradas válidas; o formato pode ter mudado")
        meta["ok"], meta["count"] = True, len(out)
        return out, meta
    except Exception as e:  # noqa: BLE001 - qualquer falha aqui só tira o oficial
        meta["error"] = f"{type(e).__name__}: {e}"
        warn(f"Relatório oficial da NBA indisponível ({meta['error']}). A continuar só com a ESPN.")
        return [], meta


# ------------------------------------------------------------------- junção
def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def fuzzy_match(o, by_team_names, threshold=0.8):
    """Último recurso: mesma equipa e nome muito parecido (acentos mal lidos, Nic/Nicolas)."""
    target = norm_name(o["name"])
    scored = sorted(
        ((SequenceMatcher(None, target, n).ratio(), pid) for pid, n in by_team_names.get(team_key(o["team"]), [])),
        reverse=True,
    )
    if scored and scored[0][0] >= threshold and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1]
    return None


def merge(espn, official, old_players, now):
    """Junta ESPN + oficial. O estado oficial (mais fino) tem prioridade."""
    players, idx_full, idx_short, by_team_names = {}, {}, {}, {}
    for pid, p in espn.items():
        players[pid] = {
            "name": p["name"], "team": p["team"], "status": p["status"], "espn_status": p["status"],
            "detail": p["detail"], "comment": p["comment"], "return": p["return"],
            "nba_status": "", "nba_reason": "", "nba_seen": "",
        }
        idx_full[name_key(p["team"], p["name"])] = pid
        idx_short.setdefault(short_key(p["team"], p["name"]), []).append(pid)
        by_team_names.setdefault(team_key(p["team"]), []).append((pid, norm_name(p["name"])))

    now_iso = now.isoformat()
    matched = set()
    for o in official:
        pid = idx_full.get(name_key(o["team"], o["name"]))
        if pid is None:
            cands = idx_short.get(short_key(o["team"], o["name"]), [])
            pid = cands[0] if len(cands) == 1 else None
        if pid is None:
            pid = fuzzy_match(o, by_team_names)
        if pid is None:
            if o["status"].lower() == "available":
                continue  # sem lesão listada e sem registo ESPN: não interessa
            pid = f"nba|{team_key(o['team'])}|{norm_name(o['name'])}"
            players[pid] = {
                "name": o["name"], "team": o["team"], "status": o["status"], "espn_status": "",
                "detail": o["reason"], "comment": "", "return": "",
                "nba_status": "", "nba_reason": "", "nba_seen": "",
            }
        rec = players[pid]
        rec.update(status=o["status"], nba_status=o["status"], nba_reason=o["reason"], nba_seen=now_iso)
        if not rec["detail"]:
            rec["detail"] = o["reason"]
        matched.add(pid)

    # Se o PDF deixar de listar o jogador (ou falhar), mantém o estado oficial
    # recente enquanto a ESPN não mudar. Evita alertas falsos de ida e volta.
    for pid, rec in players.items():
        if pid in matched or pid.startswith("nba|"):
            continue
        old = old_players.get(pid)
        if not old or not old.get("nba_status") or old.get("espn_status") != rec["espn_status"]:
            continue
        seen = parse_iso(old.get("nba_seen"))
        if seen and now - seen < timedelta(hours=STICKY_HOURS):
            rec.update(status=old["nba_status"], nba_status=old["nba_status"],
                       nba_reason=old.get("nba_reason", ""), nba_seen=old["nba_seen"])
    return players


# ------------------------------------------------------------------ mensagens
def game_suffix(p, gmap):
    g = gmap.get(team_key(p["team"]))
    if not g:
        return ""
    return f" · 🕐 {g['time']} {'vs' if g['home'] else '@'} {g['opp']}"


def entry_lines(p, gmap, trans=None):
    txt = f"{emoji(p['status'])} **{p['name']}** ({nick(p['team'])}) · {trans or label(p['status'])}"
    if p["detail"]:
        txt += f" · {p['detail']}"
    txt += game_suffix(p, gmap)
    lines = [txt]
    extra = []
    if p.get("comment"):
        extra.append(p["comment"])
    if p.get("return") and status_key(p["status"]) == "out":
        d = fmt_date_short(p["return"])
        if d:
            extra.append(f"est. return {d}")
    if extra:
        lines.append("↳ *" + " · ".join(extra) + "*")
    return lines


def build_changes(old, new, gmap):
    old_by_name = {name_key(o["team"], o["name"]): o for o in old.values()}
    new_names = {name_key(p["team"], p["name"]) for p in new.values()}
    news, changed, cleared = [], [], []
    for pid, p in new.items():
        o = old.get(pid) or old_by_name.get(name_key(p["team"], p["name"]))
        if o is None:
            news += entry_lines(p, gmap)
        elif o["status"] != p["status"]:
            lost_official = bool(o.get("nba_status")) and not p.get("nba_status") \
                and o.get("espn_status") == p.get("espn_status")
            if lost_official:
                continue  # o PDF deixou de listar o jogador, a ESPN não mudou: não é notícia
            changed += entry_lines(p, gmap, f"{label(o['status'])} ➜ {label(p['status'])}")
    for pid, o in old.items():
        if pid.startswith("nba|") or name_key(o["team"], o["name"]) in new_names:
            continue  # só jogadores da ESPN geram "removido"
        cleared.append(f"✅ **{o['name']}** ({nick(o['team'])}) · removed from the report")
    lines = []
    if news:
        lines += ["**🆕 New on the report**"] + news + [""]
    if changed:
        lines += ["**🔄 Status changed**"] + changed + [""]
    if cleared:
        lines += ["**✅ Removed from the report**"] + cleared + [""]
    return lines


def build_summary(players):
    lines, current = [], None
    for p in sorted(players.values(), key=lambda p: (severity(p["status"]), p["team"], p["name"])):
        group = label(p["status"])
        if group != current:
            if current is not None:
                lines.append("")
            lines.append(f"**{emoji(group)} {group}**")
            current = group
        txt = f"{emoji(p['status'])} **{p['name']}** ({nick(p['team'])})"
        if p["detail"]:
            txt += f" · {p['detail']}"
        lines.append(txt)
    return lines


def short(s, n=34):
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def team_digest_lines(team_name, tkey, by_team):
    groups = {}
    for p in by_team.get(tkey, []):
        k = status_key(p["status"])
        if k == "available":
            continue
        groups.setdefault(k or "?", []).append(p)
    lines = [f"**{nick(team_name)}**"]
    if not groups:
        return lines + ["✅ No injuries reported"]
    for k in sorted(groups, key=lambda k: ORDER.index(k) if k in ORDER else 99):
        items = []
        for p in sorted(groups[k], key=lambda p: p["name"]):
            nm = p["name"] if k == "out" else f"**{p['name']}**"
            items.append(nm + (f" ({short(p['detail'])})" if p["detail"] else ""))
        lines.append(f"{EMOJI.get(k, '⚪')} {LABELS.get(k, k)}: " + ", ".join(items))
    return lines


def build_digest(players, games, slate, off_meta):
    head = f"🏀 **Daily summary · {slate.strftime('%b')} {slate.day}**"
    foot = []
    if off_meta["ok"]:
        foot.append(f"-# Sources: official NBA report ({off_meta['label']}) + ESPN")
    else:
        foot.append("-# ⚠️ Official NBA report unavailable or empty: ESPN only (no Questionable/Doubtful)")
    if not games:
        lines = build_summary(players)
        return head + " (today's games unavailable, full list)\n", lines, foot
    by_team = {}
    for p in players.values():
        by_team.setdefault(team_key(p["team"]), []).append(p)
    blocks = []
    for g in games:
        lines = [f"**🕐 {g['ts']} · {g['away']['name']} @ {g['home']['name']}**"]
        lines += team_digest_lines(g["away"]["name"], g["away"]["key"], by_team)
        lines += team_digest_lines(g["home"]["name"], g["home"]["key"], by_team)
        blocks.append("\n".join(lines))
    return head + f" · {len(games)} game{'s' if len(games) != 1 else ''}\n", blocks, foot


def pack(parts, header, footer, limit=1800, sep="\n\n"):
    """Junta blocos em mensagens ≤ limit sem partir um bloco ao meio (se couber)."""
    msgs, buf = [], header
    items = []
    for part in parts:
        if len(part) > limit - 50:  # bloco gigante: parte por linhas
            cur = ""
            for ln in part.split("\n"):
                if len(cur) + len(ln) + 1 > limit - 50:
                    items.append(cur.rstrip())
                    cur = ""
                cur += ln + "\n"
            if cur.strip():
                items.append(cur.rstrip())
        else:
            items.append(part)
    for it in items + ["\n".join(footer)]:
        if it and len(buf) + len(it) + len(sep) > limit and buf.strip():
            msgs.append(buf.rstrip())
            buf = ""
        buf += (sep if buf.strip() and not buf.endswith("\n") else "") + it + "\n"
    if buf.strip():
        msgs.append(buf.rstrip())
    return msgs


def chunk_lines(lines, header, limit=1800):
    parts, buf = [], header
    for ln in lines:
        if len(buf) + len(ln) + 1 > limit:
            parts.append(buf)
            buf = ""
        buf += ln + "\n"
    if buf.strip():
        parts.append(buf)
    return parts


# --------------------------------------------------------------------- Discord
def send(webhook, text):
    body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode()
    for _ in range(3):
        req = urllib.request.Request(
            webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": DISCORD_UA}, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            raise
    raise RuntimeError("Discord: demasiados pedidos (429)")


def publish_messages(messages):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and os.environ.get("GITHUB_ACTIONS"):
        sys.exit("ERRO: segredo DISCORD_WEBHOOK_URL em falta ou vazio no GitHub.")
    for m in messages:
        if webhook:
            send(webhook, m)
            time.sleep(1)
        else:
            print("--- (modo teste, sem webhook) ---")
            print(m)


# ----------------------------------------------------------------------- estado
def load_state(path):
    """Devolve (players, existe, legado). Estado sem _v é do formato antigo."""
    if not path.exists():
        return {}, False, False
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("_v") != STATE_VERSION:
        return {}, True, True
    return data.get("players", {}), True, False


def save_state(path, players):
    path.write_text(
        json.dumps({"_v": STATE_VERSION, "players": players}, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )


# ------------------------------------------------------------------------ main
def run_check(espn, games, off_meta, official, players):
    log("=== DIAGNÓSTICO ===")
    log(f"ESPN lesões: {len(espn)} jogadores")
    for p in list(espn.values())[:3]:
        log(f"  ex.: {p['name']} ({p['team']}) {p['status']} | {p['detail']} | {p['comment'][:60]}")
    log(f"ESPN scoreboard: {len(games)} jogos")
    for g in games:
        log(f"  {g['time']} (Lisboa) {g['away']['name']} @ {g['home']['name']}")
    log(f"Oficial NBA: {'OK' if off_meta['ok'] else 'FALHOU'} | {off_meta['label']} | {off_meta['count']} entradas")
    if off_meta["error"]:
        log(f"  erro: {off_meta['error']}")
    log("  tentativas: " + (", ".join(off_meta["tried"][-6:]) or "-"))
    for o in official[:5]:
        log(f"  ex.: {o['name']} ({o['team']}) {o['status']} | {o['reason']}")
    only = [p for pid, p in players.items() if pid.startswith("nba|")]
    log(f"Jogadores só no oficial (sem par na ESPN): {len(only)}")
    for p in only[:8]:
        log(f"  {p['name']} ({p['team']}) {p['status']}")
    dist = {}
    for p in players.values():
        dist[label(p["status"])] = dist.get(label(p["status"]), 0) + 1
    log(f"Estados após junção: {dist}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["changes", "digest", "summary", "check"], default="changes")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--file", help="(teste) JSON da ESPN lesões")
    ap.add_argument("--scoreboard-file", help="(teste) JSON do scoreboard")
    ap.add_argument("--official-file", help="(teste) JSON com entradas do relatório oficial")
    ap.add_argument("--now", help="(teste) data/hora UTC ISO")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now else datetime.now(timezone.utc)
    slate = (now.astimezone(ET) - timedelta(hours=6)).date()

    # 1) ESPN lesões: obrigatório
    raw = json.loads(Path(args.file).read_text(encoding="utf-8")) if args.file else json.loads(http_get(ESPN_INJURIES))
    espn = parse_espn(raw)
    if not espn:
        sys.exit("Feed vazio ou formato inesperado, a abortar sem postar nada.")

    # 2) Jogos do dia: opcional
    games = []
    try:
        if args.scoreboard_file:
            sb = json.loads(Path(args.scoreboard_file).read_text(encoding="utf-8"))
        else:
            sb = json.loads(http_get(ESPN_SCOREBOARD.format(d=slate.strftime("%Y%m%d"))))
        games = parse_scoreboard(sb)
    except Exception as e:  # noqa: BLE001
        warn(f"Scoreboard indisponível ({type(e).__name__}: {e}). Sem horas de jogo.")
    gmap = games_by_team(games)

    # 3) Relatório oficial: opcional
    official, off_meta = load_official(args, now)

    state_path = Path(args.state)
    old, exists, legacy = load_state(state_path)
    players = merge(espn, official, old, now)

    if args.mode == "check":
        return run_check(espn, games, off_meta, official, players)

    if args.mode == "summary":
        publish_messages(chunk_lines(build_summary(players), "🏀 **NBA injuries: full list**\n"))
        return

    if args.mode == "digest":
        head, blocks, foot = build_digest(players, games, slate, off_meta)
        if games:
            publish_messages(pack(blocks, head, foot))
        else:
            publish_messages(chunk_lines(blocks + [""] + foot, head))
        return

    # modo changes
    if legacy:
        save_state(state_path, players)
        log("Estado antigo migrado em silêncio (formato novo). Nada postado.")
        return
    if not exists:
        publish_messages(chunk_lines(build_summary(players), "🏀 **NBA injuries: bot online, current status**\n"))
        save_state(state_path, players)
        log(f"Primeira execução: resumo postado e estado guardado ({len(players)} jogadores).")
        return

    old_espn = sum(1 for k in old if not k.startswith("nba|"))
    if old_espn >= 10 and len(espn) < old_espn * 0.5:
        log(f"Feed suspeito ({len(espn)} vs {old_espn} antes). Execução saltada.")
        return

    lines = build_changes(old, players, gmap)
    if lines:
        publish_messages(chunk_lines(lines, "🏀 **NBA injuries: update**\n"))
    else:
        log("Sem alterações.")
    save_state(state_path, players)  # só depois de postar com sucesso


if __name__ == "__main__":
    main()
