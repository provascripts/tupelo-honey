# Report dei controlli finali di tupelo-honey 0.0.4

Codice all'ultimo commit `091e616`, esempi e documenti a `cf8f640`, misure rigenerate su `cf8f640`.

| Punto | Esito | Prove e note |
|---|---|---|
| 1. File generati | Fatto | `*.gen.*`, un'intestazione `Code generated ... DO NOT EDIT.` o `@generated` nelle prime 10 righe e `linguist-generated` in `.gitattributes` escono dai risultati del gate, ma contano ancora come codice che usa un nome. Test con `utils.gen.ts`. Replay dell'app rifatto: finding euristici da 1 a 0 su 200 commit. |
| 2. Metrica del replay | Fatto | Debito, BACKUP ed euristici stanno in colonne separate; il target del 2% vale solo per gli euristici. rax, 190 commit: debito 2, BACKUP 6, euristici 1 (0,5%). redis 3 su 200 (1,5%), app 0 su 200, sds 0 su 49. I BACKUP di `alembic/env.py` restano hard. |
| 3. Push senza `--force` | Script pronto, push non fatto | `push_tupelo.bat` crea il tag annotato `v0.0.4` se manca e lancia `git push origin main --follow-tags`. Merge `--ff-only` su main e tag restano in attesa della prova del `systemMessage` (punto 4). |
| 4. Sessione reale | Fatto, con un punto da guardare a mano | Con `claude -p` e il plugin locale passano tutti e quattro i casi; dettagli in `bench/smoke-0.0.4.md`. Non verificabile senza interfaccia: che il `systemMessage` si veda a schermo. Haiku ha caricato `craft-standard` in 2 sessioni su 4 fra quelle che hanno modificato file. |
| 5. `init` e standard | Fatto, ma `craft-standard` non sta nei 6 KB | Nessun riferimento a `/tupelo init` in README, CHANGELOG, skills, `craft-mode.sh` e test; restano i due controlli che `init` sia rifiutato. La direttiva nomina `craft-standard` e dice quando caricarla. Il README dice che lo standard arriva con la skill, senza copie. `craft-standard` pesa 19.572 byte perché è `AGENTS.md` byte per byte, ed è esclusa per progetto dal limite; le altre skill arrivano al massimo a 5.994 byte. |
| 6. Python 3.8 | Fatto | Python 3.8.20 reale su una copia di `091e616`: 100 test OK, 7 saltati. |
| 7. Stabilità su Windows | Fatto | 59 run verdi su 59, uno dopo l'altro, su `cf8f640`, in 38 minuti. Memoria libera all'inizio: 3.377 MB su 16.088; prima di ogni run mai sotto 2.974 MB. Un primo tentativo si era fermato per memoria scarsa (circa 2 GB liberi su 16) a 57 verdi su 58: il run 57, seed 40110920, è caduto in quella finestra, con 21 test falliti in 26 secondi invece di 38 e processi figli che non partono (codice 3221225794, cioè 0xC0000142, in 12 punti del log). Baseline con retry spento su `d9e9faa`: 59 verdi su 59, quindi i 2 guasti su 40 visti prima non si sono ripresentati e la causa esterna resta dedotta dai messaggi ("unable to write new index file", "Permission denied"), non dimostrata. Prova con antivirus escluso non eseguita: la sessione non è elevata, l'antivirus attivo è Norton 360 e le sue esclusioni si impostano solo da interfaccia. Se git fallisce, gli hook escono con 0 e senza traceback (test con guasto alla k-esima chiamata, k = 1..N); il gate ritenta 3 volte i guasti transitori. Un run precedente ha dato 1 fallimento con log perso, non riprodotto. |
| 8. Testo "15-25" | Niente da cambiare | Nessuna occorrenza di "15-25" o "15–25" in `AGENTS.md`, `skills/`, README, CHANGELOG, `bin/`, `scripts/` e `tests/`. |
| 9. Hard di Redis | Fatto, soglia confermata all'80% | 82 hard: TRIVIAL 55, TODO 20, BACKUP 7. Campione di 10 TRIVIAL su 55, seed 40: quasi tutti titoli in stile Guide su una sola istruzione o etichette brevi; 3 di antirez e 7 di altri. Su tutti i 55, antirez ne ha scritti 15. Soglia confermata all'80%: alzarla al 100% ne toglierebbe solo 10, e nel replay di 200 commit di Redis nessun TRIVIAL è hard. |
| 10. Segnali dell'app | Fatto | Segnali in 8 commit su 200 (4,0%), senza NO-TEST in 2 su 200 (1,0%). Gli ORPHAN nei `.md` sono 7 righe in 2 commit, elencate qui sotto. |
| 11. Esempi in `references/` | Fatto | 26 file su 36 verificati su compilatori veri: C e C++ con `zig cc`, Zig 0.17.1, Rust, C# con .NET 9, TypeScript `--strict`, Python. L'harness OOM in C e il suo driver hanno girato sul vero `rax.c`. Trovato e corretto un esempio rotto: `cleanup-zig` usava `std.fs.cwd`, che in Zig 0.17 non esiste più. `differential-rust` è solo controllato con `cargo check --tests`, perché qui il linker non trova `msvcrt.lib`. Gli altri 10 portano in testa "Illustrative, not compiled": Go 4, Java 3, Swift 1 (manca il compilatore), `rax-excerpts-c` e `boundaries-c` (estratti). |

## Punto 10: gli ORPHAN da rivedere a mano

| Commit | Riga di `deployment.md` | Contesto | Valutazione |
|---|---|---|---|
| `1c5dd70308` | 42 | `python -c "import secrets; print(secrets.token_urlsafe(32))"` | Falso allarme: la funzione è della libreria standard e il comando resta valido. |
| `1c5dd70308` | 60 | `export POSTGRES_PASSWORD="$(python -c '... secrets.token_urlsafe(32))')"` | Falso allarme, come sopra. |
| `1c5dd70308` | 66 | `export SECRET_KEY="$(python -c '... secrets.token_urlsafe(32))')"` | Falso allarme, come sopra. |
| `1c5dd70308` | 72 | `export FIRST_SUPERUSER_PASSWORD="$(python -c '... secrets.token_urlsafe(32))')"` | Falso allarme, come sopra. |
| `32ab6dd022` | 57 | ``Set the `POSTGRES_PASSWORD` to a secure value:`` | Falso allarme: la variabile è uscita da `config.py` ma resta in `.env`, `compose.yml` e nei workflow. |
| `32ab6dd022` | 60 | `export POSTGRES_PASSWORD="$(python -c ...)"` | Falso allarme, come sopra. |
| `32ab6dd022` | 198 | ``* `POSTGRES_PASSWORD` `` | Falso allarme, come sopra. |
