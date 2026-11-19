# Лента вакансий kurdarbs.lv

Каждый день в 07:17 по Риге бот ищет вакансии на kurdarbs.lv по словам из `config.json`, отсеивает лишнее и обновляет страницу.

## Что где лежит

| Файл | Зачем |
|---|---|
| `config.json` | ключевые слова, исключения, города. **Единственный файл, который нужно править** |
| `scraper.py` | сам бот |
| `index.html` | страница-лента |
| `data/jobs.json` | найденные вакансии (бот обновляет его сам) |
| `.github/workflows/daily.yml` | расписание |

## Запуск (один раз, ~10 минут)

1. На GitHub: **New repository** → имя, например `vakances` → **Public** (бесплатные GitHub Pages работают только с публичными репозиториями) → Create.
2. **Add file → Upload files** → перетащите всё, кроме папки `.github`: `config.json`, `scraper.py`, `index.html`, `robots.txt`, `.nojekyll`, `README.md`, папку `data`. Commit.
3. Расписание: **Add file → Create new file** → в поле имени впишите `.github/workflows/daily.yml` (слэши сами создадут папки) → вставьте содержимое файла `daily.yml` → Commit.
4. **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, папка `/ (root)` → Save.
5. **Actions** → «Вакансии — ежедневное обновление» → **Run workflow**. Через ~5 минут появятся данные.
6. Страница будет по адресу `https://<ваш-логин>.github.io/vakances/`.

Если на шаге 5 ошибка «Permission denied» при `git push`: **Settings → Actions → General → Workflow permissions → Read and write permissions** → Save, и запустить снова.

## Как поменять слова

Откройте `config.json` на GitHub → карандаш → правьте → Commit.
- `search` — что бот вводит в поиск kurdarbs (сайт понимает только латышский и английский).
- `title` — по каким словам в **названии** вакансия попадает в группу. Русские слова тоже работают.
- `description` — слова, по которым вакансия берётся, даже если их нет в названии (помечается «по описанию»).
- `exclude_title` — если это есть в названии, вакансия отбрасывается.
- Слова до 4 букв ищутся только целиком (`AI` не найдётся внутри `maiņu`), длинные — как часть слова (`personāl` = personāla, personālvadība).

Изменения применяются со следующего запуска, в том числе к уже найденным вакансиям.
