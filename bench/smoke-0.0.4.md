# Smoke test di tupelo-honey 0.0.4 in sessioni reali

Sessioni reali non interattive: `claude -p` (Claude Code 2.1.319, modello Haiku) con `--plugin-dir` sul sorgente, un repository git di prova per caso, eventi dei hook letti da `--output-format stream-json --include-hook-events`.

| Caso | Risultato | Evidenza |
|---|---|---|
| Una sessione nuova mostra la direttiva | Passa | L'hook SessionStart stampa la direttiva, 1126 caratteri, che finisce con "Before the first code change, load the craft-standard skill once." |
| Un TODO nudo scritto con Write viene bloccato | Passa | PostToolUse esce con 2 e riporta `todo_case.py:6: [TODO]`; il modello corregge; lo Stop esce con 0. |
| Un TODO nudo scritto da Bash viene bloccato allo Stop | Passa | Il PostToolUse non vede Bash; lo Stop esce con 2 e riporta `bash_case.py:1: [TODO]`; il modello corregge; il secondo Stop esce con 0. |
| Con soli segnali soft il modello non riparte | Passa | Lo Stop esce con 0 e stampa `{"systemMessage": ...[NO-TEST]...}`; nessun messaggio dell'assistente dopo quello Stop; `num_turns` resta 2. |
| Al prompt successivo i segnali arrivano una volta sola | Passa | Secondo prompt: l'hook UserPromptSubmit consegna il `[NO-TEST]`. Terzo prompt: output vuoto. |

## Non verificato

Che il `systemMessage` si veda a schermo: `claude -p` lo riporta solo nell'evento dell'hook, e la resa dipende dall'interfaccia interattiva. Per provarlo, in una sessione interattiva con il plugin chiedi di creare un file `counter.h` con una `struct` nuova e nessun test: alla fine del turno deve comparire "tupelo-honey review signals, for Claude with your next message".

## Osservazione

La skill `craft-standard` è stata caricata prima della modifica in 2 sessioni su 4 fra quelle che hanno modificato file (`api.py` e `todo_case.py` sì, `counter.h` e `bash_case.py` no). La direttiva arriva sempre, ma con Haiku non viene sempre seguita; non ho misurato i modelli più grandi.
