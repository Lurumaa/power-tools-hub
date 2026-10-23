# Power Tools Hub: документ передачі для розробників

Для розробників, які переносять хаб усередину Kaleido. Короткий огляд лежить у [README.md](../README.md), тут детальний довідник.

- Робочий прототип: артефакт claude.ai https://claude.ai/artifact/6A2gzYgdr9Kk1Heeu58ShH. Це одна HTML-сторінка
  (`skillhub/index.html`), дані й файли живуть у базі та сховищі артефакту.
- Детальна специфікація canvas-сабів і переходів (формат, API `s`, пісочниця, перевірки, експорт):
  [`skillhub/CANVAS_SUBS_SPEC.md`](../skillhub/CANVAS_SUBS_SPEC.md).
- Вивантаження бібліотеки: `data/library/`, файли, на які вона посилається: `data/assets/` (розділ 7).

## 1. Що це і навіщо

**Power Tools Hub** ("Ready-made tools, built by the team") — спільна бібліотека готових інструментів рекламної команди:
скіли Claude, стилі субтитрів, переходи і музика. У кожного елемента є живе прев'ю в картці та машиночитаний JSON.
За цим JSON Claude співробітника застосовує пресет до відео локальними скриптами (розділ 4).

- Співробітник пише в чат «use subs X», «use transition X» або «use music X». Claude бере документ X з бази хабу
  і запускає потрібний скрипт.
- Якщо в Claude немає доступу до хабу, співробітник тисне в картці **Copy for Claude** і вставляє текст у чат.
  У тексті є команда і сам JSON.
- Новий пресет додається без змін платформи: форма або JSON, зокрема написаний будь-яким Claude за шаблоном.
  Перед Publish хаб перевіряє пресет. Уся логіка пресета живе в даних: параметри, GLSL переходу або JS-код canvas-стилю.

Інтерфейс хабу англійською, чотири вкладки: Skills, Subtitles, Transitions, Music.

## 2. Розділи і вміст

### Skills (`library`): 1 елемент

| name | що робить |
|---|---|
| `stickman-talking` | реклама з мультяшним персонажем / стікменом, що говорить: хук і баді, ліпсинк через Kaleido Omni, збирання в локальному редакторі |

Картка скіла показує приклад-відео (обов'язкове), SKILL.md, результат перевірки формату і розбір від Claude.
Скіл можна завантажити як .zip (**Download .zip**) або скопіювати SKILL.md.
Скрипти, на які посилається цей скіл, лежать у репозиторії: `podcast/stick/*.py`, `podcast/formats/stick_talking.json`, `podcast/pod.py`.

### Subtitles (`sublib`): 12 стилів

Саби лише вертикальні **9:16 (1080×1920)**. Перемикача формату в сабів немає, перевірка перед Publish теж іде тільки в 9:16.

| name | title | engine | вигляд |
|---|---|---|---|
| `karaoke-soft` | Soft karaoke | editor | білий Poppins Bold; сказане слово жовте і ×1.12; щільна м'яка тінь. Стиль за замовчуванням для роликів з голосом |
| `hormozi` | Hormozi | editor | капс із товстою чорною обводкою; слова заливаються жовтим у міру мовлення |
| `word-pop` | Word pop | editor | фіолетова плашка стрибає під сказане слово, білий капс з обводкою |
| `coach-marker` | Coach marker | coach | 1–2 рядки капсом, жовтий маркер ковзає під словом і дугою перестрибує на наступний рядок |
| `lilac-word` | Lilac word | word | по одному слову в центрі кадру, бузковий Inter Bold, обводка і тінь |
| `live-roll` | Live Roll | motion · roll | слова «друкуються» в міру мовлення, готові рядки їдуть угору, попередній рядок приглушений |
| `mask-slide` | Mask Slide | motion · mask | великий щільний шрифт; рядки виїжджають з-за невидимої кромки |
| `poster-stack` | Poster Stack | motion · stack | фраза стає «постером»: рядки по 1–3 слова вирівняні по ширині |
| `serif-accent` | Serif Accent | motion · accent | ключові слова (користь, результат, числа) автоматично стають жовтим курсивом із зарубками |
| `red-wipe` | Red Wipe | motion · wipe | капс Anton; кожен рядок відкриває коса червона смуга |
| `red-wipe-v2` | Red Wipe (my version) | motion · wipe | варіант Red Wipe від учасника команди: та сама механіка, смуга фіолетова (`fx.color` `#4d3681`) |
| `lava-wave` | Lava Wave | canvas | розплавлена лава тече від слова до слова (увесь код малювання всередині пресета) |

### Transitions (`translib`): 10 GLSL-шейдерів

У вікні пресета (картка і редактор) є перемикач **9:16 / 16:9**. Вікно прев'ю завжди лишається 9:16; кадр 16:9 показується
смугою по центру, а поля над і під нею закриті світлою сіро-зеленою вуаллю, без ліній по краях.
Для прев'ю використовуються намальовані кодом статичні кадри A і B, можна підставити свої (вони лишаються в браузері).

| name | duration, s | anchor | параметри | суть |
|---|---|---|---|---|
| `impact-flash` | 0.45 | 0.30 | flash, shake, chroma, kick | наїзд, однокадровий білий спалах на стику, B приземляється з трусом |
| `lens-warp` | 0.50 | 0.50 | warp, zoom, chroma, blur | «прохід крізь лінзу»: бочка, хроматизм, радіальне розмиття |
| `light-leak` | 0.60 | 0.50 | tint, intensity, direction, grain | білий засвіт плівки ховає стик |
| `liquid-morph` | 0.50 | 0.50 | scale, turbulence, edge | A розмивається, як фарба у воді, B піднімається за чорнильним фронтом |
| `luma-dissolve` | 0.47 | 0.50 | softness, invert, glow | розчинення за яскравістю; порядок обирається за кадром, що йде, тому A→B і B→A читаються однаково |
| `shake` | 0.45 | 0.50 | intensity, hits, blur, rotation, chroma | ручна тряска по одній плавній траєкторії |
| `slices` | 0.50 | 0.50 | count, angle, stagger, blur | кадр ріжеться на похилі смуги, які розлітаються |
| `spin` | 0.48 | 0.50 | turns, blur, zoom, dir | обертальний whip з кутовим розмиттям |
| `whip-pan` | 0.42 | 0.50 | angle, blur, tilt, settle | різка панорама, стик на піку швидкості |
| `zoom-punch` | 0.36 | 0.55 | zoom, blur, bounce | прискорений зум і радіальне розмиття, B з м'яким перельотом |

### Music (`musiclib`): 12 треків

Аудіо зберігається як AAC усередині .mp4: сховище артефакту не приймає «голе» аудіо. Цифри треку хаб вимірює сам під час завантаження.

| name | BPM | довжина, s | drops, s | LUFS | rights |
|---|---|---|---|---|---|
| `ashes-on-the-windowsill` | 99.9 | 42.67 | 20.3 | −13.5 | factory (у хабі лише 42-секундний фрагмент) |
| `beyond-the-stars` | 120 | 224.44 | 28.37 | −11.4 | unknown |
| `big-shot` | 101.8 | 161.83 | 9.49, 140.38 | −11.1 | unknown (джерело не вказане) |
| `hey-all-good-folks` | 103.4 | 86.71 | 57.84 | −9.3 | unknown |
| `minimal-signals` | 122.9 | 149.96 | 85.02, 116.75 | −13.7 | factory |
| `motivation-feel-good` | 149.9 | 97.65 | 14.42, 64.85 | −10.2 | no-cid |
| `motivation` | 100 | 144.46 | немає | −6.8 | factory |
| `peaceful-flow` | 141 | 203.21 | 40.71, 108.4 | −14.9 | factory |
| `rise-of-the-hero` | 134.4 | 193.37 | 8.42, 49, 185.82 | −14.1 | factory |
| `shadows-halloween` | 108 | 124.68 | 41.65, 78.61, 104.7 | −11.9 | unknown (джерело не вказане) |
| `simple-life` | 84.2 | 217.9 | 46.61, 188.71 | −15.0 | unknown |
| `sunrise` | 115.2 | 165.6 | 49.35, 98.83 | −14.1 | unknown |

## 3. Модель даних

### 3.1 Сховище і права

У кожного розділу є колекція власника хабу (бібліотека) і особисті піддерева авторів.

| розділ | бібліотека (пише лише власник) | особисте (пише лише сам автор) | індекс-документ автора |
|---|---|---|---|
| Skills | `library/<name>` | `skills/<uid>/items/<name>` | `skills/<uid>` = `{updatedAt}` |
| Subtitles | `sublib/<name>` | `subs/<uid>/items/<name>` | `subs/<uid>` |
| Transitions | `translib/<name>` | `trans/<uid>/items/<name>` | `trans/<uid>` |
| Music | `musiclib/<name>` | `music/<uid>/items/<name>` | `music/<uid>` |

- id документа дорівнює `name` (kebab-case). В інтерфейсі ключ елемента має вигляд `<scope>~<name>`, де scope — `lib` або uid.
- Читання: кожен розділ слухає бібліотеку, індекс-колекцію (за нею знаходить авторів) і `…/<uid>/items` кожного автора.
- Запис: власник пише в бібліотеку, решта — лише у своє піддерево. Це забезпечують правила бази, які задаються під час
  публікації артефакту (capabilities). Перевірено: користувач із правом Editor не може писати в колекції власника.
- Автором елементів бібліотеки в інтерфейсі підписано **Power Tools Hub**. В особистих елементах показується `credit` або автор.
- Змінювати й видаляти елемент може його автор або власник хабу (`canManage`). Видалення діє для всіх:
  - у скіла разом із документом видаляються відео і постер;
  - у треку видаляється його аудіо, якщо інший документ на нього не посилається;
  - нова версія відео чи аудіо видаляє старий файл.
- Ім'я унікальне між авторами: Publish блокується, якщо таке `name` вже є в іншого автора. Своє ім'я публікується як нова версія.
- Кнопка **Make my version** копіює чужий пресет у своє піддерево (є в сабів і переходів).
- Зараз усі особисті піддерева порожні, увесь вміст лежить у бібліотечних колекціях.
- Рантайм claude.ai, на якому стоїть хаб: `window.claude.use('db' | 'user' | 'assets' | 'downloads' | 'sample')`.
  - `user.me()` повертає `{id, isOwner, canEdit}`, `user.can('data.write')` перевіряє право запису;
  - `assets.upload/delete`; файл читається за `/_blob/<assetId>`;
  - `downloads.save` зберігає .zip скіла або .m4a треку;
  - `sample.json` будує розбір скіла від Claude.

  Поза claude.ai сторінка працює в режимі offline: перевірка формату скіла доступна, даних немає.
- Обмеження: документ до ~256 КБ (у скіла до 250 КБ, з них 200 КБ тексту), файл у сховищі до 20 МБ, шрифт до 8 МБ.

### 3.2 Службові поля (в усіх колекціях)

| поле | тип | примітка |
|---|---|---|
| `name` | string, обов'язково | `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 64; так елемент називають у чаті |
| `title` | string, обов'язково | ≤ 60 (у музики ≤ 80) |
| `description` | string | одне-два речення: як виглядає і коли застосовувати |
| `credit` | string? | підпис джерела, ≤ 60 (наприклад `Power Tools Hub`, `editor`, `word_captions.py`) |
| `author`, `updatedBy` | uid \| null | у вивантаженні `data/library/` скрізь `null` |
| `createdAt`, `updatedAt` | ISO-рядок | |
| `version` | int | +1 за кожного збереження |
| `history` | `[{v, at, by, note}]` | останні 30 записів; у вивантаженні прибрано |

### 3.3 Subtitles (`sublib`)

Спільне поле для всіх стилів: `engine` (обов'язково), одне з `editor | coach | word | motion | canvas`.

**editor / coach / word / motion** зберігають стиль у `st` і положення в `place`. Діапазони нижче взяті з `validatePreset()`;
за межами діапазону Publish блокується.

| поле | тип / діапазон | приклад |
|---|---|---|
| `st.font` | string; вбудовані шрифти: Geist, Inter, Inter Tight, Manrope, Bricolage Grotesque, Instrument Serif, Fraunces, Playfair Display, Montserrat, Poppins, Roboto, Rubik, Oswald, Anton, Bebas Neue | `"Poppins"` |
| `st.weight` | 100–900 | `700` |
| `st.size` | 12–300 px на кадрі 1080×1920 | `72` |
| `st.align` / `st.valign` | `left·center·right` / `top·middle·bottom` | `"center"` |
| `st.tracking` / `st.leading` | −200…500 (1/1000 em) / −100…200 px | `0` |
| `st.bold`, `italic`, `caps`, `underline` | bool | |
| `st.fill` | `{on, color:#hex}` | `{on:true, color:"#ffffff"}` |
| `st.stroke` | `{on, color, width 0–40, join}` | `{on:true, color:"#000000", width:9, join:"round"}` |
| `st.shadow` | `{on, color, op 0–200, angle 0–360, dist 0–100, blur 0–100}` | glow = dist 0 + яскравий колір |
| `st.bg` (плашка) | `{on, color, op 0–100, radius 0–200, pad, padX/padY 0–200, linkPad, mode lines·block, target caption·word, shadow{…}}` | у word-pop `target:"word"` |
| `st.kar` (підсвітка слова) | `{on, color, mode word·fill, scale 50–250 %}` | `{on:true, color:"#ffdd00", mode:"word", scale:112}` |
| `st.depth` (необов'язково) | `{on, size, angle, color, color2}`: 3D-видавлювання літер | малює лише локальний редактор; прев'ю хабу його не малює |
| `place` | `{py 0–1920, boxW 100–1080, boxH 50–1920}` | `{py:1152, boxW:950, boxH:360}` |
| `fontAsset` | assetId? | свій файл шрифту (.ttf/.otf/.woff/.woff2) |

Поля, які є лише в окремих рушіїв:
- **coach**: `marker = {color, padX 0–100, padY 0–100, outline 0–20, darkUnder bool, maxw 300–1080}`. Плашки й підсвітку рушій ігнорує.
- **word**: з `st` беруться шрифт, розмір, заливка, обводка, тінь, caps і `place.py`. Плашок і підсвітки немає.
- **motion**: `fx.kind` = `accent | roll | mask | stack | wipe` разом зі своїми полями:
  - accent: `{font, weight, italic, color, scale}`
  - roll: `{lines, dim}`
  - mask: `{stagger, dur}` у мс
  - stack: `{width, max}`
  - wipe: `{color, slant, exit}`

  Логіка анімації зашита в коді хабу (`drawSubFrame`), пресет задає лише параметри.

**canvas** не використовує `st` і `place`: стиль сам несе код малювання (повністю описано в `skillhub/CANVAS_SUBS_SPEC.md`).

| поле | тип / правило | приклад |
|---|---|---|
| `runtime` | int, версія API `s` | `1` |
| `fonts` | до 6 рядків Google Fonts `Family[:400,800i]` | `["Montserrat:900"]` |
| `params` | до 8 повзунків: `{name ^[a-z][A-Za-z0-9_]{0,30}$, label, min<max, value, step?}` або `{name, label, type:"color", value:"#rrggbb"}` | `size`, `y`, `color` |
| `code` | тіло `function (ctx, s)`; масив рядків або рядок; ≤ 60 000 символів | 330 рядків у Lava Wave |

Хаб зберігає й експортує `code` як масив рядків, щоб його було зручно читати й порівнювати.
Під час читання документ може прийти обгорнутим у `{data: {...}}`, усі інструменти це розуміють.

### 3.4 Transitions (`translib`)

| поле | тип / правило | приклад |
|---|---|---|
| `duration` | секунди, 0.1–3 | `0.36` |
| `anchor` | 0–1: яка частка переходу припадає до стику | `0.55` |
| `params` | `[{name, label, min, max, value}]`; лише `float`-uniform. Ім'я — GLSL-ідентифікатор, що не збігається з вбудованими (`uA uB progress ratio resolution time vUv fragColor getA getB luma mirrorUV transition main`); `max > min` | `{name:"blur", min:0.3, max:2, value:1}` |
| `shader` | тіло GLSL ES 3.00: обов'язково `vec4 transition(vec2 uv)`, без `#version`/`precision`. Доступні `getA(uv)`, `getB(uv)` (дзеркальний край), `progress` 0..1, `ratio`, `resolution`, `time`, `luma()` | рядок ~5–15 КБ |

Вікно стику: від `cut − anchor·duration` до `cut + (1 − anchor)·duration`.
У `zoom-punch` значення anchor продубльоване константою в шейдері (`ZP_CUT`), міняти треба обидва місця разом.

### 3.5 Music (`musiclib`)

| поле | тип / правило | приклад |
|---|---|---|
| `asset` | 32 hex, обов'язково: AAC у .mp4 у сховищі артефакту | `8dba85f2…` |
| `artist`, `source` | string ≤ 80 / ≤ 400 | `Pixabay, track …` |
| `rights` | `no-cid` (No Content ID) · `factory` (трек фабрики) · `licensed` · `unknown` (Not checked) | `"no-cid"` |
| `factory` | точне ім'я файлу в папці MUSIC фабрики (≤ 160); його ставлять у поле Music задачі під час доставки | `"Peaceful Flow.mp3"` |
| `moods` | до 8 з `energetic motivational uplifting epic calm emotional melancholic romantic dark fun electronic piano` | `["calm"]` |
| `bpm` | 0–300 | `149.9` |
| `beat` | перша доля, секунди, 0–10 | `0.01` |
| `drops` | до 4 чисел за часом; `[0]` = перший дроп після інтро | `[14.42, 64.85]` |
| `duration` | секунди | `97.65` |
| `lufs` | інтегральна гучність BS.1770 | `−10.2` |
| `peaks` | 600 чисел 0–255, хвиля для картки | |
| `note` | string ≤ 600 | |

### 3.6 Skills (`library`)

| поле | тип | примітка |
|---|---|---|
| `description` | string | з frontmatter SKILL.md (≤ 1024) |
| `category` | `hook podcast animation ugc static edit tool` | `"animation"` |
| `products` | string[] | мітки продуктів команди; зараз порожньо |
| `folder` | string | дорівнює `name` |
| `files` | `[{path, text}]` | лише текстові файли, ≤ 200 КБ; обов'язково `SKILL.md` |
| `binaries` | `[{path, size}]` | перелік бінарних файлів; самі файли в хаб не завантажуються |
| `video` | `{id, poster, w, h, dur, size, type, preview, origSize}` | приклад-відео, **обов'язково**, ≤ 20 МБ. MOV або файл понад 20 МБ перекодовується в браузері в 720p MP4 |
| `videoLink` | https-URL? | посилання на оригінал |
| `note` | string? | |
| `ai` | `{v, at, by, data:{summary, output, inputs[], steps[], costs[], deps[], pitfalls[]}}` \| null | розбір від Claude (`sample.json`); скидається з новою версією |

## 4. Як співробітник користується (через Claude)

Усі команди запускаються з `podcast/`. Документ зберігається як є, варіант в обгортці `{data}` теж підходить.

**Таймінги слів.** Рушіям `motion` і `canvas` потрібен `words.json`: `[[word, start, end], …]` (або `[{text, start, end}]`),
секунди від початку відео, написання як у сценарії. Підходить будь-який вирівнювач, наприклад Whisper з word timestamps.
Текст **Copy for Claude** у хабі згадує `tools/align_words.py` — цього скрипта в репозиторії немає, його місце займає ваш вирівнювач.

**«use subs X»**
1. ArtifactData get: url хабу, колекція `sublib`, doc_id `X`. Якщо там немає — список `subs` → `subs/<uid>/items/X`.
2. Зберегти в `projects/<id>/subs_style.json`.
3. Далі за `engine`:
   - `motion` / `canvas`:
     `python tools/hub_subs.py --style projects/<id>/subs_style.json --words projects/<id>/words.json --video <clean.mp4> --out <out.mp4> [--param name=value] [--offset s] [--preview 5] [--timeout 30]`.
     Скрипт малює кадр за кадром тим самим кодом, що й картка хабу (headless Chromium), і накладає кадри через ffmpeg.
     Звук копіюється. Motion — лише 9:16; canvas бере розмір кадру за формою відео.
   - `editor`: `python tools/karaoke_subs.py <id> --style projects/<id>/subs_style.json`. Працює лише всередині проєкту `pod.py`
     і через локальний редактор (див. нижче та розділ 9).
   - `word`: `python tools/word_captions.py --video <clean.mp4> --audio <vo.wav> --out <final/name> --style projects/<id>/subs_style.json [--words words.json]`
     → `<name>_subs.mp4` + `<name>_clean.mp4`. Без `--words` таймінги дає faster-whisper.
   - `coach`: окремої команди немає. Значення переносяться в конфіг `subs` у edit.json для `tools/coach_montage.py`:
     `size = st.size`, `y = place.py / 1920`, `maxw = marker.maxw`, `pad = [marker.padX, marker.padY]`,
     `colors = [[0, marker.color]]`, `dark_under_marker = marker.darkUnder`.

**Рушій `editor` докладно.** Стилі `karaoke-soft`, `hormozi`, `word-pop` рендеряться лише через локальний редактор команди:
1. Потрібен проєкт `pod.py`: `podcast/projects/<id>/state.json` (з `turns`, звідси написання слів) і `editor/projects/<id>.json`
   (його пише `pod.py timeline`).
2. `karaoke_subs.py` бере мікс таймлайну `projects/<id>/qa/timeline_mix.wav` (якщо його немає, сам запускає `pod.py audit`),
   отримує таймінги слів від Groq Whisper (ключ у `~/.config/groq_key`) і записує шар сабів у `editor/projects/<id>.json`.
3. `node serve.js` (з `podcast/`, порт 8765 за замовчуванням) роздає `editor/`; відкрити `http://localhost:8765/?project=<id>`.
4. Кнопка Export редактора шле стан на `serve.js`, той запускає `podcast/render.py`, а `render.py` малює саби через
   `karaoke_subs.render_band`. Файли з'являються в `editor/exports/`.

**«use transition X»**
1. ArtifactData get `translib/X` (або `trans/<uid>/items/X`).
2. Зберегти в `projects/<id>/trans_X.json`.
3. `python tools/transitions.py --preset projects/<id>/trans_X.json --video <clean.mp4> --cuts auto --out <out.mp4>`
   - `--cuts 1.2,3.4`: лише ці стики (`auto` = пошук за різницею кадрів);
   - `--clips a.mp4 b.mp4 …`: склеїти окремі кліпи;
   - `--param name=value`: значення повзунків користувача; `--bitrate 5M`.

   Це той самий GLSL на GPU (moderngl). Довжина ролика не змінюється. Перехід накладається на чисту картинку, саби й плашки йдуть поверх.

**«use music X»**
1. ArtifactData get `musiclib/X`: взяти `drops`, `bpm`, `factory`, `asset`.
2. Artifact read: url хабу, `path = <asset>`. Завантажується .mp4 з AAC (у репозиторії ті самі файли лежать у `data/assets/`).
3. `python tools/music_bed.py --video <creo.mp4> --music <file.mp4> --drop <drops[0]> --at <секунда стику> --out <out.mp4>`
   - підкладка −30 LUFS під голос −14, без голосу `--lufs -14`;
   - пласке підсилення без loudnorm; якщо мікс перевищує −1 dBFS, тихішає лише підкладка; fade-out 0.4 с (`--fade`);
   - картинка копіюється, довжина звуку дорівнює довжині картинки;
   - трек без дропу: `--start <секунда треку>` замість `--drop/--at`.
4. Якщо `rights = unknown`, перед використанням у рекламі перевірити, що в треку немає Content ID.

**Skill:** завантажити .zip і покласти в `.claude/skills/<name>/` (Claude Code) або завантажити в claude.ai.

## 5. Як додається новий пресет

Кнопка **Add …** відкриває редактор. Файл можна кинути на сторінку. Publish активна лише після всіх перевірок.

**Subtitles**
- Вкладка **Settings** (форма): рушій, шрифт, розмір, кольори, обводка, тінь, плашка, підсвітка, позиція.
  Там же `fx` для motion, маркер для coach, повзунки й код для canvas.
- Вкладка **JSON**: вставити або кинути .json, потім **Apply**. Працюють `normalizePreset()` і `validatePreset()`.
  Помилки блокують, зайві поля дають попередження «не будуть збережені».
- Шрифт не зі списку: кинути .ttf/.otf/.woff/.woff2 (до 8 МБ), файл іде в `fontAsset`. У колег під час рендеру цей шрифт має бути встановлений.
- Шаблони для будь-якого Claude:
  - canvas: кнопка **Copy template** копіює `skillhub/CANVAS_TEMPLATE.md`;
  - editor-стиль (`st` + `place`): `skillhub/SUBS_TEMPLATE.md`, кнопки для нього в хабі немає.

  У шаблон дописують ідею, віддають Claude, отриманий JSON вставляють через Apply.
- Перед Publish:
  - `validatePreset` без помилок;
  - ім'я не зайняте іншим автором;
  - для canvas — автоматичний прогін коду в прихованому sandbox-iframe: 48 кадрів 9:16, 640 px по довгій стороні.
    - Блокують: виняток, зависання довше 10 с, порожнеча більш ніж у половині кадрів із мовленням, у середньому понад 50 мс на кадр, кадр довше 1 с.
    - Попередження: «важко» (понад 12 мс), малювання на краю кадру або в нижній смузі UI, недетермінізм.
- Шрифт, якого немає в хабі, дає попередження. Плашки й підсвітка в word/coach теж дають попередження: ці рушії їх не малюють.

**Transitions**
- Вкладки: GLSL-код (**Compile and play**), JSON і повзунки. Можна кинути .glsl/.frag/.fs або .json.
- Перед Publish:
  - `validateTrans` без помилок;
  - шейдер компілюється;
  - `transCheck` на кадрах 90×160 і 160×90: за `progress 0` кадр точно дорівнює A, за `1` — точно B
    (відхилення до 6 рівнів у максимумі й до 0.6 в середньому, інакше блок). Повністю чорна середина дає попередження.

**Music**
- Кинути аудіо (mp3/wav/m4a/aac/ogg/flac). Браузер сам вимірює LUFS (BS.1770), BPM (за онсетами), першу долю,
  дропи (стрибок басу нижче 150 Гц, прив'язаний до долі) і хвилю.
- У формі заповнюються title, artist, source, rights, factory, moods, note.
- Аудіо пакується в AAC .mp4 через WebCodecs, межа 20 МБ.
- Блокують Publish: немає назви, немає аудіо, аналіз не закінчено, дроп пізніше кінця треку. `rights = unknown` дає попередження.

**Skills**
- Кинути папку, .zip/.skill або SKILL.md. Одразу йде перевірка формату:
  - frontmatter, YAML, `name` (формат, без `claude`/`anthropic`), `description` (≤ 1024, без тегів, є «Use when…»);
  - допустимі ключі; довжина тіла;
  - секрети (помилка), абсолютні шляхи й залежності від файлів проєкту (попередження або інфо).
- Частину помилок виправляє автофікс.
- Потрібні категорія і приклад-відео. Посилання, якщо є, лише https.

## 6. Рендер-кіт: вимоги

- Перевірено на Windows 11 з Python 3.11, Node 24 і ffmpeg 2025-08. ffmpeg і ffprobe мають бути в PATH. Якщо NVENC немає, використовується libx264.
- У консолі Windows задати `PYTHONUTF8=1`, інакше `--help` у `karaoke_subs.py` і `word_captions.py` падає на кирилиці.
- pip-пакети (перевірено на версіях у дужках):
  - `numpy` (2.2), `pillow` (11.2), `playwright` (1.64), потім `python -m playwright install chromium`;
  - `moderngl` (5.13), потрібна відеокарта з OpenGL 3.3;
  - `psutil` (`preview.py`), `requests` (Groq у `karaoke_subs.py` / `coach_subs.py`);
  - `faster-whisper` (`word_captions.py` без `--words`, QA у `pod.py`);
  - `torch` і `torchaudio` (дослівний wav2vec2-шар QA у `pod.py`, ним користуються `hook_qa.py` і `coach_subs.py`; ваги завантажуються під час першого запуску);
  - `librosa` (необов'язково, `pod.py`);
  - `praat-parselmouth` (`hook_qa.py`), `opencv-python` (`coach_montage.py`).
- Node.js: `skillhub/tools/build_page.js` (canvas у `hub_subs.py` і `preview.py`) і `podcast/serve.js` (редактор).
- Мережа: `hub_subs.py` вантажить Google Fonts, а для motion ще й CDN-скрипти сторінки хабу (js-yaml, jszip, mp4-muxer, mp4box).
  Решта мережі для коду пресета закрита.
- Ключ Groq у `~/.config/groq_key` потрібен лише для `karaoke_subs.py` і `coach_subs.py`.
- Шрифти для word/coach/editor мають бути встановлені в `%WINDIR%\Fonts` або в користувацьку папку шрифтів.
  `coach_montage.py` чекає Montserrat Black / ExtraBold та Inter Regular саме в `%WINDIR%\Fonts`.

| файл | що робить | залежить від |
|---|---|---|
| `podcast/tools/hub_subs.py` | motion/canvas саби → mp4 | `../../skillhub/index.html` (motion: сторінка на localhost, `window.__hub`), `../../skillhub/tools/build_page.js` (canvas), Playwright, node, ffmpeg |
| `podcast/tools/transitions.py` | переходи → mp4 | moderngl, ffmpeg |
| `podcast/tools/music_bed.py` | музична підкладка | numpy, ffmpeg |
| `podcast/tools/word_captions.py` | word-саби | Pillow, ffmpeg, faster-whisper або `--words` |
| `podcast/tools/karaoke_subs.py` | editor-саби як шар локального редактора | проєкт `pod.py` (`state.json`, `pod.py audit`), `../../editor/projects/<id>.json`, Groq. Відео — через Export редактора: `podcast/serve.js` → `podcast/render.py` → `karaoke_subs.render_band` |
| `podcast/tools/coach_subs.py` | таймінги й фрази для coach-сабів | `karaoke_subs.py` (Groq), `pod.py`, `hook_qa.py`, `formats/podcast3.json` (немає в репозиторії) |
| `podcast/tools/coach_montage.py`, `coach_plan.py` | монтаж talking head зі вставками, плашками й coach-сабами | Pillow, cv2, numpy, ffmpeg; шрифти Windows. `coach_plan.py` містить шляхи до матеріалів конкретного проєкту, яких у репозиторії немає |
| `podcast/tools/hook_qa.py` | аудіо-QA кліпу (дослівна перевірка) | `pod.py`, `formats/podcast3.json` (немає), parselmouth, torch/torchaudio |
| `podcast/pod.py` | конвеєр проєкту: стан, QA, таймлайн редактора, `audit` | `formats/<format>.json`, `projects/`, `../editor/`, faster-whisper, torch/torchaudio |
| `podcast/serve.js`, `podcast/render.py` | локальний сервер редактора і рендер його таймлайну в MP4 | `../editor/`, ffmpeg, `tools/karaoke_subs.py` |
| `podcast/stick/*.py`, `formats/stick_talking.json` | інструменти скіла `stickman-talking` (план, перевірка дублів, ліпсинк, таймлайн) | `pod.py`, `../../editor/`, faster-whisper |
| `skillhub/tools/preview.py` + `build_page.js` | покадрове прев'ю canvas-пресета (sheet/mp4/strip/shots) у пісочниці із закритою мережею і сторожем | `../index.html`, Playwright, node, Pillow, psutil |

## 7. Карта репозиторію

> **Про шляхи.** Інструменти знаходять одне одного за відносними шляхами, тому структуру папок треба зберегти:
> `podcast/tools/hub_subs.py` → `../../skillhub/`, `skillhub/tools/build_page.js` → `../index.html`,
> `podcast/tools/karaoke_subs.py`, `pod.py`, `serve.js`, `stick/*.py` → `editor/` поруч із `podcast/`.

```
README.md                               короткий огляд (укр.)
docs/HANDOFF.md                         цей документ
skillhub/index.html                     увесь хаб, один файл (CDN: js-yaml, jszip, mp4-muxer, mp4box; шрифти Google)
skillhub/CANVAS_SUBS_SPEC.md            специфікація canvas-сабів і переходів для розробників
skillhub/CANVAS_TEMPLATE.md             шаблон для Claude: новий canvas-стиль (= кнопка Copy template)
skillhub/SUBS_TEMPLATE.md               шаблон для Claude: новий editor-стиль (st + place)
skillhub/tools/preview.py               прототип покадрового рендера canvas:
                                        python skillhub/tools/preview.py <preset.json> <out_dir> [--fmt 9:16] [--param k=v]
skillhub/tools/build_page.js            сторінка-пісочниця з рантаймом хабу (читає ../index.html)
podcast/tools/                          hub_subs, transitions, music_bed, word_captions, karaoke_subs,
                                        coach_montage, coach_plan, coach_subs, hook_qa
podcast/pod.py, render.py, serve.js     конвеєр проєкту, рендер таймлайну редактора, локальний сервер редактора
podcast/formats/                        stick_talking.json (скіл stickman-talking), gym_monologue.json (coach-монтаж)
podcast/stick/                          plan.py, check.py, lipcheck.py, timeline.py (скіл stickman-talking)
editor/index.html                       локальний відеоредактор команди (рушій editor, таймлайни pod.py)
editor/README.md                        короткі нотатки щодо запуску редактора без проєкту
data/library/sublib/*.json              12 стилів сабів
data/library/translib/*.json            10 переходів
data/library/musiclib/*.json            12 треків
data/library/library/*.json             1 скіл
data/assets/<assetId>.<ext>             файли, на які посилаються документи (один файл на assetId)
```

Папки, які створюються під час роботи і в репозиторій не входять: `podcast/projects/<id>/`, `editor/projects/`,
`editor/videos/`, `editor/exports/`, `editor/uploads/`.

Вивантаження `data/library/` — документи бази як є, з двома змінами: `author` і `updatedBy` дорівнюють `null`, `history` прибрано.

Відповідність файлів у `data/assets/` документам:

| файл | документ · поле | що |
|---|---|---|
| `ba7c7445fbbf8e8fac5e46f23781903a.mp4` | musiclib/ashes-on-the-windowsill · asset | AAC 48 кГц, 42.7 с |
| `b5c75552b4845c5d1508e003bf1aaff7.mp4` | musiclib/beyond-the-stars · asset | 224.4 с |
| `6871c98c3a7da68da6709f1560e5b263.mp4` | musiclib/big-shot · asset | 161.8 с |
| `424296290f554a6e6ca1c8e0cb8671d9.mp4` | musiclib/hey-all-good-folks · asset | 86.7 с |
| `133786e4da96d2aa0b122bab618111ea.mp4` | musiclib/minimal-signals · asset | 150.0 с |
| `8dba85f2187aaed420f5b070f7badc6d.mp4` | musiclib/motivation-feel-good · asset | 97.6 с |
| `5717b2d9fc83cc1b897685a8c5411041.mp4` | musiclib/motivation · asset | 144.5 с |
| `0da64a756402fe17cc9be5fbb70a3128.mp4` | musiclib/peaceful-flow · asset | 203.2 с |
| `890bf03db9813db6994ec7fbfdba99c7.mp4` | musiclib/rise-of-the-hero · asset | 193.4 с |
| `2e69db77eccc637682bc659fa2cae9b7.mp4` | musiclib/shadows-halloween · asset | 124.7 с |
| `dfd8c8702cef394e5cedfbd2cf355799.mp4` | musiclib/simple-life · asset | 217.9 с |
| `d8c31e5c1b58e667adbe364c70269ae6.mp4` | musiclib/sunrise · asset | 165.6 с |
| `0789dbbbc4c05334d3060368ac4fd5af.mp4` | library/stickman-talking · video.id | приклад-відео 1080×1920, 41.8 с, H.264 + AAC |
| `16cdd93bf972603da0b82410a12dbb5e.jpg` | library/stickman-talking · video.poster | постер |

Інших бінарних файлів документи не використовують: шрифтів (`fontAsset`) у бібліотеці немає.

## 8. Що врахувати під час перенесення в Kaleido

- **Формати JSON не міняти.** Локальні скрипти читають саме ці документи, вони і є контрактом. Замінити треба лише шар зберігання:
  - `window.claude.use(...)` → БД, авторизація і файлове сховище Kaleido;
  - правила «власник пише в бібліотеку, автор пише у своє»;
  - унікальність `name`, версії та history.
- Музику в Kaleido можна зберігати як звичайне аудіо. Обгортка AAC у .mp4 потрібна лише через обмеження артефакту.
- **Код пресетів недовірений:** canvas `code` і GLSL `shader` може опублікувати будь-який автор.
  - Canvas запускати лише ізольовано: Worker + OffscreenCanvas або iframe `sandbox="allow-scripts"` без same-origin, з лімітом часу на кадр.
  - На сервері — окремий процес браузера із закритою мережею, як у `preview.py` (`CANVAS_SUBS_SPEC.md` §5, §8).
  - Перевірка перед Publish — це фільтр якості, а не межа безпеки.
- У Chrome усі прев'ю ділять один процес пісочниці, і завислий пресет гальмує решту. У хабі від цього є сторож на 2.5 с.
  - У Kaleido в сітці краще показувати постер-кадр, а живе прев'ю запускати лише у відкритого пресета.
  - У Safari і Firefox ізоляції за процесами немає.
- Рендер відео зараз лише локальний, у Claude співробітника. Якщо Kaleido рендеритиме на сервері, логіка експорту описана
  в `CANVAS_SUBS_SPEC.md` §7: саби малюються кадр за кадром детермінованим кодом, переходи — той самий GLSL.

## 9. Відомі обмеження і відкриті питання

1. **Саби лише 9:16.**
   - У хабі немає 16:9 ні в прев'ю, ні в перевірці. Motion-стилі в `hub_subs.py` теж лише вертикальні.
   - Canvas-рантайм і `hub_subs.py` технічно приймають 16:9 (розмір береться за формою відео), але хаб цього не перевіряє.
   - У `CANVAS_SUBS_SPEC.md` розділи про 16:9 для сабів (§3, §5 п. 7, §6) застаріли. Для переходів 9:16 / 16:9 актуальне.
2. **Рушій `editor` не автономний.** `karaoke-soft`, `hormozi` і `word-pop` рендеряться лише через локальний редактор
   (`podcast/serve.js` + `editor/index.html` + `podcast/render.py`). `karaoke_subs.py` потребує проєкту `pod.py`
   і ключа Groq у `~/.config/groq_key`, а відео виходить лише з Export редактора.
   Варіант для перенесення: хаб і так малює editor/word/coach-стилі в прев'ю (`drawSubFrame`), тож `hub_subs.py`
   можна розширити на ці рушії. Попіксельний збіг із фінальним рендером редактора не перевірявся.
3. **QA-шар у репозиторії неповний.** Формат за замовчуванням `podcast3` (`podcast/formats/podcast3.json`) не включено.
   Його читають `hook_qa.py`, `coach_subs.py` і `pod.py qa-file` без `--format` (ним користуються `stick/check.py`
   і `stick/timeline.py`). Проєкт формату `gym_monologue` має власні блоки `qa` і `timeline`.
4. **`st.depth`** (3D-видавлювання) малює лише локальний редактор. Прев'ю хабу показує такий стиль пласким,
   `validatePreset` поле не перевіряє. У бібліотеці таких стилів зараз немає.
5. **Музика з `rights = unknown`** (Not checked): 6 із 12 треків. У `big-shot` і `shadows-halloween` не вказане навіть джерело.
   Перед використанням у рекламі треба підтвердити відсутність Content ID.
   - У `ashes-on-the-windowsill` у хабі лише 42-секундний фрагмент.
   - У `motivation` немає дропу: використовується `--start` замість `--drop`.
6. **Рушій `coach`** не має CLI: значення переносяться вручну в конфіг `subs` для `coach_montage.py` (розділ 4).
   `marker.outline` у цей конфіг не переноситься.
7. **Шрифти:**
   - canvas підтримує лише Google Fonts;
   - свої файли шрифтів (`fontAsset`) є лише в st-рушіїв і мають бути встановлені на машині рендера;
   - `word_captions.py` і `coach_montage.py` шукають шрифти в папках Windows.
8. **Аналіз музики** (BPM, дропи) робиться в браузері і дає оцінку. Дропи можна виправити у формі.
9. **Хаб прив'язаний до рантайму claude.ai.** Поза claude.ai дані не видно, офлайн-переглядача вивантаження немає.
10. У розділі Skills поки один елемент. Бінарні файли скілів у хаб не завантажуються, зберігається лише їхній перелік.
11. **`podcast/serve.js`** уміє копіювати готові файли в папку доставки команди (опція drive). Шлях задається змінною
    `EDITOR_UPLOAD_DIR`; значення за замовчуванням прив'язане до машини команди, поза нею цю опцію не вмикати.
