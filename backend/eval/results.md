# Résultats de l'évaluation

Fournisseur LLM : `groq`

| Dataset | Exéc. | Cause correcte | Cohérence | Ancrage | Preuves clés | Durée | Conclusions |
|---|---|---|---|---|---|---|---|
| cpu-overload | 1 | 100% | 100% | 100% | 100% | 46 s | probable ×1 |
| db-pool-exhaustion | 1 | 100% | 100% | 100% | 100% | 56 s | confirmed ×1 |
| dependency-down | 1 | 100% | 100% | 100% | 100% | 43 s | confirmed ×1 |
| slow-query | 1 | 100% | 100% | 100% | 100% | 56 s | confirmed ×1 |
| trap-misleading-logs | 1 | 100% | 100% | 100% | 100% | 37 s | confirmed ×1 |
| trap-missing-metrics | 1 | 100% | 100% | 100% | 100% | 39 s | insufficient_evidence ×1 |

- **Précision globale de la cause** : 100%
- **Pièges correctement gérés** : 100%
- **Ancrage des preuves** (ids cités qui existent) : 100%
- **Latence moyenne** : 46 s par analyse

Cause correcte : mots-clés attendus présents et aucune cause interdite citée ; pour un piège, conclusion prudente et confiance sous le plafond attendu.
