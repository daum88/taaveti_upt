# 📈 Taaveti UPT — tehisaru aktsiaportfelli simulaator

Taaveti UPT on lõputöö jaoks loodud **paberportfelli simulatsioon**. Programmis võistlevad inimese juhitud konto, mitu eri investeerimisstiiliga tehisaruagenti ning passiivne võrdlusportfell. Kõik alustavad 10 000 USA dollariga, kuid raha ei investeerita päriselt: tehingud kantakse kohalikku SQLite-andmebaasi ning portfellide väärtust hinnatakse turuandmetega.

Uurimuses on kaks tehisaru otsustusarhitektuuri. Seitse **ühe mudeli strateegiakontot** kasutavad omavahel sama LLM-i ning erinevad investeerimisstrateegia, riskipiiride ja portfelli seisu poolest. Lisaks osaleb **AI Investment Committee**, kus kolm GitHub Copiloti mudelit annavad sõltumatud kvaliteedi-, momentumi- ja riskihinnangud ning neljas mudel teeb nende põhjal lõpliku otsuse. Metoodika võrdleb seega nii strateegiaid kui ka ühe mudeli ja mitme mudeli otsustusarhitektuure; mudel ja inferentsieelarve ei ole komiteekonto puhul kontrollmuutujad.

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

Esmasel seadistamisel luuakse andmebaas, vaikekontod ja instrumentide universum. `--warmup` täidab vahemälu 90 päeva OHLCV-andmete ning kuni 48 tunni uudistega. Rakendus kasutab ühe protsessi SQLite-andmebaasi WAL-režiimis: lugejad saavad töötada koos, kirjutamine on tehinguline ja tehingu- ning positsioonikirjed jäävad auditijäljeks.

### 2. Sõelatsükkel

Taustatöö käivitub serveri käivitamisel ning seejärel vaikimisi iga kolme tunni järel. Tsükli võib veebist ka käsitsi käivitada.

- **Esimene läbimine:** korraga küsitakse aktiivsete instrumentide hinnad. Edasi pääseb instrument, mille absoluutne päevane muutus on suurem kui 1% (`VOLATILITY_THRESHOLD=0.01`).
- **Teine läbimine:** kandidaatidele küsitakse vaikimisi viimase 24 tunni uudised. Uudised salvestatakse ning kandidaat antakse agendile koos hinnaliikumise ja mahuga. Uudise puudumine ei välista instrumenti.
- **Otsustamine:** seitset ühe mudeli strateegiakontot hindab nende ühine LLM järjestikku. Komiteekontol annavad kolm eri Copiloti mudelit sama hetktõmmise põhjal sõltumatu ettepaneku ning eraldi eesistuja mudel valib ühe lõpliku otsuse. Iga konto kontekst sisaldab tema strateegiat, rahajääki, positsioone ja viimaseid tehinguid. Konto võib ühe partii jooksul esitada maksimaalselt ühe tavapärase ostu-, müügi- või hoidmisotsuse.
- **Erandid:** enne agendi otsust kontrollitakse automaatselt kõiki tema positsioone stop-loss'i ja take-profit'i suhtes. Sundmüük võib seega toimuda lisaks agendi tavapärasele otsusele.

Tsükkel töötab ka siis, kui turg on suletud; turu olek antakse agendile kontekstiks ning tehing märgitakse sellisel juhul vastavalt. Hinnad on andmeallika pakutavad turuhinnad, mitte garanteeritud täitmishinnad.

### 3. Tehisaru roll

LLM ei kirjuta andmebaasi otse ega otsusta lõplikku täitmist. Ta tagastab struktureeritud ettepaneku: sümbol, tegevus, osakaal ja põhjendus. Keskne täitmismootor valideerib selle ning võib ostusummat piirata või tehingu tagasi lükata.

Ühe mudeli strateegiakontode pakkujad on DeepSeek (vaikimisi), Groq ja kohalik Ollama. Enne andmebaasi loomist valitakse `LLM_PROVIDER`-i ja vastava mudelimuutujaga neile üks ühine mudel. Komiteekonto kasutab eraldi pi protsessi kaudu GitHub Copiloti OAuth-autentimist: vaikimisi on nõustajad `claude-sonnet-4.6`, `gpt-5.4` ja `kimi-k2.7-code` ning eesistuja `gpt-5.6-sol`. Kõik mudelisidumised, mudelipõhised vastused, räsidega sisendid ja lõplik otsus talletatakse auditiks. Iga välise pi-kõne juures talletatakse ka pi seansi ID, täielik tokenikasutuse JSON ja pi mudelikataloogi hinnal põhinev hinnanguline USD-kulu; komitee kogukulu on selle nelja mudelisammu summa. Tellimuspõhise GitHub Copiloti puhul on see võrreldav hinnang, mitte tingimata tegelik arvesumma. Ükski mudel ei kirjuta andmebaasi ega kasuta pi faili-, shelli- või muid tööriistu.

### Uurimismetoodika

Võrdlus sisaldab seitset sama mudeliga strateegiakontot, üht mitme mudeli komiteekontot, inimkontot ja passiivset indeksikontot. Kõik saavad sama partii muutumatu turuhetktõmmise ja alluvad samale täitmismootorile, kuid komitee kasutab ühe otsuse jaoks nelja mudelikõnet. Tulemuste tõlgendamisel käsitletakse komiteed eraldi **AI Ensemble** rühmana, sest selle mudelivalik ja arvutusressurss erinevad ühe mudeli kontodest. Seetõttu ei saa komitee paremat või halvemat tootlust omistada ainult investeerimisstrateegiale.

## Vaikimisi kontod ja strateegiad

Allolev loend kirjeldab uue andmebaasi loomisel lisatavaid vaikimisi kontosid. Olemasolev kohalik andmebaas võib sisaldada varem loodud või ümber nimetatud kontosid. Portfellide väärtused, rahajäägid ja positsioonid muutuvad tehingute ning turuhindadega; veebiliidese edetabel näitab alati andmebaasi ajakohast seisu.

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
| `committee` | AI Ensemble | **Multi-Model Investment Committee** — kolm sõltumatut Copiloti nõustajat ja eraldi eesistuja mudel teevad ühe auditeeritava lõppotsuse täieliku investeerimisvabadusega. |

`taavet` ja ühe mudeli LLM-agendid järgivad platvormi investeerimispiiranguid. `committee` määrab ise instrumentide valiku, positsioonide arvu ja suuruse, kontsentratsiooni, sektorijaotuse, kassataseme ning väljumisotsused; sellele ei rakendata platvormi portfelli-, stop-loss- ega take-profit-piiranguid. Täitmiseks jäävad kehtima tehnilised invariandid: korrektne order ja värske hind, ostuks olemasolev raha, müügiks olemasolevad osakud ning tehingutasu. `indexer` ei läbi agendi otsustustsüklit ega järgi ühe positsiooni 30% ülempiiri, sest tema eesmärk on olla 100% investeeritud passiivne võrdlusportfell.

Uus andmebaas loob esmalt kontod `taavet`, `madis`, `mari` ja `indexer`; seejärel lisab võrdlevad strateegiaprofiilid `trend`, `breakout`, `reversion`, `defender` ja `core` ning mitme mudeli `committee` konto, kui neid veel ei ole. Seitse strateegiakontot kasutavad sama mudelit; komitee mudeliroster on eraldi püsivalt auditeeritav otsustusarhitektuur.

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
- Veebiliidese käsitsi tehing algab hinnangu ja kinnitusega: enne kinnitamist kuvatakse instrumendi hind, kogus, tasu ning portfellimõju. See on mittesiduv hinnang; kinnitamisel küsib server uue simuleeritud hinna ja täitmismootor rakendab uuesti kõik piirangud, mistõttu lõplik hind või kogus võib erineda.
- Kõik ostud, müügid, tehingutasud ja dividendid talletatakse tehingulogis. Ettevõtte sündmuste teenus võib arvestada splittide ja dividendidega.
- Kõigi portfellide lähtestamine kustutab positsioonid, tehingud, analüüsid, hinnasõela ajaloo ja edetabeli ajaloo ning taastab kontodele 10 000 USD. Kui `SPY` hind on saadaval, investeeritakse indeksikonto seejärel uuesti SPY-sse.

### Täitmismootori piirangud

Järgmised investeerimispiirangud kehtivad inimkontole ja ühe mudeli LLM-agentidele; autonoomne `committee` on neist vabastatud, kuid järgib tabelis kirjeldatud raha, omandi ja tehingu kuju tehnilisi invariantte.

| Reegel | Käitumine |
|---|---|
| Ostul peab olema raha | Ost ei saa kulutada rohkem kui konto rahajääk; vajadusel vähendatakse ostusummat. |
| Müügil peab positsioon olemas olema | Müüa saab ainult olemasolevat kogust; liiga suure müügi korral müüakse kogu olemasolev kogus. |
| Maksimaalne üksikpositsioon | Tavakonto ühe instrumendi turuväärtus võib olla kuni **30% kogu portfellist**. Liiga suurt ostu vähendatakse piirini või lükatakse tagasi. |
| Stop-loss | Kui positsiooni hind on keskmisest ostuhinnast langenud rohkem kui **8%**, müüakse kogu positsioon automaatselt. Täpselt −8% ei käivita reeglit. |
| Take-profit | Kui positsiooni hind on keskmisest ostuhinnast tõusnud rohkem kui **15%**, müüakse kogu positsioon automaatselt. Täpselt +15% ei käivita reeglit. |
| Tehingu kuju | Sümbol peab olema korrektne ning hind ja osakaal peavad olema positiivsed. Osakaal on 0–100% portfelli väärtusest. |

Ühe mudeli LLM-profiilide enda ostu- ja müügieesmärgid on **pehmed strateegiajuhised**, mitte täitmismootori asendus. Näiteks võib Madis soovida müüa +10% juures, kuid talle kehtib sellest sõltumata globaalne +15% automaatne take-profit ning −8% stop-loss. Autonoomse `committee` otsuseid need globaalsed investeerimispiirangud ei muuda.

## Käivitamine

### Eeldused

- Python 3.12 või uuem
- `uv`
- vähemalt ühe ühe-mudeli LLM-pakkuja võti või kohalik Ollama
- pi ja GitHub Copiloti tellimus/OAuth-sisselogimine komiteekonto jaoks

### Esmakordne seadistus

```bash
git clone https://github.com/daum88/taaveti_upt.git
cd taaveti_upt

python3 -m pip install --user uv
uv sync --locked
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi
# Käivita pi-s /login ja vali GitHub Copilot, seejärel välju.

cp .env.example .env
# Määra .env-is seitsmele strateegiakontole ühine LLM_PROVIDER ja vajalik võti.
# Komitee kasutab eraldi pi GitHub Copiloti OAuth-sisselogimist.

uv run python scripts/initialize.py
uv run python scripts/warmup_cache.py
scripts/app.sh start
```

Ava <http://127.0.0.1:8080>. Rakendus seotakse vaikimisi ainult kohaliku arvutiga. Mitte-kohaliku `SERVER_HOST`-i (näiteks `0.0.0.0`) korral peab `.env` sisaldama vähemalt 32-märgilist `OPERATOR_TOKEN`-it; operaatori tegevuste päring saadab selle päises `Authorization: Bearer <token>`. Loo sobiv väärtus näiteks käsuga `python -c "import secrets; print(secrets.token_urlsafe(32))"`. `ALLOW_INSECURE_NONLOOPBACK=true` on üksnes teadlik arenduserand ning seda ei tohi kasutada ühises võrgus.

Protsesse haldab `scripts/app.sh` tmuxi seansis `taaveti`:

```bash
scripts/app.sh start
scripts/app.sh status
scripts/app.sh stop
```

Kui `LLM_PROVIDER=ollama` ja Ollama API ei tööta veel, käivitab skript samas tmuxi seansis `ollama serve`. Skript ei paigalda Ollamat ega laadi mudelit alla; selleks kasuta üks kord `scripts/setup-ollama.sh`.

### Kasulikud käsud

```bash
uv run python main.py                            # Richi terminalivaade, mitte veebiserver
uv run python scripts/initialize.py               # skeem, kontod ja instrumentide universum
uv run python scripts/warmup_cache.py             # OHLCV- ja uudistevahemälu
uv run python scripts/instrument_catalogue.py import-etfs --dry-run
uv run python scripts/instrument_catalogue.py backfill-metadata --limit 100
uv run python integrity_check.py                   # süsteemi tervikluse kontroll
uv run pytest -q                      # vaikimisi võrguvaba testisari
RUN_LIVE_CHECKS=1 uv run python scripts/live_diagnostics.py  # eraldi välisteenuste diagnostika
# Erakorralise kontojäägi paranduse eelvaade; muudab seisu ainult koos --apply-ga.
uv run python scripts/repair_ledger.py --username taavet --reason "kirjelda paranduse põhjust"
uv run python scripts/repair_ledger.py --username taavet --reason "kirjelda paranduse põhjust" --apply
# Kärbi aegunud operatsioonilised andmed ja loo kontrollitud SQLite-varukoopia.
uv run python scripts/maintain_database.py
# Varukoopiate eemaldamine nõuab alati selgesõnalist lippu.
uv run python scripts/maintain_database.py --prune-backups --keep-backups 7
# Taastamiseks peata enne server; endine andmebaas säilitatakse automaatselt kõrvalfailina.
scripts/app.sh stop
uv run python scripts/maintain_database.py --restore data/backups/portfolio-YYYYMMDDTHHMMSSffffffZ.db --apply
```

## Seadistus

Kõik põhiparameetrid deklareeritakse failis `settings.py`; keskkonnamuutujad `.env` failis võivad neid üle kirjutada. `config.py` on olemasolevate moodulite ajutine ühilduvuskiht ning uus kood peab saama muutumatu `Settings`-i eksemplari kompositsioonijuurest.

| Muutuja | Vaikimisi väärtus | Tähendus |
|---|---:|---|
| `LLM_PROVIDER` | `deepseek` | kõigi strateegiaagentide ühine pakkuja: `deepseek`, `groq` või `ollama` |
| `DEEPSEEK_MODEL`, `GROQ_MODEL`, `OLLAMA_MODEL` | pakkuja vaikeväärtus | valitud pakkuja ühine mudel kõigile strateegiaagentidele |
| `AGENT_MODEL_ROSTER` | — | tehniline erand seitsme strateegiakonto sidumisele; vaikimisi kasutavad need sama mudelit |
| `PI_CLI_PATH` | `pi` | komitee jaoks käivitatava pi programmi tee |
| `PI_COPILOT_ADVISER_MODELS` | `claude-sonnet-4.6,gpt-5.4,kimi-k2.7-code` | kolm eri GitHub Copiloti nõustajamudelit |
| `PI_COPILOT_JUDGE_MODEL` | `gpt-5.6-sol` | komitee lõpliku otsuse mudel; peab nõustajatest erinema |
| `PI_COPILOT_THINKING` | `medium` | pi mudelikõnede mõtlemistase |
| `PI_COPILOT_TIMEOUT_SECONDS` | `90` | ühe komitee mudelikõne ajalimiit |
| `SERVER_HOST` | `127.0.0.1` | veebiserveri aadress; mitte-kohalik aadress nõuab operaatoritokenit või teadlikku ebaturvalist arenduserandit |
| `SERVER_PORT` | `8080` | veebiserveri port |
| `OPERATOR_TOKEN` | — | vähemalt 32-märgiline bearer-token mitte-kohalike operaatori tegevuste jaoks |
| `ALLOW_INSECURE_NONLOOPBACK` | `false` | lubab mitte-kohalikud operaatori tegevused tokenita; ainult teadlik arenduserand |
| `STARTING_BALANCE` | `10000.00` | konto algsaldo USD-des |
| `INDEX_FUND_TICKER` | `SPY` | passiivse võrdluskonto instrument |
| `FUNNEL_INTERVAL_HOURS` | `3` | automaatse turuandmete sõelatsükli intervall (ei tee AI otsuseid) |
| `DECISION_BATCH_COOLDOWN_SECONDS` | `60` | kahe käsitsi käivitatud AI otsusepartii minimaalne vahe |
| `DECISION_REMINDER_TIMEZONE` | `America/New_York` | käsitsi otsuste operaatori meeldetuletuste ajavöönd |
| `DECISION_REMINDER_WEEKDAYS` | `1,3` | meeldetuletuse nädalapäevad (`0` = E, `6` = P) |
| `DECISION_REMINDER_TIME` | `10:00` | meeldetuletuse kellaaeg kohalikus ajavööndis |
| `VOLATILITY_THRESHOLD` | `0.01` | sõelale pääsemise hinnaliikumine; 1% |
| `MAX_POSITION_RATIO` | `0.30` | tavakonto ühe positsiooni ülempiir |
| `LEADERBOARD_SNAPSHOT_RETENTION_PER_USER` | `720` | säilitatavate edetabeli hetkeseisude arv konto kohta |
| `NEWS_RETENTION_DAYS` | `30` | uudiste tõendite ja kokkuvõtete säilitusaeg päevades |
| `MARKET_SNAPSHOT_RETENTION_DAYS` | `30` | funnel'i hinnavaatluste säilitusaeg päevades |
| `DECISION_AUDIT_RETENTION_DAYS` | `365` | tehinguta LLM-i otsustus- ja analüüsiauditite säilitusaeg päevades |
| `DATABASE_BACKUP_DIR` | `data/backups` | kontrollitud SQLite-varukoopiate kataloog |
| `DATABASE_BACKUP_RETENTION_COUNT` | `7` | käsitsi käivitatud varukoopiate rotatsiooni säilitatav arv |

## AI otsuste töövoog

Serveri taustal töötav funnel värskendab hindu ja uudiseid, kuid ei kutsu LLM-i ega tee AI-kontode tehinguid. Operaator käivitab avalehe **Run decisions now** nupuga ühe käsitsi otsusepartii. Partii teeb ühe värske funnel-tsükli: seitse strateegiakontot kasutavad ühise mudeli kontopõhiseid otsuseid ning komitee käivitab kolm tööriistadeta pi nõustajakõnet ja ühe tööriistadeta eesistujakõne. Kõik loevad sama muutumatut turuhetktõmmist. Kui vähem kui kaks nõustajat annavad korrektse vastuse või eesistuja ebaõnnestub, lõpetab komitee turvaliselt ilma tavatehinguta. Nädalavaade näitab teisipäeva ja neljapäeva kell 10:00 (`America/New_York`) **meeldetuletusi**, mis liiguvad USA turupühal järgmisele avatud turupäevale. Meeldetuletus ei käivita kunagi otsuseid automaatselt.

## Kvaliteedikontroll

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv run python -m compileall -q .
uv run --group audit pip-audit

# The default pytest suite blocks TCP sockets and excludes `live` tests.
# This keeps unit/integration tests independent of market-data and LLM providers.
```

`mypy` kontrollib järk-järgult `domain`-i ja `application`-i moodulite tüübisid. Selle esimese kvaliteedivärava fookus on nende moodulite avalikel liidestel ja kohalikul implementatsioonil; vanu sõltuvusi ning väliseid adaptereid kontrollitakse järgmiste etappidega eraldi.

`repair_ledger.py` ei luba suvalist kontojääki sisestada: see saab üksnes viia nimetatud konto rahajäägi vastavusse konto viimase muutumatu tehingulogikirje `cash_balance_after` väärtusega. Vaikimisi on see eelvaade; `--apply` nõuab põhjust ja talletab enne- ning pärastväärtuse, alliktehingu, operaatori ja põhjuse tabelisse `ledger_repairs`.

### Andmete säilitamine ja varundus

Iga funnel'i tsükkel kärbib aegunud uudised, hinnavaatlused ja tehinguta otsustusauditid. Tehingud, tellimuste idempotentsuskirjed, kontojäägi paranduste auditid ning tehinguga seotud täitmiskvoodid jäävad muutumatuks. Edetabeli ajalugu on piiratud konto kohta `LEADERBOARD_SNAPSHOT_RETENTION_PER_USER` väärtusega.

`maintain_database.py` teeb enne varukoopiat mitteblokeeriva WAL-i checkpoint'i, kasutab SQLite'i järjepidevat backup API-t ja kontrollib loodud koopia terviklust ning võõrvõtmete seoseid. Tavaline käsk ei kustuta ühtegi varukoopiat. Varukoopiate rotatsioon toimub ainult koos `--prune-backups` lipuga. Taastamine nõuab peatatud serverit ja `--restore ... --apply`; enne asendamist säilitab käsk senise andmebaasi koos WAL-i kõrvalfailidega ajamärgistatud `pre-restore` koopiana. Enne suuremat taastamis- või puhastustööd kopeeri varukoopiad eraldi säilituskohta.

Ligikaudseks mahuks arvesta, et 500 instrumenti kaheksa kolm-tunnise tsükli jooksul päevas loob 30 päevaga kuni 120 000 hinnavaatlust. Uudiste ning LLM-i vastuste maht sõltub pakkujate aktiivsusest ja vastuste pikkusest; aastane tehinguta otsustusauditite aken võib seetõttu olla kümneid kuni sadu MiB. Jälgi `data/portfolio.db` ja `data/backups/` mahtu ning kohanda säilitusaknaid teadlikult.

Vaikimisi testid ei tee väliseid turuandmete ega LLM-i päringuid. Brauseritestid käivituvad samuti vaikimisi: nad teenindavad staatilist UI-d lokaalselt ning asendavad kõik API-vastused deterministlike fixture'itega. Esmakordsel seadistamisel paigalda Chromium:

```bash
uv run playwright install chromium
uv run pytest -q
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
| `POST /api/cycle` | käivita ainult turuandmete sõelatsükkel käsitsi |
| `POST /api/decision-batches` | käivita kõikide AI-kontode käsitsi otsusepartii (`202`; aktiivne/cooldown `409`) |
| `GET /api/decision-batches/status` | viimase otsusepartii püsiv olek ja kontode edenemine (ühilduvusliides) |
| `GET /api/decision-batches/week` | käsitsi otsuste nädalaülevaade, meeldetuletused ja edenemine |
| `POST /api/trade/preview` | inimkonto tehingu mittesiduv hinnang |
| `POST /api/trade` | käsitsi tehing inimkontoga; nõuab korduste vältimiseks UUID-välja `client_order_id` |
| `POST /api/chat/{agent}` | vestle agendiga |
| `POST /api/analyze/{agent}` | küsi agendilt portfellianalüüsi |
| `POST /api/build-portfolio/{agent}` | lase agendil algportfell koostada |
| `POST /api/reset` | lähtesta kõik simulatsiooniportfellid |
| `WS /ws` | reaalajas sündmuste voog |

Kõik olekut muutvad ja väliseid mudelikõnesid käivitavad `POST`/`PATCH` liidesed lubavad vaikimisi loopback-serveri localhosti operaatorit ilma lisapäiseta. Mitte-kohaliku `SERVER_HOST`-i korral nõuavad nad ka localhostist `OPERATOR_TOKEN`-iga `Authorization: Bearer <token>` päist; puuduv või vigane token annab `401` vastuse. Kataloogi haldusloend `GET /api/instruments` järgib sama operaatoripoliitikat.

## Litsents

MIT — UPT lõputöö projekt
