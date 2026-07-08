"""Pacer central para a Riot API — uma unica chave compartilhada entre processos.

Todo processo desta VPS que fala com a Riot (crawler.py, crawlerHighElo.py, jobs
futuros) deve chamar PACER.aguardar() antes de QUALQUER requisicao e
PACER.observar(headers) apos cada resposta. A coordenacao e cross-process: o
estado (proximo slot livre + intervalo vigente) vive num arquivo JSON protegido
por fcntl.flock, entao a SOMA das requisicoes de todos os processos respeita o
orcamento da chave — nao cada processo isoladamente.

O intervalo e derivado do header X-App-Rate-Limit (ex.: "20:1,100:120" = 20 req/1s
e 100 req/120s): vale a janela mais restritiva. Assim o mesmo codigo serve a
personal key de hoje e a production key de amanha sem mudar nada — os limites
reais chegam na primeira resposta.

FATOR_USO reserva folga na chave para consumidores FORA desta coordenacao (o app
EloRise no Streamlit Cloud usa a mesma chave de outro servidor): com 0.7, os
processos daqui usam no maximo 70% do orcamento. Ajustavel sem editar codigo via
RIOT_PACER_FATOR no ambiente/.env.
"""

import fcntl
import json
import os
import time

FATOR_USO = float(os.getenv("RIOT_PACER_FATOR", "0.7"))
# Fallback ate a primeira resposta trazer o X-App-Rate-Limit real:
# assume o orcamento de personal/dev key (100 req / 2 min).
INTERVALO_INICIAL = (120.0 / 100.0) / FATOR_USO
ARQUIVO_ESTADO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".riot_pacer_estado.json"
)


class RiotPacer:
    def __init__(self, arquivo=ARQUIVO_ESTADO, fator_uso=FATOR_USO):
        self.arquivo = arquivo
        self.fator_uso = fator_uso

    def _transacao(self, fn):
        """Le o estado sob flock, aplica fn(estado) -> estado, grava e destrava.

        flock e obrigatorio: sem ele dois processos leriam o mesmo 'proximo' e
        disparariam no mesmo slot, dobrando a taxa real sobre a chave.
        """
        fd = os.open(self.arquivo, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            bruto = os.read(fd, 4096)
            try:
                estado = json.loads(bruto)
            except (ValueError, TypeError):
                estado = {"proximo": 0.0, "intervalo": INTERVALO_INICIAL}
            estado = fn(estado)
            novo = json.dumps(estado).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, novo)
            return estado
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def aguardar(self):
        """Reserva o proximo slot global e dorme ate ele. Chamar antes de TODA requisicao."""
        agora = time.time()
        slot = {"t": agora}

        def reservar(estado):
            slot["t"] = max(agora, float(estado.get("proximo", 0.0)))
            estado["proximo"] = slot["t"] + float(
                estado.get("intervalo", INTERVALO_INICIAL)
            )
            return estado

        self._transacao(reservar)
        espera = slot["t"] - time.time()
        if espera > 0:
            time.sleep(espera)  # dorme FORA do lock: outros processos reservam os slots seguintes

    def observar(self, headers):
        """Ajusta o intervalo global a partir do X-App-Rate-Limit da resposta.

        Formato Riot: "limite:janela_s" separados por virgula. O intervalo minimo
        sustentavel e o max(janela/limite); dividimos pelo fator_uso para deixar
        folga aos consumidores fora desta coordenacao.
        """
        cru = headers.get("X-App-Rate-Limit", "")
        if not cru:
            return
        try:
            pares = [p.split(":") for p in cru.split(",")]
            base = max(float(janela) / float(limite) for limite, janela in pares)
        except (ValueError, ZeroDivisionError, IndexError):
            return
        intervalo = base / self.fator_uso

        def atualizar(estado):
            atual = float(estado.get("intervalo", INTERVALO_INICIAL))
            if abs(atual - intervalo) / intervalo > 0.01:
                estado["intervalo"] = intervalo
            return estado

        self._transacao(atualizar)

    def penalizar(self, segundos):
        """Apos um 429, empurra o proximo slot GLOBAL — todos os processos param juntos."""
        alvo = time.time() + max(float(segundos), 1.0)

        def empurrar(estado):
            estado["proximo"] = max(float(estado.get("proximo", 0.0)), alvo)
            return estado

        self._transacao(empurrar)


PACER = RiotPacer()
