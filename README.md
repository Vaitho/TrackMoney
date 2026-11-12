# Bot Telegram: spese e prezzi

Un bot che tiene traccia delle spese e avvisa quando scende il prezzo di un prodotto.

## Come si usa

- **Registrare una spesa**: scrivi `caffè 1,20` oppure `45 benzina`. Puoi aggiungere una data
  (`ieri`, `15/11`) e una categoria (`#regali`).
- **Registrare un'entrata**: metti un `+` davanti, `+1500 stipendio` (la parola "stipendio",
  "tredicesima" o "quattordicesima" basta anche senza il `+`). Nel `/report` compaiono entrate,
  spese e saldo.
- **Monitorare un prezzo**: incolla il link di un prodotto.

| Comando | Cosa fa |
|---|---|
| `/spese` | Elenco delle spese; toccandone una la modifichi o la elimini. Da qui si arriva alle entrate |
| `/report` | Grafico e riepilogo del mese, del mese scorso o dell'anno, con il saldo; invio automatico |
| `/budget` | Limite mensile, totale o per categoria, con avvisi all'80% e al superamento |
| `/ricorrenti` | Spese e entrate che si registrano da sole ogni mese (`+1500 stipendio 27`) |
| `/prezzi` | Prodotti monitorati: obiettivo, storico, frequenza, pausa |
| `/export` | Spese ed entrate in CSV |
| `/cancella_dati` | Elimina tutto ciò che il bot sa dell'utente (con conferma e CSV facoltativo) |
| `/annulla` | Interrompe l'operazione in corso |

Scorciatoie: `/budget 1500`, `/budget ristoranti 300`, `/ricorrenti 12,99 netflix 15`,
`/report scorso`, `/report anno`.

## Usarlo con altre persone

- **Accesso riservato**: con `ALLOWED_USERS` nel file `.env` il bot risponde solo agli ID Telegram
  elencati (`ALLOWED_USERS=111111111,222222222`). Chi non è nell'elenco riceve il proprio ID, da
  comunicare a chi gestisce il bot per essere aggiunto. Lasciando la variabile vuota il bot è aperto
  a tutti. Se un ID è scritto male il bot non parte, per non restare aperto per errore.
- **Link sicuri**: il bot rifiuta indirizzi interni (rete aziendale, `localhost`, servizi cloud) sia
  nei link incollati sia nei reindirizzamenti.
- **Più utenti insieme**: utenti diversi vengono serviti in parallelo; i messaggi dello stesso
  utente restano in ordine.
- **Negozi online**: tutti i controlli partono dallo stesso indirizzo, quindi i controlli non sono
  mai più frequenti di una volta all'ora e le richieste allo stesso sito sono distanziate di qualche
  secondo. Con molti prodotti Amazon il blocco resta possibile: in quel caso il bot mette il
  prodotto in pausa e avvisa.
- **Copie di sicurezza**: una copia del database al giorno nella cartella `backups` accanto al
  database, tenendo le ultime 7 (`BACKUP_DIR` e `BACKUP_KEEP` per cambiarle). Dopo un
  `/cancella_dati` i dati restano nelle copie fino alla loro sostituzione.

## Avvio sul proprio computer

Serve Python 3.11 o superiore.

```
python -m venv .venv
.venv\Scripts\activate            (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
```

Crea un file `.env` con il token ricevuto da @BotFather:

```
BOT_TOKEN=123456:ABC...
```

Poi avvia con `python main.py`.

## Test

```
pip install -r requirements-dev.txt
pytest
```

I test non usano Telegram né siti reali: simulano una chat (`tests/helpers.py`) su un
database temporaneo.

## Deploy su un server (Docker)

Serve un server Linux con Docker e almeno 1 GB di RAM (meglio 2: Chromium ne usa parecchia).
Lo stesso token non può essere usato da due bot accesi insieme: **spegni il bot sul computer
prima di accenderlo sul server**.

1. Copia la cartella del progetto sul server (senza `.venv`).
2. Nella cartella crea il file `.env` con `BOT_TOKEN=...`.
3. Prepara la cartella del database, che deve appartenere all'utente del container (uid 1000):
   ```
   mkdir -p data
   cp bot.db data/          # solo se vuoi portarti dietro i dati esistenti
   sudo chown -R 1000:1000 data
   ```
4. Avvia:
   ```
   docker compose up -d --build
   ```

Comandi utili:

```
docker compose logs -f        # guardare cosa sta facendo
docker compose restart        # riavviare
docker compose up -d --build  # aggiornare dopo aver cambiato il codice
ls data/backups               # le copie giornaliere del database (le ultime 7)
```

Le copie giornaliere stanno nel volume `data`, quindi sullo stesso disco del database: proteggono
da errori, non dalla perdita del server. Ogni tanto scaricane una altrove.

Se nei log compare `unable to open database file`, la cartella `data` non appartiene
all'uid 1000: ripeti il comando `chown` del punto 3.

## Struttura del codice

| File | Contenuto |
|---|---|
| `main.py` | Avvio del bot |
| `config.py` | Impostazioni lette da `.env` |
| `handlers/` | Comandi e bottoni, un file per area (spese, report, budget, ricorrenti, prezzi) |
| `parsing.py` | Interpretazione del testo: importi, date, categorie, entrate |
| `pricing.py` | Lettura del prezzo da una pagina e logica degli avvisi |
| `browser.py` | Chromium automatico per i siti che usano JavaScript |
| `urlguard.py` | Rifiuta i link verso indirizzi interni |
| `concurrency.py` | Utenti in parallelo, un messaggio alla volta per ciascuno |
| `summaries.py` | Budget, riepiloghi e periodi |
| `charts.py` | Grafici |
| `jobs.py` | Lavori periodici: controllo prezzi, ricorrenti, resoconti automatici |
| `db.py` | Database SQLite |
| `formatting.py` | Formato degli importi e nomi dei mesi |

Le parole chiave che assegnano le categorie sono in `parsing.py` (`CATEGORY_KEYWORDS`).

## Impostazioni (`.env`)

| Variabile | Predefinito | Significato |
|---|---|---|
| `BOT_TOKEN` | – | Token del bot (obbligatorio) |
| `DB_PATH` | `bot.db` | File del database |
| `MAX_SUBS_PER_USER` | `10` | Massimo di prodotti monitorati per utente |
| `TIMEZONE` | `Europe/Rome` | Fuso orario di date e resoconti |
| `ALLOWED_USERS` | vuoto | ID Telegram autorizzati, separati da virgola; vuoto = tutti |
| `BACKUP_DIR` | `backups` accanto al database | Dove salvare le copie giornaliere |
| `BACKUP_KEEP` | `7` | Quante copie tenere |
