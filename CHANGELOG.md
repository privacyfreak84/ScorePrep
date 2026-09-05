# Changelog

All notable changes to ScorePrep are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [1.2.7]

### Fixed

- **`--grid triplet` made the optimizer far too reluctant to sustain
  notes, sounding rushed/choppy ("accelerated").** `artic_weight`'s cost
  is documented as being per *sixteenth-equivalent* of invented duration,
  but was actually applied per raw grid unit -- fine under the default
  straight grid (a grid unit *is* a sixteenth there), but triplet mode's
  grid unit is 3x finer, so the same absolute extension counted as 3x
  more units and cost 3x more, making the optimizer choose far more
  short/truncated notes with visible rests instead of natural sustain,
  for no musical reason -- purely an accident of which grid happened to
  be selected. Fixed by normalizing the fabrication cost by the grid's
  scale factor. Found via a real piece: total invented-sustain duration
  under `--grid triplet` went from being ~12x lower than the equivalent
  `--grid straight` run (should be roughly comparable) to ~2x lower,
  with the remaining gap explained by the finer grid legitimately being
  able to close gaps more precisely rather than any remaining bias.
  Confirmed byte-identical no-op for `--grid straight` (the default),
  since its scale factor is 1 -- verified across both benchmark pieces
  and the full tie-temperature/experimental-flag matrix.

- **Non-384-PPQ sources were silently misquantized.** Every tick position
  was read straight off the source file and fed into grid/bar math that
  assumes `TICKS_PER_BEAT=384`, with no actual rescaling -- the existing
  `NOTE: ... results may be off` warning undersold it badly. For a
  480-PPQ source (a common DAW/export default) this meant a ~25% timing
  distortion that *compounds* over the piece, since grid snapping doesn't
  fail gracefully: what should be one beat gets treated as 1.25 beats,
  every single onset. Found via a real piece (480 PPQ) that came out
  "slow in some places, fast in others, not on time at all" -- root
  cause wasn't tuplets as first suspected, it was this. Fixed by
  rescaling every extracted tick (notes, sustain-pedal CC64 intervals)
  by `TICKS_PER_BEAT / source_ticks_per_beat` right at load time, so
  everything downstream operates in a consistent internal tick space
  regardless of the source's own PPQ. Confirmed on the real piece: beat
  count and real-world playback length now match the source to within
  quantization rounding (previously off by 46%). Zero effect on
  already-384-PPQ sources -- confirmed byte-identical against prior
  output across both benchmark pieces, full TT sweep, all experimental
  flag combinations.
- Removed `fix_same_pitch_overlaps` and the redundant duration-optimizer
  pass that followed it. `optimize_staff_durations` already bounds every
  note's ceiling by its own next same-pitch onset, so same-pitch overlaps
  were never actually possible after its first pass -- the "fix" step was
  misfiring on true unisons (two notes of the same pitch starting at the
  same instant) instead, truncating one of them to a single grid tick,
  which the following optimizer pass then silently overwrote back to the
  correct value. Net effect on real output: none -- confirmed
  byte-identical against the prior behavior across both benchmark pieces,
  a synthetic unison-heavy stress file, the full tie-temperature sweep,
  and every experimental flag combination. Slightly faster (one fewer
  full pass per staff) and removes a dead-code trap for future changes.

### Added

- `--tuplet-detection auto|off` (default `off`, same experimental/
  unvalidated status as `--melody-preservation`/`--dynamic-split`/
  `--hand-assignment`). Finds local n-tuplet bursts (quintuplets,
  septuplets, nontuplets, etc. against a 1- or 2-beat span) directly in
  the source, independent of `--grid`, and snaps just those onsets to
  *exact* even division instead of letting each one round independently
  to the outer straight/triplet grid. Doubles as the auto-detection this
  was originally scoped alongside: choosing the right subdivision locally
  per onset-cluster is a strictly better fit than one global grid choice
  for the whole piece, and directly replaces most of the reason to reach
  for `--grid triplet` manually. Full design: `docs/tuplet-detection-design.md`.

  Motivated by, and validated against, a real piece's ~9-onset burst that
  used to quantize to jagged durations (64/64/96/64/96/96/128/96/128/160
  ticks) despite the overall span being correct -- with detection on, the
  first 5 of those 9 onsets now resolve to a clean, even ~77 ticks each.
  Calibration note: an early version's fit-tolerance scaled with
  `span/N`, which structurally favors smaller N (more absolute slack for
  the same span) regardless of what's actually in the piece -- caught
  empirically when quintuplets dominated detections even on pieces with
  no independent evidence of being tuplet-heavy. Replaced with a fixed
  24-tick tolerance, calibrated against real data: a piece independently
  confirmed tuplet-dense (via its own notated score) shows 38 detected
  groups at this threshold, versus 7 and 0 on two pieces with no such
  evidence -- the best calibration signal available without ground-truth
  tuplet annotations for every benchmark piece. `benchmark_experimental.py`
  extended with a fourth `tuplet` feature for real A/B validation.
  Confirmed `off` (default) is byte-identical to pre-existing output;
  `auto` runs clean across the full TT/grid/experimental-flag matrix.
  Known v1 limitation: greedy left-to-right matching can settle for a
  partial group (e.g. 5-of-9) instead of holding out for a better
  whole-group fit -- named in the design doc, not a new surprise.

- Full tempo-track passthrough. Previously only the *first* `set_tempo`
  message in the source was read, and the output always got one flat
  tempo marking for the whole piece -- any rubato/local tempo changes in
  the source were silently discarded (contributing to the same "not on
  time" symptom above, alongside the PPQ bug). Now, when `--tempo` isn't
  given explicitly and the source has more than one genuine tempo event,
  the full curve is preserved in the output instead of flattened.
  Explicit `--tempo` still means one flat tempo, as before, to avoid
  ambiguity about what "override one value out of a dozen" should mean.

## [1.2.6.n]

### Added

- `reduce_test.py`, a standalone companion script (not part of
  ScorePrep.py, no shared code) for a separate problem: reducing a
  multi-track band/ensemble MIDI down to a single playable piano part,
  meant to feed into ScorePrep.py next when your source isn't already
  piano MIDI. See the README's "Companion tool" section. Explicitly a
  prototype, not yet benchmarked against real pieces.

### Fixed

Found while hardening `reduce_test.py` for repo inclusion:

- Missing/unreadable input file, malformed `--importance-weights`
  (wrong count, non-numeric, or negative), a non-positive
  `--max-chord-size`, and Ctrl+C/EOF during interactive mode all used to
  crash with a raw traceback (or, for `--max-chord-size`, silently
  produce confusing output instead of erroring). All now fail with a
  clear message instead.
- Selecting an unnamed track by number in interactive mode matched it
  by its index as a plain substring (e.g. "1"), which could
  accidentally match an unrelated track whose name happened to contain
  that digit (e.g. "Guitar 1"). Now matched by exact index instead.
- Every numeric CLI flag and interactive prompt now rejects
  out-of-range values (negative fractions/tick counts) up front rather
  than accepting them silently.
- The output directory is now created automatically if it doesn't
  exist, and a write failure is reported cleanly instead of crashing.
  A run with no notes found now prints a warning instead of silently
  writing an empty file with no explanation. A final "Saved <path>"
  line now confirms success, matching ScorePrep.py's own convention.
- Added `--list-tracks`, a dry-run flag that prints the track listing
  and exits without running the reduction, for checking track numbers
  before committing to melody/bass/priority choices.

## [1.2.6]

### Added

- The diagnostic report now ends with a "Confidence Warnings" section
  listing the bars where the experimental classifiers are least sure of
  themselves -- combining low-confidence melody/accompaniment calls with
  any hand-span violations from `--hand-assignment` into one "here's
  where I might be wrong" view, worst first. No switch to turn on --
  it's pure diagnostics, always shown when there's something worth
  flagging, and can't change engraving output.

### Fixed

- `hand_warnings` could be referenced before assignment when
  `--hand-assignment` was off, a latent bug from when that feature was
  added -- caught while wiring up the confidence-warnings section above,
  no observable effect before now since nothing read it in that path.
- `--split-window-bars 0` (or negative) crashed with a raw
  ZeroDivisionError instead of a clear error message. Negative
  `--max-hand-span`/`--hand-ambiguity-zone` were silently accepted
  instead of rejected. All three now validate up front with a proper
  error message; found via stress-testing the new experimental features
  against invalid/boundary inputs before shipping them.
- The duration optimizer was undercounting how many tied noteheads a note
  crossing a barline would actually need, for any tie-temperature above
  0.0. It assumed a barline split always costs exactly one extra
  notehead, but a split forces the pre-barline segment to whatever's
  left until the barline -- which is often not a clean note value even
  when the note's full duration would have been, and that remainder can
  need extra noteheads of its own. In practice this meant some candidate
  durations looked cheaper than they really were, so the optimizer
  occasionally chose more cross-bar-tied notation than your
  tie-temperature setting actually called for. Fixed by having it
  properly split the note at each barline and count each resulting
  piece on its own. Net effect on a real test piece: noticeably fewer
  cross-bar ties at the same tie-temperature (e.g. TT 0.15: 230->178 and
  286->199 across the two staves), i.e. cleaner notation than before at
  the same settings.

### Added

- `--hand-assignment on` [experimental] reconsiders notes sitting close
  to the treble/bass split point within each chord, instead of assigning
  every note purely by which side of the line it falls on. A boundary
  note moves to the other hand if it's actually closer to that hand's
  recent position than to its naively-assigned hand's (e.g. "the left
  hand is already busy an octave lower"), and only if doing so doesn't
  stretch either hand's chord past `--max-hand-span` (default 16
  semitones, a 10th). Notes not near the boundary are never touched.
  Composes with either a fixed `--split-pitch` or `--dynamic-split` --
  refines whichever boundary is already in place rather than replacing
  it. Onsets that still need more than one hand's reach even after
  reassignment are reported as a real, unavoidable stretch rather than
  guessed at further. Off by default; available in interactive mode's
  advanced options too.
- `--dynamic-split on` re-estimates the treble/bass split point every
  `--split-window-bars` bars (default 8) instead of using one fixed
  split for the whole piece, so the split follows the music's register
  drifting over time (e.g. a verse sitting lower than a chorus). Off by
  default. A sparse window falls back to the previous window's split
  rather than a fresh guess, and the per-window change is capped at 4
  semitones, so it drifts instead of jumping around. Available in
  interactive mode's advanced options too.

## [1.2.5]

### Added

- `--melody-preservation on` [experimental] biases the duration
  optimizer's weights per note using the voice-role classifier below:
  melody gets cheaper ties and costlier rests (protect its continuity),
  accompaniment gets the opposite (declutter more freely). Off by
  default -- reuses the existing cost-minimizing optimizer rather than
  adding a second decision path, so it's purely a weight bias, not a new
  algorithm. Available in interactive mode's advanced options too. The
  report's "Voice Roles" section says outright whether it actually
  influenced a given run.
- New internal `classify_voice_roles()` pass tags every note as likely
  "melody" or "accompaniment" with a confidence score, using a
  skyline-plus-smoothing heuristic (highest simultaneous pitch, chord
  density, note length, and relative velocity). This is groundwork for
  future features (melody preservation, smarter hand assignment,
  voice-aware quantization, confidence warnings), not a feature by
  itself yet -- nothing about the actual engraving output changes.
  Verified byte-identical output before/after on a real test file. A new
  experimental "Voice Roles" section in the diagnostic report shows the
  melody/accompaniment split and average confidence per staff, so the
  heuristic can be checked against real pieces before anything is built
  on top of it.
- `--profile {readable,balanced,faithful}` sets tie-temperature, pedal
  mode, grid, and duration style all at once instead of tuning them
  individually. "readable" favors fewest ties for sight-reading/practice,
  "balanced" is the benchmark-tested sweet spot, "faithful" sticks
  closest to the original performance. Available in interactive mode too.
  Any of the four flags given explicitly still overrides the profile's
  value for that flag.
- `report()` now ends with a "Measures Needing Attention" section listing
  the measures with the most combined issues (chord conflicts, rests,
  heavy tie chains, cross-bar ties, invented sustain), so you can jump
  straight to the spots actually worth a manual look instead of scanning
  the whole score. Diagnostics only -- doesn't change engraving output.

### Decided

- Closed out the question of whether the duration optimizer needs a
  voice-role-aware redesign (treating melody and accompaniment
  differently) for a future v1.3. A benchmark suite spanning 10 pieces
  across genres -- Bollywood, classical, jazz, film/anime, ambient --
  found no case where melody read well but accompaniment stayed
  cluttered in a way a global tie-temperature couldn't fix. The current
  global-tie-temperature optimizer stays as-is; no code changes.

## [1.2.4]

### Added

- `report()` now prints an `===== Optimizer Decisions =====` section per
  staff: why each note's written length was chosen, as 4 mutually
  exclusive categories (exact/no rest, rest kept, extended-but-truthful,
  invented sustain). Required actually instrumenting
  `optimize_staff_durations`'s decision loop (not just reformatting an
  existing report) -- added an optional `stats` dict parameter, populated
  additively from values the loop already computes; the note-selection
  logic itself is completely unchanged. Verified byte-identical output
  MIDI before/after on a real test file. Only collected on each staff's
  final optimization pass (after the same-pitch-overlap re-harmonize),
  so the counts reflect the actual final decision, not an intermediate
  one. Caught and fixed a real bug: fabrication is
  computed as a sum across an entire chord, but crediting a whole
  chord's worth of notes as "invented sustain" whenever that sum was
  nonzero over-counted -- a 2-note chord where only one note actually
  needed invented sustain was crediting both. Now categorized per
  individual note within the event.

## [1.2.3]

### Changed

- Reorganized the per-staff diagnostic report (`report()`) into clearly
  labeled sections (`===== Staff Split =====`, `===== Notation (TREBLE/
  BASS) =====`) with a tie-count breakdown (none / one tie / two+ ties)
  instead of one dense summary line. Pure diagnostics -- `report()` only
  reads already-finalized note data and prints, it doesn't mutate
  anything, so this has no effect on the actual engraving decisions or
  output MIDI (verified byte-identical before/after on a real test file).
  Also fixed an off-by-one in the new tie-count buckets during
  development: `true_tie_count` returns notehead count (minimum 1, even
  for a plain untied note), not tie count -- actual ties = noteheads - 1.

## [1.2.2]

### Fixed

- **Changing `--tempo` away from the detected value silently had no
  effect on real playback speed.** `rescale_notes_to_tempo` was built
  for raw MT3-style transcriptions, where ticks are laid out against a
  meaningless placeholder tempo and a user-supplied tempo is meant to
  *correct* that -- so ticks get stretched to compensate, keeping
  real-world duration locked to the original audio regardless of the
  number typed. That's correct for genuine MT3 input, but wrong for
  already-accurate sources, where a
  different `--tempo` is a deliberate request to actually speed up or
  slow down playback -- previously the stretch-and-relabel exactly
  canceled out, so the result played at identical speed no matter what
  tempo was entered. Added `--tempo-rescale {preserve-duration,
  change-speed}` (also prompted interactively when the entered tempo
  differs from the detected one). Default `preserve-duration` keeps the
  old MT3-oriented behavior; `change-speed` leaves ticks untouched so
  the tempo change is real.

## [1.2.1]

### Fixed

- **Optimizer was truncating genuinely long real notes to dodge ties.**
  The v1.2 cost model charged `tie_weight` for every tie a note needed,
  including ties required just to truthfully notate a real, long,
  evidence-backed note (zero fabrication involved). Since a single tie
  usually cost more than leaving a rest, the optimizer was chopping real
  sustained notes down to one short notehead + a big rest -- discarding
  real transcription data, and undoing exactly what tie-temperature was
  supposed to allow. Ties needed for a note's truthful,
  tie-budget-respecting length are now free (matching the pre-1.2
  behavior of always maximizing real length within budget); only ties
  spent *beyond* that truthful baseline -- i.e. fabricating extra
  sustain to close a rest -- carry a cost now. On a real 1271-note
  test file at tie-temperature=0.25: `needs-tie` went from 0/0 to
  406/360 (treble/bass) and `rests` dropped from 159/274 to 62/104.
  Re-verified: full regression suite, and the tie-budget/barline
  invariant independently re-checked across both test files, four time
  signatures, and the full temperature range -- zero violations.

## [1.2.0]

### Changed

- **Duration engraving redesign.** Replaced the three-pass patch
  sequence (`resolve_note_durations` snap-down -> `sync_chords` ->
  `fix_same_pitch_overlaps` -> `sync_chords` -> `fill_small_gaps` ->
  `sync_chords` again) with a single cost-minimizing optimizer
  (`optimize_staff_durations`) run once per onset event (a note, or a
  chord -- every member of an event always gets the same duration, so
  chords still can't fracture). For each event it picks the duration
  minimizing `tie_weight * ties + rest_weight * (rest present) +
  articulation_weight * (grid units of sustain invented beyond the
  note's real transcribed length)`. `--tie-temperature` still drives
  all three weights by default (low temperature: avoid ties, cheap to
  fabricate a small extension to kill a rest; high temperature: ties
  are free, fabrication is expensive -- fidelity to real timing wins).
  `sync_chords` and `fill_small_gaps` are gone; their jobs are now
  built into the one decision instead of patched on afterward.
- `--max-silent-gap` removed -- superseded by the optimizer's
  `articulation_weight`, which makes the same tradeoff per-note based
  on actual cost instead of a single flat threshold.
- Added `--tie-weight`, `--rest-weight`, `--articulation-weight`
  (all optional, `[advanced]`) to override any of the three costs
  individually without touching `--tie-temperature`'s other effects.
  Available in interactive mode's advanced options as an opt-in
  override (default: derived from tie-temperature, shown as the
  suggested value).
- **Fixed a latent tie-counting gap:** a note spanning a barline needs
  an extra tied notehead regardless of its duration *value* (MuseScore
  can't draw one notehead straddling a barline), but `minimal_tie_count`
  only ever counted ties from the value. This was always technically
  present, but low-impact under the old algorithm since it never
  extended a note past its own real length. The new optimizer actively
  considers extending notes to close rests, including across
  barlines, which would have made this gap load-bearing. Added
  `true_tie_count(onset, units, bar_ticks)` (value ties + one per
  barline crossed) and switched every tie-budget check -- the
  optimizer, `resolve_note_durations`'s own natural pick,
  `fix_same_pitch_overlaps`'s re-snap, and the `report()` stats -- to
  use it. Verified independently (not just via the tool's own stderr
  report) against real output: 0 tie-budget violations across
  tie-temperature 0.0-1.0 and time signatures 3/4, 4/4, 5/4, 6/8.
- Per-staff report now also prints `extended=N (X sixteenths
  invented)` -- how many notes the optimizer extended past their real
  transcribed length, and by how much, in one place alongside
  `needs-tie` and `rests`.

## [1.1.0]

### Added

- `--max-silent-gap` now configurable in interactive mode's advanced
  options, with an explicit (color-highlighted, when the terminal
  supports it) warning that raising it usually isn't the fix for
  "too many rests" at low tie-temperature -- the tie budget itself is
  almost always the real bottleneck. Raising `--tie-temperature`
  instead is usually far more effective.
- Per-staff report now prints `rests=N` alongside `needs-tie=N` -- the
  tie/rest tradeoff was always there, but only half of it was visible
  before. Verified against a real file: `rests` dropped from 391 to
  118 going from tie-temperature 0.0 to 0.1 with zero tie cost (the
  tie budget itself doesn't increase until roughly 0.15-0.2), then
  jumped sharply once the tie budget increased -- a genuine "elbow" in
  the tradeoff, not a smooth curve, and now visible in the tool's own
  output instead of requiring a manual sweep to discover.

### Improved

- **Excessive rest fragmentation:** a note's notated duration was
  computed purely from its own natural release time, with no awareness
  of when the next note starts, so almost every small, non-deliberate
  gap between a note's release and the next onset became a rest —
  the dominant source of visual clutter in output scores, not genuine
  short rests. `--max-silent-gap N` (default 2 grid units) now extends
  a note to close a small trailing gap instead of leaving a rest,
  capped so it can never create a note overlap or violate the
  temperature-scaled bar-span limit `--tie-temperature` already
  enforces. Verified against the tie-budget and bar-span invariants
  across all `grid × temperature × gap-threshold` combinations, 0
  violations.

  (this is only a workaround, a rework is needed to truly balance readbaility, ties and rests, will follow in next versions)

### Added

- `--track` now accepts a comma-separated list (`1,2`) or `all` to merge
  multiple tracks into one pass, instead of requiring separate runs per
  track. Useful for sources with separate right-hand/left-hand tracks.
  Interactive mode's track prompt updated to match.
- Interactive mode remembers the last input/output paths used (stored in
  `~/.config/scoreprep/config.json`) and offers them as defaults, so
  repeated test runs on the same file don't require retyping/pasting
  paths. If the same input file is reused, the previous output path is
  offered too (not just the auto-generated name).

## [1.0.0] — 2026-07-19

First public release. (Formerly developed under the working name
`grand_staff_cleanup.py`.)

### Added

- Grand-staff split (treble/bass), tie-temperature fidelity/readability
  dial, sustain-pedal handling (`--pedal-mode`), tempo estimation with
  three-tier fallback, transcription-noise filtering, interactive
  step-by-step mode.
- Playback-sustain decoupling: notation stays clean and tie-light while
  a separate sustain-pedal (CC64) automation track keeps MIDI playback
  sounding true to the original performance length.
- `--grid {straight,triplet}` — quantize to a grid that natively fits
  triplet-eighth subdivisions instead of flattening them onto straight
  16ths.
- `--track N` / `--channel N` — manual override for source files where
  automatic note-track detection picks the wrong track, plus
  transparency about *why* a track was auto-picked and a warning when
  another track has a comparable note count.
- `--clean-durations {dotted,powers2}` — restrict single-notehead
  durations to plain power-of-two values only, for a plainer engraving
  style.
- `--min-velocity`, `--velocity-mode {passthrough,normalize,scale}`,
  `--velocity-scale` — ghost-note floor and dynamics reshaping,
  normalization computed after floor filtering so dropped ghost notes
  can't skew the range.
- Tempo-ambiguity transparency: when two candidate tempos fit the
  rhythm almost equally well (a fundamentally unresolvable octave
  ambiguity from timing alone), the tool names both instead of
  silently guessing.

### Fixed

- **Tempo/tick mismatch:** output tempo was previously just a label —
  source tick positions were copied unchanged, so playback speed was
  wrong whenever the chosen tempo differed from the source's own
  tick-encoding tempo. Fixed by rescaling ticks through real seconds so
  playback always matches the original audio regardless of notated
  tempo.
- **Tempo estimation's octave bias:** an early onset-grid-alignment
  heuristic measured error in each candidate tempo's own grid-cell
  size, systematically biasing every estimate toward half tempo.
  Replaced with a scale-invariant ratio-based metric.
- **Leading empty measures:** if the first note didn't start at tick 0
  (typically real leading silence in the source audio), the score
  rendered that gap as blank measures. Now rebased so the score starts
  at the first note, with a message when the removed gap is non-trivial.
- **Tie-budget invariant violation:** `--tie-temperature 0.0` promises
  zero ties, but same-pitch overlap resolution could truncate a note to
  a duration that wasn't one of the "clean" tie-free values, silently
  breaking that guarantee. Fixed by re-snapping the truncated duration
  within the same tie budget.
- **Chord fracture under `--pedal-mode reflect`:** overlap resolution
  could desync one member of an otherwise-synced chord. Chords are now
  re-synced after overlap fixing.
- Chord-sync tolerance now scales continuously with `--tie-temperature`
  instead of a hard on/off switch. Missing input files report a clean
  error instead of a traceback. Defensive rounding fix for exotic time
  signatures.
