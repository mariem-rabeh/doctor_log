# Runbook : service dépendant indisponible

## Symptômes

- Logs d'erreur `ConnectionRefused`, `ConnectError` ou réponses 503 vers un autre service
  (par exemple `payment-service`).
- Métrique `error_rate` en forte hausse ; `up` à 0 pour le service appelé.
- Dans les traces, l'erreur naît dans le span d'appel HTTP vers la dépendance ; les
  parents ne font que la propager.

## Causes fréquentes

- Service arrêté, redémarrage en boucle, déploiement raté.
- Problème réseau ou DNS entre les deux services.

## Vérifications

1. Vérifier l'état du service appelé (`docker compose ps`, healthcheck).
2. Lire ses propres logs au début de l'incident.
3. Tester l'appel à la main depuis le service appelant.

## Remédiation

- Redémarrer ou restaurer la dépendance.
- Ajouter un timeout court et un circuit breaker côté appelant.
