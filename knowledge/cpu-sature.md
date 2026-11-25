# Runbook : CPU saturé

## Symptômes

- Métrique `cpu_usage` durablement au-dessus de 0,9 sur le service.
- Latence en hausse sur toutes les opérations du service, y compris celles sans base
  de données : le temps propre augmente partout, pas sur une seule étape.

## Causes fréquentes

- Boucle coûteuse ou calcul lourd introduit par un déploiement.
- Trafic bien supérieur à la capacité, trop peu d'instances.

## Vérifications

1. Vérifier que `cpu_usage` dépasse vraiment 0,7 ; sinon, chercher une autre cause.
2. Comparer l'heure du début avec les derniers déploiements.
3. Profiler le service (py-spy) sur l'opération la plus lente.

## Remédiation

- Court terme : ajouter des instances, revenir à la version précédente.
- Long terme : optimiser le code identifié par le profilage.
