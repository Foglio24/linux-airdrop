# Linux AirDrop

App nativa per questo PC Ubuntu con Wi-Fi MediaTek MT7922. Usa AWDL e il protocollo AirDrop, quindi l'obiettivo è comparire nel pannello **Condividi → AirDrop** di iPhone e iPad.

**Stato (15 ottobre 2026):** funziona. Con il Wi-Fi Direct attivo il PC compare nel menu AirDrop di iOS 27 e i trasferimenti sono completati in entrambe le direzioni: ricevuto un file di 1,5 MB dall'iPhone (`/Discover`, `/Ask`, `/Upload`) e inviato un file dal PC all'iPhone accettato dal telefono. Provato su una sola scheda (MT7922) e un solo iPhone; la modalità con soli monitor, senza Wi-Fi Direct, non completava i trasferimenti.

La correzione alla rimozione delle interfacce monitor ha consentito più arresti e ripristini dei moduli originali dopo il riavvio, senza riprodurre il blocco precedente. La protezione che richiede il riavvio resta attiva solo se viene rilevato un nuovo blocco del kernel. Consulta `verification.json` per lo stato dei controlli.

## Uso

1. Apri **Linux AirDrop** dal menu di Ubuntu.
2. Lascia il PC connesso via Ethernet e il Wi-Fi scollegato. Premi **Rendi visibile** (con il kernel 7.3 l'app carica da sola una correzione temporanea del driver) e inserisci la password nella finestra di autenticazione di Ubuntu.
3. Sul dispositivo Apple attiva Wi-Fi e Bluetooth, sbloccalo e apri **Condividi → AirDrop**. Per **inviare dall'iPhone al PC** non serve che l'iPhone sia visibile: può restare su **Ricezione disattivata** (verificato). «Tutti per 10 minuti» serve solo se vuoi che sia l'iPhone a ricevere dal PC.
4. Scegli il nome del PC. Accetta la richiesta sul computer.
5. Trovi i file in **Scaricati/LinuxAirDrop** (o nella cartella Download configurata nel sistema), in una cartella distinta per ogni trasferimento. **Interrompi visibilità** ripristina la gestione della scheda Wi-Fi.

Per inviare dal PC: con la sessione attiva premi **Invia file…**, scegli i file e poi il destinatario. Il telefono deve essere sbloccato, con ricezione AirDrop su Tutti e il pannello Condividi chiuso.

Se il PC non compare, lascia il pannello AirDrop aperto per circa un minuto. Puoi interrompere la sessione e cambiare il canale Wi-Fi (6, 36, 44) e il Wi-Fi Direct nella sezione **Visibilità**. Il **Registro** (menu ☰ o Ctrl+L) mostra cosa succede e ha un pulsante per copiarlo; ogni sessione è salvata anche in `~/.local/state/linux-airdrop/`.

## Interfaccia, icona nella barra e background

L'interfaccia usa GTK 4 e libadwaita, quindi segue lo stile di GNOME, il tema chiaro o scuro e le scorciatoie di sistema. Le impostazioni (nome, canale, Wi-Fi Direct, durata, opzioni di avvio) sono ricordate in `~/.config/linux-airdrop/settings.json`.

**Icona nella barra.** Il logo compare in alto, nella barra di sistema (serve l'estensione *AppIndicator*, già attiva su Ubuntu). Più chiaro quando il PC è visibile, attenuato quando non lo è. Il menu dell'icona ha: *Apri Linux AirDrop*, lo stato, *Rendi visibile / Interrompi visibilità* ed *Esci*.

**Background.** Con **Applicazione → Continua in background** attivo (predefinito) chiudere la finestra non ferma nulla: l'app resta attiva nella barra e, se la visibilità è attiva, il PC continua a ricevere (con una notifica con il pulsante **Interrompi**). Per riaprire la finestra: clic sull'icona → *Apri*, oppure lancia di nuovo «Linux AirDrop». Le richieste di ricezione e i file ricevuti arrivano come notifiche; ogni ricezione chiede comunque conferma in una finestra a parte. Se la sessione finisce per un errore o per scadenza a finestra chiusa, arriva una notifica e l'app resta nella barra. Senza l'icona nella barra, chiudere la finestra a sessione ferma chiude l'app. **Esci** (menu o Ctrl+Q) ferma sempre la sessione e chiude tutto. Con l'opzione disattivata, chiudere la finestra equivale a Esci.

**Durata della visibilità.** 25 minuti (predefinito), 2 ore oppure *Finché non la interrompo*. La scheda Wi-Fi resta in uso esclusivo per tutto il tempo.

**Avvio al login.** *Avvia al login* crea `~/.config/autostart/it.foglio.LinuxAirDrop.desktop`, che lancia `start.sh --background` (nessuna finestra, solo l'icona nella barra). *Rendi visibile all'avvio* attiva anche la visibilità, ma solo se è configurata l'attivazione senza password, altrimenti al login comparirebbe la richiesta della password. Per tenerlo sempre pronto scegli anche la durata *Finché non la interrompo*. Da terminale: `python3 install-user.py --autostart` (o `--no-autostart`).

## Attivazione senza password (helper di sistema)

Il programma che gestisce la scheda Wi-Fi deve girare come root. Finché resta nella tua cartella, Ubuntu chiede la password a ogni attivazione: togliere la richiesta lasciando quel file dove chiunque giri come te può modificarlo darebbe a qualsiasi programma dell'utente i privilegi di root. Per questo l'attivazione senza password usa una copia **di proprietà di root**.

Si configura **una sola volta**, con la password di amministratore, da **Applicazione → Attivazione senza password → Configura…** oppure da terminale:

```sh
sudo python3 ~/.local/share/linux-airdrop/system_helper.py install
```

Cosa installa, e nient'altro:
- `/usr/local/lib/linux-airdrop/`: copia di proprietà di root dei soli file che root esegue (`root_bridge.py`, `p2p_anchor.py`, `awdl_diag.py`, OWL e i tre moduli del driver di prova), non scrivibile da nessun altro, più il piccolo script d'ingresso `airdrop-bridge` e un elenco degli hash.
- `/usr/share/polkit-1/actions/it.foglio.LinuxAirDrop.policy`: dichiara l'azione e lega l'autorizzazione al solo percorso `airdrop-bridge` (non a `python3`, che sarebbe una shell di root).
- `/etc/polkit-1/rules.d/49-linux-airdrop.rules`: permette **solo al tuo utente**, e solo da sessione locale attiva, di avviare quell'azione senza password. Gli altri utenti continuano a dover autenticarsi come amministratori.

L'app usa l'helper solo se è *pronto*: file di root, non modificabili, con hash identici a quelli del pacchetto. Se qualcosa non torna (stato *Da aggiornare* dopo aggiornamenti dell'app, o *Non sicura*) torna da sola alla richiesta della password. Dopo aver modificato i file lato root (per esempio con un nuovo kernel e nuovi moduli) usa **Aggiorna…**. Cosa può fare, al peggio, un programma che gira come te con questa autorizzazione: attivare o interrompere AirDrop e quindi togliere e rimettere il Wi-Fi (non eseguire comandi arbitrari come root). Per rimuovere tutto: **Rimuovi** nella stessa riga, oppure `sudo python3 ~/.local/share/linux-airdrop/system_helper.py uninstall`. Stato da terminale: `python3 system_helper.py status`.

**Durata dei nomi AirDrop.** A ogni sessione il PC si annuncia con un nome tecnico nuovo e il telefono lo ricorda finché i record non scadono. Per questo i record durano 120 secondi (non i 75 minuti predefiniti della libreria): un riavvio della visibilità non lascia più «<nome del PC>» fantasma nell'elenco per più di un paio di minuti.

## Wi-Fi Direct (modalità predefinita)

Con **Visibilità → Wi-Fi Direct** attivo (è la scelta predefinita, solo sul canale 6) l'app crea un gruppo Wi-Fi Direct temporaneo e usa il suo indirizzo per le due interfacce monitor. Nella modalità con soli monitor il telefono vedeva il PC ma le connessioni AirDrop restavano in attesa; con il gruppo Wi-Fi Direct la scheda tiene fermo il canale e, secondo altri utenti di MT7922, completa il trasferimento ([resoconto](https://github.com/t4t5/omdrop-owl/pull/24), [proposta](https://github.com/t4t5/omdrop-owl/pull/55)). Su questo computer il trasferimento completo è stato verificato il 15 ottobre.

La scheda Wi-Fi deve essere scollegata dalla rete; Ethernet può restare connessa. Il telefono non si collega a nessuna rete nuova. Da terminale: `./start.sh --activate` (Wi-Fi Direct) oppure `./start.sh --no-go-anchor --activate` (soli monitor).

L'app controlla che non esista già un gruppo sulla scheda e avvia un proprio `wpa_supplicant` con cartella di controllo privata, senza toccare quello di sistema. Rimuove gruppo, monitor e processo al termine, poi ripristina i moduli originali. Richiede `wpa_supplicant` e `wpa_cli`, già presenti sul computer. Se la sessione fallisce, le ultime righe di `wpa_supplicant` compaiono nel registro (la passphrase del gruppo è oscurata).

**Prova del 15 ottobre (11:36, Wi-Fi Direct attivo):** gruppo su canale 6, monitor e OWL attivi, iPhone agganciato come peer AWDL, ma nessuna connessione verso la porta 8771 in 7 minuti; senza Wi-Fi Direct, il giorno prima, il telefono si collegava in 10 secondi. La causa era la scelta dell'interfaccia (vedi sotto). Il registro ora decodifica i nomi mDNS richiesti dal telefono (`Diagnosi AWDL`) e ogni sessione è salvata in `~/.local/state/linux-airdrop/session-AAAAMMGG-HHMMSS.log` (ultime 10).

**Causa trovata (15 ottobre, 11:50):** gruppo Wi-Fi Direct e `awdl0` hanno lo stesso MAC, quindi lo stesso indirizzo IPv6. OpenDrop passava a zeroconf l'indirizzo e zeroconf sceglieva l'interfaccia con indice più basso, cioè il gruppo: gli annunci `_airdrop._tcp` uscivano dal gruppo Wi-Fi Direct e il telefono non li vedeva mai (sull'interfaccia AWDL non usciva nessuna risposta al suo `_airdrop._tcp.local/PTR`). Ora l'interfaccia si sceglie per indice e IPv6 viene disattivato sul gruppo. Riprodotto in un namespace di rete senza toccare la scheda; poi verificato con l'iPhone: il PC compare e i trasferimenti arrivano a termine.

**Correzione del 15 ottobre:** NetworkManager non registra mai le interfacce Wi-Fi Direct (GO), quindi `nmcli device set … managed no` falliva con «Device not found» e annullava un gruppo già funzionante. Il passaggio ora è facoltativo e non interrompe più la sessione.

## Installazione e rimozione

L'installazione per l'utente non richiede sudo:

```sh
python3 install-user.py
```

Il programma viene copiato in `~/.local/share/linux-airdrop`; il lanciatore è `~/.local/share/applications/it.foglio.LinuxAirDrop.desktop`. Per rimuoverlo, chiudi prima l'app ed elimina questi due percorsi. I file ricevuti restano nella cartella Download.

Puoi anche avviare il pacchetto senza installarlo con `./start.sh`. Richiede Ubuntu con Python 3.14, GTK 4 e libadwaita con PyGObject, BlueZ, NetworkManager, `iw`, `ip` e `pkexec`, già presenti su questo computer. Le dipendenze Python del protocollo sono incluse.

## Comportamento e controlli

Il kernel Ubuntu 7.3.0-9 disabilita la modalità active monitor per i driver MT792x. L'app ora rileva questo limite prima di intervenire sulla rete. La modalità di prova include tre moduli compilati dai sorgenti esatti Ubuntu 7.3.0-9.9: riattiva il supporto solo per MT7922 e applica le correzioni pubblicate per gestione dei canali, ripristino dei filtri e velocità di trasmissione dei pacchetti a 5 GHz. [Correzione dei canali](https://marc.info/?l=linux-wireless&m=178823162355132&w=4), [correzione dei filtri](https://lists.openwall.net/linux-kernel/2026/09/04/2809).

I moduli di prova vengono caricati in memoria per la sessione. Quelli originali su disco non vengono sostituiti. Alla fine l'app tenta di ricaricare i moduli originali e ne verifica le versioni; un blocco nel kernel può richiedere un riavvio. Un riavvio usa sempre i moduli originali di Ubuntu. Questa modalità non è disponibile dopo un aggiornamento a un kernel diverso: occorre ricompilare per la nuova versione. La compilazione e i controlli ABI sono passati; un trasferimento con iOS 27 resta da verificare.

Il motore aggiornato `owl-modern` include le nove patch di [omdrop-owl](https://github.com/t4t5/omdrop-owl), con elezione del riferimento di sincronizzazione per metrica (`-E`). Usa per impostazione predefinita il canale 6 fisso (`-F`) e la strategia `intersect`, che annuncia la sovrapposizione con i canali del dispositivo Apple. Il canale si può scegliere nei dettagli oppure con `--channel 44`; `--channel-hopping` abilita la modalità alternativa sperimentale. L'annuncio Bluetooth usa il formato corrente del progetto OmDrop, senza hash dei contatti. Questi aggiornamenti hanno superato i controlli software ma non costituiscono una prova di compatibilità con iOS 27.

La password serve a creare temporaneamente il collegamento radio. Non vengono aggiunte regole sudo senza password né servizi permanenti. Il server HTTPS e la lettura/scrittura dei file funzionano come utente normale. L'helper gestisce soltanto la scheda Wi-Fi selezionata e le proprie interfacce; conserva e ripristina lo stato precedente. Non arresta NetworkManager.

Ogni ricezione richiede conferma. Un upload privo di approvazione viene rifiutato. I nomi negli archivi vengono verificati, i file esistenti non vengono sovrascritti, e i trasferimenti incompleti non sono presentati come completati. Una sessione scade dopo la durata scelta (25 minuti per impostazione predefinita); interrompila prima quando hai terminato. Il collegamento sperimentale può essere lento, soprattutto con video e file grandi.

Sorgenti, versioni e licenze sono in `backend/manifest.json` e `backend/sources`. I moduli kernel hanno sorgenti, patch e manifest separati in `backend/driver-compat` e mantengono le proprie licenze GPLv2/BSD. OWL, OpenDrop e i file dell'app sono distribuiti con GPLv3. La ricerca upstream sui cambiamenti iOS non equivale a una prova sul tuo dispositivo.
