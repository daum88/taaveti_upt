# 📈 Taaveti UPT — tehisaru aktsiaportfelli simulaator

Taaveti UPT on lõputöö jaoks loodud **paberportfelli simulatsioon**. Programmis võistlevad inimese juhitud konto, mitu eri investeerimisstiiliga tehisaruagenti ning passiivne võrdlusportfell. Kõik alustavad 10 000 USA dollariga, kuid raha ei investeerita päriselt: tehingud kantakse kohalikku SQLite-andmebaasi ning portfellide väärtust hinnatakse turuandmetega.

Rakendusel on FastAPI veebiliides, reaalajas uuenev edetabel ja Richi terminalivaade. See ei ole maaklerteenus ega investeerimissoovitus.

## Mida programm teeb

1. Hoiab jälgimisnimekirjas umbes 500 S&P 500 aktsiat ning soovi korral ETF-e.
2. Kogub Yahoo Finance'i (`yfinance`) kaudu hinnad, mahu, ajaloolise OHLCV-andmestiku ja uudised.
3. Filtreerib välja instrumendid, mille hinnaliikumine ületab seadistatud läve.
4. Annab valitud instrumentide, uudiste, SPY turukonteksti ja iga agendi portfelli tehisarule otsustamiseks.
5. Tehisaru teeb ühe otsuse: **osta**, **müü** või **hoia**. Tehing läbib alati keskse täitmismootori reeglid.
6. Arvutab kõigi kontode hetkeväärtuse, P&L-i ja järjestuse ning salvestab pärast tehingut või lõppenud tsüklit graafiku ajaloo.

Inimkasutaja saab veebiliidesest oma inimkontoga käsitsi kaubelda. Agendiga saab vestelda, lasta tal koostada algportfelli või küsida temalt põhjalikku portfellianalüüsi.

## Kuidas töövoog toimib

### 1. Andmete ettevalmistamine

Esmasel seadistamisel luuakse andmebaas, vaikekontod ja instrumentide universum. `--warmup` täidab vahemälu 14 päeva OHLCV-andmete ning kuni 48 tunni uudistega. Rakendus kasutab ühe protsessi SQLite-andmebaasi WAL-režiimis: lugejad saavad töötada koos, kirjutamine on tehinguline ja tehingu- ning positsioonikirjed jäävad auditijäljeks.

### 2. Sõelatsükkel

Taustatöö käivitub serveri käivitamisel ning seejärel vaikimisi iga kolme tunni järel. Tsükli võib veebist ka käsitsi käivitada.

- **Esimene läbimine:** korraga küsitakse aktiivsete instrumentide hinnad. Edasi pääseb instrument, mille absoluutne päevane muutus on suurem kui 1% (`VOLATILITY_THRESHOLD=0.01`).
- **Teine läbimine:** kandidaatidele küsitakse viimase kolme tunni uudised. Uudised salvestatakse ning kandidaat antakse agendile koos hinnaliikumise ja mahuga. Uudise puudumine ei välista instrumenti.
- **Otsustamine:** iga LLM-konto vaatab oma rahajääki, positsioone, viimaseid tehinguid, kandidaate ja SPY konteksti. Agent võib ühe tsükli jooksul esitada maksimaalselt ühe tavapärase ostu-, müügi- või hoidmisotsuse.
- **Erandid:** enne agendi otsust kontrollitakse automaatselt kõiki tema positsioone stop-loss'i ja take-profit'i suhtes. Sundmüük võib seega toimuda lisaks agendi tavapärasele otsusele.

Tsükkel töötab ka siis, kui turg on suletud; turu olek antakse agendile kontekstiks ning tehing märgitakse sellisel juhul vastavalt. Hinnad on andmeallika pakutavad turuhinnad, mitte garanteeritud täitmishinnad.

### 3. Tehisaru roll

LLM ei kirjuta andmebaasi otse ega otsusta lõplikku täitmist. Ta tagastab struktureeritud ettepaneku: sümbol, tegevus, osakaal ja põhjendus. Keskne täitmismootor valideerib selle ning võib ostusummat piirata või tehingu tagasi lükata.

Toetatud mudelipakkujad on DeepSeek (vaikimisi), Groq ja kohalik Ollama. Agendi stiil, sihtkassareserv, maksimaalne positsioonide arv, soovitud hinnaliikumine ja muud strateegiaparameetrid on andmebaasis, mitte eraldi Python-koodis.

## Hetkel olemasolevad kontod

Allolev loend kirjeldab selle hoidla praeguses `data/portfolio.db` failis olevaid kontosid. Portfellide väärtused, rahajäägid ja positsioonid muutuvad tehingute ning turuhindadega; veebiliidese edetabel on ajakohane vaade.

| Kasutajanimi | Tüüp | Strateegia / roll |
|---|---|---|
| `taavet` | inimene | Käsitsi juhitav inimkaupleja konto. |
| `madis` | LLM-agent | **Aggressive Momentum** — otsib tugeva mahu ja uudistega hoogsaid liikumisi; suuremad, 15–25% positsioonid. |
| `mari` | LLM-agent | **Conservative Value** — eelistab kvaliteetseid blue-chip aktsiaid mõõdukatel langustel ning väiksemaid positsioone. |
| `indexer` | indeksifond | **Passive Index** — võrdlusalus; investeerib alguses kogu raha indeksifondi (vaikimisi `SPY`) ja hoiab seda. |
| `trend` | LLM-agent | **Quality Trend Following** — jälgib tugevaid, likviidseid ja kvaliteetseid trende ning kontrollitud tagasitõmbeid. |
| `breakout` | LLM-agent | **Concentrated Breakout** — otsib vähese arvu suure veendumusega läbimurdeid, millel on tugev maht või katalüsaator. |
| `reversion` | LLM-agent | **Quality Mean Reversion** — ostab kvaliteetseid suurettevõtteid ajutise, mõõduka languse järel. |
| `defender` | LLM-agent | **Defensive Low Volatility** — eelistab väiksema volatiilsusega ettevõtteid, hajutust ja suuremat kassareservi. |
| `core` | LLM-agent | **Balanced Core Growth** — tasakaalustatud kasvustrateegia väljakujunenud kasvuliidritega. |

`taavet` ja kõik LLM-agendid on tavakontod. `indexer` on erand: ta ei läbi agendi otsustustsüklit ega järgi ühe positsiooni 30% ülempiiri, sest tema eesmärk on olla 100% investeeritud passiivne võrdlusportfell.

Uus andmebaas loob esmalt kontod `taavet`, `madis`, `mari` ja `indexer`; seejärel lisab võrdlevad LLM-profiilid `trend`, `breakout`, `reversion`, `defender` ja `core`, kui neid veel ei ole.

## Kuidas moodustub edetabel

Edetabel sisaldab **kõiki kontosid**, ka inimkontot ja passiivset indeksivõrdlusalust. Iga konto kohta arvutatakse:

```text
portfelli koguväärtus = rahajääk + kõigi avatud positsioonide koguväärtus
P&L kokku            = portfelli koguväärtus − 10 000 USD
P&L %                = P&L kokku / 10 000 USD × 100
```

Avatud positsiooni väärtus on kogus × viimane saadaval olev turuhind. Kui värsket hinda ei õnnestu saada, kasutatakse positsiooni keskmist ostuhinda, et portfelli saaks endiselt kuvada. Realiseeritud P&L on varem müüdud positsioonide kasumi või kahjumi summa; see on eraldi näitaja ning **ei ole** järjestuse alus.

Kontod sorditakse koguväärtuse järgi kahanevalt: suurima koguväärtusega konto on kohal 1. Seega ei võida kõige rohkem tehinguid teinud ega suurima protsendilise tootlusega konto, vaid konto, mille portfell on arvutushetkel kõige rohkem väärt. Veebilehe värskendamine ei kirjuta edetabeli ajalugu; hetkeseisud salvestatakse pärast edukat tehingut ja pärast lõppenud tsüklit. Vaikimisi hoitakse iga kasutaja kohta kuni 720 viimast hetkeseisu.

## Simulatsiooni ja tehingute reeglid

### Üldreeglid

- Iga tavakonto algsaldo on **10 000 USD**.
- Kasutatakse USD-d ja murdosakuid; kogused ning rahasummad hoitakse andmebaasis kaheksa kümnendkohaga täpsusega.
- Tehing on simulatsioonis kohene ning toimub rakendusele antud hinnaga. Iga edukas ost ja müük maksab fikseeritud **1 USD** tehingutasu; tasu arvestatakse rahajäägist ning talletatakse tehingulogis eraldi `FEE` reana. Slippage'it, makse ega orderiraamatu likviidsust ei modelleerita.
- Kõik ostud, müügid, tehingutasud ja dividendid talletatakse tehingulogis. Ettevõtte sündmuste teenus võib arvestada splittide ja dividendidega.
- Kõigi portfellide lähtestamine kustutab positsioonid, tehingud, analüüsid, hinnasõela ajaloo ja edetabeli ajaloo ning taastab kontodele 10 000 USD. Kui `SPY` hind on saadaval, investeeritakse indeksikonto seejärel uuesti SPY-sse.

### Täitmismootori kohustuslikud piirangud

| Reegel | Käitumine |
|---|---|
| Ostul peab olema raha | Ost ei saa kulutada rohkem kui konto rahajääk; vajadusel vähendatakse ostusummat. |
| Müügil peab positsioon olemas olema | Müüa saab ainult olemasolevat kogust; liiga suure müügi korral müüakse kogu olemasolev kogus. |
| Maksimaalne üksikpositsioon | Tavakonto ühe instrumendi turuväärtus võib olla kuni **30% kogu portfellist**. Liiga suurt ostu vähendatakse piirini või lükatakse tagasi. |
| Stop-loss | Kui positsiooni hind on keskmisest ostuhinnast langenud rohkem kui **8%**, müüakse kogu positsioon automaatselt. Täpselt −8% ei käivita reeglit. |
| Take-profit | Kui positsiooni hind on keskmisest ostuhinnast tõusnud rohkem kui **15%**, müüakse kogu positsioon automaatselt. Täpselt +15% ei käivita reeglit. |
| Tehingu kuju | Sümbol peab olema korrektne ning hind ja osakaal peavad olema positiivsed. Osakaal on 0–100% portfelli väärtusest. |

LLM-profiilide enda ostu- ja müügieesmärgid on **pehmed strateegiajuhised**, mitte täitmismootori asendus. Näiteks võib Madis soovida müüa +10% juures, kuid iga agendi suhtes kehtib sellest sõltumata globaalne +15% automaatne take-profit ning −8% stop-loss.

## Käivitamine

### Eeldused

- Python 3.12 või uuem
- `uv`
- vähemalt ühe LLM-pakkuja võti või kohalik Ollama

### Esmakordne seadistus

```bash
git clone https://github.com/daum88/taaveti_upt.git
cd taaveti_upt

python3 -m pip install --user uv
uv sync --locked

cp .env.example .env
# Muuda .env-is vähemalt LLM_PROVIDER ja valitud pakkuja võti.

uv run python main.py --init
uv run python main.py --warmup
scripts/app.sh start
```

Ava <http://127.0.0.1:8080>. Rakendus seotakse vaikimisi ainult kohaliku arvutiga. Kohaliku võrgu jaoks lisa `.env` faili teadlikult `SERVER_HOST=0.0.0.0`.

Protsesse haldab `scripts/app.sh` tmuxi seansis `taaveti`:

```bash
scripts/app.sh start
scripts/app.sh status
scripts/app.sh stop
```

Kui `LLM_PROVIDER=ollama` ja Ollama API ei tööta veel, käivitab skript samas tmuxi seansis `ollama serve`. Skript ei paigalda Ollamat ega laadi mudelit alla; selleks kasuta üks kord `scripts/setup-ollama.sh`.

### Kasulikud käsud

```bash
uv run python main.py                 # Richi terminalivaade, mitte veebiserver
uv run python main.py --init          # skeem, kontod ja instrumentide universum
uv run python main.py --warmup        # OHLCV- ja uudistevahemälu
uv run python integrity_check.py      # süsteemi tervikluse kontroll
uv run python test_suite.py           # abistav testikäsk
```

## Seadistus

Kõik põhiparameetrid on failis `config.py`; keskkonnamuutujad `.env` failis võivad neid üle kirjutada.

| Muutuja | Vaikimisi väärtus | Tähendus |
|---|---:|---|
| `LLM_PROVIDER` | `deepseek` | `deepseek`, `groq` või `ollama` |
| `SERVER_HOST` | `127.0.0.1` | veebiserveri aadress |
| `SERVER_PORT` | `8080` | veebiserveri port |
| `STARTING_BALANCE` | `10000.00` | konto algsaldo USD-des |
| `INDEX_FUND_TICKER` | `SPY` | passiivse võrdluskonto instrument |
| `FUNNEL_INTERVAL_HOURS` | `3` | automaatse sõelatsükli intervall |
| `VOLATILITY_THRESHOLD` | `0.01` | sõelale pääsemise hinnaliikumine; 1% |
| `MAX_POSITION_RATIO` | `0.30` | tavakonto ühe positsiooni ülempiir |
| `LEADERBOARD_SNAPSHOT_RETENTION_PER_USER` | `720` | säilitatavate edetabeli hetkeseisude arv konto kohta |

## Kvaliteedikontroll

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python -m compileall -q .
uv run --group audit pip-audit
```

Vaikimisi testid ei tee väliseid turuandmete ega LLM-i päringuid. Brauseritestid on eraldi märgisega ning vajavad Playwrighti ja Chromiumi:

```bash
uv run playwright install chromium
uv run pytest -q -m live tests/test_web_ui.py
```

## Olulisemad HTTP-liidesed

| Liides | Otstarve |
|---|---|
| `GET /api/health` | pakkuja ja ajastaja olek |
| `GET /api/leaderboard` | edetabel, väärtused ja P&L |
| `GET /api/watchlist?limit=50` | jälgimisnimekirja hinnad |
| `GET /api/stock/{ticker}` | instrumendi detailvaade |
| `GET /api/agent-detail/{username}` | konto, tehingute ja strateegia detailid |
| `GET /api/portfolio-history` | edetabeli ajaloo andmed |
| `GET /api/transactions` | tehingulogi |
| `POST /api/cycle` | käivita sõelatsükkel käsitsi |
| `POST /api/trade` | käsitsi tehing inimkontoga |
| `POST /api/chat/{agent}` | vestle agendiga |
| `POST /api/analyze/{agent}` | küsi agendilt portfellianalüüsi |
| `POST /api/build-portfolio/{agent}` | lase agendil algportfell koostada |
| `POST /api/reset` | lähtesta kõik simulatsiooniportfellid |
| `WS /ws` | reaalajas sündmuste voog |

## Litsents

MIT — UPT lõputöö projekt
