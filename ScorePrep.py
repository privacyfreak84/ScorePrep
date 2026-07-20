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
    `units` grid-units starting at `onset`: minimal_tie_count's
    duration-value decomposition, PLUS one extra split for every barline
    the span crosses -- a single notehead can never be drawn straddling a
    barline no matter how "clean" its duration value is, and
    minimal_tie_count alone doesn't know where the barlines are."""
    end = onset + units * GRID
    bar_crossings = max(0, (end - 1) // bar_ticks - onset // bar_ticks)
    return minimal_tie_count(units) + bar_crossings


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


def optimize_staff_durations(notes, temperature, bar_ticks, weights=None):
    """The core engraving decision for one staff, run after
    resolve_note_durations has already established each note's hard
    ceiling (bar-span cap, tie-budget cap, 'nat_units' = real evidence).

    Replaces the old three-pass patch sequence (sync_chords ->
    fix_same_pitch_overlaps -> sync_chords again -> fill_small_gaps ->
    sync_chords again) with a single cost-minimizing choice per onset
    event (one note, or every note sharing that onset -- a chord).

    For every event, picks the ONE written duration -- applied to every
    member of the event, so chords share a duration by construction and
    can never fracture into mismatched tied fragments -- that minimizes:

        tie_weight   * (extra tied noteheads beyond the first)
      + rest_weight  * (1 if a rest remains before the next onset) * (event size)
      + artic_weight * (grid units of duration invented beyond each
                         member's own real, evidence-backed 'nat_units' --
                         i.e. how much unproven legato we'd be claiming)

    Candidates are still hard-bounded by the same tie_budget
    resolve_note_durations used (so "never need more ties than the chosen
    temperature allows" holds exactly as before) and can never extend
    into another note of the SAME pitch (preserves genuine cross-pitch
    polyphony within a staff -- a real sustained note under a moving
    line -- which was always left untouched on purpose; only actual
    same-pitch retriggers are a hard constraint). Mutates 'f_end' in
    place; leaves 'nat_units'/'natural_end' untouched so later passes
    (and playback-sustain) still see the true evidence.
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

        best_v, best_cost, best_fab = 1, None, None
        for v in range(1, ceiling + 1):
            tc = true_tie_count(onset, v, bar_ticks)
            if tc > tie_budget:
                continue
            rest_present = next_onset_units is not None and v < next_onset_units
            fabrication = sum(max(0, v - nat) for nat in member_nat_units)
            cost = (tie_w * (tc - 1)
                    + rest_w * (len(event) if rest_present else 0)
                    + artic_w * fabrication)
            if best_cost is None or cost < best_cost or (cost == best_cost and fabrication < best_fab):
                best_v, best_cost, best_fab = v, cost, fabrication

        new_end = onset + best_v * GRID
        for n in event:
            n['f_end'] = new_end


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


def report(name, notes, bar_ticks):
    tie_counts = [true_tie_count(n['f_start'], (n['f_end'] - n['f_start']) // GRID, bar_ticks)
                  for n in notes]
    needs_tie = sum(1 for c in tie_counts if c > 1)
    max_ties = max(tie_counts) if tie_counts else 0
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
    print(f"  {name}: {len(notes)} notes | needs-tie={needs_tie} (max chain={max_ties}) rests={rests} "
          f"cross-bar={cross_bar} chord-conflicts={conflicts} extended={extended} "
          f"({fab_units} {GRID_UNIT_NAME} invented)", file=sys.stderr)


def run(input_path, output_path, tempo, split_pitch, temperature, time_sig=None,
        pedal_mode='ignore', min_note_ticks=None, playback_sustain=True, grid_mode='straight',
        track_selector=None, channel_override=None, duration_style='dotted',
        min_velocity=0, velocity_mode='passthrough', velocity_scale=1.0,
        tie_weight=None, rest_weight=None, artic_weight=None):
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
        print(f"Rescaling note timing from the source file's tick-encoding tempo "
              f"({source_encoding_tempo} BPM -- the reference tempo used when the source's ticks "
              f"were generated, not a musical judgement) to the notated output tempo ({tempo} BPM), "
              f"so playback speed matches the original audio regardless of what tempo the score is "
              f"labeled with.", file=sys.stderr)
    rescale_notes_to_tempo(notes, source_encoding_tempo, tempo)

    resolve_note_durations(notes, temperature, bar_ticks)

    treble = [n for n in notes if n['pitch'] >= split_pitch]
    bass = [n for n in notes if n['pitch'] < split_pitch]

    tie_budget = tie_budget_for(temperature)
    weights = optimizer_weights(temperature, tie_weight, rest_weight, artic_weight)
    optimize_staff_durations(treble, temperature, bar_ticks, weights)
    optimize_staff_durations(bass, temperature, bar_ticks, weights)
    fix_same_pitch_overlaps(treble, tie_budget, bar_ticks)
    fix_same_pitch_overlaps(bass, tie_budget, bar_ticks)
    # fixing a same-pitch overlap can shorten just one member of a chord
    # the optimizer already made uniform -- re-harmonize once more to
    # close that gap. Since this second pass can only ever shorten notes
    # further (never lengthen), it can't reopen any overlap the previous
    # step just fixed, so one extra pass is sufficient.
    optimize_staff_durations(treble, temperature, bar_ticks, weights)
    optimize_staff_durations(bass, temperature, bar_ticks, weights)

    print(f"tie-temperature={temperature:.2f}  (max_bars={1 + round(temperature * 7)}, "
          f"tie_budget={tie_budget}, weights: tie={weights[0]:.2f} rest={weights[1]:.2f} "
          f"articulation={weights[2]:.2f})", file=sys.stderr)
    print(f"Processed {len(notes)} notes -> treble {len(treble)}, bass {len(bass)}", file=sys.stderr)
    report('TREBLE', treble, bar_ticks)
    report('BASS', bass, bar_ticks)

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

    pedal_mode = 'ignore'
    min_note_ticks = None
    grid_mode = 'straight'
    duration_style = 'dotted'
    min_velocity = 0
    velocity_mode = 'passthrough'
    velocity_scale = 1.0
    tie_weight = rest_weight = artic_weight = None
    print()
    if _prompt_bool("Show advanced options? (sustain pedal handling, noise filtering, "
                     "triplet/swing grid, duration style, velocity)", default=False):
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

    print()
    run(input_path, output_path, tempo, split_pitch, temperature, time_sig,
        pedal_mode, min_note_ticks, playback_sustain, grid_mode,
        ','.join(map(str, track_indices)), channel_override,
        duration_style, min_velocity, velocity_mode, velocity_scale,
        tie_weight, rest_weight, artic_weight)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
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

    run(args.input, args.output, args.tempo, args.split_pitch, args.tie_temperature, time_sig,
        args.pedal_mode, args.min_note_ticks, args.playback_sustain == 'on', args.grid,
        args.track, args.channel, args.clean_durations, args.min_velocity,
        args.velocity_mode, args.velocity_scale,
        args.tie_weight, args.rest_weight, args.articulation_weight)


if __name__ == '__main__':
    main()
