# Power Tools Hub

Командна бібліотека готових інструментів для відеореклами, які Claude застосовує до роликів через Kaleido MCP.
Зараз це один HTML-файл, опублікований як артефакт claude.ai: https://claude.ai/artifact/6A2gzYgdr9Kk1Heeu58ShH
(база і файли живуть в артефакті). Задача розробників Kaleido: перенести хаб усередину Kaleido окремим розділом.

## Розділи

| Розділ | Що всередині | Як застосовується |
|---|---|---|
| Skills | Цілі виробничі процеси для Claude (зараз один: `stickman-talking`, мультяшний персонаж, що говорить) | Claude читає скіл і проходить кроки |
| Subtitles | 12 стилів сабів, рушії `editor`, `word`, `coach`, `motion`, `canvas`. Лише 9:16 | «use subs X»: JSON пресета → скрипт рендера |
| Transitions | 10 GLSL-переходів, прев'ю 9:16 або 16:9 | «use transition X»: `transitions.py`, той самий шейдер на GPU |
| Music | 12 треків із BPM, дропами, правами та назвою файлу на фабриці | «use music X»: `music_bed.py` ставить дроп на потрібний стик |

Будь-хто з команди може додати свій пресет: форма або JSON у хабі, у кожного автора свій розділ.
Перед публікацією хаб сам перевіряє пресет: canvas-код проганяється в пісочниці, у переходів перевіряється, що 0 = кадр A, а 1 = кадр B.

## Структура репозиторію

| Шлях | Що |
|---|---|
| `skillhub/index.html` | Увесь хаб: інтерфейс, прев'ю, редактори, перевірки, робота з базою |
| `skillhub/CANVAS_SUBS_SPEC.md` | Специфікація для розробників: формат canvas-пресета, рантайм `s`, пісочниця, перевірки, експорт |
| `skillhub/CANVAS_TEMPLATE.md`, `SUBS_TEMPLATE.md` | Шаблони, за якими будь-який Claude пише новий стиль сабів (кнопка Copy template у хабі) |
| `skillhub/tools/preview.py`, `build_page.js` | Локальне прев'ю canvas-пресета тим самим рантаймом, що в хабі |
| `podcast/tools/hub_subs.py` | Рендер сабів `motion` і `canvas` у відео: код хаба кадр за кадром у headless Chromium |
| `podcast/tools/transitions.py` | Рендер переходу у відео (moderngl, той самий GLSL) |
| `podcast/tools/music_bed.py` | Підкладка треку під ролик: дроп на стик, музика −30 LUFS під голосом −14 |
| `podcast/tools/word_captions.py` | Саби рушія `word` (по одному слову) |
| `podcast/tools/karaoke_subs.py` | Саби рушія `editor`. Потрібен локальний редактор команди (у репозиторій не входить) |
| `data/library/<колекція>/<name>.json` | Вивантаження бібліотеки з бази: `sublib`, `translib`, `musiclib`, `library` |
| `data/assets/<assetId>.mp4 / .jpg` | Файли, на які посилаються документи: аудіо треків (AAC у .mp4), приклад відео та постер скіла |

## Дані (база артефакту)

- Бібліотека власника: `library/<name>` (скіли), `sublib/<name>`, `translib/<name>`, `musiclib/<name>`. Пише лише власник.
- Особисті пресети: `skills/<uid>/items/<name>`, `subs/<uid>/items/<name>`, `trans/<uid>/items/<name>`, `music/<uid>/items/<name>`.
  Кожен пише лише у свій `<uid>`. Зараз особистих пресетів немає.
- Поле `asset` / `video` у документі — id файлу в сховищі артефакту; ці файли лежать у `data/assets/` під тим самим id.
- У вивантаженні прибрано id користувачів та історію правок.

## Як команда користується (Claude локально)

З папки `podcast/`:

```bash
python tools/hub_subs.py --style projects/<id>/subs_style.json --words words.json --video clean.mp4 --out out.mp4
python tools/transitions.py --preset projects/<id>/trans_X.json --video clean.mp4 --cuts auto --out out.mp4
python tools/music_bed.py --video creo.mp4 --music track.mp4 --drop <drops[0]> --at <секунда стику> --out out.mp4
```

`--param name=value` у сабів і переходів = значення повзунків. `words.json` = слова з таймінгами (будь-який вирівнювач, наприклад Whisper з word timestamps).

Потрібно: Python 3.11, `numpy`, `Pillow`, `moderngl`, `playwright` (+ `playwright install chromium`), ffmpeg/ffprobe у PATH
(NVENC, якщо є, інакше x264), Node.js для `skillhub/tools`. Для `word_captions.py` ще `faster-whisper`.

## Відомі обмеження

- Саби лише 9:16. Переходи 9:16 і 16:9.
- 3D-об'єм сабів (`st.depth`) рендериться у відео, але прев'ю хаба малює його пласким.
- У частини треків права «Not checked»: перед використанням перевірити, що немає Content ID.
- Аудіо зберігається як AAC у .mp4: сховище артефакту не приймає «голі» аудіофайли.
