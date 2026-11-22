# Guide de démonstration

Déroulé d'environ **10 minutes**, avec deux temps forts : un diagnostic sur un dataset,
puis un **incident provoqué en direct**.

## Avant la démo (la veille)

- [ ] `.env` contient `LLM_PROVIDER` et une `LLM_API_KEY` **dont le quota du jour est intact**.
      Avec l'offre gratuite de Groq, ne pas lancer d'évaluation la veille, car le quota
      journalier est compté sur les 24 dernières heures.
- [ ] Prévoir une **clé de secours sur un autre compte** (Groq ou Gemini).
- [ ] `docker compose up -d --build` fonctionne, et http://localhost:8080 s'affiche.
- [ ] Une analyse de dataset a déjà tourné une fois : son rapport sert de **plan B**
      si le réseau ou le LLM fait défaut pendant la démo.

## Le jour J

```bash
docker compose up -d --build     # environ 1 minute
docker compose ps                # les 7 services doivent être « running » ou « healthy »
```

Ouvrir dans le navigateur : **l'interface** (http://localhost:8080) et **Jaeger**
(http://localhost:16686).

## Déroulé

### 1. Le problème (1 min)

> En production, un incident produit des milliers de lignes de logs, des dizaines de
> métriques et des centaines de traces. doctor_log les croise automatiquement et
> **justifie** sa conclusion.

Insister sur le principe : **les chiffres viennent de Python, jamais du LLM**.
Le LLM ne fait qu'interpréter des preuves déjà calculées.

### 2. Diagnostic sur un dataset (3 min)

1. **Nouvelle investigation**, source **Dataset enregistré**, choisir `slow-query`, puis **Lancer**.
2. Montrer le **suivi en direct** : les agents Log, Metrics et Trace tournent en parallèle,
   puis viennent le RCA et la vérification.
3. Ouvrir le **rapport** et montrer, dans l'ordre :
   - la cause probable et la **confiance** (calculée en Python) ;
   - les **preuves** (`log-…`, `met-…`, `trc-…`), chacune avec sa source et sa période ;
   - les **hypothèses concurrentes** que le système a envisagées ;
   - les **actions de vérification** et les **limites de l'analyse**.

### 3. Le piège (1 min)

Lancer `trap-missing-metrics` : une métrique clé manque.
→ doctor_log répond **« preuves insuffisantes »** au lieu d'inventer une cause.

> Un bon outil de diagnostic sait dire « je ne sais pas ».

### 4. Incident en direct (4 min)

```bash
python scripts/trigger_incident.py s01      # sature le pool de connexions pendant 60 s
```

Pendant la charge, montrer dans Jaeger les traces `orders-service` qui s'allongent
sur `db.acquire_connection`.

Une fois la charge terminée :

1. **Nouvelle investigation**, source **Live (demo-app)**, période « 5 dernières minutes », puis **Lancer**.
2. Résultat attendu : **épuisement du pool de connexions DB**, conclusion `confirmed`.

Autres scénarios possibles : `s02` (CPU), `s03` (requête lente), `s04` (payment-service en panne).

### 5. Qualité mesurée (1 min)

Montrer [`backend/eval/results.md`](../backend/eval/results.md) : **6 scénarios sur 6
correctement diagnostiqués**, les **2 pièges** gérés et **100 % des preuves citées existent**.

## En cas de problème

| Symptôme | Solution |
|---|---|
| Analyse en échec, message « quota » ou 429 | Mettre la clé de secours dans `.env`, puis `docker compose up -d backend` |
| Analyse bloquée après un redémarrage du backend | Elle passe automatiquement en échec : la relancer |
| Live : « preuves insuffisantes » | Relancer `trigger_incident.py`, attendre la fin de la charge, choisir « 15 dernières minutes » |
| Rien ne s'affiche sur le port 8080 | `docker compose ps`, puis `docker compose logs backend` |
| Réseau indisponible | Montrer le rapport du dataset déjà analysé (plan B) |

Arrêt : `docker compose down`.
