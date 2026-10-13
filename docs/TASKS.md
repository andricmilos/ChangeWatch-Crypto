# ChangeWatch Crypto — plan po taskovima

Oct 13, 2026 · @Milos

## Kako koristiti ovaj plan

Projekat je podeljen na 17 taskova. Radi ih redom, jer svaki gradi na prethodnom, i nemoj preskakati "Gotovo kada" proveru pre nego što pređeš dalje. Plan je pisan za macOS, PyCharm, Python 3.12+ i pip + venv.

Svaki task ima isti oblik, pa ceo blok možeš da kopiraš u PyCharm, u svoje beleške ili u AI asistenta:

- **Cilj** — šta na kraju taska radi, u jednoj rečenici.
- **Koraci** — šta tačno praviš, redom.
- **Fajlovi** — koje fajlove praviš ili menjaš.
- **Gotovo kada** — lista provere; task je završen tek kad je sve štiklirano.
- **Zašto** — kratko objašnjenje za početnika.

Posle svakog završenog taska napravi git commit sa porukom kao `Task 3: parser Binance poruka`. Tako uvek možeš da se vratiš na verziju koja je radila.

| Task | Faza | Rezultat |
| --- | --- | --- |
| 1 | 1 — Live feed | Prazan projekat koji se pokreće u PyCharm-u |
| 2 | 1 — Live feed | Podešavanja iz `.env` fajla |
| 3 | 1 — Live feed | Binance poruke pretvorene u čiste Python objekte |
| 4 | 1 — Live feed | Stabilna veza sa Binance-om koja se sama obnavlja |
| 5 | 1 — Live feed | Najnovije cene u memoriji, sa detekcijom zastarelih podataka |
| 6 | 2 — Dashboard | `GET /api/markets` vraća prave cene |
| 7 | 2 — Dashboard | Browser dobija cene uživo preko WebSocket-a |
| 8 | 2 — Dashboard | Tamni dashboard koji se sam osvežava |
| 9 | 3 — Grafikoni | SQLite baza i model za sveće |
| 10 | 3 — Grafikoni | Istorijske sveće sa Binance REST API-ja |
| 11 | 3 — Grafikoni | Interaktivni grafikon koji se ažurira uživo |
| 12 | 4 — Watchlist i alerti | Watchlist koji ostaje posle restarta |
| 13 | 4 — Watchlist i alerti | Alerti koji se okidaju tačno jednom |
| 14 | 5 — Objava | Validacija, ograničenje zahteva, obrada grešaka |
| 15 | 5 — Objava | Testovi i linting na svaki push |
| 16 | 5 — Objava | Aplikacija se pokreće u Docker-u |
| 17 | 5 — Objava | README i javni demo |

**Savet:** sačuvaj originalnu specifikaciju projekta u repozitorijum kao `docs/SPEC.md`. Kad god ti nešto nije jasno, ili kad nekom asistentu daješ task, ta specifikacija daje ceo kontekst.

## Task 1 — Kreiranje projekta od nule

**Cilj:** projekat `changewatch-crypto` postoji u PyCharm-u, ima kompletnu strukturu foldera, virtuelno okruženje, git, i minimalnu FastAPI aplikaciju koja na `http://127.0.0.1:8000/health` vraća `{"status": "ok"}`.

**Koraci**

1. Proveri da imaš Python 3.12 ili noviji i git. U macOS Terminalu pokreni `python3 --version` i `git --version`. Ako je Python stariji od 3.12, instaliraj noviji sa python.org ili preko Homebrew-a (`brew install python@3.12`).
2. U PyCharm-u izaberi **File → New Project**:
   - Tip projekta: **Pure Python**.
   - Location: `~/PycharmProjects/changewatch-crypto`.
   - Interpreter: **Project venv** (folder `.venv` unutar projekta), Python 3.12+.
   - Uključi **Create Git repository**, isključi „welcome script”.
3. Otvori terminal u PyCharm-u (**View → Tool Windows → Terminal**). On automatski aktivira `.venv`; to prepoznaješ po `(.venv)` na početku reda. Pokreni ove komande da napraviš celu strukturu:

```bash
mkdir -p app/{models,schemas,services,api,websocket,templates,static/css,static/js} tests docs data

touch app/__init__.py app/main.py app/config.py app/database.py \
  app/models/__init__.py app/models/{watchlist,alert,candle}.py \
  app/schemas/__init__.py app/schemas/{market,alert,watchlist}.py \
  app/services/__init__.py app/services/{exchange_client,market_data_service,candle_service,alert_service}.py \
  app/api/__init__.py app/api/{routes_market,routes_alerts,routes_watchlist}.py \
  app/websocket/__init__.py app/websocket/{manager,routes}.py \
  app/templates/index.html app/static/css/style.css app/static/js/app.js \
  tests/__init__.py tests/{test_health,test_market_data,test_alerts,test_websocket,test_candles}.py \
  data/.gitkeep README.md .env.example requirements.txt requirements-dev.txt pyproject.toml
```

4. Popuni `requirements.txt` (biblioteke koje aplikacija koristi):

```text
fastapi>=0.115
uvicorn[standard]>=0.30
websockets>=13.0
httpx>=0.27
pydantic>=2.7
pydantic-settings>=2.3
sqlalchemy>=2.0
aiosqlite>=0.20
jinja2>=3.1
python-multipart>=0.0.9
```

5. Popuni `requirements-dev.txt` (alati samo za razvoj i testove):

```text
pytest>=8.0
pytest-asyncio>=0.24
ruff>=0.6
```

6. Instaliraj sve u terminalu PyCharm-a:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
```

7. Popuni `pyproject.toml` (podaci o projektu i podešavanja za Ruff i pytest):

```toml
[project]
name = "changewatch-crypto"
version = "0.1.0"
description = "Real-time cryptocurrency market monitor built with FastAPI"
requires-python = ">=3.12"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

8. Popuni `.gitignore` (napravi ga u korenu projekta ako ga PyCharm nije napravio):

```text
.venv/
__pycache__/
*.pyc
.env
data/*.db
.pytest_cache/
.ruff_cache/
.idea/
.DS_Store
```

9. Popuni `.env.example` za sada samo osnovom; Binance adrese dodaješ u Tasku 2:

```text
APP_ENV=development
LOG_LEVEL=INFO
```

10. Napiši minimalnu aplikaciju u `app/main.py`:

```python
from fastapi import FastAPI

app = FastAPI(title="ChangeWatch Crypto")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

11. Napiši prvi test u `tests/test_health.py`:

```python
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

12. Napravi Run konfiguraciju u PyCharm-u (radi i u besplatnoj verziji): **Run → Edit Configurations → + → Python**.
    - Name: `Run server`.
    - Umesto „Script path” izaberi **Module** i upiši `uvicorn`.
    - Parameters: `app.main:app --reload`.
    - Working directory: koren projekta.
13. Podesi pytest kao test runner: **Settings → Tools → Python Integrated Tools → Default test runner: pytest**.
14. U `README.md` upiši naslov projekta i jednu rečenicu opisa; ceo README pišeš u Tasku 17. Sačuvaj specifikaciju projekta kao `docs/SPEC.md`.
15. Napravi prvi commit: `git add .` pa `git commit -m "Task 1: struktura projekta"`. Ako želiš, napravi prazan repozitorijum na GitHub-u (sa MIT licencom) i poveži ga.

**Fajlovi:** cela struktura iz koraka 3, plus `.gitignore`. `Dockerfile`, `compose.yaml` i `.github/workflows/tests.yml` namerno još ne postoje; prazan workflow fajl bi pao na GitHub-u, pa ih praviš u Taskovima 15 i 16.

**Gotovo kada**

- [ ] `Run server` pokreće aplikaciju bez greške.
- [ ] `http://127.0.0.1:8000/health` u browseru prikazuje `{"status":"ok"}`.
- [ ] `http://127.0.0.1:8000/docs` prikazuje automatsku FastAPI dokumentaciju.
- [ ] `pytest` u terminalu prijavljuje 1 passed test.
- [ ] `ruff check .` i `ruff format --check .` ne prijavljuju greške.
- [ ] `git status` je čist posle commit-a, a `.venv` i `.env` nisu u repozitorijumu.

**Zašto:** virtuelno okruženje (`.venv`) drži biblioteke ovog projekta odvojeno od ostatka računara. `requirements.txt` beleži šta projekat koristi, pa ga svako (i Docker kasnije) instalira jednom komandom. Prazni fajlovi u strukturi su mesta gde će kod ići; svaki folder ima jednu odgovornost, kao u specifikaciji. `--reload` restartuje server čim sačuvaš fajl, pa ne moraš ručno.

## Task 2 — Konfiguracija

**Cilj:** sva podešavanja (lista parova, Binance adrese, baza, prag za zastarele podatke) čitaju se iz `.env` fajla preko jedne `Settings` klase, a nijedna vrednost nije zakucana u kodu.

**Koraci**

1. U `app/config.py` napravi klasu `Settings` koja nasleđuje `BaseSettings` iz `pydantic_settings`. Neka čita `.env` (`SettingsConfigDict(env_file=".env", extra="ignore")`).
2. Dodaj polja sa podrazumevanim vrednostima:

```python
app_env: str = "development"
log_level: str = "INFO"
database_url: str = "sqlite+aiosqlite:///./data/changewatch.db"
supported_symbols: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"]
binance_ws_url: str = "wss://data-stream.binance.vision"
binance_rest_url: str = "https://data-api.binance.vision"
stale_after_seconds: int = 15
reconnect_initial_delay: float = 1.0
reconnect_max_delay: float = 60.0
```

3. Dodaj `field_validator` za `supported_symbols` koji svaki simbol pretvara u velika slova i uklanja razmake, i baca grešku ako je lista prazna.
4. Dodaj funkciju `get_settings()` sa `@lru_cache`, da se podešavanja učitaju samo jednom.
5. Ažuriraj `.env.example` (lista se u `.env` piše kao JSON):

```text
APP_ENV=development
LOG_LEVEL=INFO
DATABASE_URL=sqlite+aiosqlite:///./data/changewatch.db
SUPPORTED_SYMBOLS=["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","ADAUSDT"]
BINANCE_WS_URL=wss://data-stream.binance.vision
BINANCE_REST_URL=https://data-api.binance.vision
STALE_AFTER_SECONDS=15
```

6. Kopiraj ga u pravi `.env`: `cp .env.example .env`. `.env` ostaje van git-a.
7. U `app/main.py` podesi logovanje na osnovu `log_level` (`logging.basicConfig`).
8. Napiši `tests/test_config.py`: podrazumevane vrednosti (napravi `Settings(_env_file=None)`), pretvaranje `"btcusdt "` u `"BTCUSDT"`, i greška za praznu listu.

**Fajlovi:** `app/config.py`, `app/main.py`, `.env.example`, `.env`, `tests/test_config.py`.

**Gotovo kada**

- [ ] `from app.config import get_settings; get_settings().supported_symbols` vraća 5 parova.
- [ ] Promena `STALE_AFTER_SECONDS` u `.env` menja vrednost bez izmene koda.
- [ ] Testovi za konfiguraciju prolaze.

**Zašto:** `data-stream.binance.vision` i `data-api.binance.vision` su Binance adrese namenjene samo javnim tržišnim podacima, bez naloga i API ključa. Allowlist parova u konfiguraciji je osnova bezbednosti iz specifikacije: aplikacija nikad ne prihvata proizvoljan simbol od korisnika.

## Task 3 — Normalizovani podaci i parser Binance poruka

**Cilj:** sirove Binance JSON poruke pretvaraju se u dva čista Pydantic modela (`MarketTicker` i `KlineUpdate`) sa `Decimal` cenama, a neispravne poruke se odbacuju umesto da postanu cena 0. Još nema mreže; sve se testira na primerima poruka.

**Koraci**

1. U `app/schemas/market.py` napravi model `MarketTicker` sa poljima: `symbol` (`BTCUSDT`), `display_symbol` (`BTC/USDT`), `last_price: Decimal`, `price_change`, `price_change_percent`, `high_24h`, `low_24h`, `volume_24h` (sva `Decimal | None`) i `event_time: datetime` (UTC).
2. U istom fajlu napravi `KlineUpdate`: `symbol`, `interval`, `open_time`, `close_time`, `open`, `high`, `low`, `close`, `volume` i `is_closed: bool`.
3. U `app/services/exchange_client.py` napiši funkcije:
   - `to_display_symbol("BTCUSDT") -> "BTC/USDT"`.
   - `ms_to_datetime(ms: int) -> datetime` (Binance vreme je u milisekundama, UTC).
   - `parse_ticker(data: dict) -> MarketTicker | None` za `24hrTicker` poruke. Polja: `s` simbol, `c` poslednja cena, `p` promena, `P` promena u %, `h` i `l` maksimum i minimum, `v` volumen, `E` vreme događaja.
   - `parse_kline(data: dict) -> KlineUpdate | None` za `kline` poruke. Podaci su u `data["k"]`: `t`, `T`, `i`, `o`, `h`, `l`, `c`, `v`, `x`.
   - `parse_message(raw: str, allowed: set[str])` koja raspakuje kombinovani format `{"stream": ..., "data": ...}`, gleda polje `e` i poziva pravi parser.
4. Pravila za parser:
   - Brojeve pravi sa `Decimal(str_vrednost)`, nikad preko `float`.
   - Ako `c` nedostaje, nije broj ili je ≤ 0, vrati `None` i zapiši upozorenje u log. Nikad ne vraćaj 0.
   - Simbol koji nije u allowlist-i vrati kao `None`.
   - Nepoznat tip poruke ignoriši bez greške.
5. U `tests/test_market_data.py` napiši testove sa primerima poruka kao Python rečnicima:
   - ispravan ticker → tačne `Decimal` vrednosti i `display_symbol`;
   - kombinovani format sa `stream` i `data`;
   - nedostaje `c`, `c` je `"abc"`, `c` je `"0"` → `None`;
   - simbol van allowlist-e → `None`;
   - kline sa `x: false` i sa `x: true`;
   - neispravan JSON string → `None`, bez izuzetka.

Primer ticker poruke za testove (skraćen oblik prave Binance poruke):

```json
{"stream": "btcusdt@ticker", "data": {"e": "24hrTicker", "E": 1760380800000, "s": "BTCUSDT", "p": "-512.10", "P": "-0.45", "c": "113250.55", "h": "114900.00", "l": "112600.10", "v": "18234.551"}}
```

**Fajlovi:** `app/schemas/market.py`, `app/services/exchange_client.py`, `tests/test_market_data.py`.

**Gotovo kada**

- [ ] Svi testovi u `test_market_data.py` prolaze bez internet konekcije.
- [ ] Nigde u parseru nema `float(`.
- [ ] Neispravna poruka nikad ne podiže izuzetak niti daje cenu 0.

**Zašto:** ovo je sloj „normalizacije” iz specifikacije. Ostatak aplikacije (alerti, frontend) zna samo za `MarketTicker` i `KlineUpdate`, pa promena berze kasnije menja samo ovaj fajl. `Decimal` je tačan za novac; `0.1 + 0.2` u `float`-u nije tačno `0.3`, što bi pokvarilo poređenje cena u alertima.

## Task 4 — Binance WebSocket klijent sa ponovnim povezivanjem

**Cilj:** klasa `BinanceStreamClient` drži jednu vezu ka Binance-u za sve parove, prosleđuje parsirane poruke dalje, i sama se ponovo povezuje sa rastućim čekanjem (1 s, 2 s, 4 s … najviše 60 s) kad veza pukne.

**Koraci**

1. U `Settings` (Task 2) dodaj `kline_intervals: list[str] = ["1m", "5m", "15m", "1h"]` i `receive_timeout_seconds: int = 30`.
2. U `exchange_client.py` napiši funkciju `build_stream_url(base_url, symbols, intervals)`. Ona pravi kombinovani URL, na primer `wss://data-stream.binance.vision/stream?streams=btcusdt@ticker/btcusdt@kline_1m/...`. Simboli moraju biti malim slovima. Za 5 parova i 4 intervala to je 25 tokova kroz jednu vezu.
3. Napiši čistu funkciju `compute_backoff(attempt, initial, maximum) -> float` koja vraća `min(initial * 2 ** attempt, maximum)`. Na to dodaj malo nasumičnog „jitter-a” (do 10%) u samom klijentu.
4. Napravi klasu `BinanceStreamClient`:
   - Konstruktor prima URL, callback `on_ticker`, callback `on_kline` i opcioni `connect_factory` (podrazumevano `websockets.asyncio.client.connect`). Fabrika postoji da bi testovi mogli da ubace lažnu vezu.
   - Polje `status` sa vrednostima `connecting`, `connected`, `reconnecting`, `stopped`, i `last_message_at`.
   - Metoda `async def run()`: petlja dok nije zaustavljena → poveži se → čitaj poruke sa `asyncio.wait_for(ws.recv(), timeout=receive_timeout_seconds)` → parsiraj → pozovi callback. Kad veza pukne ili istekne vreme, zapiši grešku u log, sačekaj `compute_backoff` i pokušaj ponovo. Brojač pokušaja vrati na 0 posle uspešnog povezivanja.
   - Metoda `async def stop()` postavlja `asyncio.Event` i zatvara vezu.
   - `asyncio.CancelledError` uvek prosledi dalje (`raise`), nikad ga ne „gutaj”.
5. Ping/pong ne pišeš sam: biblioteka `websockets` automatski odgovara na Binance ping (Binance šalje ping na 20 s i prekida vezu bez odgovora u roku od minut). Binance takođe prekida svaku vezu posle 24 sata; tvoja petlja to rešava kao svaki drugi prekid.
6. Dodaj privremeni blok na dnu fajla da vidiš prave cene:

```python
if __name__ == "__main__":
    # pokreni: python -m app.services.exchange_client
    # ispisuje BTC i ETH cene 20 sekundi, pa staje
    ...
```

7. U `tests/test_market_data.py` (ili novom `tests/test_exchange_client.py`) dodaj testove:
   - `build_stream_url` pravi tačan URL;
   - `compute_backoff` daje 1, 2, 4, 8 … i nikad više od 60;
   - lažna veza pošalje 2 poruke pa „pukne”, a klijent pozove callback 2 puta i pokuša ponovo da se poveže (zameni `asyncio.sleep` sa `monkeypatch` da test ne čeka);
   - `stop()` zaustavlja petlju.

**Fajlovi:** `app/config.py`, `app/services/exchange_client.py`, testovi.

**Gotovo kada**

- [ ] `python -m app.services.exchange_client` ispisuje prave BTC/USDT i ETH/USDT cene.
- [ ] Kad isključiš Wi-Fi, log pokazuje pokušaje sa rastućim pauzama; kad ga uključiš, cene se vraćaju same.
- [ ] Testovi prolaze bez interneta.

**Zašto:** jedna deljena veza ka berzi umesto jedne po korisniku je zahtev iz specifikacije i poštuje Binance limite. Exponential backoff sprečava da aplikacija „bombarduje” server dok je mreža pala.

## Task 5 — MarketDataService: stanje u memoriji

**Cilj:** `MarketDataService` čuva najnoviju cenu za svaki par, zna da li je podatak svež ili zastareo, obaveštava ostale delove aplikacije o promenama, i pokreće se i gasi zajedno sa FastAPI aplikacijom.

**Koraci**

1. U `app/services/market_data_service.py` napravi klasu `MarketDataService` koja u konstruktoru prima `Settings` i pravi `BinanceStreamClient` iz Taska 4.
2. Stanje u memoriji:
   - `self._tickers: dict[str, MarketTicker]` — poslednji ticker po simbolu;
   - `self._klines: dict[tuple[str, str], KlineUpdate]` — poslednja sveća po (simbol, interval);
   - `self._last_update_at: dict[str, datetime]` — kada je stigla poslednja poruka.
3. `handle_ticker(ticker)`: ako je `event_time` stariji od onog koji već imaš, ignoriši poruku. Ako su cena i 24h podaci isti kao prethodni, samo osveži `last_update_at` i ne obaveštavaj nikoga. U suprotnom sačuvaj i obavesti pretplatnike.
4. Pretplatnici: metoda `subscribe(callback)`, gde je callback `async` funkcija koja prima događaj (`{"type": "ticker", ...}` ili `{"type": "kline", ...}`). Ako jedan pretplatnik baci izuzetak, zapiši ga u log i nastavi sa ostalima. Ovde će se kasnije prijaviti browser broadcast (Task 7), čuvanje sveća (Task 10) i alerti (Task 13).
5. Svežina: metoda `freshness(symbol, now=None)` vraća `"live"`, `"stale"` ili `"no_data"`. Podatak je `stale` ako je stariji od `stale_after_seconds` ili ako klijent nije `connected`. Parametar `now` postoji zbog testova.
6. `snapshot()` vraća listu svih parova iz allowlist-e, sa poslednjim tickerom (ili `None`) i svežinom, plus `connection_status` klijenta.
7. `start()` pokreće `client.run()` kao `asyncio.create_task`; `stop()` zove `client.stop()` i čeka da se task završi.
8. U `app/main.py` dodaj `lifespan` funkciju:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    service = MarketDataService(get_settings())
    app.state.market_service = service
    await service.start()
    yield
    await service.stop()

app = FastAPI(title="ChangeWatch Crypto", lifespan=lifespan)
```

9. Testovi u `tests/test_market_data.py`: noviji ticker zamenjuje stari, stariji se ignoriše, isti podaci ne obaveštavaju pretplatnike, `freshness` daje `stale` posle praga i `no_data` bez podataka, pretplatnik koji baca grešku ne kvari ostale. Testovi pozivaju `handle_ticker` direktno, bez mreže.

**Fajlovi:** `app/services/market_data_service.py`, `app/main.py`, testovi.

**Gotovo kada**

- [ ] `Run server` pokreće i Binance vezu; u logu vidiš „connected”.
- [ ] `Ctrl+C` gasi server čisto, bez grešaka „Task was destroyed but it is pending”.
- [ ] Testovi za stanje i svežinu prolaze.

**Zašto:** ovo je centralno mesto „istine” iz koraka 4 data flow-a u specifikaciji. Kraj Faze 1: aplikacija prima prave cene, sama se oporavlja i zna kad su podaci zastareli. Napravi commit i proveri sve pre nego što pređeš na dashboard.

## Task 6 — REST endpointi za tržišta

**Cilj:** `GET /api/markets` i `GET /api/markets/{symbol}` vraćaju prave trenutne podatke iz `MarketDataService`, sa svežinom i statusom veze, a nepoznat simbol daje 404.

**Koraci**

1. U `app/schemas/market.py` dodaj izlazne modele:
   - `MarketView`: `symbol`, `display_symbol`, `last_price`, `price_change`, `price_change_percent`, `high_24h`, `low_24h`, `volume_24h` (sve može biti `null` dok nema podataka), `updated_at` i `freshness`.
   - `MarketsResponse`: `connection_status` i `markets: list[MarketView]`.
   - Pydantic u JSON-u šalje `Decimal` kao string (`"113250.55"`), pa nema gubitka preciznosti.
2. Napravi dependency `get_market_service(request: Request)` koja vraća `request.app.state.market_service`. Stavi je u `app/api/dependencies.py` (novi fajl).
3. Napravi funkciju `normalize_symbol(raw, allowed)`: prihvata `btcusdt`, `BTC-USDT` i `BTC/USDT`, vraća `BTCUSDT`, a za simbol van allowlist-e baca `HTTPException(404)`.
4. U `app/api/routes_market.py` napravi `APIRouter(prefix="/api/markets", tags=["markets"])` sa dve rute iz specifikacije. Ako za par još nema podataka, vrati ga sa `last_price: null` i `freshness: "no_data"`, nikad sa 0.
5. Uključi router u `main.py` sa `app.include_router(...)`.
6. Testovi u `tests/test_api_markets.py`: napravi lažni servis sa unapred upisanim tickerom i zameni pravi preko `app.dependency_overrides[get_market_service]`. Proveri listu, jedan par, mala slova i `BTC-USDT`, 404 za `DOGEUSDT`, i `null` cenu bez podataka.

**Fajlovi:** `app/schemas/market.py`, `app/api/dependencies.py`, `app/api/routes_market.py`, `app/main.py`, `tests/test_api_markets.py`.

**Gotovo kada**

- [ ] `http://127.0.0.1:8000/api/markets` prikazuje 5 parova sa pravim cenama.
- [ ] Cene u JSON-u su stringovi, a `freshness` je `live`.
- [ ] `/api/markets/DOGEUSDT` vraća 404.
- [ ] Testovi prolaze bez pokretanja prave Binance veze.

**Zašto:** dependency (`Depends`) omogućava da testovi zamene pravi servis lažnim. Tako testovi rade bez interneta, kako specifikacija traži.

## Task 7 — Browser WebSocket `/ws/market`

**Cilj:** browser se poveže na `ws://127.0.0.1:8000/ws/market`, odmah dobije trenutno stanje svih parova, a zatim svaku promenu cene, sveće i statusa veze, bez ponovnog učitavanja stranice.

**Koraci**

1. Dogovori format poruka ka browseru i zapiši ga kao komentar u `app/websocket/manager.py`:

```json
{"type": "snapshot", "data": {"connection_status": "connected", "markets": [ ... ]}}
{"type": "ticker",   "data": { ...MarketView... }}
{"type": "kline",    "data": { ...KlineUpdate... }}
{"type": "status",   "data": {"connection_status": "reconnecting"}}
```

2. U `manager.py` napravi `ConnectionManager`:
   - `connect(ws)` → `await ws.accept()` i dodaj u skup aktivnih veza;
   - `disconnect(ws)` → ukloni iz skupa;
   - `broadcast(message: dict)` → pošalji svakoj vezi sa `asyncio.wait_for(ws.send_json(message), timeout=5)`. Vezu koja baci grešku ili istekne ukloni. Spor browser tako ne blokira ostale.
3. U `BinanceStreamClient` (Task 4) dodaj callback `on_status_change(status)`. `MarketDataService` ga prosleđuje pretplatnicima kao događaj `status`.
4. U `app/websocket/routes.py` napravi rutu:
   - `@router.websocket("/ws/market")` → `manager.connect`, pošalji `snapshot`, pa u petlji `await websocket.receive_text()` (poruke od browsera ignoriši).
   - Na `WebSocketDisconnect` pozovi `manager.disconnect`.
   - Servis i manager dobijaš kroz dependency, kao u Tasku 6.
5. U `lifespan` napravi jedan `ConnectionManager`, sačuvaj ga u `app.state`, i prijavi `manager.broadcast` kao pretplatnika na `MarketDataService`. Podatke pretvaraj sa `model_dump(mode="json")`.
6. Testovi u `tests/test_websocket.py`:
   - `TestClient(app).websocket_connect("/ws/market")` sa lažnim servisom prima `snapshot` kao prvu poruku;
   - `broadcast` stiže do dve lažne veze;
   - veza koja baca grešku pri slanju se uklanja, a ostale i dalje primaju poruke.

**Fajlovi:** `app/websocket/manager.py`, `app/websocket/routes.py`, `app/services/exchange_client.py`, `app/services/market_data_service.py`, `app/main.py`, `tests/test_websocket.py`.

**Gotovo kada**

- [ ] U Chrome konzoli (`View → Developer → JavaScript Console`) ovaj kod ispisuje poruke svake sekunde:

```javascript
const ws = new WebSocket("ws://127.0.0.1:8000/ws/market");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

- [ ] Zatvaranje taba ne izaziva grešku u logu servera.
- [ ] Testovi prolaze.

**Zašto:** Binance šalje podatke serveru, a server ih deli svim browserima kroz jednu svoju vezu. Bez toga bi svaki posetilac otvarao sopstvenu vezu ka berzi.

## Task 8 — Dashboard UI

**Cilj:** na `http://127.0.0.1:8000/` stoji tamni, responzivni dashboard sa pravim cenama koje se same osvežavaju, sa pretragom, zelenim i crvenim promenama, i jasnim upozorenjem kad podaci nisu aktuelni.

**Koraci**

1. U `main.py` montiraj statičke fajlove (`app.mount("/static", StaticFiles(directory="app/static"), name="static")`) i podesi `Jinja2Templates(directory="app/templates")`. Ruta `GET /` vraća `index.html` i prosleđuje listu podržanih parova.
2. U `app/templates/index.html` napravi raspored:
   - gornja traka: naziv aplikacije, indikator veze (zelena tačka „Uživo”, žuta „Povezivanje…”, crvena „Prekinuto”) i „Poslednje ažuriranje: 20:41:07”;
   - pregled: 5 kartica sa cenom i 24h promenom;
   - tabela tržišta: Par, Cena, 24h promena, 24h %, 24h max, 24h min, Volumen, Status;
   - polje za pretragu iznad tabele;
   - prazni paneli „Grafikon”, „Watchlist” i „Alerti” koje popunjavaš u Taskovima 11–13.
   - Na dno ubaci HTMX 2.x preko CDN-a (link sa htmx.org) i `<script src="/static/js/app.js">`.
3. U `app/static/css/style.css` napravi tamnu temu preko CSS promenljivih (`--bg`, `--panel`, `--text`, `--muted`, `--green`, `--red`, `--yellow`). Koristi CSS grid koji se na užem ekranu slaže u jednu kolonu, a tabelu stavi u kontejner sa horizontalnim skrolom na telefonu.
4. U `app/static/js/app.js` napiši:
   - `connect()`: otvara WebSocket na `/ws/market` (adresu gradi iz `location.host`, sa `wss:` kada je stranica na `https:`), a kad se zatvori pokušava ponovo posle 1, 2, 4 … najviše 30 s;
   - obradu poruka `snapshot`, `ticker`, `kline` i `status` (`kline` za sada ignoriši);
   - `renderRow(market)` koji ažurira samo jedan red, ne celu tabelu;
   - zelenu klasu za pozitivnu i crvenu za negativnu promenu, i kratko (600 ms) blago isticanje ćelije kad se cena promeni;
   - filter: pri kucanju u pretragu sakrij redove koji ne sadrže tekst;
   - tajmer na 1 s koji red bez ažuriranja duže od 15 s posivi i obeleži „Zastarelo”.
5. Stanja koja moraju biti vidljiva:
   - pre prvog `snapshot`-a: „Učitavanje cena…”;
   - par bez podataka: „—”, nikad 0;
   - prekinuta veza sa serverom: crveni baner „Veza sa serverom je prekinuta — prikazane cene nisu aktuelne”;
   - status berze `reconnecting`: žuti baner „Veza sa berzom se obnavlja”.
6. Cene stižu kao stringovi. `Number()` koristi samo za prikaz preko `Intl.NumberFormat`, nikad za poređenja.

**Fajlovi:** `app/main.py`, `app/templates/index.html`, `app/static/css/style.css`, `app/static/js/app.js`.

**Gotovo kada**

- [ ] Cene se menjaju bez osvežavanja stranice.
- [ ] Pretraga „sol” ostavlja samo SOL/USDT.
- [ ] Kad zaustaviš server, za par sekundi se pojavi crveni baner; kad ga ponovo pokreneš, dashboard se sam vrati.
- [ ] Kad isključiš Wi-Fi, redovi posle 15 s postaju „Zastarelo”.
- [ ] Stranica izgleda uredno i na širini telefona (u Chrome-u: DevTools → ikonica telefona).

**Zašto:** ovo je kraj Faze 2. Najvažnije pravilo iz specifikacije je da UI nikad ne prikazuje zastarelu cenu kao aktuelnu, zato ima i serverski status i sopstveni tajmer u browseru.

## Task 9 — Baza: SQLAlchemy, SQLite i model sveće

**Cilj:** aplikacija pri pokretanju pravi SQLite bazu u `data/changewatch.db` sa tabelom `candles`. Cene se čuvaju bez gubitka preciznosti, a ista sveća ne može da se upiše dva puta.

**Koraci**

1. U `app/database.py` podesi asinhroni SQLAlchemy:
   - `class Base(DeclarativeBase)`;
   - `engine = create_async_engine(settings.database_url)`;
   - `SessionLocal = async_sessionmaker(engine, expire_on_commit=False)`;
   - `async def init_db()` koja napravi folder `data/` ako ne postoji i pozove `Base.metadata.create_all` preko `conn.run_sync`;
   - dependency `async def get_session()` koja daje jednu sesiju po zahtevu.
2. SQLite nema pravi decimalni tip, pa u `database.py` napravi tip koji `Decimal` čuva kao tekst:

```python
class DecimalString(TypeDecorator):
    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)
```

3. U `app/models/candle.py` napravi model `Candle` (SQLAlchemy 2.x stil, `Mapped[...]` i `mapped_column`):
   - `id`, `symbol` (`String(20)`, indeks), `interval` (`String(5)`);
   - `open_time` i `close_time` kao `BigInteger` u milisekundama, isto kao Binance;
   - `open_price`, `high_price`, `low_price`, `close_price`, `volume` kao `DecimalString`;
   - `UniqueConstraint("symbol", "interval", "open_time")`.
4. U `app/models/__init__.py` uvezi sve modele, da `create_all` zna za njih.
5. U `lifespan` pozovi `await init_db()` pre pokretanja `MarketDataService`.
6. Napravi `tests/conftest.py` sa fixture-om za bazu u memoriji: `create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)`, `create_all` i sesija. Testovi tako nikad ne diraju pravu bazu.
7. Testovi u `tests/test_candles.py`: upis i čitanje sveće vraća iste `Decimal` vrednosti (npr. `Decimal("113250.55000000")`), a drugi upis iste sveće baca `IntegrityError`.

**Fajlovi:** `app/database.py`, `app/models/candle.py`, `app/models/__init__.py`, `app/main.py`, `tests/conftest.py`, `tests/test_candles.py`.

**Gotovo kada**

- [ ] Posle pokretanja postoji `data/changewatch.db`. U PyCharm-u ga otvori kroz **Database** panel (Professional) ili DB Browser for SQLite i vidi tabelu `candles`.
- [ ] Testovi prolaze, a `data/changewatch.db` nije u git-u.

**Zašto:** Alembic migracije za sada ne trebaju; `create_all` je dovoljan dok se šema ne iskomplikuje, kako specifikacija i kaže. Vremena u milisekundama izbegavaju probleme sa vremenskim zonama u SQLite-u.

## Task 10 — Istorijske sveće sa REST API-ja i čuvanje 1m sveća

**Cilj:** `GET /api/markets/{symbol}/candles?interval=1m&limit=300` vraća prave istorijske sveće sa Binance-a. Zatvorene 1-minutne sveće iz živog toka upisuju se u SQLite, a kad Binance REST nije dostupan, 1m podaci dolaze iz baze.

**Koraci**

1. U `exchange_client.py` napravi `BinanceRestClient` sa `httpx.AsyncClient(base_url=settings.binance_rest_url, timeout=10)`. Metoda `get_klines(symbol, interval, limit)` zove `GET /api/v3/klines`. Binance vraća listu nizova; prvih sedam polja su: vreme otvaranja, open, high, low, close, volumen, vreme zatvaranja. Pretvori ih u `KlineUpdate` (`is_closed` je `True` za sve osim poslednje).
2. Greške: na mrežnu grešku ili status 5xx baci sopstveni `ExchangeUnavailableError`. Status 429 ili 418 znači da su Binance limiti premašeni; zapiši upozorenje i ne pokušavaj ponovo odmah.
3. U `app/services/candle_service.py` napravi `CandleService`:
   - `upsert_candles(candles)` koristi `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_update(...)` nad jedinstvenim ključem iz Taska 9;
   - `handle_event(event)` je pretplatnik na `MarketDataService`: kad stigne `kline` sa intervalom `1m` i `is_closed=True`, upiše ga. Nezatvorene sveće se ne upisuju;
   - `get_candles(symbol, interval, limit)` prvo pita Binance REST; za `1m` sačuva rezultat u bazu, i vrati sveće sortirane po vremenu. Ako REST baci `ExchangeUnavailableError`, za `1m` vrati sveće iz baze, a za ostale intervale vrati grešku 503;
   - kratak keš u memoriji (15 s po paru i intervalu) da osvežavanje stranice ne troši Binance limite.
4. Dodaj rutu u `routes_market.py`: `interval` mora biti iz `settings.kline_intervals` (inače 422), a `limit` između 1 i 1000 (podrazumevano 300). Izlaz je lista `{"time": <sekunde>, "open": "...", "high": "...", "low": "...", "close": "...", "volume": "..."}`. Vreme je u sekundama jer ga Lightweight Charts tako očekuje.
5. U `lifespan` napravi `CandleService` i prijavi `handle_event` kao pretplatnika.
6. Testovi u `tests/test_candles.py`:
   - lažni Binance odgovor preko `httpx.MockTransport` se ispravno parsira;
   - dvostruki `upsert` iste sveće ostavlja jedan red;
   - zatvorena 1m sveća se upisuje, nezatvorena ne;
   - kad `MockTransport` vrati 503, `1m` sveće dolaze iz baze;
   - nedozvoljen interval `3m` i `limit=5000` daju 422.

**Fajlovi:** `app/services/exchange_client.py`, `app/services/candle_service.py`, `app/api/routes_market.py`, `app/main.py`, `tests/test_candles.py`.

**Gotovo kada**

- [ ] `/api/markets/BTCUSDT/candles?interval=15m&limit=50` vraća 50 pravih sveća.
- [ ] Posle nekoliko minuta rada tabela `candles` dobija novi red svakog minuta po paru.
- [ ] Testovi prolaze bez interneta.

**Zašto:** specifikacija traži da se ne čuva svaki tick, već agregirane 1m sveće. Istorija dolazi sa berze, a baza je rezerva i lokalna kopija.

## Task 11 — Interaktivni grafikon

**Cilj:** klik na par u tabeli otvara grafikon sa istorijskim svećama za izabrani interval (1m, 5m, 15m, 1h). Poslednja sveća se menja uživo, bez dupliranih tačaka i bez pogrešnog redosleda.

**Koraci**

1. U `index.html` ubaci Lightweight Charts verziju 5.x kao samostalan skript:

```html
<script src="https://unpkg.com/lightweight-charts@5.2/dist/lightweight-charts.standalone.production.js"></script>
```

2. U panel „Grafikon” dodaj naziv izabranog para, dugmad `1m` `5m` `15m` `1h`, prekidač „Sveće / Linija” i `<div id="chart">` sa fiksnom visinom (npr. 420px).
3. U novom fajlu `app/static/js/chart.js` (učitaj ga pre `app.js`) napravi grafikon. U verziji 5 serija se pravi preko `addSeries`, ne starim `addCandlestickSeries`:

```javascript
const { createChart, CandlestickSeries, LineSeries } = LightweightCharts;
const chart = createChart(document.getElementById("chart"), {
  autoSize: true,
  layout: { background: { type: "solid", color: "#0f1420" }, textColor: "#c9d1e0" },
  timeScale: { timeVisible: true, secondsVisible: false },
});
let series = chart.addSeries(CandlestickSeries);
```

4. `loadHistory(symbol, interval)`:
   - pozove `/api/markets/{symbol}/candles?interval=...&limit=300`;
   - pretvori stringove u brojeve (`Number(c.open)` itd.), samo za crtanje;
   - pozove `series.setData(bars)` i `chart.timeScale().fitContent()`;
   - zapamti `lastBarTime` = vreme poslednje sveće.
5. Spajanje istorije i živih podataka (najvažniji deo taska):
   - svaki poziv `loadHistory` dobija redni broj (`loadId`); ako stigne odgovor starog poziva jer je korisnik u međuvremenu promenio par, ignoriši ga;
   - `kline` poruke za aktivni par i interval koje stignu dok se istorija učitava stavi u bafer, pa ih primeni posle `setData`;
   - `series.update(bar)` pozivaj samo ako je `bar.time >= lastBarTime`; isto vreme menja poslednju sveću, novije dodaje novu, a starije se preskače, jer bi biblioteka inače bacila grešku;
   - poruke za drugi par ili interval ignoriši.
6. Prekidač „Linija” uklanja seriju (`chart.removeSeries`), dodaje `LineSeries` sa `{ time, value: close }` i ponovo učitava istoriju.
7. Binance vremena su u UTC. Za prikaz u lokalnom vremenu pogledaj stranicu „Time zones” u dokumentaciji Lightweight Charts, ili ostavi UTC i napiši „UTC” ispod grafikona.
8. Licenca Lightweight Charts traži navođenje TradingView-a kao autora: dodaj u footer link na tradingview.com i tekst iz njihovog `NOTICE` fajla.
9. Kad je status veze `stale` ili prekinut, preko grafikona prikaži natpis „Podaci nisu aktuelni”.

**Fajlovi:** `app/templates/index.html`, `app/static/js/chart.js`, `app/static/js/app.js`, `app/static/css/style.css`.

**Gotovo kada**

- [ ] Klik na ETH/USDT u tabeli prikazuje ETH grafikon.
- [ ] Promena intervala menja sveće, a brzo kliktanje po parovima nikad ne prikaže pogrešan par.
- [ ] Poslednja sveća se pomera uživo, a kad minut istekne pojavljuje se nova, bez rupa i duplikata.
- [ ] U konzoli nema grešaka tipa „Cannot update oldest data”.
- [ ] U footeru postoji TradingView atribucija.

**Zašto:** kraj Faze 3. Specifikacija posebno ističe ovaj deo: istorija i živi tok dolaze iz dva izvora i u različito vreme, pa redosled i duplikate moraš rešiti namerno.

## Task 12 — Watchlist

**Cilj:** korisnik zvezdicom dodaje i uklanja parove iz watchlist-e. Izabrani parovi se ističu na dashboardu, ostaju posle restarta aplikacije, i isti par ne može biti dodat dvaput.

**Koraci**

1. `app/models/watchlist.py`: model `WatchlistItem` sa poljima `id`, `symbol` (`String(20)`, `unique=True`) i `created_at` (podrazumevano trenutno UTC vreme).
2. `app/schemas/watchlist.py`: `WatchlistCreate` sa poljem `symbol` i validatorom koji koristi `normalize_symbol` iz Taska 6 (simbol van allowlist-e daje 422), plus `WatchlistItemOut`.
3. `app/api/routes_watchlist.py`:
   - `GET /api/watchlist` vraća listu po redosledu dodavanja;
   - `POST /api/watchlist` vraća 201, a za par koji je već u listi 409 sa porukom „Par je već u watchlist-i”;
   - `DELETE /api/watchlist/{symbol}` vraća 204, a 404 ako para nema u listi.
   - Za duplikat se osloni na `unique` ograničenje u bazi i uhvati `IntegrityError`, ne samo na proveru u kodu.
4. UI (`app.js`):
   - u svakom redu tabele dugme ☆/★ koje zove `POST` ili `DELETE` preko `fetch`;
   - pri učitavanju stranice pozovi `GET /api/watchlist` i označi zvezdice;
   - panel „Watchlist” prikazuje izabrane parove sa živom cenom i promenom; ako je prazan, piše „Dodaj par klikom na ☆”;
   - prekidač „Samo watchlist” iznad tabele.
5. Testovi u `tests/test_watchlist.py` (baza u memoriji iz Taska 9 preko `dependency_overrides[get_session]`): dodavanje, duplikat → 409, `DOGEUSDT` → 422, brisanje, brisanje nepostojećeg → 404, i da nova sesija vidi sačuvan par.

**Fajlovi:** `app/models/watchlist.py`, `app/models/__init__.py`, `app/schemas/watchlist.py`, `app/api/routes_watchlist.py`, `app/main.py`, `app/static/js/app.js`, `tests/test_watchlist.py`.

**Gotovo kada**

- [ ] Dodaš BTC i SOL, restartuješ server, osvežiš stranicu, a zvezdice su i dalje tu.
- [ ] Dupli klik na ☆ ne pravi dva reda u bazi.
- [ ] Testovi prolaze.

**Zašto:** pošto MVP nema prijavljivanje, postoji jedna zajednička watchlist-a. Ograničenje `unique` u bazi je poslednja linija odbrane od duplikata, čak i kad dva zahteva stignu u isto vreme.

## Task 13 — Price alerts

**Cilj:** korisnik pravi alert tipa „BTC/USDT iznad 100.000”. Backend ga proverava na svakoj validnoj ceni, okida ga tačno jednom, gasi ga, i odmah ga prikazuje na dashboardu.

**Koraci**

1. `app/models/alert.py`: model `PriceAlert` sa poljima `id`, `symbol`, `target_price` (`DecimalString`), `condition` (`"above"` ili `"below"`), `is_active` (podrazumevano `True`), `created_at`, `triggered_at` (može `None`) i `triggered_price` (može `None`).
2. `app/schemas/alert.py`:
   - `AlertCreate`: `symbol` (allowlist), `target_price: Decimal` veći od 0 i najviše 8 decimala, `condition: Literal["above", "below"]`;
   - `AlertUpdate`: opciono `is_active` i `target_price`;
   - `AlertOut`.
3. `app/services/alert_service.py`, klasa `AlertService`:
   - pri pokretanju učita aktivne alerte iz baze u memoriju, grupisane po simbolu, da ne čita bazu na svaki tick;
   - posle svakog kreiranja, izmene ili brisanja osveži memoriju;
   - `handle_event(event)` reaguje samo na `ticker` događaje sa svežim podacima; zastarela ili nepostojeća cena nikad ne okida alert;
   - uslov: `above` znači `cena >= cilj`, `below` znači `cena <= cilj`, poređenje u `Decimal`;
   - okidanje tačno jednom: prvo ukloni alert iz memorije, pa uradi atomski upit `UPDATE price_alerts SET is_active = false, triggered_at = ..., triggered_price = ... WHERE id = :id AND is_active = true`. Ako je pogođen 0 redova, neko ga je već okinuo, pa ne radi ništa;
   - posle okidanja pošalji browserima `{"type": "alert_triggered", "data": {...}}` preko `ConnectionManager`-a.
4. `app/api/routes_alerts.py`: `GET /api/alerts` (opciono `?active=true`), `POST` (201), `PATCH /api/alerts/{id}` (ponovno uključivanje briše `triggered_at`), `DELETE` (204). Za nepostojeći id vrati 404.
5. UI preko HTMX-a, jer je forma ovde prirodna:
   - napravi `app/templates/partials/alerts_list.html` sa listom alerta;
   - dodaj rute `GET /partials/alerts` i `POST /partials/alerts` koje vraćaju taj HTML fragment i koriste isti `AlertService`;
   - forma: par, uslov (iznad/ispod), cena, sa `hx-post="/partials/alerts"` i `hx-target="#alerts-list"`;
   - na poruku `alert_triggered` prikaži obaveštenje u uglu ekrana („BTC/USDT je prešao 100.000 USDT”) i premesti alert u sekciju „Okinuti”.
6. Testovi u `tests/test_alerts.py`:
   - `above` i `below` tačno na granici (`Decimal("100000")` naspram `Decimal("99999.99")`);
   - 100 brzih uzastopnih ticker-a iznad cilja → alert okinut tačno jednom, jedan `alert_triggered` događaj;
   - isključen alert se nikad ne okida;
   - zastareo ticker ne okida alert;
   - validacija: negativna cena, uslov `"sideways"` i `DOGEUSDT` → 422;
   - `PATCH` ponovo uključuje okinut alert.

**Fajlovi:** `app/models/alert.py`, `app/schemas/alert.py`, `app/services/alert_service.py`, `app/api/routes_alerts.py`, `app/templates/partials/alerts_list.html`, `app/templates/index.html`, `app/static/js/app.js`, `app/main.py`, `tests/test_alerts.py`.

**Gotovo kada**

- [ ] Alert „BTC/USDT iznad (trenutna cena − 1)” okine se za par sekundi i pojavi se obaveštenje.
- [ ] Posle okidanja alert je neaktivan i ne okida se ponovo, čak ni posle restarta.
- [ ] Testovi prolaze.

**Zašto:** kraj Faze 4. Alert napravljen kad je uslov već ispunjen okida se na prvom sledećem ticku; to je očekivano ponašanje, pa ga zapiši u README. Atomski `UPDATE ... WHERE is_active = true` garantuje „samo jednom” i kad dve poruke stignu skoro istovremeno.

## Task 14 — Bezbednost i pouzdanost

**Cilj:** aplikacija ograničava broj zahteva, nikad ne pada zbog greške berze, ne otkriva interne detalje u greškama, i prolazi kroz celu listu iz odeljka 10 specifikacije.

**Koraci**

1. Ograničenje zahteva bez nove biblioteke: u `app/api/rate_limit.py` napravi dependency `RateLimiter(max_requests, window_seconds)` koja po IP adresi pamti vremena zahteva u memoriji i vraća 429 sa zaglavljem `Retry-After`. Primeni ga na `POST`, `PATCH` i `DELETE` rute (npr. 30 zahteva u minuti).
2. Dodaj ograničenje od najviše 50 aktivnih alerta; preko toga `POST /api/alerts` vraća 400 sa jasnom porukom.
3. Globalni handleri grešaka u `main.py`:
   - `ExchangeUnavailableError` → 503 `{"detail": "Berza trenutno nije dostupna"}`;
   - svaki neočekivani izuzetak → 500 `{"detail": "Interna greška"}`, a ceo stack trace ide samo u log.
4. Browser WebSocket: najviše 100 istovremenih veza (preko toga zatvori vezu kodom 1013), i ignoriši poruke od browsera duže od 1 KB.
5. Proširi `GET /health`: `status`, `exchange_connection` i `seconds_since_last_message`. Platforma za deploy u Tasku 17 koristi ovo za proveru zdravlja.
6. Prođi kroz listu i štikliraj:
   - [ ] nijedna ruta ne prihvata ime Binance toka od korisnika; simboli uvek prolaze kroz `normalize_symbol`;
   - [ ] nijedna cena nije `float`, a nijedna nedostajuća cena nije 0;
   - [ ] log na nivou INFO ne ispisuje cele sirove poruke (samo DEBUG);
   - [ ] u kodu nema API ključeva ni naloga;
   - [ ] pad Binance-a (isključen Wi-Fi) ne ruši server.
7. Testovi u `tests/test_security.py`: 31. `POST` u minuti dobija 429; 51. aktivni alert dobija 400; lažni `ExchangeUnavailableError` na `/candles` daje 503 bez stack trace-a; `/health` vraća nova polja.

**Fajlovi:** `app/api/rate_limit.py`, `app/api/routes_*.py`, `app/websocket/manager.py`, `app/main.py`, `tests/test_security.py`.

**Gotovo kada**

- [ ] Lista iz koraka 6 je kompletno štiklirana.
- [ ] Testovi prolaze.

**Zašto:** javni demo može da poseti bilo ko, uključujući botove. Ograničenje u memoriji je dovoljno za jedan proces; za više instanci trebao bi Redis, što nije deo MVP-a.

## Task 15 — Kompletiranje testova, Ruff i GitHub Actions

**Cilj:** svaka stavka iz odeljka 11 specifikacije ima bar jedan test, a GitHub na svaki push i pull request pokreće Ruff i pytest i prikazuje zelenu ili crvenu oznaku.

**Koraci**

1. Proveri da li za svaki zahtev iz specifikacije postoji test, i dopiši one koji nedostaju:

| Zahtev iz specifikacije | Gde je test |
| --- | --- |
| Parsiranje Binance poruka | `test_market_data.py` (Task 3) |
| Normalizacija simbola i cena | `test_market_data.py`, `test_api_markets.py` |
| Nedostajući ili neispravni podaci | `test_market_data.py` |
| Uslovi alerta | `test_alerts.py` (Task 13) |
| Alert se ne okida dvaput | `test_alerts.py` |
| Ponovno povezivanje posle pada veze | `test_exchange_client.py` (Task 4) |
| Slanje podataka browserima | `test_websocket.py` (Task 7) |
| Pravljenje i čitanje sveća | `test_candles.py` (Taskovi 9–10) |
| Validacija API-ja i greške | `test_api_markets.py`, `test_watchlist.py`, `test_security.py` |

2. Opciono: dodaj `pytest-cov` u `requirements-dev.txt` i pokreni `pytest --cov=app`. Cilj je oko 80% pokrivenosti za `services/`.
3. Pokreni `ruff check . --fix` i `ruff format .`, i ispravi sve što ostane.
4. Napravi `.github/workflows/tests.yml`:

```yaml
name: tests

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: ruff check .
      - run: ruff format --check .
      - run: pytest -q
```

5. Poveži projekat sa GitHub-om ako još nije (u PyCharm-u: **Git → GitHub → Share Project on GitHub**), napravi push, i otvori karticu **Actions** na GitHub-u.
6. U README dodaj oznaku statusa testova (badge) sa linkom na workflow.

**Fajlovi:** testovi, `.github/workflows/tests.yml`, `README.md`.

**Gotovo kada**

- [ ] Svaki red tabele iz koraka 1 ima test koji prolazi.
- [ ] Workflow na GitHub-u je zelen.
- [ ] Testovi prolaze i sa isključenim Wi-Fi-jem.

**Zašto:** zelena oznaka na GitHub-u je jedna od prvih stvari koje poslodavac vidi u portfolio projektu. Workflow takođe garantuje da kod radi i van tvog računara.

## Task 16 — Docker i Docker Compose

**Cilj:** `docker compose up --build` pokreće celu aplikaciju na `http://localhost:8000`, a baza ostaje sačuvana posle gašenja i ponovnog pokretanja kontejnera.

**Koraci**

1. Instaliraj Docker Desktop za Mac (ili OrbStack, koji je lakši) i proveri: `docker --version`.
2. Napravi `.dockerignore`:

```text
.venv/
.git/
.idea/
data/
tests/
__pycache__/
.env
```

3. Napravi `Dockerfile`:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --create-home appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /app/data && chown -R appuser /app/data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```

4. Napravi `compose.yaml`:

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - changewatch-data:/app/data
    restart: unless-stopped

volumes:
  changewatch-data:
```

5. Pokreni `docker compose up --build`, dodaj par u watchlist, pa uradi `docker compose down` i ponovo `up`. Watchlist mora i dalje postojati.
6. Važno: nikad ne pokreći više uvicorn workera (`--workers 2` i slično). Stanje cena i Binance veza žive u memoriji jednog procesa; više workera bi otvorilo više veza i delilo korisnike na različita stanja.

**Fajlovi:** `.dockerignore`, `Dockerfile`, `compose.yaml`.

**Gotovo kada**

- [ ] Dashboard radi iz Docker-a sa pravim cenama.
- [ ] Watchlist i alerti preživljavaju `down` i `up`.
- [ ] Kontejner ne radi kao root (`docker compose exec app whoami` ispisuje `appuser`).

**Zašto:** Docker je isti na tvom Mac-u i na serveru, pa „kod mene radi” prestaje da bude problem. Volume `changewatch-data` je „trajni disk” za bazu iz specifikacije.

## Task 17 — README i javni deploy

**Cilj:** repozitorijum ima README sa kojim neko ko ne zna projekat može da ga pokrene za 5 minuta, a aplikacija radi javno na platformi koja drži stalni proces, WebSocket veze i trajni disk.

**Koraci**

1. Napravi screenshot-ove dashboarda, grafikona i alerta (`Cmd+Shift+4`) i sačuvaj ih u `docs/screenshots/`.
2. Napiši `README.md` sa sledećim delovima:
   - naziv, jedna rečenica opisa, badge testova, link na javni demo i glavni screenshot;
   - mogućnosti (live cene, grafikoni, watchlist, alerti);
   - tehnologije;
   - pokretanje lokalno (macOS/Linux): `git clone`, `python3 -m venv .venv`, `source .venv/bin/activate`, `pip install -r requirements.txt -r requirements-dev.txt`, `cp .env.example .env`, `uvicorn app.main:app --reload`;
   - pokretanje preko Docker-a: `docker compose up --build`;
   - pokretanje testova: `pytest` i `ruff check .`;
   - arhitektura: kratak opis toka Binance → `exchange_client` → `MarketDataService` → alerti, sveće i browseri, i spisak modula;
   - ograničenja: jedan korisnik, jedan proces, rate limit u memoriji, alerti samo u UI-ju, samo Binance, alert sa već ispunjenim uslovom se okida odmah;
   - napomena da je aplikacija samo informativna i da ne izvršava trgovanje niti daje finansijske savete;
   - licenca (MIT) i TradingView atribucija za Lightweight Charts.
3. Izaberi platformu za deploy. Uslovi iz specifikacije: proces koji stalno radi, WebSocket podrška i trajni disk za SQLite. Serverless i besplatni planovi koji „uspavaju” aplikaciju posle neaktivnosti ne odgovaraju.
   - Dobre opcije: Fly.io ili Railway (Docker i volume), ili mali VPS (npr. Hetzner) sa `docker compose`. Cene i besplatne planove proveri na njihovim sajtovima u trenutku deploy-a.
4. Region servera izaberi u Evropi (npr. Frankfurt ili Amsterdam). Binance ograničava pristup sa adresa iz nekih zemalja, posebno iz SAD-a. Ako u logovima vidiš HTTP 451 ili se veza odmah prekida, uzrok je skoro sigurno region servera.
5. Na platformi podesi environment varijable iz `.env.example`, poveži volume na `/app/data`, health check na `/health`, i proveri da je aplikacija dostupna preko `https://` (browser će tada sam koristiti `wss://`, zahvaljujući Tasku 8).
6. Završna provera po „Definition of Done” iz odeljka 14 specifikacije.

**Fajlovi:** `README.md`, `docs/screenshots/*`, i konfiguracija platforme (npr. `fly.toml`) ako je platforma traži.

**Gotovo kada**

- [ ] Javni link prikazuje prave cene koje se menjaju uživo.
- [ ] Posle redeploy-a watchlist i alerti su i dalje tu.
- [ ] Prijatelj može da pokrene projekat lokalno samo uz README.
- [ ] Sve stavke iz „Definition of Done” su ispunjene.

**Zašto:** kraj Faze 5 i MVP-a. Za portfolio, javni link i dobar README često vrede koliko i sam kod.

## Izvori

- [Binance — WebSocket Market Streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams): adrese, kombinovani tokovi, polja `24hrTicker` i `kline` poruka, ping i 24h limit veze.
- [Lightweight Charts — Getting started (v5.2)](https://tradingview.github.io/lightweight-charts/docs): `addSeries`, `setData`, `update` i uslovi atribucije.
