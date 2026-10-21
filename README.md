# keep-to-memos

Convertit un export **Google Takeout (Keep)** en archive importable dans **[Memos](https://github.com/usememos/memos) 0.31**, depuis l'interface (*Paramètres → Export & Import → Import memos*).

Le script travaille hors ligne : il ne contacte pas Memos et n'a besoin d'aucun jeton. Il produit un `.zip` au **Memos Export Format 1.0**, le format des exports de Memos ([spécification](https://github.com/usememos/memos/blob/v0.31.0/docs/design/memos-export-format.md)).

Il utilise uniquement la bibliothèque standard de Python (≥ 3.9 ; testé avec 3.9 et 3.12).

## 1. Exporter Keep avec Google Takeout

1. Ouvrir <https://takeout.google.com>.
2. Cliquer sur **Tout désélectionner**, puis cocher seulement **Keep**.
3. **Étape suivante** → export unique, au format `.zip` → **Créer une exportation**.
4. Télécharger l'archive reçue par e-mail et la décompresser. Le dossier utile est `Takeout/Keep/` : un `.json` et un `.html` par note, plus les images et enregistrements joints.

## 2. Générer l'archive Memos

```sh
# 1. Relire le résultat sans rien écrire
./keep_to_memos.py ~/Downloads/Takeout/Keep --dry-run | less

# 2. Faire un essai avec quelques notes
./keep_to_memos.py ~/Downloads/Takeout/Keep --limit 5 -o essai.zip

# 3. Archive complète
./keep_to_memos.py ~/Downloads/Takeout/Keep -o keep.zip
```

| Option | Défaut | Effet |
|---|---|---|
| `-o`, `--output` | `memos-export-keep-<horodatage>.zip` | fichier produit |
| `--visibility` | `private` | `private`, `protected` ou `public` |
| `--tag` | `keep` | tag ajouté à toutes les notes ; `--tag ""` le désactive |
| `--include-trashed` | non | inclut la corbeille Keep (memos archivés) |
| `--include-archived` / `--skip-archived` | include | notes archivées de Keep |
| `--no-attachments` | (incluses) | n'embarque pas les images et enregistrements |
| `--username` | `keep` | nom inscrit dans l'archive, purement informatif |
| `--limit N` | — | seulement les N premières notes, par date de création |
| `--dry-run` | non | affiche le markdown de chaque note, n'écrit rien |

Une ligne par note : `[ok]`, `[warn]` (note incluse avec un avertissement), `[skip]` (vide, corbeille, archivée) ou `[fail]` (JSON illisible), puis un résumé.

Codes de sortie : `0` tout est bon, `1` archive écrite avec au moins un avertissement ou un JSON illisible, `2` erreur d'usage (dossier introuvable, aucune note).

## 3. Importer dans Memos

1. Se connecter avec le compte qui doit recevoir les notes. Les memos lui appartiendront, quel que soit le `--username`.
2. **Paramètres → Export & Import → Import memos**, puis choisir le `.zip`.
3. Memos affiche un aperçu (nombre de memos nouveaux ou déjà présents). Confirmer.

Procédure conseillée : importer d'abord `essai.zip` (`--limit 5`) et vérifier dans Memos les dates, les cases à cocher, les tags et les images. Importer ensuite l'archive complète. Les 5 notes déjà importées sont simplement sautées.

### Réimport sans doublon
Chaque note reçoit un identifiant déterministe, calculé à partir du nom de son fichier `.json`. Réimporter la même archive, ou une archive régénérée depuis le même Takeout, ne crée donc pas de doublons : Memos saute les memos déjà présents (politique « SKIP » par défaut). La politique « REPLACE » met à jour les memos existants avec le contenu de l'archive.

### Annuler un import
Toutes les notes portent le tag `#keep` (sauf avec `--tag ""`). Dans Memos, cliquer sur ce tag pour filtrer les notes importées, puis les supprimer. Penser aussi à la vue **Archivées**.

## Conversion

- Titre → `# Titre`, texte recopié tel quel, cases à cocher → `- [ ]` / `- [x]`.
- Liens détectés par Keep, s'ils ne figurent pas déjà dans le texte → `- [titre](url)`.
- Libellés → tags sur la dernière ligne (`Courses Été 2024!` → `#courses-été-2024`, `Maison/Réseau` → `#maison/réseau`).
- Création et dernière modification : conservées.
- Épinglée → épinglée. Archivée → archivée. Corbeille → ignorée, ou archivée avec `--include-trashed`.
- Images et enregistrements audio joints : inclus dans l'archive. Si `.jpeg` est annoncé mais que le fichier est en `.jpg`, il est quand même retrouvé.

## Limites connues

- **Taille des notes** : Memos refuse par défaut les notes de plus de 8 Kio. Le script les signale en `[warn]`. À l'import, elles apparaissent en échec, sans bloquer les autres. Un administrateur peut relever la limite (*Paramètres → Memo*) avant l'import.
- **Taille des fichiers joints** : 32 Mio par défaut côté Memos.
- **Imports limités** à environ 10 par heure et par compte côté Memos (chaque import compte deux fois : aperçu puis import).
- Perdus : couleurs, rappels, collaborateurs et partages Keep.
- Les fichiers `.html` du Takeout ne sont pas lus.
- Memos supprime les métadonnées EXIF des images à l'import.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

`tests/memoexport_validator.py` reprend en Python les règles de lecture de Memos (`core/memoexport`). Il est calibré sur l'archive de référence du dépôt Memos (`tests/fixtures/memos-golden-1.0.zip`, licence MIT). Les notes de `tests/fixtures/Keep/` sont inventées.
