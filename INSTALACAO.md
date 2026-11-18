# Como instalar o FindBus POA em outro computador

## 1. O que a máquina precisa ter

### Instalar antes (à mão)

| Programa | Versão | Para quê | Onde baixar |
|---|---|---|---|
| Python | 3.11, 3.12 ou 3.13 (marcar "Add Python to PATH") | ETL e backend | python.org |
| Java (JDK ou JRE) | 21 ou mais novo | OpenTripPlanner e validador | Microsoft Build of OpenJDK ou Eclipse Temurin |
| Git | qualquer versão recente | baixar o projeto | git-scm.com |
| PostgreSQL | 14 ou mais novo (o instalador oficial já traz as extensões `pg_trgm` e `unaccent`) | busca de lugares, linhas, horários, cache e histórico | postgresql.org/download (instalador da EDB) |

Evite o Python 3.14 ou mais novo: o pacote `osmium` pode ainda não ter versão pronta para ele.
Sem Git, dá para baixar o ZIP do repositório pelo site do GitHub.

Na instalação do PostgreSQL, **anote a senha do usuário `postgres`**: o `iniciar.ps1` pede essa senha uma vez para criar o banco do projeto. PostGIS e Stack Builder não são necessários.

### Já vem no Windows 10/11

- PowerShell 5.1 (roda o `iniciar.ps1` e faz os downloads).
- Navegador, para abrir http://localhost:8000.

### O `iniciar.ps1` instala ou baixa sozinho na primeira execução

- Bibliotecas Python, na pasta `.venv`: pandas, numpy, scipy, osmium, fastapi, uvicorn, httpx, psycopg.
- Banco `uberbus` e usuário `uberbus` no PostgreSQL, com senha aleatória gravada em `.env` (fora do git).
- `otp/otp.jar`: OpenTripPlanner 2.9 (175 MB).
- `dados/osm/sul-latest.osm.pbf`: mapa da região Sul da Geofabrik (430 MB), recortado para `otp/poa.osm.pbf` (29 MB).
- `ferramentas/gtfs-validator-cli.jar`: validador da MobilityData (40 MB).

### Hardware e rede

- RAM: 12 GB ou mais (a montagem do grafo usa até 8 GB; o OTP rodando, até 6 GB).
- Disco: 2 GB livres.
- Internet na primeira execução (~650 MB de downloads). Depois funciona offline, exceto a busca de endereço (Nominatim).
- Portas 8000 e 8080 livres; PostgreSQL rodando na 5432.
- Na primeira vez o Firewall do Windows pode perguntar sobre o Java: permitir só em "Redes privadas".

### Não precisa

Docker, PostGIS, Node.js, chave de API ou conta paga.

## 2. Passo a passo (Windows)

A primeira execução leva de 10 a 15 minutos; as seguintes sobem em menos de 1 minuto.

1. Instalar Python, Java, Git e PostgreSQL (seção 1).
2. Conferir no PowerShell:
   ```powershell
   python --version
   java -version
   ```
3. Baixar o projeto:
   ```powershell
   git clone https://github.com/becodcf/uberbuspoa.git
   cd uberbuspoa
   ```
4. Se o PowerShell bloquear scripts, liberar só nessa janela:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   ```
5. Rodar:
   ```powershell
   .\iniciar.ps1
   ```
   Ele cria o `.venv`, instala as dependências, baixa os arquivos grandes, recorta o mapa, gera o GTFS, monta o grafo, cria e carrega o banco e sobe tudo.
   Quando aparecer `Senha do usuário 'postgres' do PostgreSQL:`, digite a senha definida na instalação do PostgreSQL (nada aparece na tela enquanto digita).
6. Esperar "Grizzly server running" na janela do Java e abrir http://localhost:8000.
7. Conferir http://localhost:8000/api/saude: a resposta deve ter `"otp":"ok"` e `"banco":"ok"`.

### Linux ou macOS

Os mesmos pré-requisitos; no lugar do passo 5, a partir da pasta do projeto:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p dados/osm && curl -L -o dados/osm/sul-latest.osm.pbf https://download.geofabrik.de/south-america/brazil/sul-latest.osm.pbf
curl -L -o otp/otp.jar https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.9.0/otp-shaded-2.9.0.jar
.venv/bin/python etl/recortar_osm.py dados/osm/sul-latest.osm.pbf otp/poa.osm.pbf
.venv/bin/python etl/criar_banco.py
.venv/bin/python etl/gerar_gtfs.py
(cd otp && java -Xmx8G -jar otp.jar --build --save .)
.venv/bin/python etl/carregar_postgres.py
(cd otp && java -Xmx6G -jar otp.jar --load .) &
.venv/bin/uvicorn backend.app:app --port 8000
```

## 3. Uso no dia a dia

| Comando | Quando usar |
|---|---|
| `.\iniciar.ps1` | subir o OTP (porta 8080) e o backend (porta 8000) |
| `.\iniciar.ps1 -Rebuild` | depois de trocar os CSVs em `dados\`: regenera o GTFS e o grafo |
| `.\iniciar.ps1 -Banco` | recarregar só o PostgreSQL |
| `.\iniciar.ps1 -Validar` | rodar o gtfs-validator; relatório em `otp\validacao\report.html` |

Teste rápido (Restinga → Centro, terça às 8h; deve sugerir R16 ou 110):

```
http://localhost:8000/api/rota?de_lat=-30.1498&de_lon=-51.1467&para_lat=-30.0277&para_lon=-51.2287&quando=2026-11-17T08:00:00-03:00
```

## 4. Problemas comuns

| Sintoma | Causa provável | Solução |
|---|---|---|
| `UnsupportedClassVersionError` no `otp.jar` | Java anterior ao 21 | instalar Java 21+ |
| `OutOfMemoryError` no build do grafo | menos de 8 GB livres | fechar programas ou trocar `-Xmx8G` por `-Xmx6G` no `iniciar.ps1` |
| `/api/saude` diz "fora do ar" | OTP ainda carregando | esperar "Grizzly server running" (~30 s) |
| "porta já em uso" (8000/8080) | cópia anterior rodando | fechar a janela anterior do Java ou do backend |
| "execution of scripts is disabled" | política do PowerShell | `Set-ExecutionPolicy -Scope Process Bypass` |
| `pip install osmium` falha | Python sem pacote pronto | usar Python 3.11 a 3.13 |
| alteração no backend não aparece | `--reload` do uvicorn não detecta no Windows | reiniciar o backend |
| busca de endereço exato falha | limite do Nominatim (1 consulta/s) | esperar e tentar de novo |
| `password authentication failed` ao criar o banco | senha errada do `postgres` | rodar de novo `.venv\Scripts\python.exe etl\criar_banco.py`; se não lembrar a senha, redefinir pelo pgAdmin |
| `/api/saude` mostra `"banco": "fora do ar"` | serviço do PostgreSQL parado ou `.env` ausente | iniciar o serviço `postgresql-x64-NN` em Serviços do Windows; sem `.env`, rodar `etl\criar_banco.py` |
| sugestões não aparecem enquanto digita | banco vazio | `.\iniciar.ps1 -Banco` |
