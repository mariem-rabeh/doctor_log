# Runbook : pool de connexions à la base épuisé

## Symptômes

- Logs `QueuePool limit reached` ou `Database connection timeout`.
- Métrique `db_connections_active` au maximum du pool et `db_connections_idle` à 0.
- Dans les traces, le temps est perdu dans `db.acquire_connection` (attente d'une
  connexion), alors que les requêtes SQL elles-mêmes restent rapides.

## Causes fréquentes

- Connexions non rendues au pool (session ouverte sans `close`, transaction longue).
- Pic de trafic supérieur à la taille du pool (`pool_size` + `max_overflow`).

## Vérifications

1. Comparer `pool_size` à la valeur maximale de `db_connections_active`.
2. Chercher dans le code les sessions qui ne sont pas fermées (bloc `with`, `finally`).
3. Vérifier côté PostgreSQL les connexions `idle in transaction`.

## Remédiation

- Court terme : augmenter temporairement `pool_size`, redémarrer le service.
- Long terme : corriger la fuite de connexions, ajouter un `pool_timeout` court.
