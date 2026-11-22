// Petites fonctions d'affichage partagées par les pages.

export const formatDate = (iso: string) =>
  new Date(iso).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Erreur inattendue";
