# Changelog

All notable changes to ScorePrep are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [1.2.8]

- **Attempted fix for lost staccato articulation, tried and reverted this session -- see HANDOFF.md for the open investigation.** Found via a real measure-by-measure comparison (RiverFlowInYou.mid bar 1): the original transcription shows staccato marks when opened directly in MuseScore, at a passage where ScorePrep's output showed none. Traced the actual mechanism (after two wrong theories -- optimizer cost weights at low tie-temperature, then "the source data just doesn't have it"; neither survived checking real numbers): every one of 7 consecutive onsets in that passage has a genuine 5-8 tick gap in the source MIDI, below GRID's 96-tick (16th-note) resolution -- both the note's real end and the next onset's real start round to the *same* grid tick, so quantization erases the gap entirely, before the optimizer or tie-temperature ever get involved. Built `apply_articulation_gaps` (per staff, right before MIDI export) to shorten just the *exported* note-off -- not the notated duration -- for any note with a real, currently-swallowed source gap, restoring a clear enough gap for MuseScore's import to read as staccato.
  **Reverted after the next measure-by-measure comparison showed it over-firing**: it marked all 5 chord-onsets of an entire fast run as staccato, when the original score only marks one specific note. The "does a real gap exist at all" check can't distinguish "this note is genuinely meant to be detached" from "this is just how a fast run of separately-struck notes gets transcribed" -- both produce the same small-gap signature in the source data. This is a real, unsolved discrimination problem, not a threshold to retune with more confidence. Code fully reverted (`apply_articulation_gaps`, `STACCATO_GAP_MIN_TICKS`, `STACCATO_SHORTEN_FRAC`, `export_end` all removed); confirmed byte-identical output to the pre-attempt baseline. Kept here as a record of what was tried and why it didn't work, so it isn't re-attempted the same way.

### Fixed

- **Leading-silence rebase silently stripped genuine short rests at the
  start of a piece, shifting every downstream barline.** Found via the
  first real measure-by-measure comparison against the actual notated
  score (River Flows In You, MuseScore render vs. original sheet music):
  content that should start measure 2 (a quintuplet run) was pulled back
  into measure 1 in ScorePrep's output. Root cause: the source's first
  note begins 0.59 beats in (a real eighth-rest pickup, clearly notated
  in the original score) -- but the leading-silence rebase (meant to
  strip real transcription dead-air before a performance starts, e.g. a
  multi-second intro) stripped this too, snapping the first note to tick
  0 and shifting the entire piece earlier by that amount, silently: the
  print threshold for this rebase was already 1 beat, but the rebase
  ITSELF had no matching lower bound, so anything under 1 beat got
  stripped with zero warning. Fixed by giving the strip the same 1-beat
  floor the message already implicitly judged as the right cutoff: a
  sub-beat gap is now left alone entirely (real notated rest, not
  transcription noise), while genuine multi-beat gaps still rebase
  exactly as before. Verified: RiverFlowInYou.mid (0.59 beats) no longer
  rebases at all; kalank.mid (1.0 beat, boundary case) and Sukoon (6.0
  beats, clearly a real intro) are unaffected, rebasing exactly as
  before. This is a distinct, previously-unknown bug from the tempo
  curve and sub-grid-cluster fixes already shipped -- a one-time
  constant offset misaligning every barline throughout the piece, not a
  speed/tempo issue at all, though it very plausibly compounded the
  overall "something's off" perception those fixes were chasing.

### Added

- **Sub-grid cluster detection (`--tuplet-detection auto`, same flag as
  before -- now catches a second, smaller failure mode).** Investigated
  the "successive notes collapsing into one" symptom directly: 13 of 14
  flagged spots on RiverFlowInYou.mid were genuine same-hand collisions
  (the 14th was a melody note and an unrelated bass note sharing a tick
  coincidentally -- not a real problem, since they land on different
  staves anyway). All 13 turned out to be real, consistently-recurring
  melodic ornaments (stepwise turns/mordents, e.g. a whole-step-up-and-
  back figure), not incidental fast passagework or classic zero-duration
  grace notes -- durations were short-but-real (22-57 ticks) and the
  same figure recurs throughout the piece. New `detect_subgrid_clusters`
  runs the same detect-before-quantize-and-protect mechanism as
  `detect_tuplet_groups` (after it, skipping anything already
  tuplet-tagged), but with a completely different, narrower trigger:
  N=2-3 onsets that the outer grid would actually collide onto the same
  tick, spread evenly across that one grid slot instead. N=2/3 is
  exactly what `detect_tuplet_groups` deliberately excludes (unsafe at
  full-beat scale, where almost any ordinary rhythm trivially has 2-3
  onsets per beat) -- safe here only because the trigger is the actual
  collision itself, not the onset count, which normal-speed material
  never produces regardless of N. Needs one guard the beat-scale search
  doesn't: hands aren't split yet at this point in the pipeline, so a
  new `SUBGRID_MAX_PITCH_GAP` (12 semitones) keeps a coincidental
  melody/bass overlap (the 14th spot above, 33 semitones apart) from
  being treated as one figure.
  **False-positive investigation before shipping this:** first attempt
  (pitch+time gating alone) found 0/109/186 "clusters" on kalank/
  Sukoon/RiverFlowInYou -- wildly implausible, since Sukoon and kalank
  showed no evidence of this problem during the original investigation.
  Inspected them directly: they were ordinary long-held chords with a
  few ticks of natural strike jitter between notes (durations in the
  hundreds/thousands of ticks), sneaking through because the pitch gate
  only checks ADJACENT pairs in a chain -- a whole chord can pass as a
  "connected" chain even when its full span is wide, if each
  consecutive step happens to be small enough. Added
  `SUBGRID_MAX_NOTE_DURATION_FACTOR` (1x GRID): only notes shorter than
  one grid unit are eligible at all, which real ornaments always are and
  held chords never are. Re-verified after the fix: kalank 0, Sukoon 2
  (manually inspected -- both genuine short turns), RiverFlowInYou 11-13
  depending on how early noise-filtering interacts with one very short
  member. Also caught and fixed a related bug in the clustering itself
  during this work: the time/pitch chain could greedily extend to a 3rd
  onset that broke the actual collision (e.g. two ticks colliding, a
  third landing in the next grid slot over), losing the valid 2-onset
  match entirely -- fixed by trying successively smaller sub-clusters
  before giving up on a chain instead of only ever checking the full
  chained length.
  **One known residual case, deliberately not chased further:** a
  longer "landing" note (137 ticks -- past the ornament, back to a
  normal note) can still coincidentally collide with an already-fixed
  cluster's anchor tick. Checked whether loosening the duration cutoff
  to also catch it (e.g. 2x GRID) was safe: no -- some of the
  chord-jitter false positives found above have durations as low as
  4-5 ticks, so duration alone isn't a clean separator at any threshold,
  and loosening it risks reopening exactly the false-positive door just
  closed. Left as a known gap rather than adding more heuristics on
  weaker evidence.
  `--tuplet-detection off` unaffected by construction -- verified
  byte-identical output across runs, no new messaging.

- **Rejected tuplet-candidate windows can still leave a real quantize
  collision behind -- now repaired instead of left alone (same
  `--tuplet-detection auto` flag).** `detect_subgrid_clusters` (above)
  deliberately excludes any note >= one grid unit long, correctly -- a
  long-held note isn't an ornament -- but that leaves exactly one case
  unresolved: a genuinely long, correctly-excluded held note colliding
  with unrelated material once a candidate tuplet window containing it
  gets (correctly) rejected as not actually a tuplet. Found on
  RiverFlowInYou.mid tick ~6114: loosening the fit-tolerance threshold
  enough to accept that window would also re-accept a genuinely bad
  group elsewhere (see the `--tuplet-fit-tolerance` entry below) -- the
  threshold itself can't fix this, so `detect_tuplet_groups` now also
  reports which windows it rejected, and a new
  `resolve_rejected_tuplet_collisions` pass checks each one for a real
  same-tick collision under plain quantization and nudges just the
  colliding note(s) forward to the next free grid tick, preserving their
  real recorded duration.
  **False-positive investigation before shipping this (same discipline
  as the sub-grid work above):** the naive version -- repair any real
  collision inside any rejected window -- nudged 626 notes on kalank and
  182 on Sukoon, both supposed to be clean control files. Traced to two
  distinct causes, fixed in order: (1) "rejected window" was being
  recorded once per onset inside a burst rather than once per burst,
  so heavily overlapping windows re-processed and compounded the same
  notes' shifts repeatedly -- fixed by deduplicating recorded windows
  against onsets already claimed by an earlier one (separate bookkeeping
  from `detect_tuplet_groups`' own accept/reject `used` set, so a
  smaller valid tuplet nested inside a rejected span can still be found
  exactly as before). (2) even a single pass per burst was still wrong:
  most of the "collisions" turned out to be an ordinary bass note and a
  melody/chord note struck together across hands (e.g. pitch 46 vs 65,
  ~19 semitones apart, or two ~1000-tick sustained notes an octave
  apart) landing on the same tick *on purpose* -- nudging those apart
  would have actively damaged the output, not fixed it. Added two gates
  before a collision is treated as repairable: the pair's pitches must
  be within `SUBGRID_MAX_PITCH_GAP` (reusing the same constant and
  reasoning as sub-grid clustering -- close register signals the same
  melodic line, not an intentional cross-hand simultaneity), AND at
  least one of the pair must be shorter than
  `SUBGRID_MAX_NOTE_DURATION_FACTOR` x GRID (this feature's actual
  target is specifically a long note colliding with something short and
  fast -- two long notes colliding is ordinary, not the case being
  fixed). A near-miss window is also now bounded to failing by no more
  than `NEAR_MISS_TOLERANCE_MULT` (2x) the accept threshold, so an
  ordinary passage that coincidentally has a tuplet-candidate onset
  count (common with an extra passing/ornament note) isn't treated as
  tuplet-adjacent at all. After all of the above: kalank and Sukoon both
  verified at 0 nudges (byte-identical to `--tuplet-detection off`
  output) across a 0.10/0.15/0.20 tie-temperature spot-check;
  RiverFlowInYou lands at 18, all manually spot-checked as short (17-70
  tick), small-pitch-gap-from-neighbor collisions consistent with real
  fast passagework, not chord jitter.
  **Calibrated on one real case, same caveat as the rest of this
  investigation:** `NEAR_MISS_TOLERANCE_MULT` and the two gates above
  are tuned against RiverFlowInYou's tick-6114 case and the kalank/Sukoon
  false positives it produced -- no second real "long note collides
  after rejection" example exists yet to confirm they generalize.

- **Erratic tempo curves are now discarded and replaced automatically,
  not just flagged.** A multi-event tempo curve is judged "erratic" if
  it swings more than 2x between its slowest and fastest event, or
  jumps more than 20 BPM between adjacent events -- the same signal
  found directly on a real problem file (RiverFlowInYou.mid: 3.1x
  slowest-to-fastest, up to a 39 BPM jump within a single beat). That
  level of volatility reads as transcription timing jitter mislabeled as
  tempo changes, not genuine macro tempo direction -- passing it through
  literally means the output genuinely speeds up and slows down by tens
  of BPM within a beat, which is indistinguishable from the "sounds
  accelerated/decelerated in different places" symptom on file for this
  piece. Both `--tempo`-omitted CLI runs and the interactive wizard now
  *default to discarding an erratic curve and estimating a single
  representative tempo instead* -- the same estimation already used
  when there's no curve at all (source file's own tempo if it looks
  genuinely measured, else rhythm-based estimation from note-onset
  spacing), factored into one shared `estimate_representative_tempo()`
  helper so every fallback path agrees. New `--trust-tempo-curve` CLI
  flag (interactive: just answer the existing preserve-anyway prompt)
  forces literal preservation for anyone who genuinely wants it despite
  the warning. Verified all four paths end-to-end: CLI auto-discard
  lands on the file's own sensible 40.5 BPM (not a raw rhythm-estimate
  artifact, which landed on an implausible 200 BPM before this fix was
  wired through the shared helper); `--trust-tempo-curve` still forces
  literal preservation; explicit `--tempo` is untouched; non-erratic and
  no-curve pieces (kalank) are unaffected. Calibrated against exactly
  one real erratic-curve example -- no second case exists yet to confirm
  the thresholds generalize; revisit if a genuinely rubato piece ever
  gets mis-flagged.

### Fixed

- **Interactive mode silently flattened every real tempo curve to one flat
  BPM value, even on pieces with genuine tempo changes throughout.** The
  CLI-flag path already preserves a source's full tempo curve whenever
  `--tempo` is left unset -- but the interactive wizard's tempo question
  always produced a concrete float (even just accepting the shown
  default counts as "typing a tempo"), which `run()` correctly treats
  the same as an explicit `--tempo`: flatten to one value, discard the
  curve entirely. On a piece with real expressive tempo variation (e.g.
  RiverFlowInYou.mid's 54 genuine tempo changes, 25.2-76.9 BPM), this
  meant every wizard run forced the whole piece to a single speed,
  regardless of what the user answered -- which reads exactly like
  "accelerated in some places, slow in others" once played back at one
  fixed tempo against music that was never actually constant. This was
  the real cause of that symptom, not the tuplet-detection or hand-note
  issues investigated first. Fixed: the wizard now checks for a genuine
  multi-event tempo curve up front (same generic-default filtering as
  the CLI path) and, if found, asks whether to preserve it (default:
  yes) before ever asking for a flat BPM value -- matching what
  omitting `--tempo` already does on the CLI. Verified on
  RiverFlowInYou.mid: choosing to preserve correctly reaches the
  existing "source file has 54 tempo changes... preserving the full
  tempo curve" path; choosing to flatten still works and reaches the
  existing tempo-rescale-intent question as before. Pieces with no
  genuine curve (e.g. kalank.mid) never see the new prompt at all.
  CLI-flag path (`--tempo` omitted or given) is untouched -- this only
  changes how the wizard *decides* what value to hand `run()`.

- **Interactive mode never asked about `--tuplet-detection`.** The flag
  shipped correctly on the CLI-argument path, but the `--interactive`
  wizard's advanced-options section only prompted for melody
  preservation/dynamic split/hand assignment -- tuplet detection was
  simply missing, so anyone using the wizard had no way to turn it on
  short of editing the command by hand. Added the prompt alongside the
  other experimental features (advanced options, default off, same
  framing as the others). Verified via a full interactive-mode run that
  it now fires correctly (37 groups on RiverFlowInYou, matching the
  CLI-flag path).

- **`--tuplet-detection auto` could pick a tight partial match over a
  looser-but-valid full one.** At a given burst, the detector checks
  every candidate group size against every span, but was keeping
  whichever candidate had the *lowest average timing error* -- so a
  tight-fitting subset (e.g. 5 of a real 9-onset run) could beat out the
  full 9-onset match if its error happened to be smaller, stranding the
  remaining onsets outside any group to fall back to plain grid-snapping
  (audible as leftover uneven/colliding notes even with detection on).
  Now prefers whichever passing candidate covers the most onsets first,
  using average error only to break ties between equally-sized matches.
  Verified on the original 9-onset burst: now resolves as one clean
  nonuplet (9 of 9 evenly divided) instead of quintuplet-then-4-leftover.
  Group counts on the three benchmark pieces stay well-separated after
  the change (37/7/0 vs. the prior 38/7/0), confirming it isn't just
  finding more groups indiscriminately. `off` is unaffected by
  construction -- the fixed selection logic only runs inside
  `detect_tuplet_groups`, which `off` never calls.

- **`--dynamic-split` + `--hand-assignment`, used together, could fragment
  a single melodic line into treble-rest + isolated-bass-blip +
  treble-resumes.** Found via a real bar-by-bar comparison (RiverFlowInYou
  bar 7, original vs. ScorePrep output) -- P confirmed the difference was
  audible, not just cosmetic. Root cause: `--dynamic-split` can locally
  shift the treble/bass boundary enough to pull a note that's genuinely
  part of one continuous phrase into `--hand-assignment`'s "ambiguous,
  reconsider me" zone; the reassignment logic only looks one onset back
  by pitch centroid, so a brief melodic dip toward the other hand's
  recent register gets mistaken for a hand crossing and pulled onto the
  wrong staff, breaking the phrase. Only occurs with BOTH flags enabled
  together -- neither alone, nor the default pipeline, is affected.
  Fixed by adding one more condition to `assign_hands`: a note with no
  simultaneous chord-mate is only reassignment-eligible if at least one
  neighboring onset genuinely belongs to the other hand -- this catches
  the "solo passing tone" case without touching genuine hand crossings.
  A first version of this fix (guarding on ANY note, not just solo ones)
  was tried and reverted after it was found to break a legitimate
  octave-doubling chord in kalank.mid (forced a full-octave stretch into
  one hand); restricting the guard to solo (chord-mate-free) notes fixed
  the reported bug while leaving simultaneous-chord voicing decisions
  alone. Verified: RiverFlowInYou bar 7 no longer fragments; kalank's
  octave-doubling case preserved; default pipeline (both flags off)
  confirmed byte-identical on all three test files.

- **Rhythm-based tempo estimation was wrong on any source that isn't 384
  ticks-per-beat.** Note positions are rescaled to the internal 384 grid
  on load, but the estimator still converted them to seconds using the
  source's own resolution, counting the rescale twice. An eighth-note
  run written at 120 BPM was estimated at 75 BPM from a 480-PPQ file and
  150 BPM from a 960-PPQ one, so the score was notated against the wrong
  beat. Only affects files with no trustworthy tempo of their own (none,
  or the generic 120 BPM stamp) -- the three test files are byte-identical
  across tie-temperatures and flag combinations.

- **A genuine tempo change no longer gets thrown away as "erratic".** The
  jitter check flagged any curve with a jump over 20 BPM, so a single
  deliberate section change (e.g. 140 -> 80 BPM thirty bars in) was
  discarded and the rest of the piece played at the opening tempo. Jitter
  is dense, a real tempo change isn't: a big jump now only counts when its
  two tempo events are less than a bar apart, and the slowest/fastest
  ratio test only applies to a dense curve. RiverFlowInYou (big jumps 1-2
  beats apart, about one event per bar) is still flagged exactly as
  before. Same caveat as the original thresholds: one real positive case,
  plus one synthetic negative.

- **Interactive mode crashed right before converting whenever advanced
  options were skipped** (the default answer) -- `--tuplet-fit-tolerance`'s
  setting was only filled in on the advanced-options path. Fixed;
  interactive runs complete again.

- **reduce_test.py: tempo changes and time signature now survive the
  reduction.** Only the first tempo event was kept and the time signature
  was dropped, so a piece with a slower middle section played that
  section at the opening tempo. All tempo events and the source time
  signature are now written to the output. On a synthetic 4-instrument
  test with one 140 -> 80 BPM change, the full reduce_test -> ScorePrep
  chain went from 228.5 s to 361.7 s (source: 362.1 s).

- **reduce_test.py: notes overlapping on the same pitch were being
  destroyed downstream.** Two instruments in unison, or a note re-struck
  while still sounding, were written as overlapping notes on one track.
  MIDI can't represent that: ScorePrep read the later note as ending at
  the earlier note's release, cutting it to a few ticks, and then dropped
  it as noise. Through the full chain, kalank.mid lost 147 notes this way
  (now 9, all genuine short notes). Unisons are now merged into one key and
  an overlapping note is cut where the next one starts, so the
  output-notes count reduce_test reports is exactly what ScorePrep
  receives.

- **reduce_test.py: a pitch re-struck before its note-off was silently
  dropped on load.** The earlier note is now closed at the re-strike, the
  same way ScorePrep reads it. Notes are also now tracked per channel, so
  two instruments on different channels of one track no longer interfere.

- **reduce_test.py: chord size now counts piano keys, not source notes.**
  A pitch doubled by two instruments used two of `--max-chord-size`'s
  slots for one key, leaving a real pitch out of the chord. The
  "Average/Maximum chord size" stats count keys too.

- **MusicXML export (`--format musicxml` or `both`, or an output file
  ending in `.musicxml`).** Until now ScorePrep could only hand MuseScore
  a MIDI file, and MuseScore's importer re-derived staves, ties, rests and
  note names by itself -- sometimes undoing choices made here. The
  MusicXML file states them outright: staff per hand, every note's written
  length and ties exactly as the optimizer chose (same noteheads, same tie
  count), tuplet brackets from `--tuplet-detection`, tempo, time signature.
  New pieces this needed:
  - *Key and note spelling.* The key is estimated from the notes (`--key`
    overrides) and each note spelled for it; a note outside the key is
    spelled by whether the line moves on a half step up (sharp) or down
    (flat). Tested on 50 engraved scores from the music21 corpus: key
    signature right in 47, 98.9% of 28,274 notes spelled as printed.
    RiverFlowInYou comes out in A major.
  - *Voices.* Overlapping notes in one hand need separate voices on
    paper, and pedaled playing overlaps up to 9 deep in the test files.
    `--max-voices` (default 2) caps it; a note that overlaps too deeply,
    or only just overlaps the next one, is written ending where the next
    starts. The report counts these (RiverFlowInYou 96, kalank 427 at the
    defaults). MIDI output is untouched.
  Checked on all three test files across 8 flag combinations: every file
  passes the official MusicXML schema, opens in MuseScore, and -- read
  back by a separate reader -- contains exactly the MIDI output's notes
  apart from the shortened ones counted above. MuseScore's own playback of
  the file matches note for note. Not supported yet: `--grid triplet`
  (refused with a message), key changes mid-piece, beams, playback pedal.
  A tuplet group that straddles a barline can't take a bracket and is
  written as plain fast notes. MIDI output with default settings is
  byte-identical to before.

- **Ornament detection (`--ornament-detection auto`, default off,
  experimental).** Mordents, turns and trills are recognized as figures
  and written as one note with its ornament sign in MusicXML, with an
  accidental on the sign when the neighbor note is outside the key. A
  figure is a run of single notes each less than a 32nd apart, moving by
  step around one pitch. That speed limit comes from RiverFlowInYou,
  where real ornaments sit at 24-47 ticks between notes and ordinary
  16th runs of the same shape at 90-95. Result there: 14 mordents found,
  all "note, step up, back"; kalank and Sukoon (controls): none. Turns,
  trills and lower mordents are verified only on constructed examples so
  far. MIDI output is not changed by this flag.

- **Beat tracking: a file with an erratic tempo map is no longer
  flattened to one tempo -- its beats are found from the real note
  timing.** This is the cause of RiverFlowInYou coming out slow and
  off the beat from bar 7 on. The file is a live, rubato performance (57-76
  BPM, phrase endings slowing to about 40), and its tempo map is the
  transcriber's own beat tracking. The earlier "erratic curve" fix threw
  that map away and stamped the first tempo (40.5 BPM) on the whole piece,
  while keeping the map's beat positions: the output played back at 313
  seconds instead of 214, and the barlines (2/4, with beats missing at
  held notes) drifted away from the phrases. Now, when a map is judged
  erratic, every note is taken back to its real time in seconds and
  ScorePrep finds the beats itself: each gap between notes is read as some
  number of 16ths at a slowly varying tempo, preferring notes on beats and
  bass notes on strong beats. The output carries a beat-by-beat tempo
  curve, so playback keeps the performance's pacing (216 s).
  Checked note by note against the published score: 96% of its on-beat
  notes land on a beat (source's map: 91%) and 98% of its downbeats on a
  barline (was 54%, counting the old 2/4 bars generously); the half-beat
  pickup and 4/4 bars match the score. The source's time signature is set
  aside with its map (4/4 assumed unless `--time-sig` is given).
  New flag `--beat-tracking auto|on|off`; `--trust-tempo-curve` still
  keeps the source's map. Limits: tuned on this one piece. On the
  published score played back with artificial rubato it is exact for
  steady or lightly flexible tempo, 74% right-place-in-bar at +-10% with
  phrase-end slowing, and under 50% for heavier artificial swings. With
  `on` on files that have no tempo map it can settle on half tempo. An
  explicit `--tempo` skips it. kalank and Sukoon are byte-identical.

- **The "quintuplets" in RiverFlowInYou were an artifact of that tempo
  map.** With beats tracked, `--tuplet-detection auto` finds no
  quintuplet groups in the piece at all (14 sub-grid ornaments remain);
  the opening is plain 16ths and 8ths, as in the published score. The
  earlier fit-tolerance tension at tick ~6114 no longer arises on this
  file.

- **MusicXML: the report now separates notes ended at the next note
  (slight overlaps, any voice limit) from notes cut by `--max-voices`.**
  Measured against the published score, raising the limit from 2 to 4 on
  RiverFlowInYou changes 2 notes and no agreement figure; on kalank it
  keeps 155 more notes whole for about 8% more rests.

- **Fast notes no longer merge or vanish (`--fast-notes auto`, the new
  default).** On a 16th-note grid, a 32nd-note figure rounds two notes
  onto one tick: different pitches came out as a chord, and a repeated
  pitch lost a note. Each colliding pair is now looked at on a grid twice
  as fine and each note written where it rounds to there, as 32nds; three
  quick notes inside one 16th become a triplet of 32nds. Guards against
  splitting ordinary chords: the onsets must be at least a 64th apart and
  within an octave, and either one note is shorter than a 16th or the two
  are a step apart (a rolled chord is never a second). Checked against
  the input in real time: RiverFlowInYou had 30 places where notes played
  60+ ms apart were written on the same tick; now 1, and no note sits
  more than 100 ms from where it was played. kalank: 18 notes placed
  (8 rolled-chord-like pairs a third or more apart stay together);
  Sukoon: none. `--fast-notes off` restores the old behavior.

- **A note re-struck slightly before its previous note-off was read with
  a length of a few milliseconds and dropped as noise.** Transcriptions
  of legato repeated notes overlap them like this. Note-offs are now
  paired with the oldest open note of their pitch, and a note still
  sounding at its pitch's next onset ends there. RiverFlowInYou got 4
  notes back -- each the first note of a quick figure on a repeated A.
  When beat tracking runs, the noise filter is also applied after it
  rather than before, since note lengths on the source's untrusted beat
  map don't mean anything.

- **A long-held note is no longer cut to the length of a short note
  struck with it.** Notes starting together share the shortest member's
  written length so chords don't fracture -- but that wrote a melody note
  held four beats as a 16th and a bar of rests whenever a passing note
  coincided with it. A note held at least four times longer than its
  chord-mate, and at least a beat longer, now keeps its own length. In
  RiverFlowInYou this restores the held D that ends the opening phrase
  (score: 3.3 beats; was 0.25, now 3.5 at `--tie-temperature 0.15`). At
  the default 0.0 that note is still cut at the barline, since 0.0 never
  ties across one. This changes the control files slightly: 12-27 notes
  of ~1,300-1,400 in kalank and Sukoon are now written longer.

- **`--format both` could overwrite the input file** when the output was
  given as `<input name>.musicxml` (the MIDI went to `<input name>.mid`).
  Now refused with a message.

- **What the bar-by-bar comparison with the published score shows after
  all of the above** (RiverFlowInYou, defaults): 95% of the score's
  on-beat notes on a beat, 98% of its downbeats on a barline, 92% of
  note-to-note rhythms identical, hands 93% (97% with `--dynamic-split on
  --hand-assignment on`). Most of what still differs is the performance
  itself, not the conversion: about 180 of the score's 1,118 notes aren't
  in the transcription (one 12-bar stretch isn't played at all, another
  is played as block chords), the performer holds the opening fermata a
  bar longer, and some repeated-note figures are played with one note
  fewer than written.

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
