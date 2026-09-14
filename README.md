# Turni Wedely

Applicazione Docker per leggere automaticamente i turni di **Nicola Trussardi** da Wedely nelle sedi **WeManagers - Luxembourg Center** e **WeManagers - Luxembourg South**, con invio tramite WAHA.

## Funzioni

- dashboard web in italiano;
- configurazione Wedely e WAHA salvata nel volume `/app/data`;
- controllo manuale e test separati per login e WhatsApp;
- lunedì-sabato alle 21:00: turni del giorno successivo;
- domenica alle 21:00: turni dell'intera settimana successiva;
- fuso orario `Europe/Rome`;
- schermata diagnostica quando l'interfaccia Wedely non viene riconosciuta;
- nessuna credenziale salvata nel repository.

## Installazione con Portainer

1. Creare uno stack usando il contenuto di `docker-compose.yml`.
2. Pubblicare la porta desiderata (predefinita `8091`).
3. Aprire `http://IP_HOME_ASSISTANT:8091`.
4. Inserire nella pagina **Configurazione**:
   - username e password Wedely;
   - URL WAHA, per esempio `http://waha:3000`;
   - API key WAHA;
   - nome sessione;
   - numero destinatario nel formato internazionale, per esempio `39... `.
5. Usare prima **Test login**, poi **Test WhatsApp**, infine **Controlla ora**.

## Note WAHA

Sono supportati gli endpoint WAHA recenti `/api/sendText`; l'API key viene inviata come header `X-Api-Key`. Il numero può essere inserito con o senza `@c.us`.

## Sicurezza

Il file di configurazione è nel volume Docker e non viene versionato. Si consiglia di non esporre la dashboard direttamente su Internet.
