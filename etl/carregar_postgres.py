"""
Carrega no PostgreSQL os dados do projeto:
  - linhas, paradas e sequência de paradas do GTFS gerado (otp/gtfs-poa.zip)
  - tabela horária, itinerário e frota da EPTC (dados/*.csv), como vieram
  - lugares para a busca de endereço: ruas, lugares com nome e endereços com número do
    OpenStreetMap (otp/poa.osm.pbf) + paradas e terminais da EPTC

As tabelas de dados são recriadas a cada carga; geocode_cache e buscas são preservadas.
A conexão vem de DATABASE_URL (variável de ambiente ou arquivo .env; ver etl/criar_banco.py).

Uso:  python etl/carregar_postgres.py
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

RAIZ = Path(__file__).resolve().parent.parent
GTFS = RAIZ / "otp" / "gtfs-poa.zip"
OSM = RAIZ / "otp" / "poa.osm.pbf"
DADOS = RAIZ / "dados"
ESQUEMA = RAIZ / "db" / "esquema.sql"

# categorias do OSM que viram "lugar" na busca, com o nome em português
CATEGORIAS = {
    ("amenity", "hospital"): "Hospital", ("amenity", "clinic"): "Clínica", ("amenity", "school"): "Escola",
    ("amenity", "university"): "Universidade", ("amenity", "college"): "Faculdade",
    ("amenity", "kindergarten"): "Escola infantil", ("amenity", "library"): "Biblioteca",
    ("amenity", "place_of_worship"): "Igreja/templo", ("amenity", "townhall"): "Prefeitura",
    ("amenity", "courthouse"): "Fórum", ("amenity", "police"): "Polícia", ("amenity", "fire_station"): "Bombeiros",
    ("amenity", "bus_station"): "Rodoviária/terminal", ("amenity", "marketplace"): "Mercado",
    ("amenity", "theatre"): "Teatro", ("amenity", "cinema"): "Cinema", ("amenity", "arts_centre"): "Centro cultural",
    ("amenity", "community_centre"): "Centro comunitário", ("amenity", "social_facility"): "Assistência social",
    ("amenity", "pharmacy"): "Farmácia", ("amenity", "bank"): "Banco", ("amenity", "restaurant"): "Restaurante",
    ("amenity", "fast_food"): "Lanchonete", ("amenity", "cafe"): "Café", ("amenity", "bar"): "Bar",
    ("amenity", "fuel"): "Posto de combustível", ("amenity", "post_office"): "Correios",
    ("amenity", "doctors"): "Consultório", ("amenity", "dentist"): "Dentista",
    ("shop", "supermarket"): "Supermercado", ("shop", "mall"): "Shopping",
    ("shop", "department_store"): "Loja de departamentos",
    ("leisure", "park"): "Parque", ("leisure", "stadium"): "Estádio", ("leisure", "sports_centre"): "Centro esportivo",
    ("tourism", "museum"): "Museu", ("tourism", "hotel"): "Hotel", ("tourism", "attraction"): "Ponto turístico",
    ("tourism", "viewpoint"): "Mirante", ("railway", "station"): "Estação de trem",
    ("aeroway", "aerodrome"): "Aeroporto", ("aeroway", "terminal"): "Aeroporto",
    ("office", "government"): "Órgão público", ("healthcare", "hospital"): "Hospital",
    ("place", "suburb"): "Bairro", ("place", "neighbourhood"): "Bairro", ("place", "quarter"): "Bairro",
    ("place", "city"): "Cidade", ("place", "town"): "Cidade",
}
PESO_CATEGORIA = {"Bairro": 4, "Cidade": 4, "Hospital": 4, "Universidade": 4, "Shopping": 4, "Parque": 4,
                  "Estádio": 4, "Aeroporto": 4, "Rodoviária/terminal": 4, "Ponto turístico": 3.5, "Museu": 3.5,
                  "Mercado": 3.5, "Escola": 3, "Faculdade": 3}
VIAS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
        "living_street", "pedestrian", "service", "road", "busway", "motorway_link", "trunk_link",
        "primary_link", "secondary_link", "tertiary_link"}
# avenidas principais antes de lojas homônimas ("Assis Brasil" -> a avenida, depois o shopping)
PESO_VIA = {"motorway": 4.5, "trunk": 4.5, "primary": 4.5, "secondary": 4.2, "tertiary": 3.5}


def url_banco() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = RAIZ / ".env"
    if env.exists():
        for linha in env.read_text(encoding="utf-8").splitlines():
            if linha.startswith("DATABASE_URL="):
                return linha.split("=", 1)[1].strip()
    raise SystemExit("DATABASE_URL não definida: rode antes  python etl/criar_banco.py")


def ler_gtfs() -> dict[str, pd.DataFrame]:
    with zipfile.ZipFile(GTFS) as z:
        return {n: pd.read_csv(z.open(n + ".txt"), dtype=str, keep_default_na=False)
                for n in ("routes", "stops", "trips", "stop_times")}


def ler_csv(nome: str) -> pd.DataFrame:
    return pd.read_csv(DADOS / nome, sep=";", dtype=str, keep_default_na=False,
                       encoding="utf-8", encoding_errors="replace")


def copiar(cur, tabela: str, df: pd.DataFrame) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cols = ", ".join(df.columns)
    with cur.copy(f"COPY {tabela} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')") as cp:
        while dados := buf.read(1 << 20):
            cp.write(dados)


def lugares_osm() -> pd.DataFrame:
    import osmium

    ruas: dict[str, list] = defaultdict(list)    # nome -> [(lat, lon, comprimento, peso), ...]
    linhas: list[tuple] = []
    lugares_ref: list[tuple] = []                # bairros e cidades, para dizer onde fica cada coisa

    def categoria(tags) -> str | None:
        for (k, v), nome in CATEGORIAS.items():
            if tags.get(k) == v:
                return nome
        return None

    for obj in osmium.FileProcessor(str(OSM)).with_locations():
        tags = obj.tags
        if obj.is_node():
            if not obj.location.valid():
                continue
            lat, lon = obj.location.lat, obj.location.lon
        elif obj.is_way():
            pts = [(n.location.lat, n.location.lon) for n in obj.nodes if n.location.valid()]
            if not pts:
                continue
            hw = tags.get("highway")
            if hw in VIAS and tags.get("name"):
                meio = pts[len(pts) // 2]
                ruas[tags["name"]].append((meio[0], meio[1], len(pts), PESO_VIA.get(hw, 2.0)))
            lat, lon = float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts]))
        else:
            continue
        nome = tags.get("name")
        cat = categoria(tags) if nome else None
        if nome and cat:
            linhas.append(("lugar", nome, cat, lat, lon, PESO_CATEGORIA.get(cat, 2.5)))
            if cat in ("Bairro", "Cidade"):
                lugares_ref.append((cat, nome, lat, lon))
            # apelidos: "Redenção" -> Parque Farroupilha
            for k in ("alt_name", "short_name", "old_name", "official_name", "loc_name"):
                for apelido in (tags.get(k) or "").split(";"):
                    apelido = apelido.strip()
                    if apelido and apelido != nome:
                        linhas.append(("lugar", apelido, f"{cat} · {nome}", lat, lon, PESO_CATEGORIA.get(cat, 2.5)))
        if tags.get("addr:street") and tags.get("addr:housenumber"):
            end = f"{tags['addr:street']}, {tags['addr:housenumber']}"
            linhas.append(("endereco", end, "", lat, lon, 2.2))

    # uma entrada por rua; nomes repetidos em lugares distantes (outra cidade) ficam separados
    for nome, trechos in ruas.items():
        grupos: dict[tuple, list] = defaultdict(list)
        for la, lo, n, peso in trechos:
            grupos[(round(la / 0.03), round(lo / 0.03))].append((la, lo, n, peso))
        vistos: list[tuple] = []
        for g in sorted(grupos.values(), key=lambda g: -sum(t[2] for t in g)):
            la, lo, _, _ = max(g, key=lambda t: t[2])
            peso = max(t[3] for t in g)
            if any(abs(la - a) < 0.03 and abs(lo - b) < 0.03 for a, b in vistos):
                continue
            vistos.append((la, lo))
            linhas.append(("rua", nome, "", la, lo, peso))
    df = pd.DataFrame(linhas, columns=["tipo", "nome", "detalhe", "lat", "lon", "peso"])
    return com_localidade(df, lugares_ref)


def com_localidade(df: pd.DataFrame, ref: list[tuple]) -> pd.DataFrame:
    """Acrescenta "bairro, cidade" ao detalhe: bairro = ponto de bairro do OSM mais próximo
    (até 2,5 km), cidade = ponto de cidade mais próximo. É aproximado, mas separa duas
    "Rua Redenção" em cidades diferentes."""
    from scipy.spatial import cKDTree

    kx = 111_320 * np.cos(np.radians(-30.05))
    xy = np.column_stack([df["lon"].to_numpy() * kx, df["lat"].to_numpy() * 110_540])
    detalhe = df["detalhe"].fillna("").to_numpy(dtype=object)
    partes = [[] for _ in range(len(df))]
    for cat, limite in (("Bairro", 2500), ("Cidade", 40_000)):
        r = [x for x in ref if x[0] == cat]
        if not r:
            continue
        kd = cKDTree(np.column_stack([[x[3] * kx for x in r], [x[2] * 110_540 for x in r]]))
        d, i = kd.query(xy)
        for k, (dd, ii) in enumerate(zip(d, i)):
            if dd <= limite and r[ii][1] not in partes[k] and r[ii][1] != df["nome"].iat[k]:
                partes[k].append(r[ii][1])
    df = df.copy()
    df["detalhe"] = [" · ".join(p for p in (det, ", ".join(loc)) if p) if det not in ("Bairro", "Cidade") else det
                     for det, loc in zip(detalhe, partes)]
    return df


def main() -> None:
    t0 = time.time()
    g = ler_gtfs()
    routes = g["routes"]
    trips = g["trips"]
    st = g["stop_times"]

    linhas = pd.DataFrame({
        "route_id": routes["route_id"], "numero": routes["route_short_name"],
        "nome": routes["route_long_name"], "cor": routes["route_color"],
    })
    reconst = set(trips.loc[trips["shape_id"].str.startswith("N-"), "route_id"])
    linhas["tracado"] = np.where(linhas["route_id"].isin(reconst), "reconstruido", "antigo")

    paradas = g["stops"][["stop_id", "stop_name", "stop_lat", "stop_lon"]].rename(
        columns={"stop_name": "nome", "stop_lat": "lat", "stop_lon": "lon"})
    paradas["nome"] = paradas["nome"].str.strip()

    # sequência de paradas: a viagem com mais paradas de cada linha + sentido
    n_par = st.groupby("trip_id").size().rename("n")
    t = trips[["trip_id", "route_id", "trip_headsign"]].join(n_par, on="trip_id")
    rep = t.sort_values("n").drop_duplicates(["route_id", "trip_headsign"], keep="last")
    lp = st[st["trip_id"].isin(rep["trip_id"])].merge(rep, on="trip_id")
    lp["seq"] = lp.groupby("trip_id")["stop_sequence"].rank(method="first").astype(int)
    linha_paradas = lp.rename(columns={"trip_headsign": "sentido"})[["route_id", "sentido", "seq", "stop_id"]]
    linha_paradas = linha_paradas.assign(sentido=linha_paradas["sentido"].str.upper())

    h = ler_csv("tabela_horaria_onibus.csv")
    horarios = pd.DataFrame({
        "linha": h["linha"].str.strip(), "sentido": h["sentido"].str.strip(),
        "tipo_tabela": h["tipo_tabela"].str.strip(), "tipo_dia": h["tipo_dia"].str.strip(),
        "horario": h["horario_largada"].str.strip(),
        "adaptado": h["adaptado_deficiente"].str.upper().str.startswith("S"),
    }).drop_duplicates()

    it = ler_csv("itinerario_onibus.csv")
    itinerarios = pd.DataFrame({
        "linha": it["linha"].str.strip(), "sentido": it["sentido"].str.strip(),
        "seq": pd.to_numeric(it["numero_sequencia"], errors="coerce"),
        "tipo": it["tipo"].str.strip(), "nome": it["nome"].str.strip(),
        "logradouro": it["endereco_logradouro"].str.strip(), "codigo": it["codigo"].str.strip(),
    }).dropna(subset=["seq"]).drop_duplicates()
    itinerarios["seq"] = itinerarios["seq"].astype(int)

    fr = ler_csv("frota_onibus.csv")
    frota = fr[["prefixo", "operador", "placa", "marca", "modelo", "ano_fabricacao", "combustivel"]].copy()
    frota["ano_fabricacao"] = pd.to_numeric(frota["ano_fabricacao"], errors="coerce").astype("Int64")

    print("Lendo lugares do OpenStreetMap...")
    lugares = lugares_osm()
    terminais = itinerarios[itinerarios["tipo"] == "TERMINAL/PARADA"].drop_duplicates("codigo")
    term_nome = dict(zip(terminais["codigo"], terminais["nome"]))
    lp_linhas = linha_paradas.merge(linhas[["route_id", "numero"]], on="route_id")
    linhas_da_parada = lp_linhas.groupby("stop_id")["numero"].apply(lambda s: sorted(set(s)))
    par = paradas[paradas["stop_id"].isin(linhas_da_parada.index)].copy()
    par["tipo"] = "parada"
    par["detalhe"] = par["stop_id"].map(
        lambda s: ("Terminal · " if s in term_nome else "Parada · ") + "linhas " + ", ".join(linhas_da_parada[s][:6])
        + ("…" if len(linhas_da_parada[s]) > 6 else ""))
    par["nome"] = [term_nome.get(s, n).title() if s in term_nome else n for s, n in zip(par["stop_id"], par["nome"])]
    par["peso"] = np.where(par["stop_id"].isin(term_nome), 3.0, 1.5)
    lugares = pd.concat([lugares, par[["tipo", "nome", "detalhe", "lat", "lon", "peso"]]], ignore_index=True)
    lugares = lugares.drop_duplicates(["tipo", "nome", "lat", "lon"])

    with psycopg.connect(url_banco()) as con, con.cursor() as cur:
        cur.execute(ESQUEMA.read_text(encoding="utf-8"))
        for tabela, df in (("linhas", linhas), ("paradas", paradas), ("linha_paradas", linha_paradas),
                           ("horarios", horarios), ("itinerarios", itinerarios), ("frota", frota),
                           ("lugares", lugares)):
            copiar(cur, tabela, df)
            print(f"  {tabela}: {len(df)} linhas")
        cur.execute("ANALYZE")
        con.commit()
    print(f"Banco carregado em {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
