# README screenshots

The images the main README shows. Capture them from a real instance (real
posters, real libraries), at the sizes below, so the README looks consistent.

**How:** Chrome or Edge → DevTools (F12) → device toolbar (Ctrl+Shift+M) →
set the size → Ctrl+Shift+P → "Capture screenshot" (the visible area) or
"Capture full size screenshot". 100% zoom, dark theme, signed in as an admin.
Wait for posters and stills to load before capturing.

| File | Size | What to show |
|---|---|---|
| `home.png` | 1600 × 1000 | Home, scrolled to the top: the hero with a backdrop, the recently-added rail, downloads |
| `library.png` | 1440 × 900 | A TV library section: the poster grid, the filter row, the sidebar with every library |
| `show.png` | 1440 × 900 | A show with a backdrop and episode stills (season cards + the first episodes in view) |
| `search.png` | 1440 × 900 | ⌘K open with a query that hits both your library and TMDb (e.g. `american`) |
| `releases.png` | 1440 × 900 | The release-search window for a missing episode, with ranked and greyed-out results |
| `settings.png` | 1440 × 900 | Settings → Libraries (green "move in instantly" cards) or → Integrations (indexer health) |
| `discover.png` | 1440 × 900 | Discover → TV shows, a couple of rails |
| `mobile.png` | 390 × 844 | Home or a show page on a phone (device toolbar → iPhone 12/13/14) |

Before committing, shrink them (keeps the repository small):

```bash
pngquant --quality=70-90 --ext .png --force docs/screenshots/*.png   # apt install pngquant
```
