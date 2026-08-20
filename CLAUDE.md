# CLAUDE.md — AI Venture Studio

Permanente werkinstructies voor Claude Code (en alle agents/subagents)
binnen dit project.

## 0. Prioriteitsvolgorde

Bij een conflict geldt deze volgorde, hoogste eerst:

1. De expliciete huidige instructie van de gebruiker in de sessie.
2. Security- en approval-constraints (§4, §7, §8).
3. Dit document (CLAUDE.md).
4. Obsidian Company Brain (`obsidian/`).
5. Overige projectdocumentatie (README, docs/).

## 1. Autonomie-principe

**Maximize safe autonomy, minimize unnecessary human interruption.**

Als Claude voldoende informatie en toestemming heeft om een low-risk
stap veilig uit te voeren binnen een reeds goedgekeurde taak, dan
voert Claude die stap uit in plaats van onnodig om bevestiging te
vragen. Onderbreek de gebruiker alleen wanneer:

- de actie in §8 (Human Approval Gates) valt, of
- de instructie echt onduidelijk is, of
- de actie moeilijk terug te draaien is en buiten de scope van de
  huidige taak valt.

Dit principe staat niet boven §4/§7/§8 — het bepaalt alleen hoe Claude
zich gedraagt *binnen* de ruimte die die secties toestaan.

## 2. Projectdoel

AI Venture Studio is een 24/7 systeem dat kansen (opportunities)
verzamelt, normaliseert, onderzoekt, scoort en — na menselijke
goedkeuring bij material impact — omzet in experimenten/ventures. Flow:

`collect -> normalize -> opportunity -> research -> critic -> score ->
Telegram review -> experiment proposal`

Het systeem is besluitvormingsondersteuning en een engineering-
assistent, geen volledig autonoom systeem: material spend, productie-
deployment, DNS-wijzigingen en destructieve acties blijven onder
menselijke goedkeuring (§8).

## 3. Architectuurprincipes

- FastAPI backend is de enige API-laag; geen losse scripts die de
  database rechtstreeks buiten de app om muteren.
- PostgreSQL is de enige system of record voor operationele/
  transactionele data (§10).
- Obsidian (`obsidian/`) is de Company Brain voor kennis/strategie,
  niet voor transactionele data (§11).
- Docker Compose is het canonieke lokale/VPS-deploymentmodel.
- Nieuwe services worden pas toegevoegd als een bestaand onderdeel
  aantoonbaar tekortschiet — niet vooruitlopend op toekomstige schaal
  (§15).
- **Agent- en model-agnostisch:** Claude Code is de primaire
  engineering agent, maar businesslogica (scoring, opportunity-
  pipeline, data-modellen) mag niet onnodig hard-coupled raken aan één
  modelprovider of agent-framework. Modelaanroepen lopen via een
  aanroepbare interface/config (bijv. env-gestuurde provider/model-
  keuze), niet via provider-specifieke logica die door de hele
  codebase verspreid is.

## 4. Securityregels

- Nooit `.env`, API keys, tokens, credentials, SSH-keys of productie-
  secrets committen. `.gitignore` is leidend — bij twijfel toevoegen,
  niet weglaten.
- Vóór elke commit: expliciet scannen op secret-achtige patronen
  (keys, tokens, wachtwoorden, private-key headers) in de staged diff,
  niet alleen vertrouwen op `.gitignore`.
- Geen hardcoded secrets in code, docs, of voorbeeldbestanden — alleen
  placeholders (`change_me`, lege waarden) in `.env.example`.
- Secrets nooit in Obsidian, commit messages, of logs.
- Externe calls (Telegram, toekomstige APIs) altijd via environment
  variables, nooit inline.
- Destructieve acties (drop table, force push, reset --hard op gedeeld
  werk, verwijderen van productiedata) zijn High-Risk Write (§6, §8).

## 5. Git-werkwijze

**Toegestaan zonder aparte goedkeuring**, zodra een developmenttaak is
goedgekeurd:

- feature branches aanmaken;
- commits maken;
- lokale tests uitvoeren;
- documentatie bijwerken;
- normale code refactoren;
- branches naar GitHub pushen.

**Nooit zelfstandig, ongeacht taakgoedkeuring:**

- force-pushen (`push --force`, `push --force-with-lease`);
- main-geschiedenis herschrijven (`rebase -i` op main, `filter-repo`,
  amend van gepushte commits);
- branches met werk verwijderen zonder te verifiëren dat het werk elders
  bewaard is;
- naar productie deployen;
- secrets wijzigen (roteren, aanmaken, verwijderen);
- overige destructieve Git-acties (`clean -f`, `reset --hard` op werk
  dat niet van de huidige sessie is).

**Werkwijze voor grotere features:**
`branch -> implement -> tests -> commit -> push -> review`

Merge naar `main` volgt pas ná de review-stap: Claude mag een branch
pushen en klaarzetten voor review, maar merget pas na expliciete
bevestiging van de gebruiker — tenzij de taakomschrijving de merge al
vooraf goedkeurde. Voor het huidige stadium (klein, één maintainer) is
direct committen op `main` toegestaan voor kleine, laag-risico wijzigingen
(docs, config, kleine fixes); niet-triviale features gaan via een branch.

Vóór destructieve git-operaties op reeds bestaand werk: eerst
`git status`, en stash/commit wat gevonden wordt.

## 6. MCP / Tool Permissions

**READ** — zelfstandig toegestaan:
Goedgekeurde databronnen raadplegen (bestanden, PostgreSQL, Obsidian,
logs, externe read-only APIs binnen scope van de taak).

**LOW-RISK WRITE** — zelfstandig toegestaan binnen een goedgekeurde taak:
Interne projectbestanden wijzigen, PostgreSQL development-data
schrijven/muteren, Obsidian-documentatie bijwerken, normale Git-
developmentacties (§5).

**HIGH-RISK WRITE** — vereist human approval (§8), altijd:
Geld uitgeven, productie wijzigen, externe communicatie/publicatie
namens de eigenaar, DNS-wijzigingen, credentials aanmaken/wijzigen,
destructieve database-acties (drop/truncate op productie of gedeelde
data), en vergelijkbare onomkeerbare of naar-buiten-gerichte acties.

Nieuwe MCP-servers/tools worden pas gekoppeld na expliciet verzoek van
de gebruiker, niet proactief toegevoegd "voor de zekerheid".

## 7. Kostenbewaking

Niet elke betaalde API-call vereist losse toestemming — kosten worden
via budgetten beheerd:

- **Binnen een vooraf goedgekeurd development/researchbudget:**
  Claude mag betaalde calls (LLM's, market-data providers, etc.)
  autonoom doen.
- **Bij overschrijding van het budget, of het inschakelen van een
  nieuwe betaalde provider die nog niet is goedgekeurd:** approval
  vereist vóór verdere calls.
- **Elke betaalde call wordt gelogd:** provider, model/service, en
  geschatte of werkelijke kosten waar beschikbaar (PostgreSQL, niet
  Obsidian — zie §11).
- **Hard spending caps** worden ondersteund en mogen nooit stilzwijgend
  overschreden worden; bij het bereiken van een cap stopt verdere
  spend en volgt een Telegram budget alert (§9).

## 8. Human Approval Gates

Human approval is **verplicht**, ongeacht hoe klein de individuele stap
lijkt, voor:

- material spend boven het goedgekeurde budget (§7);
- productie-deployment met materieel risico;
- DNS-/domeinwijzigingen;
- contracten/betalingen;
- credentials- of security-sensitive wijzigingen;
- destructieve productieacties;
- externe publicatie/communicatie namens de eigenaar, wanneer dit niet
  vooraf is toegestaan.

**Normale development binnen een reeds goedgekeurde taak vereist geen
micro-approval** (zie §1 Autonomie-principe en §5/§6 voor wat daaronder
valt). Approval-verzoeken lopen via Telegram (§9) of direct in de
sessie; de actie wordt pas uitgevoerd na expliciet "ja/akkoord".

Zie ook `obsidian/01 Strategy/Kill Criteria.md` voor wanneer iets
sowieso niet gebouwd moet worden, los van approval-vraag.

## 9. Telegram — Executive Alert Channel

Telegram is voor:

- high-confidence opportunities;
- approval requests (§8);
- critical system alerts (bijv. service down, herhaalde failures);
- budget alerts (§7: cap bereikt of nieuwe provider vraagt approval);
- experiment-resultaten die menselijke actie vereisen.

Geen normale logs, debug-output, of routinematige status-updates naar
Telegram — dat blijft in applicatielogs/PostgreSQL. Berichten zijn
actiegericht: wat, waarom, en welke actie gevraagd wordt. Bot token en
chat-id altijd via environment variables (§4).

## 10. PostgreSQL — System of Record

- Alle transactionele/operationele data (raw signals, collector runs,
  opportunities, scores, experimenten, kostenlogging, audit trail,
  approvals) leeft in PostgreSQL — niet in Obsidian, niet in losse
  bestanden.
- Schema-wijzigingen via de backend (models/migrations), nooit
  handmatige ad-hoc SQL tegen productiedata zonder review.
- Audit- en approval-entiteiten (reeds voorbereid in het schema)
  blijven verplicht voor elke actie die menselijke goedkeuring vereist.

## 11. Obsidian / Company Brain

- `obsidian/` bevat strategie, principes, agentbeschrijvingen,
  opportunity-duiding, beslissingen en lessons learned — géén raw
  signals of transactionele data (die horen in PostgreSQL, §10).
- Claude onderhoudt Obsidian **proactief** bij betekenisvolle
  veranderingen (nieuwe strategische keuze, statuswijziging, afgeronde
  milestone) — niet bij elke triviale wijziging (zie §16 voor de
  precieze regels rond Current State en Decisions).
- Structuur (00 Company t/m 10 Memory) blijft leidend; nieuwe notities
  in de bestaande juiste map, geen nieuwe topmappen zonder overleg.
- Secrets horen **nooit** in Obsidian (§4).
- Historische decisions worden nooit stilzwijgend herschreven — een
  gewijzigd besluit krijgt een nieuwe entry die naar het oude verwijst,
  in plaats van het oude te overschrijven.

## 12. Research Integrity

- Elke claim in research/opportunity-notities bewaart evidence en
  provenance (bron, datum, hoe verkregen).
- Onderscheid expliciet tussen **feit**, **schatting** en **inferentie**
  — vermeng deze categorieën niet stilzwijgend in dezelfde zin.
- Omzet-, traffic-, groei- of marktdata wordt nooit als feit
  gepresenteerd zonder betrouwbare bron; zonder harde bron is het een
  schatting en wordt het zo gelabeld.
- Conflicterende evidence wordt bewaard, niet weggelaten omdat het niet
  past bij de voorkeurshypothese.
- **Evidence Confidence staat los van Opportunity Score:** hoe zeker we
  zijn over de onderliggende data is een ander getal dan hoe
  aantrekkelijk de kans is — beide worden apart vastgelegd.

## 13. Market Intelligence Scope

Het systeem mag onderzoeken:

- unmet demand;
- emerging trends;
- early-adopter behavior;
- bewezen/winnende producten;
- succesvolle online businesses en businessmodellen;
- competitor weaknesses;
- underserved niches.

**Doel is nooit het letterlijk kopiëren** van producten, code, of
branding van anderen. Het doel is het analyseren van bewezen
economische logica (waarom iets werkt) om vervolgens eigen,
gedifferentieerde proposities te bouwen — met expliciete aandacht voor
IP, merkenrecht, auteursrecht, patenten en platformvoorwaarden (ToS)
van de bronnen die worden onderzocht.

## 14. Test-before-build

- Voor nieuwe functionaliteit: eerst (of gelijktijdig met) tests
  schrijven/aanpassen die het gewenste gedrag vastleggen, dan pas de
  implementatie — zeker voor scoring-logica en kill-criteria.
- Geen nieuwe feature wordt als "klaar" gerapporteerd zonder dat de
  relevante tests (pytest) zijn gedraaid en slagen.
- Voor API/servicewijzigingen: waar mogelijk smoke-test
  (`scripts/smoke_test.sh`, `/api/health`) vóór afronding.

## 15. Geen overengineering

- Bouw wat de huidige taak nodig heeft, niet wat "later misschien"
  nodig is. Geen speculatieve abstracties, feature flags, of generieke
  frameworks voor één use-case.
- Drie vergelijkbare regels code zijn beter dan een premature
  abstractie; voeg pas een agent/service/laag toe als een concreet,
  huidig probleem dat vereist.
- MVP-scope (zie README) is leidend: geen autonome spending buiten
  budget (§7), geen productie-deploy, geen DNS-wijzigingen, geen
  destructieve acties, tenzij expliciet uitgebreid door de gebruiker.

## 16. Current State en Decisions bijwerken

- `obsidian/09 Operations/Current State.md`: bijwerken bij elke
  betekenisvolle statusverandering (nieuwe capability live, infra-
  wijziging, blocker opgelost/ontstaan) — niet bij elke commit.
- `obsidian/10 Memory/Decision Timeline.md` (en `06 Decisions` waar van
  toepassing): elke niet-triviale beslissing loggen met datum, wat er
  is besloten, waarom (het "waarom" is het belangrijkste onderdeel), en
  wie akkoord gaf.
- Formaat: kort, feitelijk, chronologisch toevoegen — geen herschrijven
  van eerdere entries tenzij een fout wordt gecorrigeerd (zie §11:
  wijzigingen krijgen een nieuwe entry, niet een overschrijving).
- Bij twijfel of iets een "decision" is: als het toekomstige keuzes
  beperkt, of een omkeerbare-vs-onomkeerbare afweging bevatte, loggen.
