# Changelog

All notable changes to ScorePrep are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
