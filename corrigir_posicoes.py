"""
corrigir_posicoes.py — Reprocessa TODAS as partidas do DB para corrigir a rota.

Re-busca cada partida na Riot API (as 2 chaves dev em paralelo), roda a MESMA inferência
dos crawlers (deteccao_rota.inferir_rotas_partida) e, para cada linha (participante):
  - se a rota é confiável  → UPDATE: posicao = rota inferida + grava os sinais crus
                              (puuid, individual_position, lane, role, smites, ...);
  - se NÃO é confiável     → DELETE da linha (descartada, como pedido).

Robusto e RESUMÍVEL: a tabela `correcao_progresso` marca cada match_id; relançar o script
continua de onde parou (e retenta os que deram 'erro'). Faz commit por partida.

Tolerante a CHAVE EXPIRADA (dev expira a cada 24h): ver GerenciadorChaves — usa a chave
saudável, e se TODAS expirarem AGUARDA você gerar nova(s) e salvar no .env, retomando sozinho
sem perder progresso. Em 429 alterna de chave (cooldown) em vez de travar ~100s.

Uso:
  python3 corrigir_posicoes.py --dry-run --limit 30   # amostra: só mostra o impacto
  python3 corrigir_posicoes.py                         # corrida real (todas as pendentes)
"""

import os
import sys
import time
import queue
import argparse
import threading
import sqlite3

import requests
from dotenv import load_dotenv

from deteccao_rota import inferir_rotas_partida
from ingest_crawler import garantir_colunas

DB = "meu_meta_dataset_global.db"

# Prefixo do match_id → roteamento regional do match-v5.
PREFIX_MACRO = {
    "BR1": "americas", "LA1": "americas", "LA2": "americas", "NA1": "americas", "OC1": "americas",
    "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe", "ME1": "europe",
    "KR": "asia", "JP1": "asia",
}

UPDATE_SQL = """UPDATE estatisticas_meta SET
    posicao=?, puuid=?, team_id=?, team_position=?, individual_position=?,
    lane=?, role=?, summoner1_id=?, summoner2_id=?, posicao_apoio=?
    WHERE id=?"""

INTERVALO = 1.3  # s entre chamadas por chave (dev: 100 req/2min → margem segura ~46/min)
RETRY_EXPIRADA = 300  # s: chave marcada expirada volta a ser RE-SONDADA depois disso
                      # (um 401/403 espurio nao a mata pra sempre → recupera sozinha)


def macro_de(match_id: str):
    return PREFIX_MACRO.get(match_id.split("_", 1)[0])


class GerenciadorChaves:
    """Pool de chaves dev com pacing por chave, cooldown em 429 e HOT-RELOAD em expiração.

    - Os workers pegam SEMPRE a chave válida disponível mais cedo → quando uma bate no rate
      limit (429), entra em cooldown e o trabalho continua na outra (sem ficar 100s parado).
    - Quando uma chave EXPIRA (401/403 — dev expira a cada 24h), é marcada como morta; o job
      segue na chave saudável. Se TODAS expirarem, ele AGUARDA, relendo o .env a cada poucos
      segundos, e só retoma quando o valor de uma chave mudar (você gerou outra e salvou).
    Nenhum progresso é perdido nesse meio-tempo (a partida só é processada quando há chave)."""

    def __init__(self, nomes, intervalo):
        self.nomes = nomes
        self.intervalo = intervalo
        self.lock = threading.Lock()
        self.chaves = [{"nome": n, "valor": self._ler(n), "next_ok": 0.0,
                        "expirada": False, "retry_em": 0.0} for n in nomes]

    @staticmethod
    def _ler(nome):
        load_dotenv(override=True)
        v = (os.getenv(nome) or "").replace('"', "").replace("'", "").strip()
        return v or None

    def recarregar(self):
        """Relê o .env; des-expira na hora a chave cujo VALOR mudou (você colou uma nova).
        Chaves falso-expiradas (valor inalterado) recuperam pela re-sondagem em adquirir().
        Retorna True se alguma chave voltou a ficar utilizável."""
        with self.lock:
            voltou = False
            for c in self.chaves:
                novo = self._ler(c["nome"])
                if novo and novo != c["valor"]:
                    c["valor"], c["expirada"], c["next_ok"], c["retry_em"] = novo, False, 0.0, 0.0
                    voltou = True
            return voltou

    def adquirir(self):
        """Bloqueia o mínimo até uma chave válida ficar livre e reserva o próximo slot dela."""
        avisou = False
        while True:
            with self.lock:
                agora = time.time()
                # chave marcada expirada volta a ser TENTADA apos o cooldown: se foi um
                # 401/403 espurio (chave ainda valida), ela se recupera sozinha aqui.
                for c in self.chaves:
                    if c["expirada"] and c["valor"] and c["retry_em"] <= agora:
                        c["expirada"] = False
                disp = [c for c in self.chaves if c["valor"] and not c["expirada"]]
                if disp:
                    c = min(disp, key=lambda x: x["next_ok"])
                    espera = c["next_ok"] - agora
                    if espera <= 0:
                        c["next_ok"] = agora + self.intervalo
                        return c
            if not disp:
                if not avisou:
                    print("⏳ TODAS as chaves expiraram/inválidas. Gere nova(s) e salve no .env "
                          "(RIOT_API_KEY / RIOT_API_KEY2) — o job retoma sozinho.", flush=True)
                    avisou = True
                time.sleep(5)
                self.recarregar()
            else:
                time.sleep(min(max(espera, 0.0), 0.5))

    def marcar_429(self, c, segundos):
        with self.lock:
            c["next_ok"] = max(c["next_ok"], time.time() + segundos)

    def marcar_expirada(self, c):
        with self.lock:
            if not c["expirada"]:
                print(f"❌ Chave {c['nome']} expirou/invalidou (401/403). Usando a outra; "
                      f"cole uma nova no .env quando puder.", flush=True)
            c["expirada"] = True
            c["retry_em"] = time.time() + RETRY_EXPIRADA  # re-sonda depois (pode ser falso 401/403)
        self.recarregar()  # talvez você já tenha colado a nova

    def alguma_valida(self):
        with self.lock:
            return any(c["valor"] and not c["expirada"] for c in self.chaves)


def fetch(url: str, mgr: GerenciadorChaves):
    """Retorna ('ok', json) | ('gone', None) [404/sumiu]. NUNCA falha por chave: em 429 troca de
    chave/aguarda cooldown; em 401/403 marca expirada e usa a outra (ou aguarda nova no .env).
    Só retorna 'erro' após várias falhas de rede/5xx seguidas."""
    falhas = 0
    while True:
        c = mgr.adquirir()
        try:
            r = requests.get(url, headers={"X-Riot-Token": c["valor"]}, timeout=15)
        except Exception:
            falhas += 1
            if falhas >= 6:
                return "erro", None
            time.sleep(2)
            continue
        sc = r.status_code
        if sc == 200:
            return "ok", r.json()
        if sc == 429:
            mgr.marcar_429(c, int(r.headers.get("Retry-After", 10)) + 1)
            continue
        if sc in (404, 400, 422):
            return "gone", None
        if sc in (401, 403):
            mgr.marcar_expirada(c)
            continue
        falhas += 1                      # 5xx e outros transitórios
        if falhas >= 6:
            return "erro", None
        time.sleep(2)


def worker(mgr, work_q, db_lock, conn, ct, dry):
    while True:
        try:
            m_id = work_q.get_nowait()
        except queue.Empty:
            return
        macro = macro_de(m_id)
        status, deletados, atualizados = "erro", 0, 0

        if not macro:
            status = "sem_dados"  # prefixo desconhecido → não dá pra re-buscar
        else:
            tipo, data = fetch(f"https://{macro}.api.riotgames.com/lol/match/v5/matches/{m_id}", mgr)
            if tipo == "gone":
                status = "sem_dados"   # partida sumiu da API → deixa as linhas como estão
            elif tipo == "ok":
                rotas = inferir_rotas_partida(data["info"])
                por_champ = {p.get("championName"): p for p in data["info"]["participants"]}
                with db_lock:
                    rows = conn.execute(
                        "SELECT id, campeao FROM estatisticas_meta WHERE match_id=?", (m_id,)
                    ).fetchall()
                    for rid, campeao in rows:
                        p = por_champ.get(campeao)
                        if not p:
                            continue  # campeão não casou (raro) → preserva a linha
                        inf = rotas.get(p.get("puuid"), {})
                        if not inf.get("confiavel"):
                            if not dry:
                                conn.execute("DELETE FROM estatisticas_meta WHERE id=?", (rid,))
                            deletados += 1
                        else:
                            if not dry:
                                conn.execute(UPDATE_SQL, (
                                    inf["rota"], p.get("puuid"), p.get("teamId"),
                                    p.get("teamPosition"), p.get("individualPosition"),
                                    p.get("lane"), p.get("role"),
                                    p.get("summoner1Id"), p.get("summoner2Id"),
                                    inf.get("apoio"), rid,
                                ))
                            atualizados += 1
                    status = "ok"
            # tipo == 'erro' → mantém status 'erro' (será retentado num próximo lançamento)

        with db_lock:
            if not dry:
                conn.execute(
                    "INSERT OR REPLACE INTO correcao_progresso(match_id,status,deletados,atualizados,ts) "
                    "VALUES (?,?,?,?,?)", (m_id, status, deletados, atualizados, int(time.time())))
                conn.commit()

        with ct["lock"]:
            ct["n"] += 1
            ct["del"] += deletados
            ct["upd"] += atualizados
            ct[status] = ct.get(status, 0) + 1
            n = ct["n"]
        if n % 200 == 0:
            print(f"[{n}/{ct['total']}] upd={ct['upd']} del={ct['del']} "
                  f"sem_dados={ct.get('sem_dados',0)} erro={ct.get('erro',0)}", flush=True)
        # pacing agora é central (GerenciadorChaves.adquirir) — sem sleep por thread aqui.


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="não grava nada; só conta o impacto")
    ap.add_argument("--limit", type=int, default=0, help="processa no máximo N partidas (amostra)")
    args = ap.parse_args()

    mgr = GerenciadorChaves(["RIOT_API_KEY", "RIOT_API_KEY2"], INTERVALO)
    n_chaves = sum(1 for c in mgr.chaves if c["valor"])
    if n_chaves == 0:
        print("❌ Nenhuma chave no .env"); sys.exit(1)

    conn = sqlite3.connect(DB, timeout=120, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=120000;")
    garantir_colunas(conn)
    # Índice em match_id é ESSENCIAL: sem ele, o SELECT/DELETE por match_id faz full scan
    # de 4,3M linhas a cada partida (inviável). Com ele vira um seek.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_id ON estatisticas_meta(match_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS correcao_progresso (
        match_id TEXT PRIMARY KEY, status TEXT, deletados INTEGER, atualizados INTEGER, ts INTEGER)""")
    conn.commit()

    todos = [r[0] for r in conn.execute("SELECT DISTINCT match_id FROM estatisticas_meta")]
    feitos = {r[0] for r in conn.execute(
        "SELECT match_id FROM correcao_progresso WHERE status IN ('ok','sem_dados')")}
    pend = [m for m in todos if m not in feitos]
    if args.limit:
        pend = pend[:args.limit]

    print(f"Partidas no DB: {len(todos)} | já feitas: {len(feitos)} | a processar agora: {len(pend)}")
    if args.dry_run:
        print("⚠️  DRY-RUN: nenhuma alteração será gravada.")
    eta_h = len(pend) * INTERVALO / max(1, n_chaves) / 3600
    print(f"Chaves ativas: {n_chaves} | ETA estimada: ~{eta_h:.1f} h")
    if not pend:
        return

    work_q = queue.Queue()
    for m in pend:
        work_q.put(m)
    ct = {"lock": threading.Lock(), "n": 0, "del": 0, "upd": 0, "total": len(pend)}
    db_lock = threading.Lock()

    # Uma thread a mais que as chaves: mantém a chave saudável saturada quando a outra
    # estiver em cooldown/expirada (o pacing por chave evita estourar o limite).
    n_threads = n_chaves + 1
    threads = [threading.Thread(target=worker, args=(mgr, work_q, db_lock, conn, ct, args.dry_run),
                                daemon=True) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("\n===== FIM =====")
    print(f"Processadas: {ct['n']} | UPDATEs: {ct['upd']} | DELETEs: {ct['del']} | "
          f"sem_dados: {ct.get('sem_dados',0)} | erro: {ct.get('erro',0)}")
    if ct["upd"] + ct["del"]:
        pct = 100 * ct["del"] / (ct["upd"] + ct["del"])
        print(f"Taxa de descarte: {pct:.2f}% das linhas reprocessadas")


if __name__ == "__main__":
    main()
