# Runbook : requête SQL lente

## Symptômes

- Logs `Slow query detected` avec une durée (`duration_ms`) élevée.
- Dans les traces, le temps propre est dans le span SQL (`SELECT`, `db.query`).
- Latence p95 en hausse ; les connexions actives peuvent monter, mais il reste des
  connexions libres (`db_connections_idle` > 0) : c'est une conséquence, pas la cause.

## Causes fréquentes

- Index manquant ou supprimé, table qui a beaucoup grossi.
- Plan d'exécution qui a changé (statistiques obsolètes).
- Verrou tenu par une autre transaction.

## Vérifications

1. Lancer `EXPLAIN ANALYZE` sur la requête citée dans les logs.
2. Vérifier les index de la table et les migrations récentes.
3. Consulter `pg_stat_activity` et `pg_locks` pendant l'incident.

## Remédiation

- Ajouter l'index manquant, lancer `ANALYZE`, réécrire la requête.
