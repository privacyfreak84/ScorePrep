#!/usr/bin/env python3
"""
ScorePrep (scoreprep.py)
Clean AI-generated piano MIDI for beautiful MuseScore engraving.

Turns a raw single-track piano MIDI transcription (e.g. from ByteDance's
piano transcription model, MT3, or Magenta) into a clean, readable
two-staff (treble/bass) grand-staff MIDI file ready to import into
MuseScore.

WHAT IT FIXES
-------------
Audio-to-MIDI transcription preserves the exact, continuous timing of a
human performance -- note-on/off down to the millisecond. Importing that
directly into notation software produces a mess:
  1. Everything lands on one staff (no treble/bass split).
  2. Note durations rarely land on clean rhythmic values, so notation
     software chains multiple tied notes together to represent the exact
     length -- ties end up "everywhere".
  3. When several notes start together but have different raw release
     times, standard notation can't give them different lengths in one
     chord, so the chord fractures into overlapping tied fragments.

WHAT THIS SCRIPT DOES
----------------------
  1. Splits notes across two tracks (treble >= split pitch, bass below),
     so MuseScore imports it as a proper grand staff.
  2. Quantizes note onsets to a 16th-note grid.
  3. Caps every note's duration so it can NEVER cross a barline --
     no more sustained notes tied across many bars.
  4. Picks each note's (or chord's -- notes sharing an onset always get
     one shared duration, so chords never fracture into mismatched tied
     fragments) written duration by minimizing a small cost: ties cost,
     a visible rest costs, and inventing sustain beyond a note's real
     transcribed length costs. --tie-temperature sets how those three
     trade off against each other -- low temperature avoids ties almost
     entirely and prefers a cheap small extension over a rest; high
     temperature prefers exact fidelity (ties wherever the real timing
     needs them) over any invented legato. This is deliberately an
     engraving decision, not just data preservation: a pianist reading
     "quarter note, rest" where the audio technically rang for 3 bars
     will just hold the note / use the pedal; they don't need that
     written out literally.
  5. Sets the output tempo.

DEFAULTS (auto-estimated when not specified)
----------------------------------------------
  Time signature: read from the source file's own time signature if
                   present, otherwise 4/4
  Tempo: read from the source file's own tempo meta message if present;
         otherwise estimated from the pattern of note-onset timing;
         otherwise 120 BPM
  Staff split point: estimated from the actual pitch distribution
                      (Otsu-style threshold search), otherwise middle C (60)

USAGE
-----
  python3 scoreprep.py input.mid output.mid --tempo 130 --time-sig 3/4

  # let tempo/time-sig/split-pitch all be estimated from the file instead:
  python3 scoreprep.py input.mid output.mid

  # no arguments at all -> interactive, step-by-step prompts:
  python3 scoreprep.py
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import mido
from mido import MidiFile, MidiTrack, Message, MetaMessage

# Engraving profiles: bundle the flags that matter most for the readability/
# fidelity tradeoff into one choice. TT values reflect the benchmark suite's
# findings (see benchmarks/ -- TT~0.10 often too choppy, ~0.15-0.20 is a
# quality plateau, diminishing returns pushing further past that). Any flag
# also given explicitly on the command line still wins over the profile.
PROFILES = {
    'readable': dict(tie_temperature=0.0, pedal_mode='ignore',
                      grid='straight', clean_durations='dotted'),
    'balanced': dict(tie_temperature=0.15, pedal_mode='ignore',
                      grid='straight', clean_durations='dotted'),
    'faithful': dict(tie_temperature=1.0, pedal_mode='reflect',
                      grid='straight', clean_durations='dotted'),
}

# Melody preservation: biases the duration optimizer's per-event weights
# using classify_voice_roles()'s output, rather than adding a second
# decision algorithm. A melody event gets ties made cheaper and rests
# made costlier (protect its continuity/truthfulness); an accompaniment
# event gets the opposite (decluttering is fine to buy with more rests
# and fewer ties). Independent of --tie-temperature -- these multiply
# whatever weights the temperature already picked.
MELODY_TIE_MULT, MELODY_REST_MULT = 0.5, 1.5
ACCOMP_TIE_MULT, ACCOMP_REST_MULT = 1.4, 0.7


def _config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'scoreprep', 'config.json')


def load_last_paths():
    """Return {'last_input': ..., 'last_output': ...} from a previous run,
    or {} if there's no saved config or it can't be read. Never raises --
    this is a convenience, not something that should ever block a run."""
    try:
        with open(_config_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_last_paths(input_path, output_path):
    """Remember the input/output paths just used, so interactive mode can
    offer them as defaults next time instead of requiring them to be
    retyped/pasted. Best-effort -- failure here should never interrupt an
    otherwise-successful conversion."""
    try:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'last_input': os.path.abspath(input_path),
                       'last_output': os.path.abspath(output_path)}, f)
    except OSError:
        pass

TICKS_PER_BEAT = 384          # matches this script's assumed source resolution
SPLIT_PITCH = 60              # middle C
DEFAULT_TIME_SIG = (4, 4)     # fallback when no time signature is given/detected

# Quantization grid unit, and the set of single-notehead-representable
# durations (no ties needed) expressed as a count of that unit.
#
# 'straight' uses a 16th note as the unit (TICKS_PER_BEAT/4 = 96 ticks) --
# only ever hits straight subdivisions, so a genuinely triplet/swung
# passage gets forced onto the nearest straight 16th, distorting its
# rhythm.
#
# 'triplet' uses TICKS_PER_BEAT/12 = 32 ticks as the unit instead -- the
# largest unit that evenly divides both a straight 16th (3 units) and a
# triplet 8th (4 units), so both families of note value are natively
# representable on the same grid without forcing one onto the other.
# CLEAN values below are each straight value * 3 (to re-express them in
# the finer unit) plus the triplet-specific values: a triplet 16th (2
# units), triplet 8th (4), and triplet quarter (8, i.e. two triplet 8ths).
# Quantization grid unit, and the set of single-notehead-representable
# durations (no ties needed) expressed as a count of that unit.
#
# 'straight' uses a 16th note as the unit (TICKS_PER_BEAT/4 = 96 ticks) --
# only ever hits straight subdivisions, so a genuinely triplet/swung
# passage gets forced onto the nearest straight 16th, distorting its
# rhythm.
#
# 'triplet' uses TICKS_PER_BEAT/12 = 32 ticks as the unit instead -- the
# largest unit that evenly divides both a straight 16th (3 units) and a
# triplet 8th (4 units), so both families of note value are natively
# representable on the same grid without forcing one onto the other.
# Straight-family values below are each re-expressed *3 for this finer
# unit; TRIPLET_ONLY adds the triplet-specific values (a triplet 16th,
# triplet 8th, and triplet quarter = two triplet 8ths).
#
# CLEAN itself is assembled per (grid mode, duration_style) pair:
# duration_style='dotted' (default) includes dotted values (3,6,12,24,48
# in straight units); 'powers2' excludes them, restricting notation to
# plain power-of-two note values only (a plainer, more old-fashioned
# look, at the cost of needing more ties for anything a dotted value
# would otherwise have covered in one notehead).
POWERS_OF_TWO = (1, 2, 4, 8, 16, 32, 64)     # 64th, 32nd, 16th, 8th, quarter, half, whole
DOTTED = (3, 6, 12, 24, 48)                  # dotted-8th, -quarter, -half, -whole, -breve
TRIPLET_ONLY = (2, 4, 8)                     # triplet 16th, triplet 8th, triplet quarter

GRID_MODES = {
    'straight': {'grid': TICKS_PER_BEAT // 4, 'unit_name': 'sixteenths', 'scale': 1},
    'triplet': {'grid': TICKS_PER_BEAT // 12, 'unit_name': 'grid units (1/12 beat)', 'scale': 3},
}


def _clean_for(grid_mode, duration_style):
    scale = GRID_MODES[grid_mode]['scale']
    values = set(u * scale for u in POWERS_OF_TWO)
    if duration_style == 'dotted':
        values |= set(u * scale for u in DOTTED)
    if grid_mode == 'triplet':
        values |= set(TRIPLET_ONLY)
    return sorted(values)


GRID = GRID_MODES['straight']['grid']
CLEAN = _clean_for('straight', 'dotted')
GRID_UNIT_NAME = GRID_MODES['straight']['unit_name']


def configure_grid(mode, duration_style='dotted'):
    """Switch the module-level GRID/CLEAN used by every quantization step
    (quantize, resolve_note_durations, optimize_staff_durations,
    fix_same_pitch_overlaps, minimal_tie_count, ...). mode: 'straight' or
    'triplet'. duration_style: 'dotted' (default, includes dotted values)
    or 'powers2' (plain power-of-two note values only). Must be called
    before any of those run."""
    global GRID, CLEAN, GRID_UNIT_NAME
    if mode not in GRID_MODES:
        raise ValueError(f"Unknown grid mode: {mode!r} (expected one of {list(GRID_MODES)})")
    if duration_style not in ('dotted', 'powers2'):
        raise ValueError(f"Unknown duration_style: {duration_style!r} (expected 'dotted' or 'powers2')")
    GRID = GRID_MODES[mode]['grid']
    CLEAN = _clean_for(mode, duration_style)
    GRID_UNIT_NAME = GRID_MODES[mode]['unit_name']
    _tie_count_cache.clear()


def bar_ticks_for(time_sig):
    """Ticks in one bar for a given (numerator, denominator) time signature.
    MIDI ticks_per_beat is always ticks-per-quarter-note regardless of the
    time signature's denominator, so a bar is:
        ticks_per_quarter * numerator * (4 / denominator)
    e.g. 4/4 -> 384*4*1   = 1536 ticks (4 quarters)
         3/4 -> 384*3*1   = 1152 ticks (3 quarters)
         6/8 -> 384*6*0.5 = 1152 ticks (3 quarters' worth, same bar length as 3/4)
    """
    num, den = time_sig
    return round(TICKS_PER_BEAT * num * 4 / den)


def parse_time_sig(s):
    """Parse a 'N/D' string into (numerator, denominator)."""
    if '/' not in s:
        raise ValueError("expected format N/D, e.g. 3/4")
    num_s, den_s = s.split('/', 1)
    num, den = int(num_s), int(den_s)
    if num <= 0 or den <= 0:
        raise ValueError("numerator and denominator must be positive")
    if den not in (1, 2, 4, 8, 16, 32):
        raise ValueError("denominator should be a power of 2 (1,2,4,8,16,32)")
    return (num, den)


def detect_source_time_sig(mid):
    """Read the first time_signature meta message found in any track, if
    any. Returns ((numerator, denominator), is_generic_default) or
    (None, False) if the source has no time signature info at all.
    is_generic_default is True when the message is byte-identical to the
    untouched MIDI spec default (4/4, clocks_per_click=24,
    notated_32nd_notes_per_beat=8) -- which many transcription/export
    tools stamp automatically regardless of the actual piece, so it
    should be treated as "no real info" rather than a trustworthy
    detection."""
    for trk in mid.tracks:
        for msg in trk:
            if msg.type == 'time_signature':
                is_generic = (msg.numerator == 4 and msg.denominator == 4 and
                              msg.clocks_per_click == 24 and
                              msg.notated_32nd_notes_per_beat == 8)
                return (msg.numerator, msg.denominator), is_generic
    return None, False


def extract_notes(track, channel=None):
    """Turn a MIDI track's note_on/note_off stream into a flat list of
    {'start','end','pitch','vel'} dicts using absolute tick times.

    If channel is given (0-15), only note_on/note_off messages on that
    MIDI channel are kept -- other channels' note_on/note_off still
    advance the running tick clock (abs_t) but don't contribute notes or
    get tracked in the active-note map, since a single track can carry
    several channels merged together (common in type-0 files) and only
    one of them may be the intended piano part."""
    abs_t = 0
    active = {}
    notes = []
    for msg in track:
        abs_t += msg.time
        is_note_msg = msg.type in ('note_on', 'note_off')
        if is_note_msg and channel is not None and msg.channel != channel:
            continue
        if msg.type == 'note_on' and msg.velocity > 0:
            if msg.note in active:
                start, vel = active.pop(msg.note)
                if abs_t > start:
                    notes.append({'start': start, 'end': abs_t,
                                  'pitch': msg.note, 'vel': vel})
            active[msg.note] = (abs_t, msg.velocity)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            if msg.note in active:
                start, vel = active.pop(msg.note)
                notes.append({'start': start, 'end': abs_t,
                              'pitch': msg.note, 'vel': vel})
    return notes


def read_pedal_intervals(mid):
    """Scan every track for sustain pedal (CC64) down/up pairs and return
    a sorted list of (down_tick, up_tick) intervals. If the pedal is still
    down at the end of a track, that interval is closed at the track's
    last event tick."""
    intervals = []
    for trk in mid.tracks:
        abs_t = 0
        down_since = None
        for msg in trk:
            abs_t += msg.time
            if msg.type == 'control_change' and msg.control == 64:
                if msg.value >= 64 and down_since is None:
                    down_since = abs_t
                elif msg.value < 64 and down_since is not None:
                    intervals.append((down_since, abs_t))
                    down_since = None
        if down_since is not None:
            intervals.append((down_since, abs_t))
    intervals.sort()
    return intervals


def extend_end_with_pedal(end_tick, pedal_intervals):
    """If end_tick falls inside a pedal-down window, the sound was still
    intentionally ringing at that point -- extend it to the pedal-up
    time. Otherwise leave it unchanged."""
    for down, up in pedal_intervals:
        if down <= end_tick < up:
            return max(end_tick, up)
        if down > end_tick:
            break  # intervals are sorted; no further interval can apply
    return end_tick


def rescale_notes_to_tempo(notes, source_encoding_tempo, notated_tempo):
    """MIDI tick positions only mean a fixed real-world duration once
    paired with a specific tempo. Transcription tools (this pipeline was
    built around pretty_midi-style output) convert real seconds -> ticks
    using some reference tempo at write time -- usually whatever the
    file's own set_tempo message says, or 120 BPM if none is present,
    since that's the standard default such libraries assume. That
    reference tempo (source_encoding_tempo) is NOT necessarily the same
    as notated_tempo, the tempo this script ultimately labels the output
    with (chosen by the user, or estimated) for readability/performance
    purposes.

    If we simply copied the source ticks unchanged into an output file
    stamped with a different tempo, playback speed would be scaled by
    notated_tempo/source_encoding_tempo relative to the original audio --
    a real bug, not a rounding error. The fix: convert every tick to real
    seconds using source_encoding_tempo, then back to ticks using
    notated_tempo, so the OUTPUT's own declared tempo is self-consistent
    with its own tick positions. Since ticks_per_beat is unchanged on
    both sides, this reduces to a simple linear scale by the tempo ratio.
    Mutates 'start'/'end' on each note in place."""
    if source_encoding_tempo == notated_tempo:
        return
    ratio = notated_tempo / source_encoding_tempo
    for n in notes:
        n['start'] = round(n['start'] * ratio)
        n['end'] = round(n['end'] * ratio)


def filter_noise_notes(notes, min_ticks):
    """Drop notes whose raw (pre-quantization) duration is below min_ticks
    -- almost certainly transcription noise rather than an intended note.
    Returns (kept_notes, dropped_count)."""
    kept = []
    dropped = 0
    for n in notes:
        if (n['end'] - n['start']) < min_ticks:
            dropped += 1
        else:
            kept.append(n)
    return kept, dropped


def filter_ghost_velocity_notes(notes, min_velocity):
    """Drop notes below min_velocity as likely ghost/noise hits. Applied
    after filter_noise_notes and BEFORE any velocity_mode processing, so
    a handful of near-zero-velocity ghost notes can't skew a 'normalize'
    range that's about to be computed from what's left. Returns
    (kept_notes, dropped_count)."""
    if min_velocity <= 0:
        return notes, 0
    kept = [n for n in notes if n['vel'] >= min_velocity]
    return kept, len(notes) - len(kept)


def apply_velocity_mode(notes, mode, scale=1.0):
    """'passthrough' (default): leave velocities untouched.
    'scale': multiply every velocity by `scale` (e.g. 0.8 = uniformly
    softer, 1.3 = uniformly more forceful), clamped to the valid 1-127
    MIDI range -- preserves the original performance's relative
    dynamics/expression, just scales it.
    'normalize': linearly remaps the piece's own observed [min, max]
    velocity range onto a standard 30-110 dynamic range. Useful when a
    transcription's velocity estimates are noisy or compressed into a
    narrow band, at the cost of no longer reflecting the source's literal
    values. Mutates each note's 'vel' in place."""
    if mode == 'passthrough' or not notes:
        return
    if mode == 'scale':
        for n in notes:
            n['vel'] = max(1, min(127, round(n['vel'] * scale)))
        return
    if mode == 'normalize':
        lo = min(n['vel'] for n in notes)
        hi = max(n['vel'] for n in notes)
        target_lo, target_hi = 30, 110
        if hi == lo:
            for n in notes:
                n['vel'] = round((target_lo + target_hi) / 2)
            return
        for n in notes:
            frac = (n['vel'] - lo) / (hi - lo)
            n['vel'] = max(1, min(127, round(target_lo + frac * (target_hi - target_lo))))
        return
    raise ValueError(f"Unknown velocity_mode: {mode!r}")


def quantize(tick):
    return round(tick / GRID) * GRID


_tie_count_cache = {}


def tie_budget_for(temperature):
    """Same tie-budget formula resolve_note_durations uses, factored out
    so any later step that re-shortens a note (e.g. fix_same_pitch_overlaps)
    can re-snap to a value within the same budget instead of accidentally
    producing a duration that needs more ties than the chosen temperature
    allows."""
    temperature = max(0.0, min(1.0, temperature))
    return 1 + round(temperature * 4)


def minimal_tie_count(units):
    """How many tied noteheads standard engraving needs to notate `units`
    sixteenths exactly, using a greedy largest-value-first decomposition
    (the same approach notation software uses)."""
    if units in _tie_count_cache:
        return _tie_count_cache[units]
    remaining = units
    count = 0
    guard = 0
    while remaining > 0 and guard < 50:
        c = max((x for x in CLEAN if x <= remaining), default=None)
        if c is None:
            break
        remaining -= c
        count += 1
        guard += 1
    _tie_count_cache[units] = count
    return count


def true_tie_count(onset, units, bar_ticks):
    """Real number of tied noteheads MuseScore needs to render a note of
    `units` grid-units starting at `onset`. Splits the span at every
    barline it crosses, then decomposes each resulting sub-segment
    independently via minimal_tie_count and sums them.

    This can't be approximated as minimal_tie_count(units) + bar_crossings
    (an earlier version did exactly that): a barline split forces the
    pre-barline segment to be whatever's left until the barline, which
    often isn't a clean value on its own even when the note's total
    duration would have decomposed cleanly -- e.g. 16 units starting 1
    unit before a barline splits into a 1-unit segment (clean, 1
    notehead) and a 15-unit remainder, which itself needs 2 noteheads
    (12+3), for 3 total -- not the 2 that "+1 per crossing" would predict.
    """
    end = onset + units * GRID
    bounds = [onset]
    b = (onset // bar_ticks + 1) * bar_ticks
    while b < end:
        bounds.append(b)
        b += bar_ticks
    bounds.append(end)
    total = 0
    for a, z in zip(bounds, bounds[1:]):
        seg_units = (z - a) // GRID
        if seg_units > 0:
            total += minimal_tie_count(seg_units)
    return total


def best_units_within_budget(raw_units, tie_budget, onset=None, bar_ticks=None):
    """Largest duration (in grid units) <= raw_units that can be notated
    within `tie_budget` tied noteheads. tie_budget=1 means "must be a
    single clean value, and must not cross a barline" (today's zero-tie
    default).

    If onset and bar_ticks are given, ties are counted the accurate way
    (true_tie_count, including barline crossings). Without them, falls
    back to duration-value-only counting (minimal_tie_count) -- used by
    call sites without bar context, where the risk of a fresh crossing is
    negligible since they only ever shorten an already-valid span."""
    raw_units = max(1, raw_units)
    for candidate in range(raw_units, 0, -1):
        tc = (true_tie_count(onset, candidate, bar_ticks) if onset is not None
              else minimal_tie_count(candidate))
        if tc <= tie_budget:
            return candidate
    return 1


def optimizer_weights(temperature, tie_weight=None, rest_weight=None, artic_weight=None):
    """Cost weights for optimize_staff_durations, derived from the single
    tie-temperature dial by default -- any of the three may be overridden
    individually via the [advanced] --tie-weight/--rest-weight/
    --articulation-weight flags for experimentation, without touching code.

    tie_weight   -- cost per extra tied notehead beyond the first. High at
                     temperature=0 (avoid ties almost entirely -- though
                     the hard tie_budget already forbids most of this;
                     this just breaks ties, pun intended, among whatever
                     the budget still allows), 0 at temperature=1 (ties
                     become an accepted, unpenalized way to notate exact
                     timing at max fidelity).
    rest_weight  -- cost of leaving a visible rest before the next onset.
                     Held constant: a rest is always somewhat
                     undesirable, but how willing the optimizer is to
                     *avoid* one by inventing extra sustain is governed
                     entirely by articulation_weight below, not this.
    artic_weight -- cost per grid unit of duration invented beyond a
                     note's real, evidence-backed sustain (its own
                     transcribed release, extended by pedal data if
                     --pedal-mode reflect is on). Low at temperature=0
                     (cheap to close a small, probably-meaningless gap --
                     this is what used to be the separate, flat
                     --max-silent-gap patch), high at temperature=1
                     (fidelity to the real performance timing is that
                     setting's whole point, so don't invent legato that
                     wasn't there -- use a tie instead, which is free at
                     that end of the dial).
    """
    temperature = max(0.0, min(1.0, temperature))
    return (
        tie_weight if tie_weight is not None else 6.0 * (1.0 - temperature),
        rest_weight if rest_weight is not None else 1.0,
        artic_weight if artic_weight is not None else 0.5 + 2.5 * temperature,
    )


def resolve_note_durations(notes, temperature=0.0, bar_ticks=None):
    """Quantize onsets and cap each note's *maximum possible* duration at
    a temperature-scaled bar span and tie budget -- this is the "real
    evidence" ceiling every later step treats as ground truth. Mutates
    each note dict in place, adding 'f_start', 'f_end', and 'nat_units'
    (the natural/evidence-backed duration in grid units -- fixed here and
    never recomputed later, so later passes always know exactly how much
    of any given duration is real vs. invented).

    temperature=0.0 -> 1 bar max span, 1 tie link
    temperature=1.0 -> 8 bar max span, 5 tie links
    """
    if bar_ticks is None:
        bar_ticks = bar_ticks_for(DEFAULT_TIME_SIG)
    temperature = max(0.0, min(1.0, temperature))
    max_bars = 1 + round(temperature * 7)      # 1..8
    tie_budget = tie_budget_for(temperature)

    for n in notes:
        q_start = quantize(n['start'])
        raw_end = max(q_start + GRID, quantize(n['end']))
        raw_dur = raw_end - q_start

        bar_start = (q_start // bar_ticks) * bar_ticks
        room = bar_start + max_bars * bar_ticks - q_start

        capped_dur = min(raw_dur, room)
        raw_units = max(1, capped_dur // GRID)

        final_units = best_units_within_budget(raw_units, tie_budget, q_start, bar_ticks)

        n['f_start'] = q_start
        n['f_end'] = q_start + final_units * GRID
        n['nat_units'] = final_units
        # the true/natural sustain end (pre-flooring, pre-bar-cap) -- kept
        # around purely for playback purposes later; never used for the
        # notated duration itself
        n['natural_end'] = max(n['f_end'], raw_end)


def classify_voice_roles(notes):
    """Heuristic per-note classification into 'melody' vs 'accompaniment'.

    This is foundational infrastructure, not a shipped feature: it only
    *annotates* each note dict with 'voice_role' and 'voice_confidence'
    (0.0-1.0). Nothing downstream -- the optimizer, the treble/bass split,
    engraving output -- reads these fields yet. The point is to have a
    real, inspectable signal to validate against actual pieces before any
    future feature (melody preservation, intelligent hand assignment,
    voice-aware quantization, confidence warnings) is allowed to change
    engraving behavior based on it.

    Approach: classic "skyline" (the highest simultaneous pitch at a given
    onset is usually the melody) as the base signal, smoothed by
    penalizing an isolated skyline note that jumps far from its
    neighbors -- a common false positive where a dense accompaniment
    chord's top note briefly pokes above the real tune. Chord density,
    note duration, and relative velocity contribute secondary evidence:
    melody in piano transcriptions tends to be sparser (single notes more
    than chords), longer, and often (not always) the loudest note in its
    onset. Must run on the full, unsplit note list -- accompaniment
    figuration can sit in the treble register (e.g. Alberti bass), so
    this can't be inferred from split_pitch alone.

    Expects 'f_start', 'nat_units', 'pitch', 'velocity' already set
    (i.e. call after resolve_note_durations). Mutates notes in place.
    """
    if not notes:
        return

    by_onset = defaultdict(list)
    for n in notes:
        by_onset[n['f_start']].append(n)
    onsets = sorted(by_onset)

    durations = sorted(n['nat_units'] for n in notes if n.get('nat_units', 0) > 0)
    median_dur = durations[len(durations) // 2] if durations else 1

    for onset in onsets:
        chord = by_onset[onset]
        top = max(chord, key=lambda n: n['pitch'])
        avg_vel = sum(n['vel'] for n in chord) / len(chord)
        for n in chord:
            score = 0.45 if n is top else 0.0
            score += 0.15 if len(chord) == 1 else -0.05 * min(len(chord) - 1, 3)
            score += 0.20 * min(1.0, n.get('nat_units', 0) / median_dur) if median_dur else 0.0
            score += 0.15 if n['vel'] >= avg_vel else 0.0
            n['_voice_score'] = max(0.0, min(1.0, score))

    # Smoothing pass: an isolated skyline spike far from the established
    # line is usually a chord's top note poking up, not a real melodic
    # leap -- demote it in favor of whichever chord member actually
    # continues the line, if one exists within a semitone-jump threshold.
    JUMP_SEMITONES = 12
    prev_pitch = None
    for onset in onsets:
        chord = by_onset[onset]
        top = max(chord, key=lambda n: n['pitch'])
        if prev_pitch is not None and abs(top['pitch'] - prev_pitch) > JUMP_SEMITONES and len(chord) > 1:
            closer = min(chord, key=lambda n: abs(n['pitch'] - prev_pitch))
            if closer is not top and abs(closer['pitch'] - prev_pitch) <= JUMP_SEMITONES:
                top['_voice_score'] *= 0.5
                closer['_voice_score'] = min(1.0, closer['_voice_score'] + 0.25)
        prev_pitch = max(chord, key=lambda n: n['_voice_score'])['pitch']

    for n in notes:
        score = n.pop('_voice_score')
        n['voice_role'] = 'melody' if score >= 0.5 else 'accompaniment'
        n['voice_confidence'] = round(abs(score - 0.5) * 2, 2)


def optimize_staff_durations(notes, temperature, bar_ticks, weights=None, stats=None,
                              melody_preservation=False):
    """The core engraving decision for one staff, run after
    resolve_note_durations has already established each note's hard
    ceiling (bar-span cap, tie-budget cap, 'nat_units' = real evidence).

    Replaces the old three-pass patch sequence (sync_chords ->
    fix_same_pitch_overlaps -> sync_chords again -> fill_small_gaps ->
    sync_chords again) with a single cost-minimizing choice per onset
    event (one note, or every note sharing that onset -- a chord).

    Every event first gets its truthful baseline: the longest duration
    representable within the tie budget that's still <= each member's
    real evidence ('nat_units', from resolve_note_durations) -- i.e.
    exactly what the old algorithm always did, maximizing truthful
    length within budget, no cost attached. A genuinely long real note
    that needs 2 ties to notate accurately keeps those 2 ties for free;
    ties are never a reason to truncate real data.

    From that baseline, extending further (to close a rest) is then
    optionally considered, and THAT is where cost applies -- only ties
    spent on top of the baseline, plus any invented (unevidenced)
    sustain, are weighed against the rest they'd remove:

        tie_weight   * (ties beyond what the truthful baseline already needs)
      + rest_weight  * (1 if a rest remains before the next onset) * (event size)
      + artic_weight * (grid units of duration invented beyond each
                         member's own real, evidence-backed 'nat_units')

    Never searches below the truthful baseline -- there's no scenario
    where truncating a real note below its own evidence-backed,
    budget-respecting length is ever the right call. Candidates are
    still hard-bounded by the same tie_budget resolve_note_durations
    used, and can never extend into another note of the SAME pitch
    (preserves genuine cross-pitch polyphony within a staff -- a real
    sustained note under a moving line -- which was always left
    untouched on purpose; only actual same-pitch retriggers are a hard
    constraint). Mutates 'f_end' in place; leaves 'nat_units'/
    'natural_end' untouched so later passes (and playback-sustain)
    still see the true evidence.

    melody_preservation: if True and notes carry 'voice_role' (i.e.
    classify_voice_roles has run), each event's tie/rest weights are
    biased by its dominant voice role before the cost search --
    MELODY_TIE_MULT/MELODY_REST_MULT for a melody event, ACCOMP_TIE_MULT/
    ACCOMP_REST_MULT otherwise. Purely a weight bias on the existing cost
    search, not a separate algorithm.
    """
    if not notes:
        return
    if weights is None:
        weights = optimizer_weights(temperature)
    tie_w, rest_w, artic_w = weights
    tie_budget = tie_budget_for(temperature)
    max_bars = 1 + round(max(0.0, min(1.0, temperature)) * 7)

    onsets = sorted(set(n['f_start'] for n in notes))
    next_onset_after = {a: b for a, b in zip(onsets, onsets[1:])}

    by_pitch = defaultdict(list)
    for n in notes:
        by_pitch[n['pitch']].append(n['f_start'])
    next_same_pitch = {}
    for pitch, starts in by_pitch.items():
        starts = sorted(set(starts))
        for a, b in zip(starts, starts[1:]):
            next_same_pitch[(pitch, a)] = b

    by_onset = defaultdict(list)
    for n in notes:
        by_onset[n['f_start']].append(n)

    for onset, event in by_onset.items():
        bar_start = (onset // bar_ticks) * bar_ticks
        bar_room_units = max(1, (bar_start + max_bars * bar_ticks - onset) // GRID)
        next_onset = next_onset_after.get(onset)
        next_onset_units = ((next_onset - onset) // GRID) if next_onset is not None else None

        ceiling = bar_room_units
        member_nat_units = []
        for n in event:
            same_pitch_next = next_same_pitch.get((n['pitch'], onset))
            member_ceiling = bar_room_units
            if same_pitch_next is not None:
                member_ceiling = min(member_ceiling, max(1, (same_pitch_next - onset) // GRID))
            ceiling = min(ceiling, member_ceiling)
            member_nat_units.append(n['nat_units'])

        # truthful baseline: longest duration <= each member's real evidence,
        # still within the tie budget -- shortest member wins for a chord (so
        # no member's real length is ever overstated), matching the old
        # "force chord to shortest" default behavior.
        baseline = min(best_units_within_budget(nat, tie_budget, onset, bar_ticks)
                        for nat in member_nat_units)
        baseline = min(baseline, ceiling)
        baseline_tc = true_tie_count(onset, baseline, bar_ticks)

        best_v, best_cost, best_fab = baseline, None, None
        if melody_preservation and any('voice_role' in n for n in event):
            is_melody = any(n.get('voice_role') == 'melody' for n in event)
            eff_tie_w = tie_w * (MELODY_TIE_MULT if is_melody else ACCOMP_TIE_MULT)
            eff_rest_w = rest_w * (MELODY_REST_MULT if is_melody else ACCOMP_REST_MULT)
            if stats is not None:
                key = 'melody_biased' if is_melody else 'accompaniment_biased'
                stats[key] = stats.get(key, 0) + 1
        else:
            eff_tie_w, eff_rest_w = tie_w, rest_w
        for v in range(baseline, ceiling + 1):
            tc = true_tie_count(onset, v, bar_ticks)
            if tc > tie_budget:
                continue
            rest_present = next_onset_units is not None and v < next_onset_units
            fabrication = sum(max(0, v - nat) for nat in member_nat_units)
            cost = (eff_tie_w * max(0, tc - baseline_tc)
                    + eff_rest_w * (len(event) if rest_present else 0)
                    + artic_w * fabrication)
            if best_cost is None or cost < best_cost or (cost == best_cost and fabrication < best_fab):
                best_v, best_cost, best_fab = v, cost, fabrication

        new_end = onset + best_v * GRID
        for n in event:
            n['f_end'] = new_end

        if stats is not None:
            extended_ties = true_tie_count(onset, best_v, bar_ticks) - baseline_tc
            rest_present_final = next_onset_units is not None and best_v < next_onset_units
            for n in event:
                member_fab = max(0, best_v - n['nat_units'])
                if member_fab > 0:
                    cat = 'invented_sustain'
                elif extended_ties > 0:
                    cat = 'extended_truthful'
                elif rest_present_final:
                    cat = 'rest_kept'
                else:
                    cat = 'exact_no_rest'
                stats[cat] = stats.get(cat, 0) + 1


def fix_same_pitch_overlaps(notes, tie_budget=1, bar_ticks=None):
    """Prevent a pitch's note-off happening after the next note-on of the
    same pitch (can happen after rounding).

    Truncating to the next onset produces an arbitrary tick, not
    necessarily one of the "clean" durations resolve_note_durations chose
    -- left alone, that can silently need more tied noteheads than the
    requested tie_budget allows (invisible until something actually counts
    ties). So after truncating, re-snap down to the largest duration that
    still respects tie_budget (accounting for barline crossings when
    bar_ticks is given); this can only shorten further, so it can't
    reopen the overlap just fixed."""
    by_pitch = defaultdict(list)
    for n in notes:
        by_pitch[n['pitch']].append(n)
    for pitch, lst in by_pitch.items():
        lst.sort(key=lambda n: n['f_start'])
        for i in range(len(lst) - 1):
            if lst[i]['f_end'] > lst[i + 1]['f_start']:
                start = lst[i]['f_start']
                truncated_units = max(1, (lst[i + 1]['f_start'] - start) // GRID)
                final_units = best_units_within_budget(truncated_units, tie_budget, start, bar_ticks)
                lst[i]['f_end'] = start + final_units * GRID


def build_track(notes, name):
    events = []
    for n in notes:
        events.append((n['f_start'], 'on', n['pitch'], n['vel']))
        events.append((n['f_end'], 'off', n['pitch'], 0))
    events.sort(key=lambda e: (e[0], 0 if e[1] == 'off' else 1))

    trk = MidiTrack()
    trk.append(MetaMessage('track_name', name=name, time=0))
    last_tick = 0
    for tick, typ, pitch, vel in events:
        delta = max(0, tick - last_tick)
        if typ == 'on':
            trk.append(Message('note_on', note=pitch, velocity=vel, time=delta))
        else:
            trk.append(Message('note_off', note=pitch, velocity=0, time=delta))
        last_tick = tick
    trk.append(MetaMessage('end_of_track', time=0))
    return trk


def merge_intervals(intervals):
    """Merge overlapping/touching (start, end) tick intervals into a
    minimal sorted list of non-overlapping ones."""
    if not intervals:
        return []
    ivs = sorted(intervals)
    merged = [list(ivs[0])]
    for s, e in ivs[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def compute_playback_pedal_windows(all_notes):
    """For every note whose true/natural sustain runs past its notated
    end, build a sustain-pedal-down window covering the gap -- so the
    note's written value stays short and clean (for engraving) while the
    audible sound during MIDI playback still rings out to its natural
    length. Standard MIDI sustain-pedal semantics: while CC64 is held
    down, a note keeps sounding after its own note-off until pedal-up.
    Returns a merged, non-overlapping list of (down_tick, up_tick)."""
    intervals = []
    for n in all_notes:
        if n['natural_end'] > n['f_end']:
            intervals.append((n['f_start'], n['natural_end']))
    return merge_intervals(intervals)


def build_pedal_track(pedal_windows):
    """Build a MIDI track of CC64 sustain-pedal down/up events for the
    given windows, on channel 0 (shared with the treble/bass tracks, so
    it affects both)."""
    events = []
    for down, up in pedal_windows:
        events.append((down, 127))
        events.append((up, 0))
    events.sort(key=lambda e: (e[0], -e[1]))  # down before up at same tick

    trk = MidiTrack()
    trk.append(MetaMessage('track_name', name='Sustain (playback only)', time=0))
    last_tick = 0
    for tick, value in events:
        delta = max(0, tick - last_tick)
        trk.append(Message('control_change', control=64, value=value, channel=0, time=delta))
        last_tick = tick
    trk.append(MetaMessage('end_of_track', time=0))
    return trk


def parse_track_selector(s, num_tracks):
    """Parse a --track value: a single index ('2'), a comma-separated list
    ('1,2'), or 'all'. Returns a sorted, de-duplicated list of track
    indices. Raises ValueError on bad input (caller turns that into a
    clean CLI error, not a traceback)."""
    s = s.strip().lower()
    if s == 'all':
        return list(range(num_tracks))
    indices = set()
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            indices.add(int(part))
        except ValueError:
            raise ValueError(f"'{part}' is not a valid track index")
    if not indices:
        raise ValueError("no track index given")
    return sorted(indices)


def summarize_tracks(mid):
    """Return [(index, name_or_None, note_on_count, sorted_channels_used), ...]
    for every track, for diagnostics and manual --track selection."""
    summary = []
    for i, trk in enumerate(mid.tracks):
        name = None
        note_count = 0
        channels = set()
        for m in trk:
            if m.type == 'track_name' and name is None:
                name = m.name
            if m.type == 'note_on' and m.velocity > 0:
                note_count += 1
                channels.add(m.channel)
        summary.append((i, name, note_count, sorted(channels)))
    return summary


def find_note_track(mid):
    """Pick the track with the most note_on events -- works whether the
    source is a single-track (type 0/1) file or already multi-track."""
    best_i, best_count = None, -1
    for i, trk in enumerate(mid.tracks):
        count = sum(1 for m in trk if m.type == 'note_on' and m.velocity > 0)
        if count > best_count:
            best_i, best_count = i, count
    return best_i


def describe_track_ambiguity(summary, chosen_idx):
    """If some other track has a substantial fraction of the chosen
    track's note_on count (>=20%), the auto-pick might not be what the
    user wants (e.g. a second melody/accompaniment instrument on its own
    track) -- return a warning string naming the alternative(s), or None
    if the chosen track is clearly dominant."""
    chosen_count = next(c for i, _, c, _ in summary if i == chosen_idx)
    if chosen_count <= 0:
        return None
    rivals = [(i, n, c) for i, n, c, _ in summary
              if i != chosen_idx and c >= chosen_count * 0.2 and c > 0]
    if not rivals:
        return None
    names = ", ".join(f"track {i}{f' (\"{n}\")' if n else ''} [{c} notes]" for i, n, c in rivals)
    return (f"Note: other track(s) also contain a substantial number of notes -- {names}. "
            f"If track {chosen_idx} isn't the piano part you expect, pass --track N to override.")


def detect_source_tempo(mid):
    """Read the first set_tempo meta message found in any track, if any.
    Returns (bpm, is_generic_default) or (None, False) if the source has
    no tempo info at all. is_generic_default is True when the raw tempo
    value is exactly 500000 microsec/beat (120 BPM) -- the untouched MIDI
    spec default that many transcription/export tools stamp automatically
    without actually measuring the piece's real tempo, so it should be
    treated as "no real info" rather than a trustworthy detection."""
    for trk in mid.tracks:
        for msg in trk:
            if msg.type == 'set_tempo':
                bpm = round(mido.tempo2bpm(msg.tempo), 1)
                is_generic = (msg.tempo == 500000)
                return bpm, is_generic
    return None, False


def describe_tempo_ambiguity(ranked_candidates):
    """Given estimate_tempo_candidates' ranked [(bpm, score), ...] output,
    return a warning string if the runner-up is a near-tied octave/simple
    ratio away from the winner (score within 25% relative of the best),
    since that means the estimate is genuinely ambiguous rather than
    confidently resolved -- or None if the winner is clearly ahead."""
    if len(ranked_candidates) < 2:
        return None
    best_bpm, best_score = ranked_candidates[0]
    for bpm, score in ranked_candidates[1:]:
        if best_score == 0 or score <= best_score * 1.25:
            ratio = bpm / best_bpm
            if any(abs(ratio - r) < 0.05 for r in (0.5, 2.0, 1 / 3, 3.0, 1.5, 2 / 3)):
                return (f"Note: {best_bpm} BPM and {bpm} BPM fit the rhythm almost equally well -- "
                        f"this is a fundamental octave/ratio ambiguity that can't be resolved from "
                        f"timing alone (doubling/halving tempo and note values together sounds "
                        f"identical). If playback sounds twice too fast or slow, try --tempo {bpm}.")
    return None


def estimate_tempo_from_rhythm(mid, notes):
    """Estimate BPM purely from the pattern of note onsets, for files with
    no usable embedded tempo.

    Converts onsets to real seconds using the source's own tick-encoding
    tempo (see rescale_notes_to_tempo) as the ticks<->seconds scaling
    factor -- this only affects that conversion, not the estimate's
    musical correctness. Builds a histogram of gaps between consecutive
    onsets, generates several candidate tempos from the top common gaps
    (each tried as a 16th/8th/quarter-note pulse), and scores every
    candidate by how well the *ratio* of each observed gap to that
    candidate's quarter-note length matches a "nice" rhythmic ratio
    (1/4, 1/3, 1/2, 2/3, 1, 1.5, 2, 3, 4 -- 16ths, triplets, 8ths,
    quarters, etc), using RELATIVE error (a fraction of the ratio, not
    of the candidate's own grid-cell size).

    This scale-invariant scoring matters: an earlier version measured
    misalignment directly against each candidate's own 16th-note grid
    cell, which is a *bigger* cell for slower candidates -- so the same
    absolute timing jitter always looked proportionally smaller under a
    slower candidate, systematically biasing every result toward half
    tempo regardless of which was actually correct. The ratio-based
    metric doesn't have that bias, since it compares gaps to quarter-note
    length as a dimensionless ratio.

    Octave ambiguity itself (100 vs 200 BPM, etc) is fundamentally
    unresolvable from IOI timing alone whenever the piece's note values
    form a clean power-of-two ladder -- doubling the tempo and halving
    every note value reproduces identical audio, so no amount of rhythm
    analysis alone can tell them apart without outside knowledge (a
    known limitation in tempo induction generally, not specific to this
    script). Rather than silently guessing in that case, this returns
    the single best-scoring candidate but also returns the list of
    near-tied alternatives (see estimate_tempo_candidates) so callers can
    warn the user instead of presenting false confidence.

    Returns None if there isn't enough onset data to make a confident guess.
    """
    result = estimate_tempo_candidates(mid, notes)
    return result[0][0] if result else None


def estimate_tempo_candidates(mid, notes):
    """Does the analysis for estimate_tempo_from_rhythm, but returns the
    full ranked list of (bpm, score) candidates (best first, lower score
    = better fit) instead of just the winner, so callers can detect and
    report near-tied octave ambiguity. Returns [] if there isn't enough
    onset data."""
    scaling_bpm, _ = detect_source_tempo(mid)
    scaling_tempo = mido.bpm2tempo(scaling_bpm or 120.0)
    tpb = mid.ticks_per_beat

    onset_ticks = sorted(set(n['start'] for n in notes))
    if len(onset_ticks) < 8:
        return []

    onset_sec = [mido.tick2second(t, tpb, scaling_tempo) for t in onset_ticks]
    iois = [b - a for a, b in zip(onset_sec, onset_sec[1:])]
    # keep only plausible subdivision-length gaps (20ms - 1s); longer gaps
    # are rests/held notes, not the underlying pulse
    iois = [x for x in iois if 0.02 <= x <= 1.0]
    if len(iois) < 8:
        return []

    buckets = defaultdict(int)
    for x in iois:
        buckets[round(x * 100)] += 1  # 10ms buckets
    top_buckets = sorted(buckets.items(), key=lambda kv: -kv[1])[:5]

    def normalize(bpm):
        while bpm < 60:
            bpm *= 2
        while bpm > 200:
            bpm /= 2
        return round(bpm, 1)

    candidates = set()
    for bucket, _count in top_buckets:
        pulse_sec = bucket / 100
        if pulse_sec <= 0:
            continue
        for subdivisions_per_beat in (4, 2, 1):  # pulse = 16th, 8th, quarter
            quarter_sec = pulse_sec * subdivisions_per_beat
            if quarter_sec <= 0:
                continue
            candidates.add(normalize(60.0 / quarter_sec))
    if not candidates:
        return []

    # "nice" ratios a gap-to-quarter-note ratio should land near:
    # 16th, triplet-8th, 8th, triplet-quarter, quarter, dotted-quarter,
    # half, dotted-half/3-beats, whole
    NICE_RATIOS = [0.25, 1 / 3, 0.5, 2 / 3, 1.0, 1.5, 2.0, 3.0, 4.0]

    def score(bpm):
        quarter_sec = 60.0 / bpm
        total = 0.0
        for x in iois:
            ratio = x / quarter_sec
            nearest = min(NICE_RATIOS, key=lambda n: abs(ratio - n))
            total += abs(ratio - nearest) / nearest
        return total / len(iois)

    ranked = sorted(((bpm, score(bpm)) for bpm in candidates), key=lambda kv: kv[1])
    return ranked


def estimate_split_pitch(notes, fallback=SPLIT_PITCH):
    """Guess a natural treble/bass split point from the actual pitch
    distribution using an Otsu-style threshold search: the pitch value
    that maximizes the separation between the two resulting note clusters
    (a proxy for "where the two hands naturally divide"). Falls back to
    middle C if there isn't enough pitch spread to make a confident guess."""
    pitches = [n['pitch'] for n in notes]
    if len(pitches) < 10:
        return fallback
    lo, hi = min(pitches), max(pitches)
    if hi - lo < 4:
        return fallback

    hist = defaultdict(int)
    for p in pitches:
        hist[p] += 1
    total = len(pitches)
    sum_total = sum(p * c for p, c in hist.items())

    best_t, best_var = fallback, -1.0
    weight_below, sum_below = 0, 0
    for t in range(lo, hi + 1):
        weight_below += hist.get(t, 0)
        if weight_below == 0:
            continue
        weight_above = total - weight_below
        if weight_above == 0:
            break
        sum_below += t * hist.get(t, 0)
        mean_below = sum_below / weight_below
        mean_above = (sum_total - sum_below) / weight_above
        between_var = weight_below * weight_above * (mean_below - mean_above) ** 2
        if between_var > best_var:
            best_var = between_var
            best_t = t + 1  # pitches >= best_t become treble

    return best_t


def compute_dynamic_split_points(notes, bar_ticks, window_bars=8, fallback=SPLIT_PITCH, max_step=4):
    """Per-window treble/bass split, instead of one fixed value for the
    whole piece. Reuses estimate_split_pitch's Otsu-style estimate, just
    run separately on each window_bars-bar window of notes so the split
    can follow the music's register drifting over time (e.g. a verse
    sitting lower than a chorus), rather than forcing one hand-position
    compromise across the entire piece.

    Two things keep this from being noisy or jumpy in practice:
    - a sparse window (too few notes, or too little pitch spread, for
      estimate_split_pitch to make a confident call) falls back to the
      PREVIOUS window's chosen split rather than snapping back to the
      global fallback, so a quiet passage doesn't cause a spurious jump;
    - the per-window step is hard-capped at max_step semitones, since a
      hand can't instantly relocate and an abrupt split-point jump would
      look like a notation error, not a musical one.

    Returns (window_ticks, {window_index: split_pitch}); window_index for
    a note is n['f_start'] // window_ticks.
    """
    window_ticks = bar_ticks * window_bars
    by_window = defaultdict(list)
    for n in notes:
        by_window[n['f_start'] // window_ticks].append(n)
    windows = sorted(by_window)

    raw = {}
    prev_estimate = fallback
    for w in windows:
        prev_estimate = estimate_split_pitch(by_window[w], fallback=prev_estimate)
        raw[w] = prev_estimate

    smoothed = {}
    prev = None
    for w in windows:
        if prev is None:
            smoothed[w] = raw[w]
        else:
            delta = max(-max_step, min(max_step, raw[w] - prev))
            smoothed[w] = prev + delta
        prev = smoothed[w]

    return window_ticks, smoothed


def assign_hands(notes, max_hand_span=16, ambiguity_zone=3):
    """Refines the naive per-note treble/bass split -- each note already
    carries n['_split_pitch'], the boundary its onset uses, from either
    the fixed --split-pitch or compute_dynamic_split_points -- by looking
    at each onset event (chord) as a whole instead of one note at a time.

    Only notes within `ambiguity_zone` semitones of their onset's split
    point are ever reconsidered; a note clearly above or below the line
    is left exactly where the threshold put it. An ambiguous note is
    moved to the other hand only if both hold:
      (a) it sits closer to that hand's actual recent position -- the
          pitch centroid of its own last onset -- than to its
          naively-assigned hand's recent position. This is the "left
          hand is already busy an octave lower" case: a boundary note
          gets pulled toward whichever hand it's registrally closer to
          given where that hand actually just was, not just the fixed
          threshold;
      (b) doing so doesn't push either hand's resulting pitch-cluster
          for this onset past max_hand_span semitones -- a hand can't
          physically stretch further than that regardless of what
          continuity would prefer.

    Mutates each note's new 'hand' field ('treble'/'bass'); doesn't
    touch pitch, timing, or anything else. Returns (reassigned_count,
    warnings) where warnings is a list of (onset, span) for onsets whose
    FINAL assignment still leaves a hand spanning more than
    max_hand_span semitones -- a real, unavoidable stretch the source
    material has, not something reassignment could fix; surfaced as a
    diagnostic; never auto-corrected further (there's no safe universal
    fix -- could mean an arpeggiated read, a genuine two-hand chord
    split some other way, or a transcription artifact).
    """
    if not notes:
        return 0, []

    by_onset = defaultdict(list)
    for n in notes:
        by_onset[n['f_start']].append(n)
    onsets = sorted(by_onset)

    for n in notes:
        n['hand'] = 'treble' if n['pitch'] >= n['_split_pitch'] else 'bass'
    naive = {id(n): n['hand'] for n in notes}

    prev_centroid = {'treble': None, 'bass': None}
    warnings = []
    for onset in onsets:
        event = by_onset[onset]
        split = event[0]['_split_pitch']

        for n in event:
            if abs(n['pitch'] - split) > ambiguity_zone:
                continue
            other = 'bass' if n['hand'] == 'treble' else 'treble'
            if prev_centroid[other] is None:
                continue
            dist_current = (abs(n['pitch'] - prev_centroid[n['hand']])
                             if prev_centroid[n['hand']] is not None else 0)
            dist_other = abs(n['pitch'] - prev_centroid[other])
            if dist_other >= dist_current:
                continue

            trial = defaultdict(list)
            for m in event:
                h = other if m is n else m['hand']
                trial[h].append(m['pitch'])
            if all((max(v) - min(v)) <= max_hand_span for v in trial.values() if v):
                n['hand'] = other

        for hand in ('treble', 'bass'):
            pitches = [n['pitch'] for n in event if n['hand'] == hand]
            if pitches:
                prev_centroid[hand] = sum(pitches) / len(pitches)
                if (max(pitches) - min(pitches)) > max_hand_span:
                    warnings.append((onset, max(pitches) - min(pitches)))

    reassigned = sum(1 for n in notes if n['hand'] != naive[id(n)])
    return reassigned, warnings


def bar_diagnostics(notes, bar_ticks, top_n=10):
    """Per-bar rollup of the same signals report() already computes (chord
    conflicts, rests, heavy tie chains, cross-bar ties, invented sustain),
    so a reader can jump straight to the handful of measures actually worth
    a manual look instead of scanning the whole score. Pure read-over of
    already-finalized notes, same as report(); doesn't affect engraving."""
    bars = defaultdict(lambda: {'conflict': 0, 'rest': 0, 'heavy_tie': 0,
                                 'cross_bar_tie': 0, 'invented': 0})

    onsets = sorted(set(n['f_start'] for n in notes))
    next_onset_after = {a: b for a, b in zip(onsets, onsets[1:])}

    by_onset = defaultdict(set)
    for n in notes:
        by_onset[n['f_start']].add(n['f_end'] - n['f_start'])
    for onset, durs in by_onset.items():
        if len(durs) > 1:
            bars[onset // bar_ticks]['conflict'] += 1

    for n in notes:
        bar = n['f_start'] // bar_ticks
        units = (n['f_end'] - n['f_start']) // GRID
        ties = true_tie_count(n['f_start'], units, bar_ticks) - 1
        if ties >= 2:
            bars[bar]['heavy_tie'] += 1
        if bar != (n['f_end'] - 1) // bar_ticks:
            bars[bar]['cross_bar_tie'] += 1
        nxt = next_onset_after.get(n['f_start'])
        if nxt is not None and nxt - n['f_end'] > 0:
            bars[bar]['rest'] += 1
        if units - n.get('nat_units', 0) > 0:
            bars[bar]['invented'] += 1

    scored = [(bar, sum(c.values()), c) for bar, c in bars.items()]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:top_n]


def confidence_warnings(notes, bar_ticks, hand_warnings=None, low_conf_threshold=0.3, top_n=10):
    """Combines two experimental signals -- classify_voice_roles's
    per-note confidence and assign_hands's span-violation warnings --
    into a single per-bar view of where ScorePrep is least sure it made
    the right call, rather than presenting every automated decision as
    equally certain. A "here's where I might be wrong" readout, in the
    spirit of the confidence-warnings idea from the feature brainstorm --
    this is the version of it grounded in signals classify_voice_roles
    and assign_hands actually compute, not a display gimmick with
    invented-looking numbers behind it.

    Operates on the full (pre-split) note list so it isn't limited to one
    staff, since a hand-span violation by definition involves both. Pure
    diagnostic -- doesn't change engraving output, and has no on/off
    switch of its own since it can't make anything worse.

    Returns [(bar, severity, low_conf_count, span_semitones)], worst
    first, capped at top_n. A hand-span violation is weighted higher
    than a handful of ambiguous voice-role calls -- an unplayable chord
    is a bigger deal than uncertainty about which line is the melody.
    """
    by_bar_low_conf = defaultdict(int)
    for n in notes:
        if n.get('voice_confidence', 1.0) < low_conf_threshold:
            by_bar_low_conf[n['f_start'] // bar_ticks] += 1

    by_bar_span = defaultdict(int)
    for onset, span in (hand_warnings or []):
        bar = onset // bar_ticks
        by_bar_span[bar] = max(by_bar_span[bar], span)

    scored = []
    for bar in set(by_bar_low_conf) | set(by_bar_span):
        low_conf = by_bar_low_conf.get(bar, 0)
        span = by_bar_span.get(bar, 0)
        severity = span * 2 + low_conf
        if severity > 0:
            scored.append((bar, severity, low_conf, span))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:top_n]


def report(name, notes, bar_ticks, opt_stats=None):
    """Pure diagnostic pass over already-finalized notes -- reads f_start/
    f_end/nat_units, mutates nothing, so restructuring this has zero effect
    on the actual engraving decisions made upstream."""
    tie_counts = [true_tie_count(n['f_start'], (n['f_end'] - n['f_start']) // GRID, bar_ticks)
                  for n in notes]
    # true_tie_count returns notehead count (a plain untied note is 1 notehead,
    # not 0) -- actual tie count is noteheads - 1.
    ties = [c - 1 for c in tie_counts]
    tie_0 = sum(1 for t in ties if t == 0)
    tie_1 = sum(1 for t in ties if t == 1)
    tie_2plus = sum(1 for t in ties if t >= 2)
    max_ties = max(ties) if ties else 0
    cross_bar = sum(1 for n in notes if (n['f_start'] // bar_ticks) != ((n['f_end'] - 1) // bar_ticks))
    by_onset = defaultdict(set)
    for n in notes:
        by_onset[n['f_start']].add(n['f_end'] - n['f_start'])
    conflicts = sum(1 for durs in by_onset.values() if len(durs) > 1)
    onsets = sorted(set(n['f_start'] for n in notes))
    next_onset_after = {a: b for a, b in zip(onsets, onsets[1:])}
    rests = sum(1 for n in notes
                if (nxt := next_onset_after.get(n['f_start'])) is not None and nxt - n['f_end'] > 0)
    fab_units = sum(max(0, (n['f_end'] - n['f_start']) // GRID - n.get('nat_units', 0)) for n in notes)
    extended = sum(1 for n in notes if (n['f_end'] - n['f_start']) // GRID > n.get('nat_units', 0))

    print(f"\n===== Notation ({name}) =====", file=sys.stderr)
    print(f"Notes:                {len(notes)}", file=sys.stderr)
    print(f"Tie chains -- none: {tie_0}  one tie: {tie_1}  two+ ties: {tie_2plus}  "
          f"(longest chain: {max_ties})", file=sys.stderr)
    print(f"Cross-bar ties:       {cross_bar}", file=sys.stderr)
    print(f"Rests introduced:     {rests}", file=sys.stderr)
    print(f"Chord conflicts:      {conflicts}", file=sys.stderr)
    if extended:
        print(f"Invented sustain:     {extended} note(s), {fab_units} {GRID_UNIT_NAME} total",
              file=sys.stderr)
    else:
        print(f"Invented sustain:     none needed at this tie-temperature", file=sys.stderr)

    if opt_stats:
        print(f"\n===== Optimizer Decisions ({name}) =====", file=sys.stderr)
        print("(why each note's written length was chosen -- categories are mutually", file=sys.stderr)
        print(" exclusive, checked in this order)", file=sys.stderr)
        print(f"Exact, no rest:        {opt_stats.get('exact_no_rest', 0)}  "
              f"(truthful baseline already reached the next note)", file=sys.stderr)
        print(f"Rest kept:             {opt_stats.get('rest_kept', 0)}  "
              f"(extending wasn't worth the cost, or there was no room to)", file=sys.stderr)
        print(f"Extended, truthful:    {opt_stats.get('extended_truthful', 0)}  "
              f"(used more ties than the bare minimum to close/shrink a rest, "
              f"still within real evidence)", file=sys.stderr)
        print(f"Invented sustain:      {opt_stats.get('invented_sustain', 0)}  "
              f"(extended past real evidence to close/shrink a rest)", file=sys.stderr)

    top_bars = bar_diagnostics(notes, bar_ticks)
    if top_bars:
        print(f"\n===== Measures Needing Attention ({name}) =====", file=sys.stderr)
        print("(top measures by combined issue count, 1-indexed -- worth a manual look)",
              file=sys.stderr)
        for bar, total, c in top_bars:
            parts = []
            if c['conflict']:
                parts.append(f"{c['conflict']} chord conflict(s)")
            if c['rest']:
                parts.append(f"{c['rest']} rest(s)")
            if c['heavy_tie']:
                parts.append(f"{c['heavy_tie']} heavy tie chain(s)")
            if c['cross_bar_tie']:
                parts.append(f"{c['cross_bar_tie']} cross-bar tie(s)")
            if c['invented']:
                parts.append(f"{c['invented']} invented sustain")
            print(f"  Measure {bar + 1}: {', '.join(parts)}", file=sys.stderr)

    if notes and 'voice_role' in notes[0]:
        n_melody = sum(1 for n in notes if n['voice_role'] == 'melody')
        n_accomp = len(notes) - n_melody
        avg_conf = sum(n['voice_confidence'] for n in notes) / len(notes)
        low_conf = sum(1 for n in notes if n['voice_confidence'] < 0.3)
        used = opt_stats is not None and ('melody_biased' in opt_stats or 'accompaniment_biased' in opt_stats)
        print(f"\n===== Voice Roles ({name}{'' if used else ', experimental'}) =====", file=sys.stderr)
        if used:
            print("(used this run to bias the duration optimizer's weights -- see "
                  "melody-preservation above)", file=sys.stderr)
        else:
            print("(heuristic melody/accompaniment classification -- not used by any "
                  "engraving decision yet; shown for validation only)", file=sys.stderr)
        print(f"Melody: {n_melody}  Accompaniment: {n_accomp}  "
              f"Avg confidence: {avg_conf:.2f}  Low-confidence (<0.3): {low_conf}",
              file=sys.stderr)


def run(input_path, output_path, tempo, split_pitch, temperature, time_sig=None,
        pedal_mode='ignore', min_note_ticks=None, playback_sustain=True, grid_mode='straight',
        track_selector=None, channel_override=None, duration_style='dotted',
        min_velocity=0, velocity_mode='passthrough', velocity_scale=1.0,
        tie_weight=None, rest_weight=None, artic_weight=None,
        preserve_source_duration=True, melody_preservation=False,
        dynamic_split=False, split_window_bars=8,
        hand_assignment=False, max_hand_span=16, hand_ambiguity_zone=3):
    """Runs the full cleanup pipeline. Shared by --interactive and normal
    CLI-argument mode. tempo, split_pitch, and time_sig may be None, in
    which case they're estimated from the source file."""
    temperature = max(0.0, min(1.0, temperature))
    configure_grid(grid_mode, duration_style)
    if grid_mode == 'triplet':
        print("grid=triplet -- quantizing to a grid that natively fits both straight and "
              "triplet-eighth subdivisions, instead of forcing everything onto straight 16ths",
              file=sys.stderr)
    if duration_style == 'powers2':
        print("clean-durations=powers2 -- restricting single-notehead durations to plain "
              "power-of-two values (no dotted notes); anything that would've used a dotted "
              "value now needs a tie instead", file=sys.stderr)

    try:
        mid = MidiFile(input_path)
    except FileNotFoundError:
        sys.exit(f"Error: input file not found: {input_path}")
    except (IsADirectoryError, PermissionError) as e:
        sys.exit(f"Error: can't read '{input_path}': {e}")
    except Exception as e:
        sys.exit(f"Error: '{input_path}' doesn't look like a valid MIDI file ({e})")
    if mid.ticks_per_beat != TICKS_PER_BEAT:
        print(f"NOTE: source ticks_per_beat={mid.ticks_per_beat}, expected {TICKS_PER_BEAT}. "
              f"Grid/bar math assumes {TICKS_PER_BEAT}; results may be off.", file=sys.stderr)

    track_summary = summarize_tracks(mid)
    if track_selector is not None:
        try:
            track_indices = parse_track_selector(str(track_selector), len(mid.tracks))
        except ValueError as e:
            sys.exit(f"Error: --track: {e}")
        bad = [t for t in track_indices if not (0 <= t < len(mid.tracks))]
        if bad:
            sys.exit(f"Error: --track index/indices {bad} out of range -- file has "
                     f"{len(mid.tracks)} track(s) (valid: 0-{len(mid.tracks) - 1}).")
        if len(track_indices) == 1:
            i = track_indices[0]
            _, name, count, channels = track_summary[i]
            print(f"--track {i} given -- using it explicitly "
                  f"({count} note_on event(s){f', name \"{name}\"' if name else ''}"
                  f"{f', channels used: {channels}' if channels else ''})", file=sys.stderr)
        else:
            parts = "; ".join(f"track {i}{f' (\"{n}\")' if n else ''} [{c} notes]"
                               for i, n, c, _ch in (track_summary[t] for t in track_indices))
            print(f"--track {','.join(map(str, track_indices))} given -- merging "
                  f"{len(track_indices)} tracks: {parts}", file=sys.stderr)
    else:
        auto_idx = find_note_track(mid)
        if auto_idx is None:
            sys.exit("No note events found in any track.")
        track_indices = [auto_idx]
        _, name, count, channels = track_summary[auto_idx]
        print(f"No --track given -- auto-selected track {auto_idx} as the note track "
              f"({count} note_on event(s){f', name \"{name}\"' if name else ''}"
              f"{f', channels used: {channels}' if channels else ''})", file=sys.stderr)
        ambiguity = describe_track_ambiguity(track_summary, auto_idx)
        if ambiguity:
            print(ambiguity, file=sys.stderr)

    if channel_override is not None:
        if not (0 <= channel_override <= 15):
            sys.exit(f"Error: --channel {channel_override} out of range (valid: 0-15).")
        print(f"--channel {channel_override} given -- filtering to that channel only",
              file=sys.stderr)

    notes = []
    for t in track_indices:
        notes.extend(extract_notes(mid.tracks[t], channel=channel_override))
    notes.sort(key=lambda n: (n['start'], n['pitch']))
    if not notes:
        listing = "\n".join(
            f"  track {i}: {c} note_on event(s){f', name \"{n}\"' if n else ''}"
            f"{f', channels used: {ch}' if ch else ''}"
            for i, n, c, ch in track_summary)
        sys.exit(f"Error: no notes found on track(s) {track_indices}"
                 f"{f' channel {channel_override}' if channel_override is not None else ''}. "
                 f"Tracks in this file:\n{listing}")

    auto_min = min_note_ticks is None
    if auto_min:
        min_note_ticks = max(1, GRID // 4)  # a 64th note -- clearly below any intended value
    notes, dropped = filter_noise_notes(notes, min_note_ticks)
    if dropped:
        print(f"Dropped {dropped} note(s) shorter than {min_note_ticks} ticks "
              f"({'auto threshold' if auto_min else 'explicit --min-note-ticks'}) "
              f"as likely transcription noise", file=sys.stderr)

    notes, vel_dropped = filter_ghost_velocity_notes(notes, min_velocity)
    if vel_dropped:
        print(f"Dropped {vel_dropped} note(s) below velocity {min_velocity} "
              f"(--min-velocity) as likely ghost notes", file=sys.stderr)
    if velocity_mode != 'passthrough':
        print(f"velocity-mode={velocity_mode}"
              f"{f' (scale={velocity_scale})' if velocity_mode == 'scale' else ''} "
              f"-- computed after noise/ghost-note filtering, so dropped notes don't skew it",
              file=sys.stderr)
    apply_velocity_mode(notes, velocity_mode, velocity_scale)

    # Leading-silence rebase: if the first note doesn't start at tick 0,
    # notation software will render that gap as empty leading measures.
    # This is almost always real silence at the start of the source audio
    # (an intro, spoken section, etc.) faithfully transcribed, not a bug --
    # but the score shouldn't render it as blank bars, so shift everything
    # so the first note starts the piece.
    leading_offset = min((n['start'] for n in notes), default=0)
    if leading_offset > 0:
        for n in notes:
            n['start'] -= leading_offset
            n['end'] -= leading_offset
        gap_beats = leading_offset / TICKS_PER_BEAT
        if gap_beats >= 1.0:
            print(f"First note begins {gap_beats:.1f} beats ({leading_offset} ticks) into the "
                  f"source file -- almost always real leading silence in the source audio (e.g. "
                  f"an intro before playing starts), faithfully transcribed, not a bug. Rebasing "
                  f"so the output score starts at the first note instead of showing empty leading "
                  f"measures.", file=sys.stderr)

    if pedal_mode == 'reflect':
        pedal_intervals = read_pedal_intervals(mid)
        if leading_offset > 0:
            pedal_intervals = [(max(0, d - leading_offset), max(0, u - leading_offset))
                                for d, u in pedal_intervals]
        extended = 0
        for n in notes:
            new_end = extend_end_with_pedal(n['end'], pedal_intervals)
            if new_end != n['end']:
                extended += 1
                n['end'] = new_end
        print(f"pedal-mode=reflect -- extended {extended} note(s) whose release fell "
              f"during a pedal-down window", file=sys.stderr)
    else:
        print("pedal-mode=ignore -- sustain pedal data not used (default)", file=sys.stderr)

    if time_sig is None:
        detected_sig, sig_is_generic = detect_source_time_sig(mid)
        if detected_sig is not None and not sig_is_generic:
            time_sig = detected_sig
            print(f"No --time-sig given -- using source file time signature: "
                  f"{time_sig[0]}/{time_sig[1]}", file=sys.stderr)
        else:
            time_sig = DEFAULT_TIME_SIG
            if detected_sig is not None and sig_is_generic:
                print(f"No --time-sig given -- source file's time signature ({detected_sig[0]}/{detected_sig[1]}) "
                      f"is byte-identical to the untouched MIDI spec default, which most transcription tools "
                      f"stamp automatically without actually detecting it. Treating as unknown and defaulting "
                      f"to {DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]} -- there's no reliable way to infer the "
                      f"real time signature from note timing, so please pass --time-sig explicitly if this "
                      f"piece isn't in {DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]}.", file=sys.stderr)
            else:
                print(f"No --time-sig given -- using default: {DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]}",
                      file=sys.stderr)
    bar_ticks = bar_ticks_for(time_sig)

    if tempo is None:
        detected, tempo_is_generic = detect_source_tempo(mid)
        if detected is not None and not tempo_is_generic:
            tempo = detected
            print(f"No --tempo given -- using source file tempo: {tempo} BPM", file=sys.stderr)
        else:
            if detected is not None and tempo_is_generic:
                print(f"No --tempo given -- source file tempo ({detected} BPM) is byte-identical to the "
                      f"untouched MIDI spec default, which most transcription tools stamp automatically "
                      f"without actually measuring it. Falling through to rhythm-based estimation instead.",
                      file=sys.stderr)
            ranked = estimate_tempo_candidates(mid, notes)
            estimated = ranked[0][0] if ranked else None
            tempo = estimated if estimated is not None else 120.0
            print(f"No --tempo given and no usable tempo in source file -- "
                  f"{'estimated from note-onset rhythm' if estimated is not None else 'using neutral default'}: "
                  f"{tempo} BPM", file=sys.stderr)
            ambiguity = describe_tempo_ambiguity(ranked) if ranked else None
            if ambiguity:
                print(ambiguity, file=sys.stderr)

    if split_pitch is None:
        split_pitch = estimate_split_pitch(notes)
        print(f"No --split-pitch given -- estimated natural treble/bass split at "
              f"pitch {split_pitch} (from pitch distribution)", file=sys.stderr)

    source_encoding_tempo, _ = detect_source_tempo(mid)
    source_encoding_tempo = source_encoding_tempo or 120.0
    if source_encoding_tempo != tempo:
        if preserve_source_duration:
            print(f"Rescaling note timing from the source file's tick-encoding tempo "
                  f"({source_encoding_tempo} BPM -- the reference tempo used when the source's ticks "
                  f"were generated, not a musical judgement) to the notated output tempo ({tempo} BPM), "
                  f"so playback speed matches the original audio regardless of what tempo the score is "
                  f"labeled with.", file=sys.stderr)
            rescale_notes_to_tempo(notes, source_encoding_tempo, tempo)
        else:
            print(f"tempo-rescale=change-speed -- leaving note ticks as-is and labeling the output "
                  f"{tempo} BPM, so playback is genuinely {tempo / source_encoding_tempo:.2f}x the "
                  f"source's speed (not rescaled to preserve the original real-world duration).",
                  file=sys.stderr)

    resolve_note_durations(notes, temperature, bar_ticks)
    classify_voice_roles(notes)

    if dynamic_split:
        window_ticks, split_points = compute_dynamic_split_points(
            notes, bar_ticks, split_window_bars, fallback=split_pitch)
        for n in notes:
            n['_split_pitch'] = split_points[n['f_start'] // window_ticks]
        vals = sorted(split_points.values())
        print(f"dynamic-split=on -- split point re-estimated every {split_window_bars} bars "
              f"({len(split_points)} window(s), range {vals[0]}-{vals[-1]}, "
              f"median {vals[len(vals) // 2]}) instead of the fixed pitch {split_pitch}",
              file=sys.stderr)
    else:
        for n in notes:
            n['_split_pitch'] = split_pitch

    if hand_assignment:
        reassigned, hand_warnings = assign_hands(notes, max_hand_span, hand_ambiguity_zone)
        print(f"hand-assignment=on -- {reassigned} note(s) reassigned across the naive "
              f"treble/bass split based on chord context and hand continuity "
              f"(max-hand-span={max_hand_span}, ambiguity-zone={hand_ambiguity_zone})",
              file=sys.stderr)
        if hand_warnings:
            print(f"  {len(hand_warnings)} onset(s) still exceed max-hand-span even after "
                  f"reassignment -- likely a genuine stretch in the source, not something "
                  f"reassignment alone can fix; widest: {max(w for _, w in hand_warnings)} semitones",
                  file=sys.stderr)
    else:
        for n in notes:
            n['hand'] = 'treble' if n['pitch'] >= n['_split_pitch'] else 'bass'
        hand_warnings = []

    treble = [n for n in notes if n['hand'] == 'treble']
    bass = [n for n in notes if n['hand'] == 'bass']

    tie_budget = tie_budget_for(temperature)
    weights = optimizer_weights(temperature, tie_weight, rest_weight, artic_weight)
    optimize_staff_durations(treble, temperature, bar_ticks, weights,
                              melody_preservation=melody_preservation)
    optimize_staff_durations(bass, temperature, bar_ticks, weights,
                              melody_preservation=melody_preservation)
    fix_same_pitch_overlaps(treble, tie_budget, bar_ticks)
    fix_same_pitch_overlaps(bass, tie_budget, bar_ticks)
    # fixing a same-pitch overlap can shorten just one member of a chord
    # the optimizer already made uniform -- re-harmonize once more to
    # close that gap. Since this second pass can only ever shorten notes
    # further (never lengthen), it can't reopen any overlap the previous
    # step just fixed, so one extra pass is sufficient.
    treble_opt_stats, bass_opt_stats = {}, {}
    optimize_staff_durations(treble, temperature, bar_ticks, weights, treble_opt_stats,
                              melody_preservation=melody_preservation)
    optimize_staff_durations(bass, temperature, bar_ticks, weights, bass_opt_stats,
                              melody_preservation=melody_preservation)
    if melody_preservation:
        print(f"melody-preservation=on -- duration optimizer weights biased per event "
              f"by voice role (melody: tie x{MELODY_TIE_MULT} rest x{MELODY_REST_MULT}, "
              f"accompaniment: tie x{ACCOMP_TIE_MULT} rest x{ACCOMP_REST_MULT})",
              file=sys.stderr)

    print(f"tie-temperature={temperature:.2f}  (max_bars={1 + round(temperature * 7)}, "
          f"tie_budget={tie_budget}, weights: tie={weights[0]:.2f} rest={weights[1]:.2f} "
          f"articulation={weights[2]:.2f})", file=sys.stderr)
    print(f"\n===== Staff Split =====", file=sys.stderr)
    print(f"Processed {len(notes)} notes -> treble {len(treble)}, bass {len(bass)}", file=sys.stderr)
    report('TREBLE', treble, bar_ticks, treble_opt_stats)
    report('BASS', bass, bar_ticks, bass_opt_stats)

    conf_warnings = confidence_warnings(notes, bar_ticks, hand_warnings)
    if conf_warnings:
        print(f"\n===== Confidence Warnings (experimental) =====", file=sys.stderr)
        print("(bars where the automated classifiers are least certain -- worth a look; "
              "1-indexed)", file=sys.stderr)
        for bar, severity, low_conf, span in conf_warnings:
            parts = []
            if low_conf:
                parts.append(f"{low_conf} ambiguous melody/accompaniment call(s)")
            if span:
                parts.append(f"hand-span {span} semitones (exceeds --max-hand-span)")
            print(f"  Measure {bar + 1}: {', '.join(parts)}", file=sys.stderr)

    treble_track = build_track(treble, 'Treble')
    bass_track = build_track(bass, 'Bass')

    tempo_track = MidiTrack()
    tempo_track.append(MetaMessage('time_signature', numerator=time_sig[0], denominator=time_sig[1],
                                    clocks_per_click=24, notated_32nd_notes_per_beat=8, time=0))
    tempo_track.append(MetaMessage('set_tempo', tempo=mido.bpm2tempo(tempo), time=0))
    tempo_track.append(MetaMessage('end_of_track', time=0))

    out = MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    out.tracks.append(tempo_track)
    out.tracks.append(treble_track)
    out.tracks.append(bass_track)

    if playback_sustain:
        pedal_windows = compute_playback_pedal_windows(treble + bass)
        if pedal_windows:
            extended_notes = sum(1 for n in treble + bass if n['natural_end'] > n['f_end'])
            total_extension_ticks = sum(n['natural_end'] - n['f_end'] for n in treble + bass
                                         if n['natural_end'] > n['f_end'])
            avg_extension_16ths = round(total_extension_ticks / max(1, extended_notes) / GRID, 1)
            print(f"playback-sustain=on -- notation kept clean, but added sustain-pedal automation "
                  f"({len(pedal_windows)} window(s)) so {extended_notes} note(s) still ring out to "
                  f"their real length during playback (avg extension ~{avg_extension_16ths} {GRID_UNIT_NAME})",
                  file=sys.stderr)
            out.tracks.append(build_pedal_track(pedal_windows))
        else:
            print("playback-sustain=on -- no notes needed extending (notated length already matched "
                  "the real sustain)", file=sys.stderr)
    else:
        print("playback-sustain=off -- MIDI playback will sound exactly as short as the written "
              "notation (may sound choppy for pieces with lots of shortened notes)", file=sys.stderr)

    out.save(output_path)
    print(f"Saved {output_path}", file=sys.stderr)
    save_last_paths(input_path, output_path)


def _prompt(msg, default=None, cast=str, validate=None):
    """Small helper for interactive prompts: shows a default, casts the
    input, and re-asks on invalid input."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{msg}{suffix}: ").strip()
        if raw == "" and default is not None:
            return default
        try:
            value = cast(raw)
        except (ValueError, TypeError):
            print(f"  Couldn't parse that as {cast.__name__}, try again.")
            continue
        if validate is not None:
            ok, err = validate(value)
            if not ok:
                print(f"  {err}")
                continue
        return value


def _warn(text):
    """Wrap text in a warning color (yellow) when stdout is an actual
    terminal; plain text otherwise (piped output, redirected to a file,
    non-ANSI terminals) so escape codes never corrupt non-interactive use."""
    if sys.stdout.isatty():
        return f"\033[33m{text}\033[0m"
    return text


def _prompt_bool(msg, default=False):
    suffix = " [Y/n]" if default else " [y/N]"
    raw = input(f"{msg}{suffix}: ").strip().lower()
    if raw == "":
        return default
    return raw in ('y', 'yes')


def interactive_mode():
    print("=" * 60)
    print("scoreprep.py -- interactive mode")
    print("Splits a raw piano MIDI transcription into a clean, tie-light")
    print("two-staff grand staff MIDI ready for MuseScore.")
    print("(press Enter to accept the [default] shown for any question)")
    print("=" * 60)

    def input_exists(p):
        if not os.path.isfile(p):
            return False, f"File not found: {p}"
        return True, None

    last = load_last_paths()
    last_input = last.get('last_input')
    default_input = last_input if last_input and os.path.isfile(last_input) else None
    input_path = _prompt("Input MIDI file path", default=default_input, validate=input_exists)

    # load the file now so we can suggest data-driven defaults
    mid = MidiFile(input_path)

    track_summary = summarize_tracks(mid)
    auto_track_idx = find_note_track(mid)
    if auto_track_idx is None:
        print("Error: no note events found in any track.")
        sys.exit(1)
    if len(mid.tracks) > 1:
        print(f"\nFound {len(mid.tracks)} track(s):")
        for i, name, count, channels in track_summary:
            marker = " <- most notes" if i == auto_track_idx else ""
            print(f"  track {i}: {count} note_on event(s)"
                  f"{f', name \"{name}\"' if name else ''}"
                  f"{f', channels used: {channels}' if channels else ''}{marker}")
        ambiguity = describe_track_ambiguity(track_summary, auto_track_idx)
        if ambiguity:
            print(ambiguity.replace("pass --track N to override", "pick a different track below"))

        def valid_track(s):
            try:
                idxs = parse_track_selector(s, len(mid.tracks))
            except ValueError as e:
                return False, str(e)
            bad = [t for t in idxs if not (0 <= t < len(mid.tracks))]
            if bad:
                return False, f"Track(s) {bad} out of range -- must be 0-{len(mid.tracks) - 1}."
            return True, None
        raw_track = _prompt("Note track index (single, comma-list, or 'all')",
                             default=str(auto_track_idx), validate=valid_track)
        track_indices = parse_track_selector(raw_track, len(mid.tracks))
    else:
        track_indices = [auto_track_idx]

    channel_override = None
    # only offer a channel prompt when every selected track actually has >1 channel in use
    all_channels = sorted(set(ch for t in track_indices for ch in track_summary[t][3]))
    if len(all_channels) > 1:
        print(f"\nSelected track(s) carry multiple MIDI channels ({all_channels}) -- "
              f"if these merge more than one instrument, pick just one channel, or "
              f"keep 'all' to use all notes regardless of channel.")

        def valid_channel(s):
            if s.strip().lower() == 'all':
                return True, None
            try:
                c = int(s)
            except (TypeError, ValueError):
                return False, "Must be an integer 0-15, or 'all'."
            if 0 <= c <= 15:
                return True, None
            return False, "Must be 0-15."
        raw_channel = _prompt("MIDI channel ('all' or 0-15)", default='all', validate=valid_channel)
        channel_override = None if raw_channel.strip().lower() == 'all' else int(raw_channel)

    notes = []
    for t in track_indices:
        notes.extend(extract_notes(mid.tracks[t], channel=channel_override))
    notes.sort(key=lambda n: (n['start'], n['pitch']))
    if not notes:
        print(f"Error: no notes found on track(s) {track_indices}"
              f"{f' channel {channel_override}' if channel_override is not None else ''}.")
        sys.exit(1)
    notes, _ = filter_noise_notes(notes, max(1, GRID // 4))

    base, _ = os.path.splitext(input_path)
    if last_input and os.path.abspath(last_input) == os.path.abspath(input_path) and last.get('last_output'):
        default_output = last['last_output']  # same input as last time -- likely re-testing options
    else:
        default_output = base + "_grandstaff.mid"
    output_path = _prompt("Output MIDI file path", default=default_output)

    detected_tempo, tempo_is_generic = detect_source_tempo(mid)
    if detected_tempo is not None and not tempo_is_generic:
        print(f"(detected tempo in source file: {detected_tempo} BPM)")
        tempo_default = detected_tempo
    else:
        if detected_tempo is not None and tempo_is_generic:
            print(f"(source file tempo ({detected_tempo} BPM) is byte-identical to the untouched MIDI "
                  f"spec default -- most transcription tools stamp this automatically without actually "
                  f"measuring it, so treating it as unknown)")
        ranked = estimate_tempo_candidates(mid, notes)
        rhythm_tempo = ranked[0][0] if ranked else None
        if rhythm_tempo is not None:
            print(f"(estimated from note-onset rhythm: {rhythm_tempo} BPM -- this kind of estimate can "
                  f"land on exactly half or double the real tempo, so double-check it sounds right)")
            ambiguity = describe_tempo_ambiguity(ranked)
            if ambiguity:
                print(f"({ambiguity})")
            tempo_default = rhythm_tempo
        else:
            print("(not enough onsets to estimate a tempo -- using 120 BPM)")
            tempo_default = 120.0
    tempo = _prompt("Tempo (BPM)", default=tempo_default, cast=float)

    preserve_source_duration = True
    if tempo != tempo_default:
        print("You entered a tempo different from the detected/estimated one. This can mean two "
              "different things:")
        print("  preserve-duration: ticks get rescaled so the piece's real-world length still "
              "matches the original audio -- use this if you're correcting a wrong/unreliable "
              "tempo detection.")
        print("  change-speed: ticks stay as-is, so this tempo genuinely changes playback speed -- "
              "use this if you deliberately want the output to play faster or slower.")

        def valid_rescale_mode(s):
            if s.strip().lower() in ('preserve-duration', 'change-speed'):
                return True, None
            return False, "Must be 'preserve-duration' or 'change-speed'."
        rescale_mode = _prompt("Tempo change intent (preserve-duration/change-speed)",
                                default='preserve-duration', validate=valid_rescale_mode)
        preserve_source_duration = (rescale_mode.strip().lower() == 'preserve-duration')

    def valid_time_sig(s):
        try:
            parse_time_sig(s)
        except ValueError as e:
            return False, str(e)
        return True, None
    detected_sig, sig_is_generic = detect_source_time_sig(mid)
    if detected_sig is not None and not sig_is_generic:
        print(f"(detected time signature in source file: {detected_sig[0]}/{detected_sig[1]})")
        sig_default = f"{detected_sig[0]}/{detected_sig[1]}"
    else:
        if detected_sig is not None and sig_is_generic:
            print(f"(source file time signature ({detected_sig[0]}/{detected_sig[1]}) is byte-identical "
                  f"to the untouched MIDI spec default -- most transcription tools stamp this "
                  f"automatically without actually detecting it, so treating it as unknown)")
        print(f"(defaulting to {DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]}; there's no reliable way to "
              f"guess this from note timing alone -- please check the real time signature yourself if "
              f"you're not sure it's {DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]})")
        sig_default = f"{DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]}"
    time_sig_str = _prompt("Time signature (N/D)", default=sig_default, validate=valid_time_sig)
    time_sig = parse_time_sig(time_sig_str)

    def valid_pitch(p):
        if 0 <= p <= 127:
            return True, None
        return False, "MIDI pitch must be 0-127 (60 = middle C)."
    estimated_split = estimate_split_pitch(notes)
    if estimated_split != SPLIT_PITCH:
        print(f"(estimated natural treble/bass split from pitch distribution: {estimated_split})")
    split_pitch = _prompt("Staff split pitch (MIDI note number, 60 = middle C)",
                           default=estimated_split, cast=int, validate=valid_pitch)

    def valid_profile(p):
        if p in ('readable', 'balanced', 'faithful', 'custom'):
            return True, None
        return False, "Must be 'readable', 'balanced', 'faithful', or 'custom'."
    print("\nEngraving profile: 'readable' favors fewest ties/simplest rhythms (best")
    print("for sight-reading or practice). 'balanced' is the benchmark-tested sweet")
    print("spot between readability and fidelity. 'faithful' sticks closest to the")
    print("original performed timing. 'custom' sets tie-temperature and the")
    print("pedal/grid/duration flags below individually.")
    profile = _prompt("Engraving profile (readable/balanced/faithful/custom)",
                       default='balanced', validate=valid_profile)

    if profile != 'custom':
        temperature = PROFILES[profile]['tie_temperature']
        print(f"  -> tie-temperature={temperature}")
    else:
        def valid_temp(t):
            if 0.0 <= t <= 1.0:
                return True, None
            return False, "Must be between 0.0 and 1.0."
        print("\nTie temperature: 0.0 = fewest ties, most rests (readable, less exact).")
        print("                  1.0 = closest fidelity to original timing, more ties.")
        temperature = _prompt("Tie temperature (0.0-1.0)", default=0.0, cast=float, validate=valid_temp)

    print("\nPlayback sustain: keeps the written notation exactly as clean as the tie")
    print("temperature above produces, but adds sustain-pedal automation so MIDI")
    print("playback still rings notes out to their real length instead of sounding")
    print("choppy. Doesn't affect what MuseScore displays -- only how it sounds.")
    playback_sustain = _prompt_bool("Add playback sustain pedal automation?", default=True)

    pedal_mode = PROFILES[profile]['pedal_mode'] if profile != 'custom' else 'ignore'
    min_note_ticks = None
    grid_mode = PROFILES[profile]['grid'] if profile != 'custom' else 'straight'
    duration_style = PROFILES[profile]['clean_durations'] if profile != 'custom' else 'dotted'
    min_velocity = 0
    velocity_mode = 'passthrough'
    velocity_scale = 1.0
    tie_weight = rest_weight = artic_weight = None
    print()
    adv_prompt = "Show advanced options? (noise filtering, velocity"
    adv_prompt += ")" if profile != 'custom' else ", sustain pedal handling, triplet/swing grid, duration style)"
    if _prompt_bool(adv_prompt, default=False):
        if profile == 'custom':
            print("\nSustain pedal: 'ignore' drops pedal data entirely (default).")
            print("               'reflect' extends a note's length to the pedal-up point")
            print("               if its release happens while the pedal is still held --")
            print("               a more musically honest sustain length.")

            def valid_pedal(p):
                if p in ('ignore', 'reflect'):
                    return True, None
                return False, "Must be 'ignore' or 'reflect'."
            pedal_mode = _prompt("Pedal mode (ignore/reflect)", default='ignore', validate=valid_pedal)

            print("\nQuantization grid: 'straight' (default) only hits straight 16th-note")
            print("                    subdivisions -- a genuinely triplet/swung passage gets")
            print("                    forced onto the nearest straight 16th, distorting it.")
            print("                    'triplet' uses a finer grid that natively fits both")
            print("                    straight and triplet-eighth subdivisions.")

            def valid_grid(g):
                if g in ('straight', 'triplet'):
                    return True, None
                return False, "Must be 'straight' or 'triplet'."
            grid_mode = _prompt("Quantization grid (straight/triplet)", default='straight',
                                 validate=valid_grid)

            print("\nDuration style: 'dotted' (default) allows single noteheads with dots")
            print("                 (dotted-quarter, etc). 'powers2' restricts to plain")
            print("                 power-of-two values only -- a plainer look, at the cost")
            print("                 of needing a tie wherever a dot would've done the job.")

            def valid_duration_style(d):
                if d in ('dotted', 'powers2'):
                    return True, None
                return False, "Must be 'dotted' or 'powers2'."
            duration_style = _prompt("Duration style (dotted/powers2)", default='dotted',
                                      validate=valid_duration_style)
        configure_grid(grid_mode, duration_style)

        auto_min = max(1, GRID // 4)
        print(f"\nMinimum note length: raw notes shorter than this (in ticks, before "
              f"quantization) are dropped as likely transcription noise.")

        def valid_min_ticks(v):
            if v >= 0:
                return True, None
            return False, "Must be 0 or greater."
        min_note_ticks = _prompt("Minimum note length in ticks", default=auto_min,
                                  cast=int, validate=valid_min_ticks)

        print("\nMinimum velocity: drop notes quieter than this as likely ghost notes.")
        print("                   0 = off (default) -- quiet-but-intentional notes are")
        print("                   legitimate, so this isn't auto-enabled.")

        def valid_min_vel(v):
            if 0 <= v <= 127:
                return True, None
            return False, "Must be 0-127."
        min_velocity = _prompt("Minimum velocity (0-127)", default=0, cast=int,
                                validate=valid_min_vel)

        print("\nVelocity mode: 'passthrough' (default) leaves velocities untouched.")
        print("                'scale' multiplies every velocity by a factor, preserving")
        print("                the performance's relative dynamics. 'normalize' remaps")
        print("                the piece's own velocity range onto a standard 30-110")
        print("                range (computed after the minimum-velocity filter above).")

        def valid_velocity_mode(v):
            if v in ('passthrough', 'normalize', 'scale'):
                return True, None
            return False, "Must be 'passthrough', 'normalize', or 'scale'."
        velocity_mode = _prompt("Velocity mode (passthrough/normalize/scale)",
                                 default='passthrough', validate=valid_velocity_mode)
        if velocity_mode == 'scale':
            def valid_scale(s):
                if s > 0:
                    return True, None
                return False, "Must be greater than 0."
            velocity_scale = _prompt("Velocity scale factor (e.g. 0.8 softer, 1.3 stronger)",
                                      default=1.0, cast=float, validate=valid_scale)

        default_tie_w, default_rest_w, default_artic_w = optimizer_weights(temperature)
        print(f"\nDuration optimizer weights: every note's written length is chosen to "
              f"minimize a cost of (ties + rests + invented sustain). --tie-temperature above "
              f"already sets sensible values for these ({default_tie_w:.2f} / "
              f"{default_rest_w:.2f} / {default_artic_w:.2f}) -- only override if you want to "
              f"tune the tradeoff directly.")
        if _prompt_bool("Override the optimizer weights individually?", default=False):
            tie_weight = _prompt("Tie weight (cost per extra tied notehead)",
                                  default=default_tie_w, cast=float)
            rest_weight = _prompt("Rest weight (cost of leaving a visible rest)",
                                   default=default_rest_w, cast=float)
            artic_weight = _prompt("Articulation weight (cost per grid unit of invented sustain)",
                                    default=default_artic_w, cast=float)
        else:
            tie_weight = rest_weight = artic_weight = None

        print("\nMelody preservation [experimental]: biases the duration optimizer's")
        print("weights per note using a heuristic melody/accompaniment classifier --")
        print("melody gets cheaper ties and costlier rests (protect its continuity),")
        print("accompaniment gets the opposite (declutter more freely). Check the")
        print("report's Voice Roles section before trusting this on a given piece.")
        melody_preservation = _prompt_bool("Enable melody preservation?", default=False)

        print("\nDynamic split: instead of one fixed treble/bass split pitch for the")
        print("whole piece, re-estimate it every N bars so it follows the music's")
        print("register drifting over time (e.g. a verse sitting lower than a chorus).")
        dynamic_split = _prompt_bool("Enable dynamic split?", default=False)
        split_window_bars = 8
        if dynamic_split:
            def valid_window(n):
                return (n > 0, "Must be a positive number of bars.")
            split_window_bars = _prompt("Window size in bars", default=8, cast=int,
                                         validate=valid_window)

        print("\nHand assignment [experimental]: within each chord, reconsider notes")
        print("sitting close to the split point -- a note moves to the other hand if")
        print("it's actually closer to that hand's recent position (e.g. \"the left")
        print("hand is already busy an octave lower\") and doing so doesn't stretch")
        print("either hand past its span limit. Notes not near the boundary are")
        print("never touched; this only refines the split above, not replace it.")
        hand_assignment = _prompt_bool("Enable hand assignment?", default=False)
        max_hand_span, hand_ambiguity_zone = 16, 3
        if hand_assignment:
            def valid_nonneg(n):
                return (n >= 0, "Must be zero or a positive number of semitones.")
            max_hand_span = _prompt("Max hand span in semitones (16 = a 10th)",
                                     default=16, cast=int, validate=valid_nonneg)
            hand_ambiguity_zone = _prompt("Ambiguity zone in semitones (how close to the "
                                           "split point counts as reconsiderable; 0 disables "
                                           "reassignment)",
                                           default=3, cast=int, validate=valid_nonneg)
    else:
        melody_preservation = False
        dynamic_split = False
        split_window_bars = 8
        hand_assignment = False
        max_hand_span, hand_ambiguity_zone = 16, 3

    print()
    run(input_path, output_path, tempo, split_pitch, temperature, time_sig,
        pedal_mode, min_note_ticks, playback_sustain, grid_mode,
        ','.join(map(str, track_indices)), channel_override,
        duration_style, min_velocity, velocity_mode, velocity_scale,
        tie_weight, rest_weight, artic_weight, preserve_source_duration,
        melody_preservation, dynamic_split, split_window_bars,
        hand_assignment, max_hand_span, hand_ambiguity_zone)


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--profile', choices=list(PROFILES), default=None)
    pre_args, _ = pre.parse_known_args()

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--profile', choices=list(PROFILES), default=None,
                     help='Set --tie-temperature, --pedal-mode, --grid, and --clean-durations '
                          'all at once. "readable": fewest ties, simplest rhythms -- best for '
                          'sight-reading/practice. "balanced": the benchmark-tested sweet spot '
                          'between readability and fidelity. "faithful": closest to the original '
                          'performed timing, pedal reflected. Any of the four flags also given '
                          'explicitly still overrides the profile\'s value for that flag.')
    ap.add_argument('input', nargs='?', default=None,
                     help='source MIDI file (raw transcription). Omit both input and '
                          'output, or pass --interactive, to be prompted step by step instead.')
    ap.add_argument('output', nargs='?', default=None,
                     help='where to write the cleaned grand-staff MIDI')
    ap.add_argument('--interactive', action='store_true',
                     help='force the interactive prompt mode even if input/output are given')
    ap.add_argument('--tempo', type=float, default=None,
                     help='output tempo in BPM (default: read from the source file\'s own '
                          'tempo if present; otherwise estimated from note-onset rhythm; '
                          'otherwise 120)')
    ap.add_argument('--time-sig', type=str, default=None, metavar='N/D',
                     help='time signature, e.g. 3/4 or 6/8 (default: read from the source '
                          'file\'s own time signature if present, otherwise 4/4)')
    ap.add_argument('--split-pitch', type=int, default=None,
                     help='MIDI pitch >= this goes to treble, below goes to bass '
                          '(default: estimated from the pitch distribution, otherwise 60/middle C)')
    ap.add_argument('--tie-temperature', type=float, default=0.0, metavar='0.0-1.0',
                     help='0.0 = fewest ties, most rests, single-bar/single-notehead durations '
                          '(default). 1.0 = closest fidelity to the original performed timing, '
                          'ties wherever the source needs them, chords allowed to fracture. '
                          'Values in between scale the tie budget, max bar-span, and how much '
                          'chord-duration disagreement is tolerated before forcing a shared '
                          'value, all linearly.')
    ap.add_argument('--playback-sustain', choices=['on', 'off'], default='on',
                     help='"on" (default): keep the written notation exactly as clean as '
                          '--tie-temperature produces, but add sustain-pedal automation so MIDI '
                          'playback still rings notes out to their real length instead of sounding '
                          'choppy. "off": playback matches the written notation exactly, which can '
                          'sound short/choppy for pieces with a lot of shortened notes.')
    ap.add_argument('--pedal-mode', choices=['ignore', 'reflect'], default='ignore',
                     help='[advanced] "ignore" (default): sustain pedal (CC64) data is not used. '
                          '"reflect": if a note\'s release happens while the pedal is still down, '
                          'extend its raw duration to the pedal-up point before quantizing -- a '
                          'more musically honest sustain length feeding into the same tie/rest logic.')
    ap.add_argument('--min-note-ticks', type=int, default=None,
                     help='[advanced] drop any note shorter than this many ticks (raw, before '
                          'quantization) as likely transcription noise (default: 24 ticks, a 64th '
                          'note -- clearly below any intended value)')
    ap.add_argument('--grid', choices=['straight', 'triplet'], default='straight',
                     help='[advanced] "straight" (default): quantize to straight 16th-note '
                          'subdivisions only. "triplet": quantize to a finer grid that natively '
                          'fits both straight and triplet-eighth subdivisions, for pieces with a '
                          'genuine triplet/swing feel that straight-16th quantization would '
                          'otherwise flatten out.')
    ap.add_argument('--track', type=str, default=None, metavar='N|N,M,...|all',
                     help='[advanced] use track N (0-indexed) as the note source instead of '
                          'auto-picking whichever track has the most note_on events. Also '
                          'accepts a comma-separated list (e.g. "1,2") to merge multiple tracks '
                          '-- useful for sources with separate right-hand/left-hand tracks -- or '
                          '"all" to merge every track. Run once without --track to see the '
                          'auto-pick and a listing of other tracks in the error message if '
                          'extraction finds nothing.')
    ap.add_argument('--channel', type=int, default=None, metavar='N',
                     help='[advanced] restrict the chosen track to MIDI channel N (0-15) only -- '
                          'useful if a single track merges multiple instruments\' channels '
                          'together. Default: use all channels on the track.')
    ap.add_argument('--clean-durations', choices=['dotted', 'powers2'], default='dotted',
                     help='[advanced] "dotted" (default): single noteheads may use dotted '
                          'values (dotted-8th, dotted-quarter, ...). "powers2": restrict to '
                          'plain power-of-two note values only (no dots) for a plainer, more '
                          'old-fashioned look -- anything that would need a dot instead gets a '
                          'tie.')
    ap.add_argument('--min-velocity', type=int, default=0, metavar='N',
                     help='[advanced] drop notes with velocity below N (0-127) as likely ghost '
                          'notes. Default: 0 (off) -- quiet-but-intentional notes are legitimate, '
                          'unlike very short notes, so this isn\'t auto-enabled the way '
                          '--min-note-ticks is.')
    ap.add_argument('--velocity-mode', choices=['passthrough', 'normalize', 'scale'],
                     default='passthrough',
                     help='[advanced] "passthrough" (default): leave velocities untouched. '
                          '"scale": multiply every velocity by --velocity-scale, preserving the '
                          'performance\'s relative dynamics. "normalize": remap the piece\'s own '
                          'observed velocity range onto a standard 30-110 range -- useful if a '
                          'transcription\'s velocity estimates are noisy or compressed, at the '
                          'cost of no longer being the source\'s literal values. Computed after '
                          '--min-velocity filtering, so dropped ghost notes don\'t skew the range.')
    ap.add_argument('--velocity-scale', type=float, default=1.0, metavar='X',
                     help='[advanced] multiplier used by --velocity-mode scale (e.g. 0.8 = '
                          'uniformly softer, 1.3 = uniformly more forceful). Ignored otherwise.')
    ap.add_argument('--tie-weight', type=float, default=None, metavar='X',
                     help='[advanced] override the duration optimizer\'s cost per extra tied '
                          'notehead (higher = more tie-averse). Default: derived from '
                          '--tie-temperature.')
    ap.add_argument('--rest-weight', type=float, default=None, metavar='X',
                     help='[advanced] override the duration optimizer\'s cost for leaving a '
                          'visible rest before the next note. Default: 1.0.')
    ap.add_argument('--articulation-weight', type=float, default=None, metavar='X',
                     help='[advanced] override the duration optimizer\'s cost per grid unit of '
                          'sustain invented beyond a note\'s real transcribed length (higher = '
                          'more faithful to real note-off timing and less willing to fabricate '
                          'legato to close a rest). Default: derived from --tie-temperature.')
    ap.add_argument('--tempo-rescale', choices=['preserve-duration', 'change-speed'],
                     default='preserve-duration',
                     help='Only matters if --tempo differs from the source file\'s own detected '
                          'tempo. "preserve-duration" (default): rescale note ticks so the '
                          'piece\'s real-world length still matches the original audio -- use this '
                          'if --tempo is correcting a wrong/unreliable detection. "change-speed": '
                          'leave ticks as-is, so --tempo genuinely changes playback speed.')
    ap.add_argument('--melody-preservation', choices=['on', 'off'], default='off',
                     help='[experimental] "off" (default): duration optimizer weights are the '
                          'same for every note. "on": bias weights per note using the heuristic '
                          'melody/accompaniment classifier -- melody gets cheaper ties and '
                          'costlier rests (protect its continuity), accompaniment gets the '
                          'opposite (declutter more freely). Built on a still-experimental '
                          'classifier; check the Voice Roles section of the report before '
                          'trusting this on a given piece.')
    ap.add_argument('--dynamic-split', choices=['on', 'off'], default='off',
                     help='"off" (default): one fixed --split-pitch for the whole piece. "on": '
                          're-estimate the treble/bass split every --split-window-bars bars '
                          'instead, so the split follows the music\'s register drifting over '
                          'time (e.g. a verse sitting lower than a chorus) rather than forcing '
                          'one compromise split for the entire piece. --split-pitch (given or '
                          'auto-estimated) is used as the starting fallback for windows too '
                          'sparse to estimate their own.')
    ap.add_argument('--split-window-bars', type=int, default=8, metavar='N',
                     help='Window size in bars for --dynamic-split. Default: 8. Ignored unless '
                          '--dynamic-split on.')
    ap.add_argument('--hand-assignment', choices=['on', 'off'], default='off',
                     help='[experimental] "off" (default): a note goes to treble/bass purely by '
                          'which side of the split pitch it falls on. "on": within each chord, '
                          'reconsider notes sitting close to the split point -- a note is moved '
                          'to the other hand if it\'s actually closer to that hand\'s recent '
                          'position (e.g. "the left hand is already busy an octave lower") and '
                          'doing so doesn\'t stretch either hand past --max-hand-span. Notes not '
                          'near the boundary are never touched. Composes with --split-pitch or '
                          '--dynamic-split, whichever is set -- this only refines their boundary, '
                          'it doesn\'t replace it.')
    ap.add_argument('--max-hand-span', type=int, default=16, metavar='SEMITONES',
                     help='Widest pitch span (in semitones) --hand-assignment will allow within '
                          'one hand\'s notes at a single onset. Default: 16 (a 10th). 0 means no '
                          'chord at all fits in one hand -- every onset gets flagged. Onsets that '
                          'exceed this even after reassignment are reported, not auto-fixed.')
    ap.add_argument('--hand-ambiguity-zone', type=int, default=3, metavar='SEMITONES',
                     help='How close (in semitones) to the split point a note has to be before '
                          '--hand-assignment will reconsider it. Default: 3.')
    if pre_args.profile:
        ap.set_defaults(**PROFILES[pre_args.profile])
    args = ap.parse_args()

    if args.interactive or args.input is None:
        try:
            interactive_mode()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            sys.exit(1)
        return

    if args.output is None:
        ap.error("output file is required when input is given (or omit both for interactive mode)")

    time_sig = None
    if args.time_sig is not None:
        try:
            time_sig = parse_time_sig(args.time_sig)
        except ValueError as e:
            ap.error(f"--time-sig: {e}")

    if args.split_window_bars <= 0:
        ap.error("--split-window-bars must be a positive number of bars")
    if args.max_hand_span < 0:
        ap.error("--max-hand-span must be zero or a positive number of semitones")
    if args.hand_ambiguity_zone < 0:
        ap.error("--hand-ambiguity-zone must be zero or a positive number of semitones")

    run(args.input, args.output, args.tempo, args.split_pitch, args.tie_temperature, time_sig,
        args.pedal_mode, args.min_note_ticks, args.playback_sustain == 'on', args.grid,
        args.track, args.channel, args.clean_durations, args.min_velocity,
        args.velocity_mode, args.velocity_scale,
        args.tie_weight, args.rest_weight, args.articulation_weight,
        args.tempo_rescale == 'preserve-duration', args.melody_preservation == 'on',
        args.dynamic_split == 'on', args.split_window_bars,
        args.hand_assignment == 'on', args.max_hand_span, args.hand_ambiguity_zone)


if __name__ == '__main__':
    main()
