# ScorePrep

**Clean AI-generated piano MIDI for beautiful MuseScore engraving.**

```
python3 ScorePrep.py transcription.mid clean.mid --tempo 130
```

---

## Before / after

<!-- TODO: screenshots
Original AI transcription (MuseScore import) → mess of ties, one staff, fractured chords
                    ↓
After ScorePrep.py  → clean two-staff grand staff, minimal ties, correct engraving
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
- Interactive step-by-step mode — just run `ScorePrep.py` with no
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
- **Cost-based duration optimizer** — the biggest change in this
  release. Every note's (or chord's) written duration used to be chosen
  in three separate bolted-on passes (snap-to-largest-value, then force
  chord members to agree, then a flat-threshold gap-filler) — three
  patches for what's really one decision. It's now one pass that picks
  the duration minimizing a small cost. Every note first gets its
  truthful baseline: the longest duration representable within the tie
  budget that's still within its own real, transcribed length — ties
  needed to notate that truthfully are always free, never traded away
  (a genuinely long, pedal-sustained note keeps its ties regardless of
  weights). *From* that baseline, extending further to close a rest is
  then optionally considered, weighing the ties and invented sustain
  it would cost against the rest it would remove.
  `--tie-temperature` sets how that extension decision trades off — low
  temperature prefers a cheap small extension over a rest; high
  temperature prefers leaving the rest over any invented legato. The
  three weights (`--tie-weight`, `--rest-weight`,
  `--articulation-weight`) can be overridden individually if you want
  to tune the tradeoff directly instead of through the single dial.
- The per-staff report line now prints `rests=N` and `extended=N (X
  sixteenths invented)` alongside `needs-tie` — both halves of the
  tie/rest tradeoff, plus exactly how much sustain the optimizer
  fabricated to get there, visible in one place.
- **Barline-aware tie counting** — a note spanning a barline can't be
  drawn as one notehead straddling it, and splitting at the barline can
  leave a remainder that itself needs more than one further notehead
  (not just one extra, as an earlier version of this assumed). Every
  tie-budget check properly decomposes each barline-split segment, so
  `needs-tie` and the optimizer's own decisions are never off by an
  uncounted tie.
- **`--track` / `--channel`** — manual override for multi-instrument
  source files, with automatic-pick transparency (which track, why,
  and a warning if another track has a comparable note count).
  `--track` also accepts a comma-list (`1,2`) or `all` to merge multiple
  tracks in one pass — useful for sources with separate right-hand/
  left-hand tracks, instead of processing each one separately
- **Remembers your last input/output paths** (interactive mode) — no
  more retyping/pasting the same file paths on every test run
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

### Experimental

Off by default, built on a heuristic melody/accompaniment classifier
(`classify_voice_roles`) — validate against a piece you know well before
trusting these:

- **`--profile {readable,balanced,faithful}`** — sets `--tie-temperature`,
  `--pedal-mode`, `--grid`, and `--clean-durations` together instead of
  tuning them individually. `balanced` is the benchmark-tested sweet
  spot (see the benchmark suite for methodology)
- **`--melody-preservation on`** — biases the duration optimizer per
  note by voice role: melody gets cheaper ties/costlier rests
  (protect continuity), accompaniment gets the opposite (declutter
  more freely)
- **`--dynamic-split on`** — re-estimates the treble/bass split every
  `--split-window-bars` bars instead of one fixed split for the whole
  piece, so it follows the music's register drifting over time
- **`--hand-assignment on`** — within each chord, reconsiders notes
  near the split point and moves a note to the other hand if it's
  actually closer to that hand's recent position and doing so doesn't
  exceed `--max-hand-span`
- The diagnostic report also gains three sections regardless of which
  experimental flags are on: **"Measures Needing Attention"** (bars with
  the most chord conflicts/rests/heavy ties, worth a manual look),
  **"Voice Roles"** (the melody/accompaniment split and confidence per
  staff), and **"Confidence Warnings"** (bars where the classifiers are
  least sure of themselves — combines voice-role confidence with any
  `--hand-assignment` span violations). All three are read-only; none
  of them change engraving output by themselves.

## Examples

```bash
# Basic — estimate everything
python3 ScorePrep.py transcription.mid clean.mid

# Literal engraving — closest to the source's exact timing
python3 ScorePrep.py transcription.mid clean.mid --tie-temperature 1.0

# Triplet-heavy transcription
python3 ScorePrep.py transcription.mid clean.mid --grid triplet

# Multi-track source, piano is track 2
python3 ScorePrep.py transcription.mid clean.mid --track 2

# Interactive, step-by-step
python3 ScorePrep.py
```

## Installation

Requires Python 3 and [`mido`](https://pypi.org/project/mido/):

```bash
pip install mido
python3 ScorePrep.py --help
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
| `--track N\|N,M,...\|all` | Use track N, merge several tracks, or merge all tracks, instead of auto-picking |
| `--tie-weight` | [advanced] override the optimizer's cost per extra tied notehead. Default: derived from `--tie-temperature` |
| `--rest-weight` | [advanced] override the optimizer's cost for leaving a visible rest. Default: `1.0` |
| `--articulation-weight` | [advanced] override the optimizer's cost per grid unit of invented sustain. Default: derived from `--tie-temperature` |
| `--channel N` | Restrict the chosen track to one MIDI channel |
| `--tempo-rescale {preserve-duration,change-speed}` | When `--tempo` overrides the source's own tempo: rescale ticks to keep real-world length (default), or leave ticks as-is so playback speed genuinely changes |
| `--profile {readable,balanced,faithful}` | [experimental] Sets `--tie-temperature`, `--pedal-mode`, `--grid`, and `--clean-durations` together. Any of those four given explicitly still overrides the profile's value for it |
| `--melody-preservation {on,off}` | [experimental] Biases the duration optimizer per note using the heuristic voice-role classifier — cheaper ties/costlier rests for likely melody, the reverse for likely accompaniment. Default: `off` |
| `--dynamic-split {on,off}` | [experimental] Re-estimates the treble/bass split every `--split-window-bars` bars instead of using one fixed split for the whole piece. Default: `off` |
| `--split-window-bars N` | Window size (in bars) for `--dynamic-split`. Default: `8` |
| `--hand-assignment {on,off}` | [experimental] Within each chord, reconsiders notes near the split point and moves them to whichever hand they're actually closer to, if doing so doesn't exceed `--max-hand-span`. Default: `off` |
| `--max-hand-span N` | Widest pitch span (semitones) `--hand-assignment` allows within one hand's chord. Default: `16` (a 10th) |
| `--hand-ambiguity-zone N` | How close (semitones) to the split point a note must be before `--hand-assignment` reconsiders it. Default: `3` |
| `--interactive` | Force step-by-step prompts |

Run `ScorePrep.py --help` for full, current wording on every flag.
</details>

## FAQ

<details>
<summary>How do I know which --tie-temperature is "best" for my piece?</summary>

There's no universal answer — it's a genuine readability/fidelity
tradeoff, not something with one correct value. But it's not a pure
guessing game either: the per-staff report line prints both
`needs-tie=N` and `rests=N` for every run, so you can compare the
actual tradeoff numerically across a few values before opening
anything in MuseScore.

In practice the tradeoff isn't smooth — it tends to have a sharp
"elbow." Raising `--tie-temperature` a little from `0.0` (try `0.1`)
often cuts rests substantially at minimal tie cost, because it mostly
just relaxes the bar-span cap, not the tie budget itself. Past a
certain point (often somewhere around `0.15`–`0.2`) the tie budget
itself increases and `needs-tie` can jump sharply. Try a small sweep
(`0.0`, `0.1`, `0.2`, `0.3`...), look at where `rests` drops a lot while
`needs-tie` stays low, and start there.
</details>

<details>
<summary>I want fewer rests but --tie-temperature also adds ties I don't want. Why can't I control these separately?</summary>

You can — `--rest-weight` and `--articulation-weight` (and `--tie-weight`)
override the three costs the duration optimizer balances individually,
without touching `--tie-temperature`'s other effects (bar-span cap,
chord-sync tolerance). Lower `--articulation-weight` to make the
optimizer more willing to invent sustain and close rests without adding
ties; raise `--rest-weight` if you'd rather leave rests than fabricate
anything. These are deliberately a bit hidden (not the first thing
`--help` shows) since `--tie-temperature` alone is enough for most
pieces — reach for the individual weights only once you've found the
single dial isn't giving you the specific tradeoff you want.
</details>

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

## Companion tool: reduce_test.py

A separate, standalone script — not part of ScorePrep.py, not a
ScorePrep feature, no shared code between them. **Prototype, not yet
benchmarked against real pieces.**

ScorePrep expects single-instrument piano MIDI as input. If what you
actually have is a multi-track band/ensemble file (vocals, guitars,
bass, drums, ...) and you want a piano-reduction arrangement first,
`reduce_test.py` handles that separate problem: it merges the pitched
tracks (drums excluded), thins dense chords down to a playable size,
collapses repeated-attack/glide artifacts, and writes out a single-track
MIDI at ScorePrep's expected 384 ticks-per-beat — meant to be fed into
`ScorePrep.py` next, not a replacement for it.

This is a deliberate scope boundary, not an oversight: deciding *which
notes survive* a reduction is an arrangement decision, while ScorePrep
only ever decides *how to notate* content that's already settled. Kept
as a separate tool so that boundary stays clear.

```bash
python3 reduce_test.py band_multitrack.mid --list-tracks   # inspect tracks first
python3 reduce_test.py band_multitrack.mid reduced.mid --melody-track "Lead Vocal" --bass-track "Bass"
python3 ScorePrep.py reduced.mid clean.mid --tie-temperature 0.15   # then the usual pipeline
```

Or run it with no arguments for the same step-by-step interactive mode
ScorePrep.py offers. Run `reduce_test.py --help` for the full flag list.

## Roadmap

Not promising a big list — just what's actively being considered next:

- Chord-conflict resolution direction (prefer shortest vs. longest)
- Same-pitch overlap priority (truncate earlier vs. delay later note)
- Instrument/channel assignment on output (`program_change`)
- Custom track naming
- Validating the experimental voice-role-based features (`--melody-preservation`,
  `--dynamic-split`, `--hand-assignment`) against real pieces before considering
  any of them stable or on by default
- Exact repeat detection (post-quantization measure hashing, velocity excluded) + 1st/2nd-ending suggestion as a direct extension — optional, user-confirmed, not auto-applied; explicitly excludes fuzzy/ornamented/transposed repeat matching

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

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
