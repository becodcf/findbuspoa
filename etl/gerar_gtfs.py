"""
Gera um GTFS atualizado de Porto Alegre combinando:
  - GTFS antigo da EPTC (paradas com coordenadas, traçados, sequência de paradas, duração das viagens)
  - CSVs novos da EPTC (tabela horária e itinerário atuais)
  - malha viária do OpenStreetMap (para reconstruir traçados que o GTFS antigo não tem)

Para cada linha + sentido do CSV novo:
  - Se existe no GTFS antigo e o itinerário não mudou: cada horário de saída clona a viagem
    antiga mais parecida (mesmo sentido, horário mais próximo, de preferência o mesmo tipo
    de dia) e herda paradas, shape e duração.
  - Se o itinerário mudou (o traçado reconstruído a partir das ruas do CSV diverge do
    antigo) ou a linha não existe no GTFS antigo: o traçado é reconstruído sobre o OSM
    (ver etl/tracados.py) e as paradas do GTFS que ficam sobre ele viram a sequência.
Os horários intermediários ficam vazios (como no GTFS original) e são interpolados pelo
OpenTripPlanner.

Tabelas horárias: OFICIAL (ou EVENTUAL) no período normal; as sazonais (VERAO, CARNAVAL,
FERIASINVERNO...) entram nas datas definidas em PERIODOS. Cada combinação de datas vira um
service_id próprio em calendar_dates.txt.

Uso:  python etl/gerar_gtfs.py
Saída: otp/gtfs-poa.zip  e  etl/relatorio.txt
"""
from __future__ import annotations

import io
import sys
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracados as T  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
GTFS_ANTIGO = DADOS / "arquivo-gtfs.zip"
CSV_HORARIOS = DADOS / "tabela_horaria_onibus.csv"
CSV_ITINERARIO = DADOS / "itinerario_onibus.csv"
SAIDA = RAIZ / "otp" / "gtfs-poa.zip"
RELATORIO = RAIZ / "etl" / "relatorio.txt"

INICIO = date(2026, 11, 1)
FIM = date(2027, 10, 31)

# Tabela usada fora dos períodos especiais, em ordem de prioridade.
TABELAS_BASE = ["OFICIAL", "EVENTUAL"]

# Períodos das tabelas sazonais, da mais específica para a mais geral (a primeira que a
# linha tiver vale). As datas são ESTIMATIVAS com base no calendário usual da EPTC:
# confirmar nos avisos da EPTC e ajustar a cada ano.
# Ficam de fora as tabelas sem data previsível: PASSELIVRE*, EVENTUALSEGUNDAFEIRA,
# EVENTUALSEXTAFEIRA (a EPTC as ativa por aviso).
PERIODOS = [
    ("FINADOS", "2026-11-02", "2026-11-02"),
    ("PASSELIVRENATAL", "2026-12-25", "2026-12-25"),
    ("PASSELIVRENATAL", "2027-01-01", "2027-01-01"),
    ("FERIADONAVEGANTES", "2027-02-02", "2027-02-02"),
    ("PRIMEIRODEMAIO", "2027-05-01", "2027-05-01"),
    ("CARNAVAL", "2027-02-06", "2027-02-10"),
    ("FERIASINVERNO", "2027-07-12", "2027-07-23"),
    ("FEVEREIRO", "2027-02-01", "2027-02-28"),
    ("VERAO", "2027-01-04", "2027-02-28"),
    ("DEZEMBRO", "2026-12-21", "2027-01-03"),
]

# Feriados (nacionais + RS + Porto Alegre) no período: usam a tabela de FERIADO.
FERIADOS = [
    "2026-11-02", "2026-11-15", "2026-11-20", "2026-12-25",
    "2027-01-01", "2027-02-02", "2027-03-26", "2027-04-21", "2027-05-01",
    "2027-05-27", "2027-09-07", "2027-09-20", "2027-10-12",
]

TIPOS_DIA = ["DIAUTIL", "SABADO", "DOMINGO", "FERIADO"]
SIGLA_DIA = {"DIAUTIL": "DU", "SABADO": "SAB", "DOMINGO": "DOM", "FERIADO": "FER"}
# sufixo do service_id antigo (@1/@2/@3) -> tipo_dia
DIA_ANTIGO = {"1": "DIAUTIL", "2": "SABADO", "3": "DOMINGO"}

# Itinerário alterado: o traçado reconstruído (confiável) diverge do antigo em mais que isso.
LIMIAR_ALTERADO = 0.25
VELOCIDADE_PADRAO_KMH = 18  # último recurso para estimar duração


def hms_para_seg(s: str) -> int:
    partes = [int(p) for p in s.split(":")]
    while len(partes) < 3:
        partes.append(0)
    h, m, sec = partes
    return h * 3600 + m * 60 + sec


def seg_para_hms(x: int) -> str:
    return f"{x // 3600:02d}:{x % 3600 // 60:02d}:{x % 60:02d}"


def comprimento_shapes_km(shapes: pd.DataFrame) -> pd.Series:
    s = shapes.copy()
    s["shape_pt_sequence"] = s["shape_pt_sequence"].astype(int)
    s = s.sort_values(["shape_id", "shape_pt_sequence"])
    lat = np.radians(s["shape_pt_lat"].astype(float).to_numpy())
    lon = np.radians(s["shape_pt_lon"].astype(float).to_numpy())
    dlat, dlon = np.diff(lat, prepend=lat[0]), np.diff(lon, prepend=lon[0])
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(np.roll(lat, 1)) * np.sin(dlon / 2) ** 2
    d = 2 * 6371 * np.arcsin(np.sqrt(a))
    d[(s["shape_id"] != s["shape_id"].shift()).to_numpy()] = 0  # não somar entre shapes
    return pd.Series(d, index=s.index).groupby(s["shape_id"]).sum()


def corrigir_duracoes(trips: pd.DataFrame, km: pd.Series, log: list[str]) -> None:
    """O GTFS antigo tem viagens com chegada == saída (ou absurdamente curtas).
    Troca essas durações pela mediana das viagens válidas do mesmo padrão; na falta,
    estima pelo comprimento do traçado."""
    minimo = np.maximum(300, trips["n_paradas"] * 20)  # >= 5 min e >= 20 s por parada
    valida = trips["duracao"] >= minimo
    trips["duracao_valida"] = valida
    trips.loc[~valida, "duracao"] = np.nan
    for chave in (["linha", "sentido", "shape_id"], ["linha", "sentido"], ["linha"]):
        trips["duracao"] = trips["duracao"].fillna(trips.groupby(chave)["duracao"].transform("median"))
    trips["duracao"] = trips["duracao"].fillna(trips["shape_id"].map(km) / VELOCIDADE_PADRAO_KMH * 3600)
    trips["duracao"] = trips["duracao"].fillna(1800).astype(int)
    log.append(f"Viagens antigas com duração inválida corrigidas: {int((~valida).sum())} de {len(trips)}")


def velocidade_por_hora(trips: pd.DataFrame, km: pd.Series) -> dict[int, float]:
    """Velocidade mediana (km/h) das viagens antigas válidas, por hora de saída."""
    v = trips[trips["duracao_valida"]].copy()
    v["kmh"] = v["shape_id"].map(km) / (v["duracao"] / 3600)
    v = v[(v["kmh"] > 5) & (v["kmh"] < 60)]
    por_hora = v.groupby(v["saida"] // 3600 % 24)["kmh"].median()
    geral = float(v["kmh"].median()) if len(v) else VELOCIDADE_PADRAO_KMH
    return {h: float(por_hora.get(h, geral)) for h in range(24)}


def alinhar_shapes(shapes: pd.DataFrame, malha: T.Malha, log: list[str]) -> pd.DataFrame:
    """Os traçados do GTFS antigo estão ~90 m deslocados em relação às ruas (as paradas não).
    Cada traçado é deslocado para cima das vias do OSM. O resultado fica em cache."""
    cache = T.CACHE.parent / "shapes_alinhados.pkl"
    chave = (GTFS_ANTIGO.stat().st_mtime, T.OSM.stat().st_mtime)
    if cache.exists():
        c = pd.read_pickle(cache)
        if c.get("chave") == chave:
            log.append(f"Traçados antigos realinhados às ruas do OSM: {c['n']} (cache)")
            return c["shapes"]
    s = shapes.copy()
    s["q"] = s["shape_pt_sequence"].astype(int)
    s = s.sort_values(["shape_id", "q"])
    partes, n = [], 0
    for sid, p in s.groupby("shape_id", sort=False):
        lat, lon, desloc = malha.alinhar(p["shape_pt_lat"].astype(float).to_numpy(),
                                         p["shape_pt_lon"].astype(float).to_numpy())
        n += desloc > 0
        partes.append(p.assign(shape_pt_lat=np.round(lat, 6).astype(str), shape_pt_lon=np.round(lon, 6).astype(str)))
    out = pd.concat(partes).drop(columns="q")
    pd.to_pickle({"chave": chave, "shapes": out, "n": n}, cache)
    log.append(f"Traçados antigos realinhados às ruas do OSM: {n} de {s['shape_id'].nunique()}")
    return out


def ler_gtfs(caminho: Path) -> dict[str, pd.DataFrame]:
    with zipfile.ZipFile(caminho) as z:
        return {
            n[:-4]: pd.read_csv(z.open(n), dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace")
            for n in z.namelist() if n.endswith(".txt")
        }


# ---------------------------------------------------------------------------
# Calendário
# ---------------------------------------------------------------------------
def montar_calendario(h: pd.DataFrame, log: list[str]):
    """Decide, para cada linha e cada data, qual (tabela, tipo_dia) roda.
    Devolve {(linha, tabela, tipo_dia): service_id} e {service_id: [datas]}."""
    feriados = {date.fromisoformat(f) for f in FERIADOS}
    periodos = [(t, date.fromisoformat(a), date.fromisoformat(b)) for t, a, b in PERIODOS]
    disponivel = h.groupby("linha").apply(lambda g: set(zip(g["tipo_tabela"], g["tipo_dia"])), include_groups=False)

    datas = [INICIO + timedelta(days=i) for i in range((FIM - INICIO).days + 1)]
    info_data = []
    for d in datas:
        td = "FERIADO" if d in feriados else ["DIAUTIL"] * 5 + ["SABADO", "DOMINGO"]
        td = td if isinstance(td, str) else td[d.weekday()]
        especiais = [t for t, a, b in periodos if a <= d <= b]
        info_data.append((d, td, especiais + TABELAS_BASE))

    uso: dict[tuple[str, str, str], list[date]] = defaultdict(list)
    sem_servico = 0
    for linha, tem in disponivel.items():
        for d, td, ordem in info_data:
            escolha = None
            for t in ordem:
                if (t, td) in tem:
                    escolha = (t, td)
                elif td == "FERIADO" and (t, "DOMINGO") in tem:
                    escolha = (t, "DOMINGO")
                if escolha:
                    break
            if escolha:
                uso[(linha, *escolha)].append(d)
            else:
                sem_servico += 1

    servico_de_datas: dict[tuple, str] = {}
    contador: dict[str, int] = defaultdict(int)
    mapa = {}
    for chave, ds in sorted(uso.items()):
        k = tuple(ds)
        if k not in servico_de_datas:
            base = f"{chave[1]}-{SIGLA_DIA[chave[2]]}"
            contador[base] += 1
            servico_de_datas[k] = base if contador[base] == 1 else f"{base}-{contador[base]}"
        mapa[chave] = servico_de_datas[k]
    datas_do_servico = {sid: list(k) for k, sid in servico_de_datas.items()}
    usadas = defaultdict(set)
    for (linha, tab, _), _sid in mapa.items():
        usadas[tab].add(linha)
    log.append("Tabelas horárias em uso (linhas): " + ", ".join(f"{t}={len(v)}" for t, v in sorted(usadas.items())))
    log.append(f"Serviços (service_id) gerados: {len(datas_do_servico)}")
    return mapa, datas_do_servico


# ---------------------------------------------------------------------------
def main() -> None:
    log: list[str] = []
    g = ler_gtfs(GTFS_ANTIGO)
    routes, trips, st, stops = g["routes"], g["trips"], g["stop_times"], g["stops"]
    malha = T.carregar_malha()
    g["shapes"] = alinhar_shapes(g["shapes"], malha, log)
    km_shape = comprimento_shapes_km(g["shapes"])

    # ---------- viagens antigas: saída, duração e tipo de dia ----------
    st["stop_sequence"] = st["stop_sequence"].astype(int)
    st = st.sort_values(["trip_id", "stop_sequence"])
    com_hora = st[st["departure_time"] != ""]
    ini = com_hora.groupby("trip_id").first()
    fim = com_hora.groupby("trip_id").last()
    info = pd.DataFrame({
        "saida": ini["departure_time"].map(hms_para_seg),
        "chegada": fim["arrival_time"].map(hms_para_seg),
    })
    info["duracao"] = info["chegada"] - info["saida"]
    info["n_paradas"] = st.groupby("trip_id").size()
    trips = trips.merge(info, left_on="trip_id", right_index=True)
    trips = trips.merge(routes[["route_id", "route_short_name"]], on="route_id")
    trips["linha"] = trips["route_short_name"].str.upper().str.strip()
    trips["sentido"] = trips["trip_headsign"].str.upper().str.strip()
    trips["tipo_dia"] = trips["service_id"].str.split("@").str[-1].map(DIA_ANTIGO)
    corrigir_duracoes(trips, km_shape, log)
    vel_hora = velocidade_por_hora(trips, km_shape)
    log.append("Velocidade mediana das viagens antigas (km/h) às 7h/12h/18h/23h: "
               + ", ".join(f"{vel_hora[x]:.1f}" for x in (7, 12, 18, 23)))

    # ---------- horários novos ----------
    h = pd.read_csv(CSV_HORARIOS, sep=";", dtype=str, encoding="utf-8", encoding_errors="replace")
    for c in ("linha", "sentido", "tipo_tabela", "tipo_dia"):
        h[c] = h[c].str.upper().str.strip()
    tabelas_validas = set(TABELAS_BASE) | {t for t, _, _ in PERIODOS}
    sem_tabela = sorted(set(h["linha"]) - set(h.loc[h["tipo_tabela"].isin(tabelas_validas), "linha"]))
    h = h[h["tipo_tabela"].isin(tabelas_validas) & h["tipo_dia"].isin(TIPOS_DIA)]
    h = h.drop_duplicates(["linha", "sentido", "tipo_tabela", "tipo_dia", "horario_largada"])
    servico, datas_servico = montar_calendario(h, log)
    h["service_id"] = [servico.get(k) for k in zip(h["linha"], h["tipo_tabela"], h["tipo_dia"])]
    h = h[h["service_id"].notna()]
    log.append(f"Linhas com horário utilizável: {h['linha'].nunique()}")
    if sem_tabela:
        log.append(f"Linhas só com tabelas sem data definida (ignoradas): {len(sem_tabela)} -> {' '.join(sem_tabela)}")

    # ---------- traçados reconstruídos a partir do itinerário ----------
    itinerarios = T.ler_itinerarios(CSV_ITINERARIO)
    coord = {s: (float(a), float(b)) for s, a, b in zip(stops["stop_id"], stops["stop_lat"], stops["stop_lon"])}
    rumos = T.rumo_das_paradas(stops, g["shapes"], st, trips)
    indice = T.IndiceParadas(stops, rumos)
    shapes_antigos = {k: v.assign(q=v["shape_pt_sequence"].astype(int)).sort_values("q")
                      for k, v in g["shapes"].groupby("shape_id")}
    grupos = {k: df for k, df in trips.groupby(["linha", "sentido"])}

    novos_padroes = {}   # (linha, sentido) -> dict(shape, paradas, km, fator_duracao)
    alterados, novos, rejeitados, sem_itinerario = [], [], [], []
    for (linha, sentido) in sorted(set(zip(h["linha"], h["sentido"]))):
        it = itinerarios.get((linha, sentido))
        base = grupos.get((linha, sentido))
        if it is None:
            if base is None:
                sem_itinerario.append(f"{linha}/{sentido}")
            continue
        p_ini = coord.get(it.terminal_ini)
        p_fim = coord.get(it.terminal_fim) if it.terminal_fim else p_ini
        tr = T.tracar(malha, it.ruas, p_ini, p_fim)
        if base is not None:
            if tr is None or not tr.confiavel:
                continue
            sid = base["shape_id"].mode().iloc[0]
            s = shapes_antigos[sid]
            div = T.divergencia(tr.lat, tr.lon, s["shape_pt_lat"].astype(float).to_numpy(),
                                s["shape_pt_lon"].astype(float).to_numpy())
            if div <= LIMIAR_ALTERADO:
                continue
            alterados.append(f"{linha}/{sentido} ({div:.0%})")
            fator = tr.km / max(km_shape.get(sid, tr.km), 0.5)
        else:
            if tr is None or not tr.aceitavel:
                rejeitados.append(f"{linha}/{sentido}")
                continue
            novos.append(f"{linha}/{sentido}")
            fator = None
        paradas = indice.no_tracado(tr.lat, tr.lon)
        for term, pos in ((it.terminal_ini, 0), (it.terminal_fim or it.terminal_ini, -1)):
            if term not in coord:
                continue
            # só põe o terminal na ponta se ele estiver mesmo perto da ponta do traçado
            tx, ty = T.para_xy(*coord[term])
            px, py = T.para_xy(tr.lat[pos], tr.lon[pos])
            if np.hypot(tx - px, ty - py) > 300:
                continue
            if not paradas or (paradas[0] if pos == 0 else paradas[-1]) != term:
                if term in paradas and len(paradas) > 1:
                    paradas.remove(term)
                paradas.insert(0, term) if pos == 0 else paradas.append(term)
        if len(paradas) < 2:
            rejeitados.append(f"{linha}/{sentido}")
            (alterados if base is not None else novos).pop()
            continue
        novos_padroes[(linha, sentido)] = {"tr": tr, "paradas": paradas, "fator": fator, "it": it}

    log.append(f"Itinerários alterados desde o GTFS antigo (traçado reconstruído): {len(alterados)} -> {' '.join(alterados)}")
    log.append(f"Linha+sentido sem traçado antigo, reconstruídos pelo itinerário: {len(novos)} -> {' '.join(novos)}")
    if rejeitados:
        log.append(f"Linha+sentido sem traçado confiável (ignorados): {len(rejeitados)} -> {' '.join(rejeitados)}")
    if sem_itinerario:
        log.append(f"Linha+sentido sem traçado antigo nem itinerário (ignorados): {len(sem_itinerario)} -> {' '.join(sem_itinerario)}")

    # ---------- viagens ----------
    rotas_antigas = routes.assign(linha=routes["route_short_name"].str.upper().str.strip()).drop_duplicates("linha").set_index("linha")
    rotas_novas = {}
    novos_trips, mapa_clone, st_novos = [], [], []
    for (linha, sentido, svc), hs in h.groupby(["linha", "sentido", "service_id"]):
        tipo_dia = hs["tipo_dia"].iloc[0]
        padrao = novos_padroes.get((linha, sentido))
        base = grupos.get((linha, sentido))
        if padrao is None and base is None:
            continue
        if linha in rotas_antigas.index:
            route_id = rotas_antigas.loc[linha, "route_id"]
        else:
            route_id = linha
            it = padrao["it"]
            rotas_novas[route_id] = {
                "route_id": route_id, "agency_id": "EPTC", "route_short_name": linha,
                "route_long_name": f"{it.nome_ini.split(',')[0].title()} / {it.nome_fim.split(',')[0].title()}",
                "route_desc": "", "route_type": "3", "route_url": "https://linhas.eptc.com.br",
                "route_color": "FF0000", "route_text_color": "FFFFFF",
            }
        if base is not None:
            mesmo_dia = base[base["tipo_dia"] == tipo_dia]
            if mesmo_dia.empty:
                mesmo_dia = base
            saidas_base = mesmo_dia["saida"].to_numpy()
        for _, r in hs.iterrows():
            saida = hms_para_seg(r["horario_largada"])
            tid = f"{linha}-{sentido}-{svc}-{r['horario_largada'].replace(':', '')}"
            acess = "1" if str(r["adaptado_deficiente"]).upper().startswith("S") else "2"
            if base is not None:
                # distância circular no dia, para 23:50 casar com 00:10
                d = np.abs(saidas_base - saida) % 86400
                d = np.where(d <= 43200, d, 86400 - d)
                modelo = mesmo_dia.iloc[int(d.argmin())]
            if padrao is not None:
                if padrao["fator"] is not None:
                    dur = int(modelo["duracao"] * padrao["fator"])
                else:
                    dur = int(padrao["tr"].km / vel_hora[saida // 3600 % 24] * 3600)
                dur = max(dur, 300, len(padrao["paradas"]) * 20)
                shape_id = f"N-{linha}-{sentido}"
                headsign, direcao = sentido.title(), "1" if sentido.startswith("CENTRO") else "0"
                st_novos.append((tid, padrao["paradas"], saida, dur))
            else:
                shape_id, headsign, direcao = modelo["shape_id"], modelo["trip_headsign"], modelo["direction_id"]
                mapa_clone.append((tid, modelo["trip_id"], saida, int(modelo["duracao"])))
            novos_trips.append({
                "route_id": route_id, "service_id": svc, "trip_id": tid, "trip_headsign": headsign,
                "direction_id": direcao, "shape_id": shape_id, "wheelchair_accessible": acess,
            })

    novos_trips = pd.DataFrame(novos_trips)
    clones = pd.DataFrame(mapa_clone, columns=["trip_id", "modelo", "saida", "duracao"])

    # stop_times: copia a sequência do modelo, horário só na 1ª e última parada
    modelos = st[st["trip_id"].isin(clones["modelo"])][["trip_id", "stop_id", "stop_sequence"]]
    modelos = modelos.rename(columns={"trip_id": "modelo"})
    novos_st = clones.merge(modelos, on="modelo")
    linhas_rec = [(tid, sid, i + 1, saida, dur, len(ps)) for tid, ps, saida, dur in st_novos for i, sid in enumerate(ps)]
    rec = pd.DataFrame(linhas_rec, columns=["trip_id", "stop_id", "stop_sequence", "saida", "duracao", "n"])
    novos_st = pd.concat([novos_st, rec.drop(columns="n")], ignore_index=True)
    primeiro = novos_st.groupby("trip_id")["stop_sequence"].transform("min")
    ultimo = novos_st.groupby("trip_id")["stop_sequence"].transform("max")
    hora = pd.Series("", index=novos_st.index)
    eh_pri = novos_st["stop_sequence"] == primeiro
    eh_ult = novos_st["stop_sequence"] == ultimo
    hora[eh_pri] = novos_st.loc[eh_pri, "saida"].map(seg_para_hms)
    hora[eh_ult] = (novos_st["saida"] + novos_st["duracao"])[eh_ult].map(seg_para_hms)
    novos_st["arrival_time"] = hora
    novos_st["departure_time"] = hora
    novos_st["timepoint"] = (hora != "").map({True: "1", False: "0"})
    novos_st = novos_st.sort_values(["trip_id", "stop_sequence"])[
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "timepoint"]]

    # ---------- shapes ----------
    usados_shape = set(novos_trips["shape_id"])
    shape_novo = {f"N-{l}-{s}": (l, s) for (l, s) in novos_padroes}
    shapes_out = []
    for sid in sorted(usados_shape):
        if sid in shape_novo:
            tr = novos_padroes[shape_novo[sid]]["tr"]
            lat, lon = tr.lat, tr.lon
        else:
            s = shapes_antigos[sid]
            lat, lon = s["shape_pt_lat"].astype(float).to_numpy(), s["shape_pt_lon"].astype(float).to_numpy()
        shapes_out.append(pd.DataFrame({
            "shape_id": sid, "shape_pt_lat": np.round(lat, 6), "shape_pt_lon": np.round(lon, 6),
            "shape_pt_sequence": np.arange(1, len(lat) + 1),
        }))
    shapes_out = pd.concat(shapes_out, ignore_index=True)

    # ---------- calendário ----------
    calendar_dates = pd.DataFrame(
        [{"service_id": sid, "date": d.strftime("%Y%m%d"), "exception_type": "1"}
         for sid, ds in sorted(datas_servico.items()) if sid in set(novos_trips["service_id"]) for d in ds])

    # ---------- tabelas restantes ----------
    rotas = pd.concat([routes[routes["route_id"].isin(set(novos_trips["route_id"]))],
                       pd.DataFrame(list(rotas_novas.values()))], ignore_index=True)
    # texto branco sobre cores escuras (aviso route_color_contrast do validador)
    def texto(cor: str) -> str:
        try:
            r, g_, b = (int(cor[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return "000000"
        return "000000" if 0.299 * r + 0.587 * g_ + 0.114 * b > 150 else "FFFFFF"
    rotas["route_text_color"] = rotas["route_color"].map(texto)
    usados_stop = set(novos_st["stop_id"])
    agencia = g["agency"].copy()
    out = {
        "agency": agencia,
        "routes": rotas,
        "stops": stops[stops["stop_id"].isin(usados_stop)],
        "shapes": shapes_out,
        "trips": novos_trips,
        "stop_times": novos_st,
        "calendar_dates": calendar_dates,
        "feed_info": pd.DataFrame([{
            "feed_publisher_name": "projetoUberBus (derivado de dados EPTC)",
            "feed_publisher_url": "https://github.com/becodcf/uberbuspoa",
            "feed_lang": "pt",
            "feed_start_date": INICIO.strftime("%Y%m%d"),
            "feed_end_date": FIM.strftime("%Y%m%d"),
            "feed_version": date.today().isoformat(),
            "feed_contact_url": "https://github.com/becodcf/uberbuspoa/issues",
        }]),
    }
    if "fare_attributes" in g:
        out["fare_attributes"] = g["fare_attributes"]

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(SAIDA, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, df in out.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False, lineterminator="\n")
            z.writestr(f"{nome}.txt", buf.getvalue())

    linhas_csv = set(h["linha"])
    linhas_feed = set(rotas["route_short_name"].str.upper().str.strip())
    log.append("")
    log.append(f"Linhas com horário no CSV: {len(linhas_csv)}; no feed: {len(linhas_feed & linhas_csv)}")
    faltam = sorted(linhas_csv - linhas_feed)
    log.append(f"Linhas que ficaram de fora: {len(faltam)} -> {' '.join(faltam)}")
    log.append("")
    log.append(f"GTFS gerado: {SAIDA}")
    for nome, df in out.items():
        log.append(f"  {nome}.txt: {len(df)} linhas")
    RELATORIO.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    main()
