# FindBus POA — MVP

Planejador de viagens de ônibus para Porto Alegre usando só dados abertos e software gratuito.

```
dados/ (EPTC)  ──►  etl/gerar_gtfs.py  ──►  otp/gtfs-poa.zip  ──►  OpenTripPlanner (:8080)
                                                                        ▲
                       navegador  ──►  backend/app.py (FastAPI :8000) ──┘
                                              ├──► PostgreSQL (busca de lugares, linhas, horários, cache, histórico)
                                              └──► Nominatim (endereço exato, com cache no PostgreSQL)
```

## Rodar

Pré-requisitos: Python 3.11+, Java 21+ e PostgreSQL 14+. Passo a passo completo em [INSTALACAO.md](INSTALACAO.md).

```powershell
.\iniciar.ps1            # sobe OTP + backend; abra http://localhost:8000
.\iniciar.ps1 -Rebuild   # depois de atualizar os CSVs em dados\
```

O OTP leva cerca de 30 s para carregar. `GET /api/saude` mostra se ele está no ar.

```powershell
.\iniciar.ps1 -Validar   # roda o gtfs-validator (MobilityData); relatório em otp\validacao\report.html
eport.html
```

Na primeira execução o script baixa automaticamente os arquivos que não vão para o git:
- `otp.jar`: https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.9.0/otp-shaded-2.9.0.jar
- mapa OSM: extrato da região Sul da Geofabrik (https://download.geofabrik.de/south-america/brazil/sul-latest.osm.pbf, ~430 MB), recortado para POA por `etl/recortar_osm.py` em `otp/poa.osm.pbf` (~29 MB)
- `gtfs-validator-cli.jar` (MobilityData, v8.0.1)

## API

| Rota | Descrição |
|---|---|
| `GET /api/sugestoes?q=ramiro barcelos` | lugares enquanto se digita: ruas, lugares, endereços com número, paradas (PostgreSQL) |
| `GET /api/geocode?q=Rua Ramiro Barcelos 2350` | opções de endereço exato para a pessoa escolher (Nominatim, máx. 1 req/s, cache no PostgreSQL) |
| `GET /api/linhas?q=` e `GET /api/linhas/{numero}` | linhas; paradas, ruas e horários de uma linha |
| `GET /api/paradas/proximas?lat=&lon=&raio=400` | paradas perto de um ponto e as linhas de cada uma |
| `GET /api/rota?de_lat=&de_lon=&para_lat=&para_lon=&quando=2026-11-11T08:00:00-03:00` | até 3 opções de viagem com trechos, horários e geometria (polyline) |
| `GET /api/saude` | status do OTP e do banco |

## Como o GTFS é gerado

- **Paradas**: GTFS EPTC de 18/09/2025.
- **Horários de saída**: `tabela_horaria_onibus.csv` (04/11/2026). Fora dos períodos especiais vale a tabela OFICIAL ou, na falta dela, EVENTUAL. As tabelas sazonais (VERAO, FEVEREIRO, DEZEMBRO, CARNAVAL, FERIASINVERNO, FINADOS, FERIADONAVEGANTES, PRIMEIRODEMAIO, PASSELIVRENATAL) entram nas datas de `PERIODOS` em `etl/gerar_gtfs.py`. Cada combinação de datas vira um `service_id` em `calendar_dates.txt`.
- **Traçados**:
  - linha que existe no GTFS antigo e não mudou: cada saída clona a viagem antiga mais próxima (mesma linha/sentido/tipo de dia) e herda paradas, traçado e duração;
  - linha nova ou com itinerário alterado: o traçado é reconstruído sobre o OSM a partir da sequência de ruas do `itinerario_onibus.csv` (`etl/tracados.py`), e as paradas do GTFS antigo que ficam sobre ele, no sentido certo, viram a sequência de paradas. A duração vem da velocidade mediana das viagens antigas naquela hora.
  - os traçados antigos vinham ~90 m deslocados em relação às ruas; o ETL os realinha ao OSM.
- Os horários intermediários são interpolados pelo OTP.
- Validade: 01/11/2026 a 31/10/2027 (feriados em `FERIADOS`).

O resumo de cada execução fica em `etl/relatorio.txt`: linhas reconstruídas, itinerários alterados e linhas que ficaram de fora.

## Banco de dados (PostgreSQL)

Esquema em `db/esquema.sql`; carga por `etl/carregar_postgres.py` (~45 s), que roda no `-Rebuild`, no `-Banco` e na primeira execução.

| Tabela | Conteúdo |
|---|---|
| `linhas`, `paradas`, `linha_paradas` | linhas e paradas do GTFS gerado |
| `horarios`, `itinerarios`, `frota` | CSVs da EPTC como vieram (todas as tabelas horárias, inclusive as sazonais) |
| `lugares` | busca enquanto se digita: ruas, lugares com nome e endereços com número do OSM + paradas; índice trigrama sem acento |
| `geocode_cache` | respostas do Nominatim (preservada entre cargas) |
| `buscas` | histórico das buscas de rota (preservada entre cargas) |

Na página, as sugestões vêm do banco; o Nominatim só é chamado quando a pessoa pede o endereço exato (Enter ou "Procurar endereço exato"), porque a política dele proíbe uso como autocompletar. O sistema nunca escolhe o endereço sozinho: sem escolha, mostra as opções.

## Limitações conhecidas

1. 34 linhas ficam de fora: a maioria não tem itinerário no CSV da EPTC (148, 160, 430, LI.1…); algumas têm traçado reconstruído pouco confiável. A lista está no relatório.
2. As datas das tabelas sazonais são estimativas; confirmar nos avisos da EPTC. PASSE LIVRE (exceto Natal) e as tabelas EVENTUAL* de dia específico não são usadas.
3. Traçados reconstruídos dependem de casar o nome da rua da EPTC com o do OSM; ~4% dos nomes não casam e o trecho é ligado pelo caminho mais curto.
4. Não há tempo real (GPS), só a tabela horária.
