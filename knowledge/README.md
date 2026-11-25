# Base de connaissances (RAG)

Procédures d'exploitation (runbooks) que le RCA reçoit comme **documents de référence**.
Pendant une analyse, le nœud `retrieve` choisit les documents les plus proches des preuves
mesurées et les ajoute au prompt du RCA, avec les identifiants `doc-001`, `doc-002`...

Règles :

- Un document est une **connaissance**, jamais une **preuve** : il ne compte pas dans la
  confiance et ne peut pas soutenir une hypothèse.
- Un fichier `.md` par sujet ; la première ligne `# Titre` donne son titre.
- Ne jamais copier ici un `expected_diagnosis.json` des datasets : l'évaluation donnerait
  alors la réponse au système.

Ce fichier README n'est pas indexé.
