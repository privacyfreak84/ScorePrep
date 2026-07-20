# Changelog

All notable changes to ScorePrep are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
