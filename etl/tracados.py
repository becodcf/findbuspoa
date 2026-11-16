"""
Reconstrução de traçados de ônibus a partir da sequência de ruas do itinerário da EPTC
(`dados/itinerario_onibus.csv`) sobre a malha viária do OpenStreetMap (`otp/poa.osm.pbf`).

Como funciona:
  1. Lê as vias do OSM e monta um grafo dirigido (respeita mão única, exceto quando
     ônibus/psv é liberado na contramão).
  2. Normaliza os nomes de rua dos dois lados ("AV PROF OSCAR PEREIRA" e
     "Avenida Professor Oscar Pereira" viram a mesma chave) e casa cada logradouro do
     itinerário com as vias do OSM.
  3. Percorre o itinerário rua a rua: a partir do ponto atual, um Dijkstra procura o nó
     mais próximo da próxima rua, com custo baixo para a rua atual e a próxima, médio para
     as outras ruas do itinerário e alto para o resto. Começa no terminal de origem e
     termina no terminal de destino (coordenadas do stops.txt via `codigo` = `stop_id`).
  4. As paradas do GTFS antigo que ficam sobre o traçado (até 30 m, no mesmo sentido de
     circulação) viram a sequência de paradas da linha.
"""
from __future__ import annotations

import difflib
import heapq
import math
import pickle
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

RAIZ = Path(__file__).resolve().parent.parent
OSM = RAIZ / "otp" / "poa.osm.pbf"
CACHE = RAIZ / "etl" / "cache" / "malha.pkl"
VERSAO_CACHE = 3

# Projeção local equirretangular (metros), suficiente para a escala de uma cidade.
LAT0 = -30.05
KX = 111_320 * math.cos(math.radians(LAT0))
KY = 110_540


def para_xy(lat, lon):
    return np.asarray(lon, dtype=float) * KX, np.asarray(lat, dtype=float) * KY


# ---------------------------------------------------------------------------
# Nomes de rua
# ---------------------------------------------------------------------------
TIPOS = {
    "AVENIDA": "AV", "AV": "AV", "RUA": "R", "R": "R", "ESTRADA": "ESTR", "ESTR": "ESTR",
    "PRACA": "PCA", "PCA": "PCA", "TRAVESSA": "TRAV", "TRAV": "TRAV", "TRVS": "TRAV",
    "ACESSO": "AC", "AC": "AC", "BECO": "BC", "BC": "BC", "VIADUTO": "VDT", "VDT": "VDT",
    "LARGO": "LG", "LG": "LG", "ALAMEDA": "AL", "AL": "AL", "TUNEL": "TUN", "TUN": "TUN",
    "VIA": "VA", "VA": "VA", "PASSAGEM": "PSG", "PSG": "PSG", "RODOVIA": "ROD", "ROD": "ROD",
    "ELEVADA": "EL", "PONTE": "PTE", "BOULEVARD": "BV", "BV": "BV", "RETORNO": "RTL", "RTL": "RTL",
}
TITULOS = {
    "DOUTOR": "DR", "DR": "DR", "DOUTORA": "DR", "DRA": "DR",
    "PROFESSOR": "PROF", "PROFESSORA": "PROF", "PROF": "PROF", "PROFA": "PROF",
    "CORONEL": "CEL", "CEL": "CEL", "GENERAL": "GEN", "GEN": "GEN",
    "ENGENHEIRO": "ENG", "ENGENHEIRA": "ENG", "ENG": "ENG", "CAPITAO": "CAP", "CAP": "CAP",
    "TENENTE": "TEN", "TEN": "TEN", "SARGENTO": "SGT", "SGT": "SGT", "MARECHAL": "MAL", "MAL": "MAL",
    "BRIGADEIRO": "BRIG", "BRIG": "BRIG", "DESEMBARGADOR": "DES", "DES": "DES",
    "SENADOR": "SEN", "SEN": "SEN", "DEPUTADO": "DEP", "DEP": "DEP", "VEREADOR": "VER", "VER": "VER",
    "PRESIDENTE": "PRES", "PRES": "PRES", "GOVERNADOR": "GOV", "GOV": "GOV",
    "ALMIRANTE": "ALM", "ALM": "ALM", "COMENDADOR": "COM", "COM": "COM", "MONSENHOR": "MONS",
    "MONS": "MONS", "PADRE": "PE", "PE": "PE", "MAJOR": "MAJ", "MAJ": "MAJ",
    "ARQUITETO": "ARQ", "ARQ": "ARQ", "IRMAO": "IR", "IRMA": "IR", "DONA": "D", "DOM": "D", "D": "D",
    "MINISTRO": "MIN", "MIN": "MIN", "VISCONDE": "VISC", "VISC": "VISC", "CONSELHEIRO": "CONS",
    "CONS": "CONS", "BARAO": "BAR", "BAR": "BAR", "DUQUE": "DQ", "DQ": "DQ", "CARDEAL": "CARD",
    "FREI": "FR", "FR": "FR", "MESTRE": "ME", "MADRE": "MADRE", "IRMAOS": "IR",
}
EQUIVALENTES = {"SANTO": "STO", "SANTA": "STA", "SAO": "S", "NOSSA": "NSA", "SENHORA": "SRA",
                "JUNIOR": "JR", "SAINT": "ST"}
PALAVRAS_VAZIAS = {"DE", "DA", "DO", "DAS", "DOS", "E", "DI", "DEL"}

_UNID = ["", "UM", "DOIS", "TRES", "QUATRO", "CINCO", "SEIS", "SETE", "OITO", "NOVE", "DEZ",
         "ONZE", "DOZE", "TREZE", "QUATORZE", "QUINZE", "DEZESSEIS", "DEZESSETE", "DEZOITO", "DEZENOVE"]
_DEZ = ["", "", "VINTE", "TRINTA", "QUARENTA", "CINQUENTA", "SESSENTA", "SETENTA", "OITENTA", "NOVENTA"]
_CEM = ["", "CENTO", "DUZENTOS", "TREZENTOS", "QUATROCENTOS", "QUINHENTOS", "SEISCENTOS",
        "SETECENTOS", "OITOCENTOS", "NOVECENTOS"]


def por_extenso(n: int) -> str:
    if n == 0:
        return "ZERO"
    if n == 100:
        return "CEM"
    partes = []
    if n >= 1000:
        mil, n = divmod(n, 1000)
        partes.append("MIL" if mil == 1 else por_extenso(mil) + " MIL")
    if n >= 100:
        c, n = divmod(n, 100)
        partes.append(_CEM[c])
    if n >= 20:
        d, n = divmod(n, 10)
        partes.append(_DEZ[d])
    if n:
        partes.append(_UNID[n])
    return " E ".join(partes)


def _limpa(nome: str) -> list[str]:
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"(\d+)", lambda m: " " + por_extenso(int(m.group(1))) + " " if len(m.group(1)) <= 4 else m.group(1), s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return s.split()


def chaves_nome(nome: str) -> tuple[str, str, str]:
    """Devolve (tipo, chave_completa, chave_sem_titulos)."""
    toks = _limpa(nome)
    tipo = ""
    if toks and toks[0] in TIPOS and len(toks) > 1:
        tipo = TIPOS[toks[0]]
        toks = toks[1:]
    toks = [EQUIVALENTES.get(t, t) for t in toks if t not in PALAVRAS_VAZIAS]
    completa = [TITULOS.get(t, t) for t in toks]
    sem_tit = [t for t in toks if t not in TITULOS] or completa
    return tipo, " ".join(completa), " ".join(sem_tit)


# ---------------------------------------------------------------------------
# Malha viária
# ---------------------------------------------------------------------------
VIAS = {
    "motorway": 1.0, "motorway_link": 1.0, "trunk": 1.0, "trunk_link": 1.0,
    "primary": 1.0, "primary_link": 1.0, "secondary": 1.0, "secondary_link": 1.0,
    "tertiary": 1.2, "tertiary_link": 1.2, "unclassified": 1.5, "residential": 1.5,
    "living_street": 2.0, "service": 2.5, "busway": 0.8, "road": 1.5, "bus_guideway": 0.8,
}


@dataclass
class Malha:
    lat: np.ndarray
    lon: np.ndarray
    adj: list            # adj[u] = [(v, metros, via, multiplicador_contramao), ...]
    via_peso: list       # peso base da via (tipo de highway)
    via_ruas: list       # via -> lista de ids de rua (nomes normalizados)
    ruas: list           # id -> (tipo, chave_completa, chave_sem_titulos, nome_original)
    vias_da_rua: dict = field(default_factory=dict)
    nos_da_rua: dict = field(default_factory=dict)
    kd: cKDTree | None = None
    idx_completa: dict = field(default_factory=dict)
    idx_sem_tit: dict = field(default_factory=dict)

    def preparar(self):
        self.vias_da_rua = defaultdict(list)
        for v, rs in enumerate(self.via_ruas):
            for r in rs:
                self.vias_da_rua[r].append(v)
        nos = defaultdict(set)
        for u, arestas in enumerate(self.adj):
            for (w, _, via, _) in arestas:
                for r in self.via_ruas[via]:
                    nos[r].add(u)
                    nos[r].add(w)
        self.nos_da_rua = {r: s for r, s in nos.items()}
        x, y = para_xy(self.lat, self.lon)
        # só o maior componente conexo: um nó "mais próximo" numa via isolada (estacionamento,
        # trecho cortado no recorte do mapa) faria o Dijkstra não achar nada
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
        us = [u for u, a in enumerate(self.adj) for _ in a]
        vs = [w for a in self.adj for (w, _, _, _) in a]
        n = len(self.adj)
        _, rotulo = connected_components(coo_matrix((np.ones(len(us)), (us, vs)), shape=(n, n)), directed=False)
        maior = np.bincount(rotulo).argmax()
        com_aresta = rotulo == maior
        self._nos_validos = np.flatnonzero(com_aresta)
        self.kd = cKDTree(np.column_stack([x[com_aresta], y[com_aresta]]))
        self.idx_completa = defaultdict(list)
        self.idx_sem_tit = defaultdict(list)
        for i, (tipo, comp, sem, _) in enumerate(self.ruas):
            self.idx_completa[comp].append(i)
            self.idx_sem_tit[sem].append(i)
        self._chaves_sem_tit = list(self.idx_sem_tit)
        self._toks = [set(r[2].split()) for r in self.ruas]
        self._por_token = defaultdict(set)
        for i, ts in enumerate(self._toks):
            for t in ts:
                self._por_token[t].add(i)
        self._cache_nomes: dict[str, list[int]] = {}

    def alinhar(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
        """Desloca um traçado antigo para cima das ruas do OSM (vários vêm dezenas de metros
        fora). Devolve (lat, lon, metros deslocados); não mexe se não houver ganho claro."""
        if getattr(self, "_kd_vias", None) is None:
            x, y = para_xy(self.lat, self.lon)
            px, py = [], []
            for u, ar in enumerate(self.adj):
                for v, L, _, _ in ar:
                    if u < v:
                        n = max(1, int(L // 8))
                        t = np.arange(n) / n
                        px.append(x[u] + (x[v] - x[u]) * t)
                        py.append(y[u] + (y[v] - y[u]) * t)
            self._kd_vias = cKDTree(np.column_stack([np.concatenate(px), np.concatenate(py)]))
        sx, sy, _, _ = densificar(lat, lon, 10)
        if len(sx) > 400:
            sel = np.linspace(0, len(sx) - 1, 400).astype(int)
            sx, sy = sx[sel], sy[sel]
        pts = np.column_stack([sx, sy])

        def custo(dx, dy):
            return float(np.median(self._kd_vias.query(pts + [dx, dy])[0]))

        # busca em grade (grossa e depois fina): com ruas paralelas por perto, o vizinho
        # mais próximo de um ponto deslocado costuma ser a rua errada
        d0 = custo(0, 0)
        melhor = (d0, 0.0, 0.0)
        for passo, raio, cx, cy in ((15, 135, 0, 0), (3, 15, None, None)):
            if cx is None:
                cx, cy = melhor[1], melhor[2]
            for dx in np.arange(cx - raio, cx + raio + 0.1, passo):
                for dy in np.arange(cy - raio, cy + raio + 0.1, passo):
                    c = custo(dx, dy)
                    if c < melhor[0]:
                        melhor = (c, float(dx), float(dy))
        d1, *off = melhor
        desloc = float(np.hypot(*off))
        if desloc < 8 or d1 > d0 * 0.6:
            return lat, lon, 0.0
        return lat + off[1] / KY, lon + off[0] / KX, desloc

    def no_mais_proximo(self, lat: float, lon: float) -> int:
        x, y = para_xy(lat, lon)
        _, i = self.kd.query([float(x), float(y)])
        return int(self._nos_validos[i])

    def casar_nome(self, nome: str) -> list[int]:
        """Ids das ruas do OSM que correspondem a um logradouro da EPTC ([] se nenhuma)."""
        if nome in self._cache_nomes:
            return self._cache_nomes[nome]
        tipo, comp, sem = chaves_nome(nome)
        achou: list[int] = []
        for cands in (self.idx_completa.get(comp, []), self.idx_sem_tit.get(sem, [])):
            if cands:
                mesmo_tipo = [c for c in cands if self.ruas[c][0] == tipo]
                achou = mesmo_tipo or cands
                break
        if not achou and len(sem) >= 5:
            parecidos = difflib.get_close_matches(sem, self._chaves_sem_tit, n=3, cutoff=0.86)
            for p in parecidos:
                achou.extend(self.idx_sem_tit[p])
        if not achou:
            # um nome contido no outro (EPTC e OSM costumam omitir nomes do meio)
            toks = set(sem.split())
            if len(toks) >= 2:
                cands = set.intersection(*(self._por_token.get(t, set()) for t in toks))
                if not cands:
                    cands = {c for c in self._por_token.get(sem.split()[-1], set())
                             if len(self._toks[c]) >= 2 and self._toks[c] <= toks}
                achou = sorted(cands)[:5] if len(cands) <= 5 else []
        if not achou and "-" in nome:
            # "R SAO PAULO-LOMBA DO PINHEIRO", "VDT DOS ACORIANOS-ACESSO OESTE"
            achou = self.casar_nome(nome.split("-")[0].strip())
        self._cache_nomes[nome] = achou
        return achou


def _construir_malha() -> Malha:
    import osmium

    no_idx: dict[int, int] = {}
    lat: list[float] = []
    lon: list[float] = []
    ruas: list = []
    rua_idx: dict[str, int] = {}
    via_peso: list[float] = []
    via_ruas: list[list[int]] = []
    arestas: list[tuple[int, int, int, int]] = []  # (u, v, via, sentido: 0 duplo, 1 só u->v)

    def id_rua(nome: str) -> int:
        tipo, comp, sem = chaves_nome(nome)
        k = f"{tipo}|{comp}"
        if k not in rua_idx:
            rua_idx[k] = len(ruas)
            ruas.append((tipo, comp, sem, nome))
        return rua_idx[k]

    class Leitor(osmium.SimpleHandler):
        def way(self, w):
            t = w.tags
            hw = t.get("highway")
            if hw not in VIAS:
                return
            onibus_ok = t.get("bus") in ("yes", "designated") or t.get("psv") in ("yes", "designated")
            if t.get("access") in ("no", "private") and not onibus_ok:
                return
            if t.get("area") == "yes":
                return
            refs = []
            for n in w.nodes:
                if not n.location.valid():
                    return
                if n.ref not in no_idx:
                    no_idx[n.ref] = len(lat)
                    lat.append(n.location.lat)
                    lon.append(n.location.lon)
                refs.append(no_idx[n.ref])
            nomes = []
            for k in ("name", "alt_name", "old_name", "official_name", "short_name", "loc_name"):
                if t.get(k):
                    nomes.extend(x.strip() for x in t.get(k).split(";") if x.strip())
            if t.get("ref"):
                nomes.extend(r.strip() for r in t.get("ref").split(";"))
            via = len(via_peso)
            via_peso.append(VIAS[hw])
            via_ruas.append(sorted({id_rua(n) for n in nomes}))
            ow = t.get("oneway", "")
            mao_unica = ow in ("yes", "1", "true", "-1") or (
                (hw.startswith("motorway") or t.get("junction") in ("roundabout", "circular")) and ow != "no")
            if t.get("oneway:bus") == "no" or t.get("oneway:psv") == "no" or "opposite" in t.get("busway", ""):
                mao_unica = False
            if ow == "-1":
                refs = refs[::-1]
            for a, b in zip(refs, refs[1:]):
                arestas.append((a, b, via, 1 if mao_unica else 0))

    Leitor().apply_file(str(OSM), locations=True)
    lat_a, lon_a = np.array(lat), np.array(lon)
    x, y = para_xy(lat_a, lon_a)
    adj: list[list] = [[] for _ in range(len(lat))]
    for a, b, via, unica in arestas:
        d = float(math.hypot(x[a] - x[b], y[a] - y[b]))
        adj[a].append((b, d, via, 1.0))
        # contramão continua possível, mas cara: dá saída quando o OSM está incompleto
        adj[b].append((a, d, via, 8.0 if unica else 1.0))
    return Malha(lat_a, lon_a, adj, via_peso, via_ruas, ruas)


def carregar_malha() -> Malha:
    if CACHE.exists() and CACHE.stat().st_mtime >= OSM.stat().st_mtime:
        with open(CACHE, "rb") as f:
            versao, m = pickle.load(f)
        if versao == VERSAO_CACHE:
            m.preparar()
            return m
    m = _construir_malha()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump((VERSAO_CACHE, m), f, protocol=pickle.HIGHEST_PROTOCOL)
    m.preparar()
    return m


# ---------------------------------------------------------------------------
# Traçado
# ---------------------------------------------------------------------------
FATOR_RUA_ATUAL = 0.25     # rua atual e próxima rua do itinerário
FATOR_ITINERARIO = 0.6     # outras ruas do itinerário
FATOR_FORA = 2.5           # ruas fora do itinerário (multiplica o peso do tipo de via)
LIMITE_CUSTO = 12_000      # se a próxima rua não aparece até esse custo, é pulada


@dataclass
class Tracado:
    lat: np.ndarray
    lon: np.ndarray
    n_ruas: int
    ruas_casadas: int
    passos_falhos: int
    frac_no_itinerario: float
    inicio_ok: bool
    fim_ok: bool

    @property
    def km(self) -> float:
        x, y = para_xy(self.lat, self.lon)
        return float(np.hypot(np.diff(x), np.diff(y)).sum() / 1000)

    @property
    def confiavel(self) -> bool:
        """Critério estrito, usado para substituir um traçado antigo."""
        return (self.n_ruas > 0 and self.ruas_casadas / self.n_ruas >= 0.9 and self.passos_falhos == 0
                and self.frac_no_itinerario >= 0.85 and self.inicio_ok and self.fim_ok)

    @property
    def aceitavel(self) -> bool:
        """Critério para linhas que não têm traçado nenhum."""
        return (self.n_ruas > 0 and self.ruas_casadas / self.n_ruas >= 0.7
                and self.frac_no_itinerario >= 0.7 and (self.inicio_ok or self.fim_ok) and self.km >= 1)


def _dijkstra(m: Malha, origem: int, alvos: set[int], fator: list[float]):
    dist = {origem: 0.0}
    ant: dict[int, int] = {}
    heap = [(0.0, origem)]
    feito = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in feito:
            continue
        if u in alvos:
            cam = [u]
            while cam[-1] != origem:
                cam.append(ant[cam[-1]])
            return cam[::-1]
        if d > LIMITE_CUSTO:
            return None
        feito.add(u)
        for v, L, via, mult in m.adj[u]:
            nd = d + L * fator[via] * mult
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                ant[v] = u
                heapq.heappush(heap, (nd, v))
    return None


def tracar(m: Malha, ruas_eptc: list[str], inicio: tuple[float, float] | None,
           fim: tuple[float, float] | None) -> Tracado | None:
    # ruas consecutivas repetidas viram uma só
    seq = [r for i, r in enumerate(ruas_eptc) if i == 0 or r != ruas_eptc[i - 1]]
    casadas = [(r, set(m.casar_nome(r))) for r in seq]
    passos = [ids for _, ids in casadas if ids]
    if not passos:
        return None
    todas = set().union(*passos)

    base = [FATOR_FORA * p for p in m.via_peso]
    for r in todas:
        for v in m.vias_da_rua.get(r, ()):
            base[v] = min(base[v], FATOR_ITINERARIO)

    def fator_para(ruas: set[int]) -> list[float]:
        f = base.copy()
        for r in ruas:
            for v in m.vias_da_rua.get(r, ()):
                f[v] = FATOR_RUA_ATUAL
        return f

    falhos = 0
    if inicio:
        atual = m.no_mais_proximo(*inicio)
        caminho = [atual]
        anterior: set[int] = set()
    else:
        # sem terminal de origem: começa onde a 1ª rua encontra a 2ª
        primeira = passos[0]
        nos = list(set().union(*(m.nos_da_rua[r] for r in primeira)))
        atual = nos[0]
        if len(passos) > 1:
            alvo = set().union(*(m.nos_da_rua[r] for r in passos[1]))
            comum = [n for n in nos if n in alvo]
            atual = comum[0] if comum else atual
        caminho = [atual]
        anterior = primeira
        passos = passos[1:]

    for ids in passos:
        alvos = set().union(*(m.nos_da_rua.get(r, set()) for r in ids))
        if atual in alvos:
            anterior = ids
            continue
        cam = _dijkstra(m, atual, alvos, fator_para(anterior | ids))
        if cam is None:
            falhos += 1
            continue
        caminho.extend(cam[1:])
        atual = cam[-1]
        anterior = ids

    if fim:
        destino = m.no_mais_proximo(*fim)
        if destino != atual:
            cam = _dijkstra(m, atual, {destino}, fator_para(anterior))
            if cam is None:
                falhos += 1
            else:
                caminho.extend(cam[1:])

    if len(caminho) < 2:
        return None
    # parte do trajeto que está sobre ruas do itinerário
    x, y = para_xy(m.lat[caminho], m.lon[caminho])
    seg = np.hypot(np.diff(x), np.diff(y))
    no_it = 0.0
    for i, (a, b) in enumerate(zip(caminho, caminho[1:])):
        for v, _, via, _ in m.adj[a]:
            if v == b:
                if any(r in todas for r in m.via_ruas[via]):
                    no_it += seg[i]
                break
    total = float(seg.sum()) or 1.0
    return Tracado(
        lat=m.lat[caminho], lon=m.lon[caminho], n_ruas=len(seq),
        ruas_casadas=sum(1 for _, ids in casadas if ids), passos_falhos=falhos,
        frac_no_itinerario=no_it / total, inicio_ok=inicio is not None, fim_ok=fim is not None,
    )


@dataclass
class Itinerario:
    linha: str
    sentido: str
    ruas: list[str]
    terminal_ini: str | None
    terminal_fim: str | None
    nome_ini: str
    nome_fim: str


def ler_itinerarios(caminho: Path) -> dict[tuple[str, str], Itinerario]:
    import pandas as pd

    d = pd.read_csv(caminho, sep=";", dtype=str, encoding="utf-8", encoding_errors="replace")
    d["linha"] = d["linha"].str.upper().str.strip()
    d["sentido"] = d["sentido"].str.upper().str.strip()
    d["seq"] = pd.to_numeric(d["numero_sequencia"], errors="coerce")
    d = d.dropna(subset=["seq"]).drop_duplicates(["linha", "sentido", "seq", "tipo", "endereco_logradouro", "codigo"])
    res = {}
    for (linha, sentido), g in d.sort_values("seq").groupby(["linha", "sentido"]):
        term = g[g["tipo"] == "TERMINAL/PARADA"]
        ini = term[term["seq"] == term["seq"].min()]
        fim = term[term["seq"] == term["seq"].max()]
        ruas = g.loc[g["tipo"] == "LOGRADOURO", "endereco_logradouro"].dropna().tolist()
        res[(linha, sentido)] = Itinerario(
            linha, sentido, ruas,
            ini["codigo"].iloc[0] if len(ini) else None,
            fim["codigo"].iloc[0] if len(fim) and fim["seq"].iloc[0] != ini["seq"].iloc[0] else None,
            str(ini["nome"].iloc[0]) if len(ini) else "", str(fim["nome"].iloc[0]) if len(fim) else "",
        )
    return res


# ---------------------------------------------------------------------------
# Geometria auxiliar
# ---------------------------------------------------------------------------
def densificar(lat: np.ndarray, lon: np.ndarray, passo: float = 5.0):
    """Pontos a cada `passo` metros: (x, y, distância acumulada, rumo em graus)."""
    x, y = para_xy(lat, lon)
    dx, dy = np.diff(x), np.diff(y)
    seg = np.hypot(dx, dy)
    ok = seg > 0
    x0, y0, dx, dy, seg = x[:-1][ok], y[:-1][ok], dx[ok], dy[ok], seg[ok]
    if len(seg) == 0:
        return np.array([x[0]]), np.array([y[0]]), np.array([0.0]), np.array([0.0])
    acum = np.concatenate([[0], np.cumsum(seg)])
    n = np.maximum(1, np.ceil(seg / passo).astype(int))
    i = np.repeat(np.arange(len(seg)), n)
    t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
    px = x0[i] + dx[i] * t
    py = y0[i] + dy[i] * t
    s = acum[i] + seg[i] * t
    rumo = np.degrees(np.arctan2(dx[i], dy[i])) % 360
    return px, py, s, rumo


def dif_angulo(a, b):
    d = np.abs(np.asarray(a) - np.asarray(b)) % 360
    return np.minimum(d, 360 - d)


def divergencia(lat_a, lon_a, lat_b, lon_b, tol: float = 80.0) -> float:
    """Fração (máxima dos dois lados) do comprimento de um traçado a mais de `tol` m do outro.

    Vários traçados do GTFS antigo estão deslocados dezenas de metros em relação ao OSM
    (as paradas não), então o deslocamento mediano é compensado antes da comparação."""
    ax, ay, _, _ = densificar(lat_a, lon_a, 10)
    bx, by, _, _ = densificar(lat_b, lon_b, 10)
    ka = cKDTree(np.column_stack([ax, ay]))
    off = np.zeros(2)
    for _ in range(3):
        d, i = ka.query(np.column_stack([bx + off[0], by + off[1]]))
        sel = d < 150
        if sel.sum() < 10:
            break
        off += [np.median(ax[i[sel]] - bx[sel] - off[0]), np.median(ay[i[sel]] - by[sel] - off[1])]
    if np.hypot(*off) > 150:
        off = np.zeros(2)
    bx, by = bx + off[0], by + off[1]
    da, _ = cKDTree(np.column_stack([bx, by])).query(np.column_stack([ax, ay]))
    db, _ = ka.query(np.column_stack([bx, by]))
    return float(max((da > tol).mean(), (db > tol).mean()))


def rumo_das_paradas(stops, shapes, stop_times, trips) -> dict[str, float]:
    """Sentido de circulação (rumo em graus) de cada parada, tirado dos traçados antigos que
    passam por ela. Paradas atendidas nos dois sentidos (terminais) ficam de fora."""
    pares = stop_times[["trip_id", "stop_id"]].merge(trips[["trip_id", "shape_id"]], on="trip_id")
    pares = pares[["shape_id", "stop_id"]].drop_duplicates()
    st_xy = dict(zip(stops["stop_id"], zip(*para_xy(stops["stop_lat"].astype(float), stops["stop_lon"].astype(float)))))
    sh = shapes.copy()
    sh["shape_pt_sequence"] = sh["shape_pt_sequence"].astype(int)
    sh = sh.sort_values(["shape_id", "shape_pt_sequence"])
    rumos: dict[str, list[float]] = defaultdict(list)
    for shape_id, pts in sh.groupby("shape_id"):
        ids = pares.loc[pares["shape_id"] == shape_id, "stop_id"]
        ids = [s for s in ids if s in st_xy]
        if not ids or len(pts) < 2:
            continue
        px, py, _, rumo = densificar(pts["shape_pt_lat"].astype(float).to_numpy(),
                                     pts["shape_pt_lon"].astype(float).to_numpy())
        d, i = cKDTree(np.column_stack([px, py])).query(np.array([st_xy[s] for s in ids]))
        for s, dd, ii in zip(ids, d, i):
            if dd < 60:
                rumos[s].append(float(rumo[ii]))
    res = {}
    for s, rs in rumos.items():
        r = np.radians(rs)
        media = math.degrees(math.atan2(np.sin(r).mean(), np.cos(r).mean())) % 360
        if dif_angulo(rs, media).max() <= 60:
            res[s] = media
    return res


class IndiceParadas:
    def __init__(self, stops, rumos: dict[str, float]):
        self.ids = stops["stop_id"].to_numpy()
        x, y = para_xy(stops["stop_lat"].astype(float), stops["stop_lon"].astype(float))
        self.xy = np.column_stack([x, y])
        self.kd = cKDTree(self.xy)
        self.rumo = np.array([rumos.get(s, np.nan) for s in self.ids])

    def no_tracado(self, lat, lon, raio: float = 30.0, raio_sem_rumo: float = 15.0) -> list[str]:
        """Paradas sobre o traçado, na ordem em que o ônibus passa por elas."""
        px, py, s, rumo = densificar(lat, lon)
        kd = cKDTree(np.column_stack([px, py]))
        mat = kd.sparse_distance_matrix(self.kd, raio, output_type="coo_matrix")
        if mat.nnz == 0:
            return []
        pt, par, dist = mat.row, mat.col, mat.data
        r_par = self.rumo[par]
        sem = np.isnan(r_par)
        ok_rumo = ~sem & (dif_angulo(np.nan_to_num(r_par), rumo[pt]) <= 50)
        # sem rumo conhecido: só bem perto e do lado direito da via
        ang = np.radians(rumo[pt])
        vx, vy = self.xy[par, 0] - px[pt], self.xy[par, 1] - py[pt]
        direita = (np.sin(ang) * vy - np.cos(ang) * vx) < 0  # produto vetorial: < 0 => à direita
        ok_sem = sem & (dist <= raio_sem_rumo) & (direita | (dist <= 4))
        ok = ok_rumo | ok_sem
        pt, par, dist = pt[ok], par[ok], dist[ok]
        ordem = np.lexsort((s[pt], par))
        pt, par, dist = pt[ordem], par[ordem], dist[ordem]
        escolhidos = []  # (posição no traçado, parada)
        i = 0
        while i < len(par):
            j = i
            melhor = i
            while j + 1 < len(par) and par[j + 1] == par[i] and s[pt[j + 1]] - s[pt[j]] < 150:
                j += 1
                if dist[j] < dist[melhor]:
                    melhor = j
            escolhidos.append((s[pt[melhor]], self.ids[par[melhor]]))
            i = j + 1
        escolhidos.sort()
        res = []
        for _, sid in escolhidos:
            if not res or res[-1] != sid:
                res.append(sid)
        return res
