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
- Tempo-curve passthrough: a source with real tempo changes keeps them in
  the output.
- **Beat tracking** — when the source's tempo map looks erratic (big
  tempo jumps packed within a bar of each other), its beat positions
  can't be trusted either. ScorePrep then goes back to when each note was
  actually played and finds the beats itself, following the player's
  speeding up and slowing down, and writes a beat-by-beat tempo curve so
  playback keeps the original pacing. On the one piece with a published
  score to check against (a rubato performance of River Flows in You),
  96% of the score's on-beat notes land on a beat and 98% of its
  downbeats on a barline; the source's own map managed 91% and 54%.
  `--beat-tracking on` forces it for any file (experimental: on files
  with no tempo map it may read the piece at half or double tempo),
  `off` restores the old flatten-to-one-tempo behavior, and
  `--trust-tempo-curve` keeps the source's map as it is. The source's
  time signature is set aside along with its map (4/4 is assumed; pass
  `--time-sig` otherwise), and a pickup is written at the end of an
  opening bar of rests
- Time signature: read from the source, or `--time-sig`
- Interactive step-by-step mode — just run `ScorePrep.py` with no
  arguments

### MusicXML export
- **`--format musicxml`** (or just give the output file a `.musicxml`
  name) writes a MusicXML score instead of MIDI; `--format both` writes
  the two side by side. A MIDI file can only hand MuseScore the notes,
  and its importer then works out staves, ties, rests and note names
  again by itself. The MusicXML file carries ScorePrep's own decisions
  straight through: which hand plays each note, every written length and
  tie exactly as the duration optimizer chose it, tuplet brackets, and
  ornament signs.
- **Key signature and note spelling** — the key is estimated from the
  notes (or set with `--key`), and every note is spelled for that key
  (C♯ or D♭). A note outside the key is spelled by where it goes next:
  up a half step reads as a sharp, down as a flat. Checked against 50
  engraved scores (28,274 notes): the estimated key signature matched
  the printed one in 47, and 98.9% of notes were spelled as printed.
- **Voices** — notes in one hand that overlap need separate voices on
  paper. A note that only just overlaps the next one (fingers or pedal
  holding over) is written ending where the next note starts, whatever
  the voice limit; against the published River Flows in You score this
  moved written lengths from 74% to 82% agreement. `--max-voices`
  (default 2) then caps how many voices each staff may use, cutting notes
  that overlap more deeply. The report counts the two separately. On a
  heavily pedaled piece (kalank) the cap cuts 159 notes at 2 voices, 29
  at 3 and 4 at 4, for about 7% more rests on the page at 3 -- worth
  raising there. The MIDI output always keeps every note's full length.
- What it doesn't carry yet: beams are left to the notation software,
  the playback-only sustain pedal stays in the MIDI file, the key
  signature is one for the whole piece, and `--grid triplet` isn't
  supported (use `--tuplet-detection auto` for tuplets).

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
- **Fast notes kept apart** — two notes too quick for the 16th-note
  grid are written as 32nds instead of merging into a chord (or, on a
  repeated pitch, losing a note). `--fast-notes off` turns this off
- A note held much longer than a short note struck with it (four times
  longer, and at least a beat) keeps its own length instead of being cut
  to match

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
- **`--tuplet-detection auto`** — finds local n-tuplet bursts
  (quintuplets, septuplets, nontuplets, etc. against a 1- or 2-beat
  span), independent of `--grid`, and snaps just those onsets to exact
  even division instead of letting each one round independently to the
  outer grid. Not built on `classify_voice_roles` like the other three
  here -- its own detection pass, see the `detect_tuplet_groups`
  docstring in `ScorePrep.py` for the algorithm and known limitations
  (fit tolerance calibrated on only 3 pieces so far).
  `--tuplet-fit-tolerance relative|absolute` sets how strictly a
  candidate group must fit even spacing
- **`--ornament-detection auto`** — recognizes mordents, turns and
  trills (very quick stepwise figures around one note, each note faster
  than a 32nd) and writes each as a single note with its ornament sign in
  MusicXML output, adding a small accidental when the neighbor note
  isn't the one the key gives. MIDI output is unchanged — it has no way
  to hold an ornament sign. The speed threshold comes from one piece
  where real ornaments and ordinary 16th-note runs of the same shape
  were clearly apart; treat it as a first calibration
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

# MusicXML score instead of MIDI (or --format both for the pair)
python3 ScorePrep.py transcription.mid clean.musicxml

# MusicXML with tuplet brackets and ornament signs, in a key you choose
python3 ScorePrep.py transcription.mid clean.musicxml --tuplet-detection auto --ornament-detection auto --key F#m

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
| `--tempo` | Output tempo (BPM), flat for the whole piece. Default: the source's own tempo curve if it has a genuine one, otherwise source tempo → rhythm estimate → 120 |
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
| `--tuplet-detection {auto,off}` | [experimental] Detect local n-tuplet bursts and 2-3 note clusters too tight for the grid, and space those onsets evenly. Default: `off` |
| `--tuplet-fit-tolerance {relative,absolute}` | With `--tuplet-detection auto`: `relative` (default) requires a fit within 15% of the group's own subdivision width; `absolute` uses the older flat 24-tick threshold |
| `--trust-tempo-curve` | Keep the source's multi-event tempo curve and beat positions even when the curve looks erratic |
| `--fast-notes {auto,off}` | `auto` (default): notes too quick for the 16th grid, which would merge onto one tick, are written as 32nds. `off`: leave them on the 16th grid |
| `--beat-tracking {auto,on,off}` | Find the beats from the real note timing. `auto` (default): when the source's tempo map looks erratic. `on`: always (experimental). `off`: never. Ignored when `--tempo` is given |
| `--format {midi,musicxml,both}` | What to write. Default: decided by the output file's extension (`.musicxml` / `.xml` → MusicXML, anything else → MIDI) |
| `--key KEY` | MusicXML only. Key to write in: `A`, `F#m`, `"Bb minor"`, or a number of sharps (+) / flats (−) like `-3`. Default: estimated from the notes |
| `--max-voices N` | MusicXML only. Most voices per staff, 1–4. Default: `2` |
| `--ornament-detection {auto,off}` | [experimental] Write mordents, turns and trills as ornament signs in MusicXML output. Default: `off` |
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

Chord size is counted in piano keys: two instruments doubling the same
pitch become one key, and a pitch re-struck while still sounding is cut
where the next strike starts, so what it writes is exactly what ScorePrep
reads. The source's tempo changes and time signature are carried through
to the output.

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

- ~~MusicXML export~~ -- **implemented as `--format musicxml|both`**,
  with key estimation and pitch spelling (see "MusicXML export" above
  and the Changelog). Remaining open items:
  - `--grid triplet` in MusicXML: needs triplet brackets worked out beat
    by beat; refused with a clear message for now.
  - Key changes inside a piece: one key signature is written for the
    whole score.
  - Beams and the playback sustain pedal aren't written.
  - Spelling was validated on classical scores; the three test pieces
    here have no printed score to check against.
- Rebrand ScorePrep beyond "AI MIDI cleaner" into something bigger --
  not yet scoped (what the bigger identity actually is, what stays vs.
  changes in the pitch/positioning, whether the name itself changes).
  Flagged as a direction to think through, not a concrete plan yet.
- Local (non-AI) audio-to-MIDI transcription, as a new companion tool --
  a classical-DSP alternative to what upstream AI transcribers (MT3,
  ByteDance, Magenta) do, feeding into ScorePrep's existing cleanup
  pipeline instead of depending on one. Realistic scope, broken down by
  sub-problem (this is a research-depth gap in places, not just an
  engineering one -- being upfront about where the ceiling actually is):
  - Onset detection and tempo/beat tracking: solid without AI (spectral
    flux, e.g. librosa/aubio's classical algorithms) -- not really a gap
  - Monophonic pitch tracking (one voice/instrument at a time): also
    solid without AI (YIN/autocorrelation-based)
  - Polyphonic multi-pitch estimation (simultaneous notes/chords):
    genuinely harder -- overlapping harmonics from simultaneous notes
    are ambiguous from spectral data alone in a way classical methods
    (NMF, harmonic sum spectra) measurably plateau on below deep
    learning, per pre-2010s MIREX/MAPS benchmark results. Doable for
    sparse voicing; hard for dense/heavily-pedaled material
  - Multi-instrument separation from a mixed/mp3 source: the hardest
    part and where the AI-vs-not gap is widest -- classical source
    separation (NMF, ICA) generally needs more channels or stronger
    assumptions than a stereo mp3 gives
  - Realistic v1 scope: solo piano or a single melodic instrument,
    reasonably sparse voicing -- not full mixed-band multi-instrument
    transcription at AI-model quality, which isn't realistic classically
- Improve and update `reduce_test.py` -- it's lagged behind
  `ScorePrep.py`'s pace of development for a while now (last major work
  was drum exclusion, pre-collapse pipeline, cross-track unison dedup,
  voice importance scoring, register collision detection, glide/vibrato
  collapse, interactive mode). Needs a fresh look at what it's missing
  relative to where the main tool has moved since.
- ~~Ornament detection~~ -- **implemented as `--ornament-detection
  auto|off`** (default `off`, experimental): mordents, turns and trills
  become ornament signs in MusicXML output. Remaining open items:
  - Calibrated on one real piece (14 mordents found, all the same
    shape); turns and trills are only tested on constructed examples.
    Needs more real pieces before `auto` could be the default.
  - Slower ornaments (notes of 16th length or more at the notated
    tempo) are deliberately left as written-out notes.
- ~~Irregular tuplet support~~ -- **implemented as `--tuplet-detection
  auto|off`** (default `off`, same unvalidated-experimental status as the
  other three flags below), see Changelog and the
  `detect_tuplet_groups` docstring. Doubles as per-passage auto-detection
  as originally scoped. Remaining open items, in priority order:
  - Validate via `benchmark_experimental.py`'s new `tuplet` feature on
    real pieces (same bar the other three experimental features are
    already held to) before ever considering `auto` as the default.
  - Fixed 24-tick fit-tolerance (n-tuplets) and 1x-GRID duration cutoff
    (sub-grid clusters) were both calibrated against only a handful of
    real pieces; worth revisiting as more real pieces go through the
    benchmark.
  - One known residual case, not yet addressed: a normal-duration note
    landing right after a sub-grid cluster can still coincidentally
    collide with that cluster's now-fixed anchor tick. Loosening the
    duration cutoff to also catch it isn't safe on current evidence
    (some real false-positive chord-jitter notes have durations as low
    as 4-5 ticks) -- needs a different signal, not just a looser number.
- ~~Full tempo-track passthrough~~ -- done, see Changelog. While
  investigating this on the same real piece above, also found and fixed
  a bigger, previously-unknown bug: non-384-PPQ sources were never
  actually rescaled into the internal grid space, silently distorting
  quantization on any such file (~25% for a 480-PPQ source, compounding
  over the piece) -- see Changelog for details. Later hardened further:
  erratic multi-event tempo curves (transcription timing jitter
  mislabeled as tempo changes, not real macro tempo direction) are now
  auto-detected and discarded in favor of one estimated representative
  tempo, with `--trust-tempo-curve` as an explicit override -- see
  Changelog.
- Grace-note detection/notation -- now much cheaper to add, since
  MusicXML has a real grace-note element and the ornament pass already
  finds quick lead-in notes. Currently no dedicated classifier;
  short grace notes either get caught by `--min-note-ticks` filtering or
  survive as oddly-short competing notes rather than real grace-note
  glyphs. Moderate effort (duration alone can't distinguish a grace note
  from a legitimately fast written note) and moderate frequency across
  expressive piano arrangements generally. Related to the ornament
  detection item above but distinct: a true grace note has no rhythmic
  value of its own at all, which is a different data model than a
  fast-but-real ornament note
- Mid-passage clef borrowing (a fast run temporarily notated in the
  other staff's clef to avoid ledger lines) -- lowest priority: rarest of
  the four, and would need a genuinely different per-note clef data model
  rather than the current treble/bass split, which is a bigger
  architectural cost than anything else on this list for a cosmetic
  engraving nicety. `--hand-assignment` already gives a reasonable
  pitch-based approximation; only worth picking up if real benchmarked
  pieces turn out to need it often enough to justify the cost
- Chord-conflict resolution direction (prefer shortest vs. longest)
- Same-pitch overlap priority (truncate earlier vs. delay later note)
- Instrument/channel assignment on output (`program_change`)
- Custom track naming
- Validating the experimental voice-role-based features (`--melody-preservation`,
  `--dynamic-split`, `--hand-assignment`, `--tuplet-detection`) against real pieces
  before considering any of them stable or on by default
- Exact repeat detection (post-quantization measure hashing, velocity excluded) + 1st/2nd-ending suggestion as a direct extension — optional, user-confirmed, not auto-applied; explicitly excludes fuzzy/ornamented/transposed repeat matching

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Notes / assumptions

- Works internally at 384 ticks per beat (the default for ByteDance /
  MT3 / Magenta piano transcription exports). A source at any other
  resolution (e.g. 480 from most DAWs) is rescaled to it on load.
- Picks whichever track in the source file has the most `note_on`
  events by default (overridable with `--track`/`--channel`).

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
