Make a subtitle preset for our team's Power Tools Hub. Reply with ONE JSON object only, no comments, no extra text.
It is pasted into Power Tools Hub -> Subtitles -> Add -> JSON tab. Only the fields below exist; anything else is dropped,
so translate the request into these knobs (closest possible look) and say nothing about fields that don't exist.

MY REQUEST: <describe the look here>

FORMAT (all fields required, values must stay inside the ranges):
{
 "name": "kebab-case-id",            // lowercase latin, digits, hyphens; how people call it: "use subs <name>"
 "title": "Short Title",             // up to 60 chars
 "engine": "editor",                 // keep "editor" (phrase blocks + spoken-word highlight, renders to video)
 "description": "One sentence: the look and when to use it.",
 "st": {
  "font": "Montserrat",              // one of: Geist, Inter, Inter Tight, Manrope, Bricolage Grotesque, Instrument Serif,
                                     //   Fraunces, Playfair Display, Montserrat, Poppins, Roboto, Rubik, Oswald, Anton, Bebas Neue
  "weight": 800,                     // 100..900
  "size": 72,                        // px on a 1080x1920 frame, 12..300 (typical 60..100)
  "align": "center",                 // left | center | right
  "valign": "middle",                // top | middle | bottom (inside the block box)
  "tracking": 0,                     // letter spacing in 1/1000 em, -200..500
  "leading": 0,                      // extra line spacing px, -100..200 (line height = size*1.25 + leading)
  "bold": false, "italic": false, "caps": false, "underline": false,
  "fill":   { "on": true, "color": "#ffffff" },                                  // text colour
  "stroke": { "on": true, "color": "#000000", "width": 8, "join": "round" },     // outline, width 0..40 px
  "shadow": { "on": true, "color": "#000000", "op": 120, "angle": 90, "dist": 4, "blur": 16 },
            // ONE shadow of the whole text: op = density 0..200 %, blur 0..100, dist 0..100, angle 0..360.
            // GLOW = shadow with dist 0, a bright colour, blur 20..45, op 120..200.
  "bg": {                            // plate behind the text
   "on": false, "color": "#ffffff", "op": 100, "radius": 14,     // op 0..100 %, radius 0..200
   "pad": 16, "padX": 22, "padY": 8, "linkPad": false,           // padX/padY 0..200 (pad used when linkPad true)
   "mode": "lines",                  // lines = a plate under each line | block = one plate for the whole phrase
   "target": "caption",              // caption = plates as in mode | word = a plate only under the spoken word
   "shadow": { "on": false, "color": "#000000", "op": 45, "angle": 135, "dist": 8, "blur": 18 }
  },
  "kar": {                           // spoken-word highlight
   "on": true, "color": "#ffdd00",
   "mode": "word",                   // word = only the spoken word changes colour and grows | fill = words fill in as spoken
   "scale": 115                      // spoken-word size in %, 50..250 (word mode; 105..125 looks good, more = letters crowd)
  }
 },
 "place": { "py": 1300, "boxW": 940, "boxH": 300 }
            // py = vertical centre of the block on 1920 px (900..1500 is safe; bottom ~320 px = app UI),
            // boxW = max line width 400..1080 (narrower = fewer words per line), boxH = block box height 50..1920
}

HOW IT BEHAVES (fixed, not configurable): words come with speech timings; text is cut into phrase blocks
(up to ~10 words / 3 s, cut at sentence ends and pauses, lines wrapped inside boxW); a block appears at once
and is replaced by the next one; in word mode the spoken word changes colour and grows over ~66 ms.

RULES: readable on bright AND dark footage (outline 6..12 px, or a dense shadow/glow, or a plate);
hex colours like #a1b2c3; heavy weights (700-900) for big captions.
