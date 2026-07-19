# ScorePrep

**Clean AI-generated piano MIDI for beautiful MuseScore engraving.**

```
python3 scoreprep.py transcription.mid clean.mid --tempo 130
```

---

## Before / after

<!-- TODO: screenshots
Original AI transcription (MuseScore import) → mess of ties, one staff, fractured chords
                    ↓
After scoreprep.py  → clean two-staff grand staff, minimal ties, correct engraving
-->

*(screenshots coming soon)*

---

## Why this exists

Most piano transcription systems (MT3, ByteDance's piano transcription
model, Magenta, etc.) produce MIDI that sounds correct but engraves
poorly. They preserve a human performance's exact, continuous timing —
onsets and releases down to the millisecond — which is exactly what you
don't want handed straight to notation software. Importing that MIDI
into MuseScore directly produces:

- everything crammed onto one staff, no treble/bass split
- ties *everywhere*, because almost no note duration lands on a clean
  rhythmic value
- chords that fracture into overlapping tied fragments, because the
  notes that make them up don't release at exactly the same instant
- noise: sub-audible blips, sustain-pedal bleed read as held notes,
  wrong-octave tempo guesses, blank leading measures

ScorePrep automates the cleanup a transcription like that needs before
it's actually usable in notation software — while preserving the
original performance as closely as practical.

## Philosophy

**ScorePrep is intentionally *not* a MIDI editor.** It's an engraving
preprocessor: raw transcription MIDI in, notation-ready grand-staff MIDI
out.

By default, it tries to produce the cleanest score that still sounds
like the original performance. Most flags are `[advanced]` and exist to
accommodate unusual source files — a multi-track export, a genuine
triplet feel, an unusual sustain-pedal style — rather than normal piano
transcriptions. For a typical single-track MT3/ByteDance/Magenta export,
running with no flags at all (or just `--tempo`, if the source doesn't
have a trustworthy one) is the expected common case.

## Features

### Core
- Grand-staff split (treble/bass), estimated automatically from the
  actual pitch distribution or overridable with `--split-pitch`
- **`--tie-temperature 0.0–1.0`** — the central fidelity/readability
  dial. `0.0`: fewest ties, most rests, most readable. `1.0`: closest
  to the source's exact timing, more ties.
- Tempo estimation: source file's own tempo → rhythm-pattern estimate →
  120 BPM fallback, with transparent reasoning printed at every step
- Time signature: read from the source, or `--time-sig`
- Interactive step-by-step mode — just run `scoreprep.py` with no
  arguments

### Musical cleanup
- **Sustain-pedal handling** (`--pedal-mode reflect`) — extends a
  note's true sustained length to the pedal-up point, instead of
  ignoring pedal data
- **Playback-sustain decoupling** — notation stays clean and tie-light
  while a separate automation track keeps *playback* sounding true to
  the original performance length
- **`--grid triplet`** — quantizes to a grid that natively fits
  triplet-eighth subdivisions, instead of flattening a genuinely
  triplet passage onto straight 16ths
- **`--clean-durations powers2`** — plain power-of-two note values
  only, no dotted notes, for a plainer engraving style
- **Velocity cleanup** — `--min-velocity` drops ghost notes;
  `--velocity-mode scale`/`normalize` reshapes dynamics
- Transcription-noise filtering (`--min-note-ticks`) for sub-audible
  blips

### Robustness
- **`--track` / `--channel`** — manual override for multi-instrument
  source files, with automatic-pick transparency (which track, why,
  and a warning if another track has a comparable note count)
- **Tempo-ambiguity detection** — when two candidate tempos fit the
  rhythm almost equally well, ScorePrep names both instead of silently
  guessing (this is a fundamental limit of rhythm-only tempo induction,
  not something any heuristic can always resolve)
- **Leading-silence rebasing** — a source file that doesn't start
  playing at tick 0 no longer renders as blank leading measures
- **Real playback preservation** — output always plays back at the
  source's true tempo, independent of whatever tempo you choose to
  *notate* with
- Confidence messages throughout: every auto-estimated value explains
  what it picked and why, so nothing is a silent guess

## Examples

```bash
# Basic — estimate everything
python3 scoreprep.py transcription.mid clean.mid

# Literal engraving — closest to the source's exact timing
python3 scoreprep.py transcription.mid clean.mid --tie-temperature 1.0

# Triplet-heavy transcription
python3 scoreprep.py transcription.mid clean.mid --grid triplet

# Multi-track source, piano is track 2
python3 scoreprep.py transcription.mid clean.mid --track 2

# Interactive, step-by-step
python3 scoreprep.py
```

## Installation

Requires Python 3 and [`mido`](https://pypi.org/project/mido/):

```bash
pip install mido
python3 scoreprep.py --help
```

## Advanced options

<details>
<summary>Full flag reference</summary>

| Option | Description |
|---|---|
| `--tempo` | Output tempo (BPM). Default: source's own tempo → rhythm estimate → 120 |
| `--time-sig N/D` | Time signature, e.g. `3/4`. Default: read from source, else 4/4 |
| `--split-pitch` | MIDI note number for the treble/bass split. Default: estimated from pitch distribution |
| `--tie-temperature` | `0.0`–`1.0` fidelity/readability dial |
| `--playback-sustain {on,off}` | Decouple playback length from notated length via pedal automation |
| `--pedal-mode {ignore,reflect}` | Whether sustain-pedal data extends note length |
| `--min-note-ticks` | Drop notes shorter than this (raw ticks) as noise |
| `--grid {straight,triplet}` | Straight-16th vs. triplet-fitting quantization grid |
| `--clean-durations {dotted,powers2}` | Allow dotted note values, or restrict to plain powers of two |
| `--min-velocity` | Drop notes quieter than this (0–127) as ghost notes |
| `--velocity-mode {passthrough,normalize,scale}` | Leave velocities alone, remap to a standard range, or scale uniformly |
| `--velocity-scale` | Multiplier used by `--velocity-mode scale` |
| `--track N` | Use track N instead of auto-picking |
| `--channel N` | Restrict the chosen track to one MIDI channel |
| `--interactive` | Force step-by-step prompts |

Run `scoreprep.py --help` for full, current wording on every flag.
</details>

## FAQ

<details>
<summary>Why doesn't the estimated tempo always match the original?</summary>

Tempo induction from rhythm alone is fundamentally ambiguous in some
cases: a piece played at 100 BPM in straight 16ths sounds *identical*
to the same piece at 200 BPM in straight 32nds — there's no rhythmic
signal that can tell those apart. When ScorePrep detects this kind of
near-tie between candidates, it names both instead of pretending to be
sure. If playback sounds twice too fast or slow, try the other option
it names, or pass `--tempo` explicitly.
</details>

<details>
<summary>Why are leading empty measures removed?</summary>

If the first note in the source file doesn't start at tick 0, notation
software renders that gap as blank measures. This is virtually always
real silence in the source audio (an intro, a spoken section) that got
faithfully transcribed — not a bug — but a score with several blank
bars at the start isn't useful, so ScorePrep rebases so the score
starts at the first note. A message explains when this happens.
</details>

<details>
<summary>Why are playback and notation handled separately?</summary>

Clean, readable notation needs simplified durations (fewer ties,
snapped to standard note values). But snapping every note to a clean
value can make MIDI *playback* sound choppy if a note's true sustained
length gets trimmed. ScorePrep resolves this by keeping notation clean
while adding a separate sustain-pedal automation track that restores
the true ring-out length for playback — so the score reads well and
still sounds like the performance.
</details>

## Roadmap

Not promising a big list — just what's actively being considered next:

- Way too many rests everywhere in the output pieces currently, must fix this
- Chord-conflict resolution direction (prefer shortest vs. longest)
- Same-pitch overlap priority (truncate earlier vs. delay later note)
- Instrument/channel assignment on output (`program_change`)
- Custom track naming

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Notes / assumptions

- Assumes a source resolution of 384 ticks per beat (the default for
  ByteDance / MT3 / Magenta piano transcription exports). If your file
  uses a different resolution, the script still runs but warns that the
  grid/bar math may be off.
- Picks whichever track in the source file has the most `note_on`
  events by default (overridable with `--track`/`--channel`).

## License

Not yet licensed for redistribution — personal project, published for
reference. *(Update this section before publishing publicly if you want
to allow reuse — e.g. MIT license.)*
