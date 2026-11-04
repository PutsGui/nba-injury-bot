#!/usr/bin/env python3
"""Bot de lesões da NBA para Discord (fonte: feed público da ESPN).

Uso:
  python nba_injuries.py            -> posta só o que MUDOU desde a última vez
  python nba_injuries.py --summary  -> posta o resumo completo atual
Sem DISCORD_WEBHOOK_URL definido, só imprime no ecrã (modo de teste).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
DEFAULT_STATE = Path(__file__).with_name("state.json")
UA = "DiscordBot (https://github.com/PutsGui/nba-injury-bot, 1.0)"  # usado só no Discord

# Ordem de gravidade e cor de cada estado
EMOJI = {
    "out": "🔴",
    "doubtful": "🟠",
    "questionable": "🟡",
    "day-to-day": "🟡",
    "probable": "🟢",
}
ORDER = list(EMOJI.keys())


def emoji(status):
    s = status.lower().strip()
    for key, icon in EMOJI.items():
        if s.startswith(key):
            return icon
    return "⚪"


def severity(status):
    s = status.lower().strip()
    for i, key in enumerate(ORDER):
        if s.startswith(key):
            return i
    return len(ORDER)


BROWSER_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]


def fetch():
    """Vai buscar o feed da ESPN, tentando cabeçalhos de browser se for recusado."""
    last = None
    for ua in BROWSER_UAS:
        req = urllib.request.Request(
            ESPN_URL,
            headers={
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.espn.com/",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            print(f"ESPN devolveu HTTP {e.code} com '{ua[:40]}...'")
            last = e
        except urllib.error.URLError as e:
            print(f"Erro de rede: {e.reason}")
            last = e
    raise last


def parse(data):
    """Transforma o JSON da ESPN num dicionário simples, um registo por jogador."""
    out = {}
    for team in data.get("injuries", []):
        team_name = team.get("displayName", "?")
        for inj in team.get("injuries", []):
            ath = inj.get("athlete") or {}
            name = ath.get("displayName") or "?"
            pid = str(ath.get("id") or f"{name}|{team_name}")
            details = inj.get("details") or {}
            # A ESPN pode usar "type" (parte do corpo) e "detail" (ex.: Soreness).
            # Junta o que existir, sem repetir, ex.: "Left Knee Soreness".
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
                "comment": inj.get("shortComment") or "",
            }
    return out


def line(p, extra=None):
    txt = f"{emoji(p['status'])} **{p['name']}** ({p['team']}) · {extra or p['status']}"
    if p["detail"]:
        txt += f" · {p['detail']}"
    return txt


def build_changes(old, new):
    news, changed, cleared = [], [], []
    for pid, p in new.items():
        o = old.get(pid)
        if o is None:
            news.append(line(p))
        elif o["status"] != p["status"]:
            changed.append(line(p, f"{o['status']} ➜ {p['status']}"))
    for pid, o in old.items():
        if pid not in new:
            cleared.append(f"✅ **{o['name']}** ({o['team']}) · saiu do relatório")
    lines = []
    if news:
        lines += ["**🆕 Novos no relatório**"] + news + [""]
    if changed:
        lines += ["**🔄 Estado alterado**"] + changed + [""]
    if cleared:
        lines += ["**✅ Removidos do relatório**"] + cleared + [""]
    return lines


def build_summary(new):
    lines = []
    ordered = sorted(new.values(), key=lambda p: (severity(p["status"]), p["team"], p["name"]))
    current = None
    for p in ordered:
        group = p["status"]
        if group != current:
            if current is not None:
                lines.append("")
            lines.append(f"**{emoji(group)} {group}**")
            current = group
        txt = f"{emoji(p['status'])} **{p['name']}** ({p['team']})"
        if p["detail"]:
            txt += f" · {p['detail']}"
        lines.append(txt)
    return lines


def chunks(lines, header, limit=1800):
    parts, buf = [], header
    for ln in lines:
        if len(buf) + len(ln) + 1 > limit:
            parts.append(buf)
            buf = ""
        buf += ln + "\n"
    if buf.strip():
        parts.append(buf)
    return parts


def send(webhook, text):
    body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode()
    for _ in range(3):
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
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


def publish(lines, header):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and os.environ.get("GITHUB_ACTIONS"):
        # Sem isto, uma falha no segredo daria "verde" sem postar nada.
        sys.exit("ERRO: segredo DISCORD_WEBHOOK_URL em falta ou vazio no GitHub.")
    for part in chunks(lines, header):
        if webhook:
            send(webhook, part)
            time.sleep(1)
        else:
            print("--- (modo teste, sem webhook) ---")
            print(part)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true", help="posta o resumo completo")
    ap.add_argument("--file", help="ler JSON local em vez de ir à ESPN (testes)")
    ap.add_argument("--state", default=str(DEFAULT_STATE), help="ficheiro de estado")
    args = ap.parse_args()

    if args.file:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        data = fetch()

    current = parse(data)
    if not current:
        # Proteção: feed vazio ou formato mudou. Não postar "toda a gente recuperou".
        sys.exit("Feed vazio ou formato inesperado, a abortar sem postar nada.")

    if args.summary:
        publish(build_summary(current), "🏀 **Lesões NBA: resumo atual**\n")
        return

    state_path = Path(args.state)
    dump = lambda: json.dumps(current, ensure_ascii=False, indent=1, sort_keys=True)
    if not state_path.exists():
        # Primeira execução: posta o resumo (prova que está vivo) e só depois guarda.
        publish(build_summary(current), "🏀 **Lesões NBA: bot ligado, estado atual**\n")
        state_path.write_text(dump(), encoding="utf-8")
        print(f"Primeira execução: resumo postado e estado guardado ({len(current)} jogadores).")
        return

    old = json.loads(state_path.read_text(encoding="utf-8"))
    # Proteção: se o feed encolher mais de metade de repente, é quase de certeza
    # um feed incompleto. Salta esta execução sem postar e sem guardar.
    if len(old) >= 10 and len(current) < len(old) * 0.5:
        print(f"Feed suspeito ({len(current)} vs {len(old)} antes). Execução saltada.")
        return
    lines = build_changes(old, current)
    if lines:
        publish(lines, "🏀 **Lesões NBA: atualização**\n")
    else:
        print("Sem alterações.")
    # Só guarda depois de postar com sucesso, para tentar de novo se falhar
    state_path.write_text(dump(), encoding="utf-8")


if __name__ == "__main__":
    main()
