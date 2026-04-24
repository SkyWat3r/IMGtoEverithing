# imgEMOJI

Script Python pour convertir une image en mosaïque d'emojis et produire un PNG en sortie.

Le fichier principal est [emoji_maker.py](/var/home/lbazin/PycharmProject/imgEMOJI/emoji_maker.py:1).

Le projet est maintenant rangé dans le package [imgemoji_app](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/__init__.py:1), avec :

- [imgemoji_app/gui.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/gui.py:1) pour l'interface et l'orchestration ;
- [imgemoji_app/palette.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/palette.py:1) pour la palette, les polices emoji et Twemoji ;
- [imgemoji_app/rendering.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/rendering.py:1) pour la grille et le rendu image ;
- [imgemoji_app/estimation.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/estimation.py:1) pour les estimations ;
- [imgemoji_app/constants.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/constants.py:1), [common.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/common.py:1), [cache.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/cache.py:1), [models.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/models.py:1) et [errors.py](/var/home/lbazin/PycharmProject/imgEMOJI/imgemoji_app/errors.py:1) pour le socle partagé.

Le script fonctionne maintenant de deux façons :

- sans argument : ouverture d'une interface graphique ;
- avec des arguments `--...` : exécution en ligne de commande.

## Prérequis

- Python 3
- `Pillow`
- `numpy`
- `tkinter` si vous voulez utiliser l'interface graphique

Sur Fedora, `tkinter` est généralement fourni par un paquet du type :

```bash
sudo dnf install python3-tkinter
```

Si vous utilisez le venv du projet :

```bash
.venv/bin/python3 emoji_maker.py --input images/pcgb20_0589_fine.png --output result.png --columns 80
```

## Fonctionnement

Le script :

1. charge une image d'entrée ;
2. la redimensionne vers une grille ;
3. compare chaque case à une palette d'emojis ;
4. rend un PNG final composé d'emojis.

Le rendu des emojis peut se faire de deux façons :

- via une police emoji locale ;
- via des images Twemoji téléchargées en ligne puis mises en cache dans `.emoji_cache/twemoji/72x72`.

Le script maintient aussi un historique local des rendus dans `.emoji_cache/render_history.json` pour estimer le temps des prochains rendus.

## Commande minimale

```bash
.venv/bin/python3 emoji_maker.py \
  --input images/pcgb20_0589_fine.png \
  --output result.png \
  --columns 80
```

## Interface graphique

Lancez simplement :

```bash
.venv/bin/python3 emoji_maker.py
```

Si `tkinter` est disponible, une fenêtre s'ouvre pour choisir :

- le fichier d'entrée ;
- le fichier de sortie ;
- le nombre de colonnes et lignes ;
- la taille des emojis ;
- la palette ;
- le fond ;
- la police ;
- la source des emojis ;
- le seuil alpha ;
- l'option d'étirement.
- les paramètres de vidéo progressive.

L'interface est maintenant découpée proprement :

- une zone de paramètres communs ;
- un onglet `Image -> Image` ;
- un onglet `Image -> Vidéo` ;
- un onglet `Vidéo -> Vidéo` laissé vide pour les prochains champs / futures fonctionnalités.

Elle affiche aussi :

- une estimation du temps de rendu selon l'onglet actif ;
- le nombre de cases/emojis à traiter pour le rendu image ;
- une estimation qui devient plus fiable après plusieurs rendus.

L'onglet `Image -> Vidéo` permet de générer une animation où l'image devient progressivement moins pixelisée en augmentant le nombre de cases emoji.

Si `tkinter` n'est pas installé, le script affiche un message d'erreur explicite et vous pouvez continuer à utiliser le mode CLI.

## Paramètres

### `--input`

Chemin de l'image source.

Exemple :

```bash
--input images/photo.png
```

Obligatoire.

### `--output`

Chemin du PNG généré.

Exemple :

```bash
--output result.png
```

Obligatoire.

### `--columns`

Nombre de colonnes d'emojis dans le rendu final.

Plus la valeur est grande, plus l'image sera détaillée, mais plus le fichier sera lourd et le rendu lent.

Exemple :

```bash
--columns 120
```

### `--rows`

Nombre de lignes d'emojis dans le rendu final.

Si vous donnez uniquement `--rows`, le script calcule automatiquement le nombre de colonnes pour conserver les proportions.

Exemple :

```bash
--rows 60
```

### `--emoji-size`

Taille d'une case emoji dans l'image finale, en pixels.

Valeur par défaut : `20`

Exemple :

```bash
--emoji-size 24
```

Si vous augmentez cette valeur, l'image finale aura une plus grande résolution.

### `--scale`

Facteur utilisé quand ni `--columns` ni `--rows` ne sont fournis.

Valeur par défaut : `1.0`

Exemple :

```bash
--scale 1.5
```

Utile pour laisser le script calculer automatiquement la densité de la grille.

### `--palette`

Palette d'emojis à utiliser pour reconstruire l'image.

Deux formats sont acceptés :

- une liste d'emojis séparés par des virgules ou des espaces ;
- un chemin vers un fichier texte contenant les emojis.

Exemples :

```bash
--palette "🟥,🟧,🟨,🟩,🟦,🟪,⬛,⬜"
```

```bash
--palette palette.txt
```

Si ce paramètre n'est pas fourni, le script utilise une palette par défaut intégrée.

### `--background`

Couleur de fond utilisée pour les zones vides.

Valeur par défaut : `transparent`

Formats acceptés :

- `transparent`
- un nom de couleur comme `white` ou `black`
- une couleur hexadécimale comme `#112233`

Exemples :

```bash
--background transparent
```

```bash
--background white
```

```bash
--background "#1a1a1a"
```

### `--font`

Chemin vers une police emoji à utiliser explicitement.

Exemple :

```bash
--font /chemin/vers/NotoColorEmoji.ttf
```

À utiliser surtout si vous voulez forcer un rendu par police locale.

Remarque :
sur certaines distributions Linux, les polices emoji modernes ne sont pas correctement rendues par Pillow. Dans ce cas, préférez `--emoji-source twemoji`.

### `--emoji-source`

Choisit la source de rendu des emojis.

Valeurs possibles :

- `auto`
- `font`
- `twemoji`

Valeur par défaut : `auto`

Détail :

- `auto` : essaie d'abord une police locale couleur ; si ce n'est pas exploitable, bascule vers Twemoji ;
- `font` : force l'utilisation d'une police locale ;
- `twemoji` : force l'utilisation des assets Twemoji téléchargés en ligne.

Exemples :

```bash
--emoji-source auto
```

```bash
--emoji-source font
```

```bash
--emoji-source twemoji
```

Important :
le mode `twemoji` nécessite un accès réseau au premier lancement pour télécharger les emojis manquants.

## Estimation du temps

Avant chaque rendu, le script essaie d'estimer le temps nécessaire.

Cette estimation est basée sur :

- le nombre total de cases dans la grille ;
- la taille des emojis ;
- la taille de la palette ;
- la source de rendu choisie ;
- les statistiques réelles des rendus précédents.

Après chaque image générée, le script enregistre :

- le temps réel ;
- le nombre total de cases ;
- le nombre d'emojis effectivement dessinés ;
- la taille de la grille ;
- la taille de la palette ;
- la source utilisée.

Plus vous lancez de rendus, plus l'estimation devient utile.

En mode CLI, vous verrez deux lignes :

```text
Estimate: ...
Done: ...
```

En mode graphique, l'estimation apparaît dans la fenêtre et le temps réel est affiché à la fin du rendu.

### `--stretch`

Autorise une déformation de l'image si `--columns` et `--rows` sont fournis en même temps.

Sans `--stretch`, le script refuse cette combinaison pour éviter de casser le ratio d'origine.

Exemple :

```bash
--columns 120 --rows 80 --stretch
```

### `--alpha-threshold`

Seuil entre `0.0` et `1.0` pour décider si une case est considérée comme vide selon son alpha.

Valeur par défaut : `0.05`

Exemple :

```bash
--alpha-threshold 0.15
```

Plus la valeur est élevée, plus les zones semi-transparentes risquent d'être ignorées.

### `--video-output`

Chemin de sortie de l'animation.

Formats pris en charge :

- `.gif` sans dépendance externe ;
- `.mp4` si `ffmpeg` est installé.

Exemples :

```bash
--video-output progression.gif
```

```bash
--video-output progression.mp4
```

### `--video-fps`

Nombre d'images par seconde pour l'animation.

Valeur par défaut : `5`

Exemple :

```bash
--video-fps 5
```

### `--video-start-columns`

Nombre de colonnes au début de la vidéo.

Valeur par défaut : `1`

Exemple :

```bash
--video-start-columns 1
```

### `--video-max-columns`

Nombre de colonnes à la fin de la vidéo.

Si ce paramètre est absent, le script utilise :

- `--columns` si fourni ;
- sinon `500`.

Exemple :

```bash
--video-max-columns 500
```

### `--video-step-columns`

Pas entre deux frames de la vidéo.

Valeur par défaut : `2`

Avec `1 -> 11` et un pas de `2`, le script produit :

```text
1, 3, 5, 7, 9, 11
```

Exemple :

```bash
--video-step-columns 2
```

Ce réglage est pratique pour un effet de dépixelisation progressif.

## Exemples complets

### Rendu standard

```bash
.venv/bin/python3 emoji_maker.py \
  --input images/pcgb20_0589_fine.png \
  --output result.png \
  --columns 80
```

### Rendu forcé avec Twemoji

```bash
.venv/bin/python3 emoji_maker.py \
  --input images/pcgb20_0589_fine.png \
  --output result.png \
  --columns 80 \
  --emoji-source twemoji
```

### Palette réduite

```bash
.venv/bin/python3 emoji_maker.py \
  --input images/pcgb20_0589_fine.png \
  --output result.png \
  --columns 100 \
  --emoji-size 18 \
  --palette "⬛ ⬜ 🟥 🟨 🟦 🟩"
```

### Fond blanc

```bash
.venv/bin/python3 emoji_maker.py \
  --input images/pcgb20_0589_fine.png \
  --output result.png \
  --columns 100 \
  --background white
```

### Vidéo progressive en GIF

```bash
.venv/bin/python3 emoji_maker.py \
  --input image.png \
  --output result.png \
  --emoji-source twemoji \
  --video-output progression.gif \
  --video-fps 5 \
  --video-start-columns 1 \
  --video-max-columns 500 \
  --video-step-columns 2
```

Pour une image carrée, cela produit typiquement une progression du type :

```text
1x1, 3x3, 5x5, 7x7, 9x9, ...
```

L'effet visuel est une image d'abord très grossière, puis de plus en plus détaillée.

## Cache Twemoji

Quand `--emoji-source twemoji` ou `--emoji-source auto` utilise Twemoji, les images téléchargées sont stockées ici :

```text
.emoji_cache/twemoji/72x72
```

Cela évite de retélécharger les mêmes emojis à chaque exécution.

Les statistiques d'estimation sont stockées ici :

```text
.emoji_cache/render_history.json
```

## Vidéo et animation

Le bouton `Créer la vidéo` dans l'interface graphique génère une animation progressive à partir de l'image choisie.

Fonctionnement :

- chaque frame est un rendu emoji complet de la même image ;
- le nombre de colonnes augmente à chaque étape ;
- la hauteur est recalculée automatiquement pour garder les proportions ;
- toutes les frames sont centrées sur une même taille de canevas pour éviter les sauts visuels.

Par défaut :

- `1` colonne au départ ;
- pas de `2` ;
- `5` FPS ;
- sortie GIF.

Si `ffmpeg` est installé plus tard, vous pourrez aussi exporter en MP4.

## Dépannage

### Le rendu contient des carrés ou des symboles bizarres

La police locale utilisée ne sait pas afficher correctement les emojis.

Solution recommandée :

```bash
--emoji-source twemoji
```

### Le script échoue en mode `twemoji`

Cause probable :

- pas d'accès réseau ;
- DNS bloqué ;
- environnement sandboxé.

Dans ce cas, il faut autoriser l'accès réseau ou préremplir le cache `.emoji_cache/twemoji/72x72`.

### L'image finale est très grosse

Réduisez :

- `--columns` ou `--rows`
- `--emoji-size`

### Le rendu visuel n'est pas très fidèle

Le résultat dépend fortement de la palette choisie.

Pour améliorer le rendu :

- utilisez une palette plus adaptée à votre image ;
- augmentez le nombre de colonnes ;
- testez plusieurs tailles d'emojis.

## Résumé rapide

Commande recommandée dans l'état actuel du projet :

```bash
.venv/bin/python3 emoji_maker.py \
  --input images/pcgb20_0589_fine.png \
  --output result.png \
  --columns 80 \
  --emoji-source twemoji
```
