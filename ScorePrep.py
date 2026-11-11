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
import re
import sys
from collections import defaultdict
from fractions import Fraction
from math import gcd
from xml.sax.saxutils import escape as xml_escape

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

TICKS_PER_BEAT = 384          # internal grid resolution; sources at any other PPQ are rescaled to it in run()
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
POWERS_OF_TWO = (1, 2, 4, 8, 16, 32, 64)     # 16th, 8th, quarter, half, whole, breve, 4 whole notes
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
GRID_SCALE = GRID_MODES['straight']['scale']


def configure_grid(mode, duration_style='dotted'):
    """Switch the module-level GRID/CLEAN used by every quantization step
    (quantize, resolve_note_durations, optimize_staff_durations,
    minimal_tie_count, ...). mode: 'straight' or
    'triplet'. duration_style: 'dotted' (default, includes dotted values)
    or 'powers2' (plain power-of-two note values only). Must be called
    before any of those run."""
    global GRID, CLEAN, GRID_UNIT_NAME, GRID_SCALE
    if mode not in GRID_MODES:
        raise ValueError(f"Unknown grid mode: {mode!r} (expected one of {list(GRID_MODES)})")
    if duration_style not in ('dotted', 'powers2'):
        raise ValueError(f"Unknown duration_style: {duration_style!r} (expected 'dotted' or 'powers2')")
    GRID = GRID_MODES[mode]['grid']
    CLEAN = _clean_for(mode, duration_style)
    GRID_UNIT_NAME = GRID_MODES[mode]['unit_name']
    GRID_SCALE = GRID_MODES[mode]['scale']
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
    # Pair every note_off with the OLDEST unfinished note_on of its pitch.
    # Transcriptions of legato repeated notes often overlap them: the new
    # note_on arrives a few ms before the previous note's note_off. Pairing
    # that late note_off with the newest note_on instead (as this used to)
    # gives the new note a length of a few ms -- then dropped as noise --
    # which is how the first note of a quick figure on a repeated pitch
    # went missing. A piano key can't sound twice at once, so after
    # pairing, a note still sounding at its pitch's next onset is ended
    # there.
    abs_t = 0
    open_notes = defaultdict(list)       # pitch -> [index into notes], oldest first
    notes = []
    for msg in track:
        abs_t += msg.time
        is_note_msg = msg.type in ('note_on', 'note_off')
        if is_note_msg and channel is not None and msg.channel != channel:
            continue
        if msg.type == 'note_on' and msg.velocity > 0:
            open_notes[msg.note].append(len(notes))
            notes.append({'start': abs_t, 'end': None, 'pitch': msg.note, 'vel': msg.velocity})
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            if open_notes[msg.note]:
                notes[open_notes[msg.note].pop(0)]['end'] = abs_t
    last_onset = {}
    for n in reversed(notes):
        nxt = last_onset.get(n['pitch'])
        if nxt is not None and (n['end'] is None or n['end'] > nxt):
            n['end'] = nxt               # re-struck before it was released (or never released)
        last_onset[n['pitch']] = n['start']
    return [n for n in notes if n['end'] is not None and n['end'] > n['start']]


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
    so any later step that re-shortens a note can re-snap to a value
    within the same budget instead of accidentally producing a duration
    that needs more ties than the chosen temperature allows."""
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


TUPLET_CANDIDATES = (5, 7, 9, 10, 11)
TUPLET_SPANS_BEATS = (1, 2)
# Fixed absolute-tick tolerance, NOT a fraction of span/N: an earlier
# version used avg_error <= FRACTION * (span/N), but that gives smaller N
# strictly more absolute tick slack than larger N for the same span
# (span/N is bigger when N is smaller) -- structurally biasing detection
# toward quintuplets regardless of what's actually in the piece, borne
# out empirically (quintuplets dominated every N-distribution tested,
# even on pieces with no independent evidence of being tuplet-heavy). A
# fixed tick budget applies the same absolute bar to every candidate N.
# 24 ticks (~a 64th note) was chosen from real data: at this tolerance,
# a piece independently known to be tuplet-dense (confirmed via its own
# hand-notated score showing tuplet brackets in nearly every bar) shows
# 5-12x more detected groups than two pieces with no such evidence --
# the strongest calibration signal available without ground-truth
# tuplet annotations for every benchmark piece.
TUPLET_FIT_TOLERANCE_TICKS = 24  # only used when fit_mode='absolute'

# The design doc for this feature originally specified the fit tolerance
# as a PERCENTAGE of the subdivision width (15% of one ideal sub-division),
# not a flat tick count -- but what got implemented was the flat
# TUPLET_FIT_TOLERANCE_TICKS above, applied the same regardless of how
# tightly-packed the group is. Found via a real case: a 1-beat quintuplet
# (subdivision width 76.8 ticks) had one member land 26 ticks from its
# ideal position -- a big miss relative to how tight the group is -- but
# the group's *average* error (15.4) still cleared the flat 24-tick bar,
# so the whole group got force-evened anyway, overriding that member's
# real recorded timing. 15% of 76.8 is only ~11.5 -- correctly stricter
# for a tightly-packed group like this one. A flat 24 happens to roughly
# match 15% for a 2-beat quintuplet (subdivision ~154 ticks), which is
# likely what it was originally calibrated against -- fine there, too
# lenient for smaller spans/higher N. 'relative' (this constant) is now
# the default; 'absolute' (the original flat constant above) is kept
# available for anyone who wants the older, more lenient behavior.
TUPLET_FIT_TOLERANCE_PCT = 0.15

# How far past the accept threshold a failing tuplet-candidate fit may be
# and still be treated as a genuine "near miss" for
# resolve_rejected_tuplet_collisions, rather than an ordinary passage that
# coincidentally has n_cand onsets in its span with no real tuplet
# relationship. See that function and TUPLET_FIT_TOLERANCE_PCT's comment
# for the motivating case.
NEAR_MISS_TOLERANCE_MULT = 2.0

# Max semitone gap allowed between consecutive members of a candidate
# sub-grid cluster (see detect_subgrid_clusters below). Real ornamental
# figures found in practice (mordents/turns: e.g. a whole-step-and-back)
# stay within a handful of semitones; the one false-positive case found
# during investigation (a melody note and an unrelated bass note landing
# on the same coarse tick purely by coincidence) was 33 semitones apart.
# 12 (one octave) comfortably separates the two without hand-assignment
# info, which isn't available this early in the pipeline.
SUBGRID_MAX_PITCH_GAP = 12

# Only notes shorter than one grid unit are eligible for sub-grid
# clustering at all. Found necessary empirically: without it, ordinary
# long-held chords with a few ticks of natural strike jitter between
# their notes (durations in the hundreds/thousands of ticks -- nothing
# like a real ornament) got caught by the pitch/time chain, because that
# chain only checks ADJACENT pairs -- a whole chord can sneak through as
# a "connected" chain even though its full span is wide, if each
# consecutive step happens to be small enough. Real ornaments found in
# practice are all well under one grid unit long (22-57 ticks on a
# 96-tick grid); a long-held note is never part of one regardless of how
# close its onset lands to a neighbor's.
SUBGRID_MAX_NOTE_DURATION_FACTOR = 1.0  # x GRID


def detect_tuplet_groups(notes, fit_mode='relative'):
    """Find local n-tuplet bursts (5/7/9/10/11-against-1-or-2-beats)
    directly in the raw, unquantized tick stream -- before any grid
    quantization or treble/bass split, since a real tuplet run can
    straddle both hands and either step could otherwise sever a group
    before it's ever recognized. See docs/tuplet-detection-design.md.

    Deliberately excludes N in {2,3,4,8}: straight/triplet grids already
    represent those natively and shouldn't be second-guessed here.

    A genuine hit gets its member onsets snapped to *exact* even
    division (ideal_tick = start + round(k * span / N)) instead of each
    one rounding independently to the outer grid -- that's specifically
    what fixes the uneven-duration artifact independent per-note
    grid-snapping produces on real tuplet material. Tags each accepted
    member with '_tuplet'=True and pre-fills 'f_start'/'f_end' with its
    exact evenly-divided tick span; resolve_note_durations and
    optimize_staff_durations both skip their normal cap/cost-search
    logic for a tagged note and leave these values untouched -- a
    tuplet member's duration is "its even share of the group", not a
    cost-optimal or evidence-capped choice.

    Requires an EXACT onset-count match against a candidate window (no
    partial-fill tolerance) and a strict fit-error threshold, so
    coincidental fast-but-not-tuplet clustering (grace-note runs,
    trills, glissandi) falls through untouched to normal quantization --
    a wrong tuplet call reads worse than no tuplet call at all.
    fit_mode='relative' (default): threshold scales with the group's own
    subdivision width (TUPLET_FIT_TOLERANCE_PCT), so a tightly-packed
    group (small span, high N) is held to a proportionally stricter
    standard than a loosely-packed one. fit_mode='absolute': the older
    flat TUPLET_FIT_TOLERANCE_TICKS regardless of subdivision width --
    kept available for anyone who wants the more lenient original
    behavior. See TUPLET_FIT_TOLERANCE_PCT's comment for why relative is
    now the default. Greedy, left-to-right, non-overlapping: a later,
    better-fitting grouping starting inside an already-accepted one is
    never reconsidered (acceptable v1 heuristic, see design doc).

    Mutates notes in place. Returns (group_count, rejected_windows):
    group_count is the number of accepted groups (for the run() log line);
    rejected_windows is a list of onset-tick lists, one per position where
    an exact-onset-count candidate was found but failed the fit-tolerance
    check -- consumed by resolve_rejected_tuplet_collisions to catch the
    real grid-tick collisions a correctly-rejected window can still leave
    behind. Members of a rejected window are NOT added to `used` and
    remain eligible for a later, smaller window starting at the same
    position, or for detect_subgrid_clusters, exactly as before this
    return value was added -- only rejected_windows' own bookkeeping
    (near_miss_used) dedupes so the same burst isn't recorded once per
    onset inside it."""
    if not notes:
        return 0, []

    by_onset = defaultdict(list)
    for n in notes:
        by_onset[n['start']].append(n)
    starts = sorted(by_onset)

    used = set()
    near_miss_used = set()  # separate from `used`: only dedupes which
    # near-miss windows get recorded for resolve_rejected_tuplet_collisions
    # below, so overlapping/nested near-misses spawned by every onset
    # inside an already-recorded burst aren't each recorded as their own
    # window (that would process the same notes' collisions repeatedly
    # and compound). Deliberately NOT merged into `used` itself, so a
    # smaller valid tuplet nested inside a rejected span can still be
    # found normally, exactly as before this dedup was added.
    group_count = 0
    rejected_windows = []
    for i, start in enumerate(starts):
        if start in used:
            continue
        best = None  # (avg_error, n_cand, group_starts)
        # Also track the best-but-failing exact-onset-count candidate at
        # this position (same size/error tie-break preference as `best`),
        # so a caller can later check whether a rejected window still
        # produces a real grid-tick collision -- see
        # resolve_rejected_tuplet_collisions.
        near_miss = None
        # Prefer whichever passing candidate covers the MOST onsets, not
        # whichever happens to have the lowest average error -- a tight
        # partial match (e.g. 5 of a real 9-onset burst) must not win over
        # a looser-but-still-valid full match, or the leftover onsets are
        # stranded outside any group and fall back to plain grid-snapping.
        # Average error only breaks ties between equally-sized matches.
        for span_beats in TUPLET_SPANS_BEATS:
            span = span_beats * TICKS_PER_BEAT
            window_end = start + span
            window_starts = [s for s in starts[i:] if s < window_end and s not in used]
            for n_cand in TUPLET_CANDIDATES:
                if len(window_starts) != n_cand:
                    continue
                ideal = [start + round(k * span / n_cand) for k in range(n_cand)]
                avg_error = sum(abs(a - b) for a, b in zip(window_starts, ideal)) / n_cand
                threshold = (TUPLET_FIT_TOLERANCE_PCT * (span / n_cand) if fit_mode == 'relative'
                             else TUPLET_FIT_TOLERANCE_TICKS)
                if avg_error > threshold:
                    # Only a window that FAILED NARROWLY is a real near-miss
                    # worth checking for collision-repair -- an ordinary
                    # passage that coincidentally has n_cand onsets in the
                    # span (common: extra passing/ornament notes bump an
                    # otherwise-straight run's count up to 9 or 10) can miss
                    # by hundreds of ticks and has no tuplet relationship to
                    # repair. NEAR_MISS_TOLERANCE_MULT bounds how far past
                    # the accept threshold a failing fit can be and still
                    # count -- calibrated from the one real case motivating
                    # this (RiverFlowInYou tick ~6114: avg error 22 against
                    # a ~11.5 threshold, ~1.9x) with headroom to 2x.
                    if (avg_error <= NEAR_MISS_TOLERANCE_MULT * threshold
                            and (near_miss is None or n_cand > near_miss[1]
                                 or (n_cand == near_miss[1] and avg_error < near_miss[0]))):
                        near_miss = (avg_error, n_cand, span, window_starts)
                    continue
                if best is None or n_cand > best[1] or (n_cand == best[1] and avg_error < best[0]):
                    best = (avg_error, n_cand, span, window_starts)
        if best is not None:
            _, n_cand, span, group_starts = best
            group_start = group_starts[0]
            ideal_ticks = [group_start + round(k * span / n_cand) for k in range(n_cand + 1)]
            for k, s in enumerate(group_starts):
                for n in by_onset[s]:
                    n['_tuplet'] = True
                    n['_tuplet_start'] = ideal_ticks[k]
                    n['_tuplet_end'] = ideal_ticks[k + 1]
                    # (group start, span, N, slot index) -- not used for MIDI
                    # output; lets the MusicXML writer draw the bracket
                    n['_tuplet_group'] = (group_start, span, n_cand, k)
                used.add(s)
            group_count += 1
        elif near_miss is not None:
            nm_starts = near_miss[3]
            if not any(s in near_miss_used for s in nm_starts):
                rejected_windows.append(nm_starts)
                near_miss_used.update(nm_starts)
    return group_count, rejected_windows


def detect_subgrid_clusters(notes):
    """Catch small (2-3 note) clusters that would collide onto the same
    coarse grid tick because their raw spacing is tighter than one grid
    unit -- e.g. a quick melodic turn/mordent squeezed into less time
    than a single 16th note. Run this AFTER detect_tuplet_groups (skips
    anything already tuplet-tagged) and before generic quantization.

    Deliberately narrower than detect_tuplet_groups in one specific way:
    N=2/3 are exactly what that function excludes, because at full-beat
    scale almost any ordinary rhythm trivially has 2-3 onsets in a beat
    window, and evenly dividing those would destroy real, intentional
    non-uniform rhythms (a dotted-8th-plus-16th, say). That risk doesn't
    apply here because the trigger condition is completely different:
    this only fires when the outer grid would ACTUALLY collide two
    onsets onto one tick, which normal-speed material never does
    regardless of N -- it's the collision itself that's the safe,
    narrow signal, not the onset count.

    Still needs one more guard the beat-scale search doesn't: at this
    point in the pipeline hands haven't been split or assigned yet, so
    without it, a melody note and an unrelated bass note that happen to
    land on the same coarse tick purely by coincidence would be treated
    as one figure and have their real timing perturbed to satisfy each
    other. Gated with SUBGRID_MAX_PITCH_GAP -- real ornaments discovered
    in practice move within a handful of semitones; the one coincidental
    cross-hand case found during investigation was 33 semitones apart.

    A qualifying cluster's onsets are spread evenly across the ONE outer
    grid slot they'd otherwise collide into -- same tagging as a tuplet
    (_tuplet/_tuplet_start/_tuplet_end), so every downstream step that
    already trusts a tuplet-tagged onset (duration search, hand
    assignment, tie logic) needs no separate handling for this case.

    Also requires every candidate note to be shorter than one grid unit
    (SUBGRID_MAX_NOTE_DURATION_FACTOR) -- see that constant's comment.
    A long-held note sharing a nearby tick with a short one (e.g. an
    ordinary chord with a few ticks of natural strike jitter between its
    notes) is simply left out of consideration; only the short note(s),
    if any, are eligible.

    Mutates notes in place. Returns the number of clusters found."""
    if not notes:
        return 0

    max_dur = GRID * SUBGRID_MAX_NOTE_DURATION_FACTOR
    by_onset = defaultdict(list)
    for n in notes:
        if not n.get('_tuplet') and (n['end'] - n['start']) < max_dur:
            by_onset[n['start']].append(n)
    starts = sorted(by_onset)

    count = 0
    i = 0
    while i < len(starts):
        cluster = [starts[i]]
        j = i + 1
        while j < len(starts) and len(cluster) < 3:
            if starts[j] - cluster[-1] >= GRID:
                break
            prev_pitches = [n['pitch'] for n in by_onset[cluster[-1]]]
            cand_pitches = [n['pitch'] for n in by_onset[starts[j]]]
            if min(abs(a - b) for a in prev_pitches for b in cand_pitches) > SUBGRID_MAX_PITCH_GAP:
                break
            cluster.append(starts[j])
            j += 1

        # The longest time/pitch-chained candidate doesn't always actually
        # collide as a whole (e.g. its first 2 members round to the same
        # grid tick but its 3rd rounds to the next one) -- try shrinking
        # before giving up, so that real 2-member collision isn't missed
        # just because it happened to chain onto a 3rd non-colliding note.
        matched = False
        for size in range(len(cluster), 1, -1):
            sub = cluster[:size]
            anchor = round(sub[0] / GRID) * GRID
            if all(round(t / GRID) * GRID == anchor for t in sub):
                n_cand = size
                ideal = [anchor + round(k * GRID / n_cand) for k in range(n_cand + 1)]
                for k, t in enumerate(sub):
                    for n in by_onset[t]:
                        n['_tuplet'] = True
                        n['_tuplet_start'] = ideal[k]
                        n['_tuplet_end'] = ideal[k + 1]
                        n['_tuplet_group'] = (anchor, GRID, n_cand, k)
                count += 1
                i += size
                matched = True
                break
        if not matched:
            i += 1
    return count


def place_half_grid_notes(notes):
    """Stop fast notes from merging. The grid is 16ths, so a 32nd-note
    figure -- a quick note and the one after it half a 16th later --
    rounds onto ONE tick: two different pitches come out as a chord, and a
    repeated pitch loses a note outright. Here every pair of onsets that
    would collide like that is looked at on a grid twice as fine, and the
    note that belongs on the half-way point is written there as a 32nd,
    along with the short note (if any) filling the first half of the same
    16th.

    Three guards, the same ones detect_subgrid_clusters needed, for the
    same reason (ordinary chords whose notes are struck a few ticks apart
    must stay chords): the two onsets are at least a 64th apart, within an
    octave of each other, and either one of the two notes is shorter than
    a 16th or the two are a step apart (nobody rolls a chord of seconds;
    a held-over note a step from the next one is melody). Notes already placed by tuplet or sub-grid detection are
    left alone.

    Tags moved notes like a sub-grid pair ('_tuplet', start/end, group), so
    every later step treats them the same way. Mutates notes in place;
    returns how many notes were placed on a half-way point."""
    half = GRID // 2
    if GRID % 2 or not notes:
        return 0
    by_onset = defaultdict(list)
    for n in notes:
        if not n.get('_tuplet'):
            by_onset[n['start']].append(n)
    starts = sorted(by_onset)

    def short(t):
        return any(n['end'] - n['start'] < GRID for n in by_onset[t])

    def near(a, b):
        return min(abs(x['pitch'] - y['pitch']) for x in by_onset[a] for y in by_onset[b]) <= SUBGRID_MAX_PITCH_GAP

    def stepwise(a, b):              # a half or whole step apart: a melodic move, never a rolled chord
        return min(abs(x['pitch'] - y['pitch']) for x in by_onset[a] for y in by_onset[b]) <= 2

    def fast_pair(a, b, min_gap):
        return b - a >= min_gap and near(a, b) and (short(a) or short(b) or stepwise(a, b))

    # Three onsets inside one 16th can't be told apart even on the finer
    # grid: write them as a triplet of 32nds across that 16th.
    moved = 0
    if GRID % 3 == 0:
        i = 0
        while i + 2 < len(starts):
            a, b, c = starts[i:i + 3]
            T = quantize(a)
            # all three inside the 16th that starts at T (the last may sit past
            # its middle and round forward), and the next onset beyond it
            nxt = starts[i + 3] if i + 3 < len(starts) else None
            if (a >= T - GRID // 4 and quantize(b) == T and c < T + GRID - GRID // 6
                    and (nxt is None or nxt >= T + GRID - GRID // 6)
                    # all three really are quick notes -- a long third note is
                    # where the figure LANDS (a touch early), not part of it
                    and all(n['end'] - n['start'] < GRID for t in (a, b, c) for n in by_onset[t])
                    and fast_pair(a, b, GRID // 6) and fast_pair(b, c, GRID // 6)):
                for k, t in enumerate((a, b, c)):
                    for n in by_onset[t]:
                        n['_tuplet'] = True
                        n['_tuplet_start'], n['_tuplet_end'] = T + k * GRID // 3, T + (k + 1) * GRID // 3
                        n['_tuplet_group'] = (T, GRID, 3, k)
                        moved += 1
                i += 3
            else:
                i += 1
        starts = [t for t in starts if not any(n.get('_tuplet') for n in by_onset[t])]

    placed = {}                      # onset tick -> half-grid tick it is moved to
    for a, b in zip(starts, starts[1:]):
        if quantize(a) != quantize(b) or b - a < GRID // 4 or not near(a, b):
            continue
        if not (short(a) or short(b) or stepwise(a, b)):
            continue
        if a in placed:
            continue                 # three onsets in one slot: the first pair already took it
        T = quantize(a)
        ha, hb = round(a / half) * half, round(b / half) * half
        if ha < hb:
            # the finer grid tells them apart by itself: each goes where it rounds to
            if ha != T:
                placed[a] = ha
            if hb != T:
                placed[b] = hb
        elif (a + b) / 2 >= T + half / 2 or not short(a):
            placed[b] = T + half     # both round to one point: split around the slot's middle...
        else:
            placed[a] = T - half     # ...or around its start, whichever the pair sits nearer
    for t, h in placed.items():
        slot = (h // GRID) * GRID
        for n in by_onset[t]:
            if n['end'] - n['start'] >= GRID and h < t:
                continue             # a long note isn't pulled back onto a 32nd
            n['_tuplet'] = True
            n['_tuplet_start'], n['_tuplet_end'] = h, h + half
            n['_tuplet_group'] = (slot, GRID, 2, 1)
            moved += 1
        # the short note filling the first half of that 16th becomes the other 32nd
        for s in starts:
            if s in placed or quantize(s) != slot:
                continue
            for n in by_onset[s]:
                # (a note of the same pitch always does, however long it was
                # held: its key is struck again half a 16th later)
                if not n.get('_tuplet') and (
                        any(n['pitch'] == m['pitch'] for m in by_onset[t])
                        or (n['end'] - n['start'] < GRID and any(
                            abs(n['pitch'] - m['pitch']) <= SUBGRID_MAX_PITCH_GAP for m in by_onset[t]))):
                    n['_tuplet'] = True
                    n['_tuplet_start'], n['_tuplet_end'] = slot, slot + half
                    n['_tuplet_group'] = (slot, GRID, 2, 0)
    return moved


def resolve_rejected_tuplet_collisions(notes, rejected_windows):
    """For each rejected tuplet-candidate window from detect_tuplet_groups
    (an exact onset-count match that failed the fit-tolerance check --
    see TUPLET_FIT_TOLERANCE_PCT), check whether any of its members would
    still land on the same outer-grid tick under plain independent
    quantize(). If so, push each colliding onset forward to the next free
    grid tick -- a local, minimal separation -- instead of letting it
    silently overlap the note before it.

    This exists specifically for the gap detect_subgrid_clusters can't
    cover: that function excludes any note >= one grid unit long
    (SUBGRID_MAX_NOTE_DURATION_FACTOR), correctly, since a long-held
    chord note isn't an ornament -- but that leaves a genuinely long,
    correctly-excluded held note free to collide with unrelated material
    once a candidate tuplet window containing it gets (correctly)
    rejected as not-actually-a-tuplet. Found via RiverFlowInYou.mid tick
    ~6114: loosening the accept/reject threshold enough to keep that
    group would also re-accept a genuinely bad group elsewhere (see
    TUPLET_FIT_TOLERANCE_PCT's comment) -- the threshold itself can't fix
    this, only a separate collision-repair step can.

    Deliberately NOT a blind scan for coincidental same-tick landings
    anywhere in the piece: only members of a window detect_tuplet_groups
    already evaluated and rejected are ever touched. That's what makes
    this safe against ordinary chords -- detect_tuplet_groups' by_onset
    grouping already guarantees a window's members are distinct onsets,
    never simultaneous chord notes, since a chord collapses to a single
    onset there before window-building ever happens.

    Adjusts 'start'/'end' in place, shifted by the same delta so each
    note's real recorded duration is preserved -- NOT 'f_start'/'f_end'
    directly, so the shift flows through resolve_note_durations' normal
    quantize()/tie-budget logic exactly like any other note. A nudged
    note is not tagged '_tuplet' and gets no special-casing later. Skips
    any window member already tagged '_tuplet' by a later
    detect_subgrid_clusters pass, since that note already has a
    resolved, trusted onset.

    A real quantize-tick collision only gets repaired if the colliding
    pair's pitches are within SUBGRID_MAX_PITCH_GAP -- the same gate
    detect_subgrid_clusters uses, and for the same reason: found via
    real data (control files kalank/sukoon) that most "collisions" a
    rejected window turns up are an ordinary bass note and a melody/chord
    note struck together across hands (e.g. pitch 46 vs 65, ~19
    semitones apart) that are SUPPOSED to land on the same tick --
    nudging those apart would actively damage the output, not fix it.
    A close pitch gap (same register, plausibly the same line) is the
    signal that two onsets are genuinely sequential melodic content that
    happens to collide, not an intentional simultaneity.

    A SECOND gate, also found via the same control-file check: pitch
    proximity alone still wasn't enough -- two long, sustained notes a
    few ticks apart (e.g. two ~1000-tick notes an octave apart, clearly
    a chord/doubling struck together with ordinary jitter) passed the
    pitch gate and were still wrongly getting nudged apart. This
    function's actual purpose (per the RiverFlowInYou tick ~6114 case
    that motivated it) is specifically a genuinely long HELD note
    colliding with an unrelated SHORT, fast-passage note -- so require
    at least one of the pair to be short (< SUBGRID_MAX_NOTE_DURATION_FACTOR
    x GRID, the same "short enough to be a fast/ornamental note" cutoff
    detect_subgrid_clusters already uses). Two long notes colliding is
    left alone -- that's ordinary simultaneity, not the target case.

    A collision that fails either gate is left exactly as plain
    quantization would leave it -- untouched, same as before this
    function existed.

    Mutates notes in place. Returns the number of notes nudged."""
    if not rejected_windows:
        return 0

    by_onset = defaultdict(list)
    for n in notes:
        by_onset[n['start']].append(n)
    max_short_dur = GRID * SUBGRID_MAX_NOTE_DURATION_FACTOR

    nudged = 0
    for window in rejected_windows:
        eligible = sorted(t for t in window if any(not n.get('_tuplet') for n in by_onset.get(t, [])))
        if len(eligible) < 2:
            continue
        prev_assigned = None
        prev_t = None
        for t in eligible:
            q = quantize(t)
            if prev_assigned is not None and q <= prev_assigned:
                prev_notes = by_onset.get(prev_t, [])
                cand_notes = by_onset[t]
                prev_pitches = [n['pitch'] for n in prev_notes]
                cand_pitches = [n['pitch'] for n in cand_notes]
                one_is_short = (any((n['end'] - n['start']) < max_short_dur for n in prev_notes)
                                 or any((n['end'] - n['start']) < max_short_dur for n in cand_notes))
                if (prev_pitches and cand_pitches and one_is_short
                        and min(abs(a - b) for a in prev_pitches for b in cand_pitches)
                        <= SUBGRID_MAX_PITCH_GAP):
                    q = prev_assigned + GRID
            if q != quantize(t):
                delta = q - t
                for n in by_onset[t]:
                    if n.get('_tuplet'):
                        continue
                    n.setdefault('_played_start', n['start'])   # detect_ornaments wants the real onset
                    n['start'] += delta
                    n['end'] += delta
                    nudged += 1
            prev_assigned = q
            prev_t = t
    return nudged


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
        if n.get('_tuplet'):
            # already evenly divided by detect_tuplet_groups -- skip the
            # normal grid-quantize/bar-cap/tie-budget logic entirely,
            # since imposing any of that here would just reintroduce the
            # unevenness detection was built to remove
            n['f_start'] = n['_tuplet_start']
            n['f_end'] = n['_tuplet_end']
            n['nat_units'] = max(1, round((n['_tuplet_end'] - n['_tuplet_start']) / GRID))
            n['natural_end'] = n['f_end']
            continue

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


# See the grouping comment in optimize_staff_durations. Counted on the test
# files before choosing: at 4x this splits 0-1 chords in RiverFlowInYou and
# 9-19 (of 121-210) in the two pedaled pieces; at 2x it would split 15-69.
HELD_SPLIT_RATIO = 4


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

    # Notes struck together share one written length (the shortest
    # member's), so a chord never fractures over small differences in
    # release. The exception is a note really HELD while its chord-mate is
    # only touched -- a melody note sustained four beats over a passing
    # 16th. Sharing a length there writes the held note as a 16th and a
    # bar of rests. So a member held HELD_SPLIT_RATIO times longer than
    # the shortest, and at least a beat longer, starts its own group and
    # keeps its own length. Notes already placed by tuplet / fast-note
    # detection keep the slot they were given.
    groups = []
    for onset, event in by_onset.items():
        members = sorted((n for n in event if not n.get('_tuplet')), key=lambda n: n['nat_units'])
        if not members:
            continue
        group = [members[0]]
        for n in members[1:]:
            if (n['nat_units'] >= HELD_SPLIT_RATIO * group[0]['nat_units']
                    and n['nat_units'] - group[0]['nat_units'] >= 4 * GRID_SCALE):
                groups.append((onset, group))
                group = [n]
            else:
                group.append(n)
        groups.append((onset, group))

    for onset, event in groups:

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
            # artic_w is calibrated per *sixteenth-equivalent* of invented
            # duration (see optimizer_weights docstring), but fabrication
            # is counted in raw grid units -- under --grid triplet those
            # are 3x finer than a sixteenth, so the same absolute stretch
            # would otherwise cost 3x more than intended, making the
            # optimizer far more reluctant to extend notes (more/shorter
            # notes, more rests) for no musical reason, purely because of
            # which grid happened to be selected. Dividing by GRID_SCALE
            # re-expresses it in sixteenth-equivalents so temperature
            # means the same thing in either grid mode.
            cost = (eff_tie_w * max(0, tc - baseline_tc)
                    + eff_rest_w * (len(event) if rest_present else 0)
                    + artic_w * fabrication / GRID_SCALE)
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


def read_tempo_curve(mid, tick_scale=1.0):
    """Every set_tempo message across all tracks, absolute tick positions
    rescaled into the internal TICKS_PER_BEAT space (see tick_scale in
    run()) and merged/sorted by tick. Returns [(tick, raw_tempo), ...]
    where raw_tempo is microseconds-per-beat (mido's native unit, ready
    to hand straight to a MetaMessage). A source with only ONE tempo
    event that's byte-identical to the untouched MIDI spec default
    (500000 microsec/beat = 120 BPM -- see detect_source_tempo) is
    treated the same as having no real tempo info at all and returns [];
    a genuine multi-event curve is kept even if one of its events happens
    to also be exactly 120 BPM."""
    events = {}
    for trk in mid.tracks:
        abs_t = 0
        for msg in trk:
            abs_t += msg.time
            if msg.type == 'set_tempo':
                events[round(abs_t * tick_scale)] = msg.tempo
    if not events:
        return []
    curve = sorted(events.items())
    if len(curve) == 1 and curve[0][1] == 500000:
        return []
    return curve


# Thresholds for flagging a multi-event tempo curve as more likely to be
# transcription timing jitter mislabeled as tempo changes than genuine
# macro tempo direction (verse/chorus rubato, a real accelerando, etc).
# Calibrated against exactly one real problem case (RiverFlowInYou.mid:
# 3.05x between slowest/fastest event, up to a 39 BPM jump between
# adjacent events one beat apart) -- there's no second erratic-curve
# example yet to confirm these generalize, so treat this as a first-pass
# heuristic to warn/redefault on, not a proven boundary. Revisit with more
# real multi-tempo files if it ever mis-flags a genuine rubato piece.
TEMPO_CURVE_RATIO_THRESHOLD = 2.0
TEMPO_CURVE_JUMP_THRESHOLD_BPM = 20.0

# Jitter is dense; a deliberate tempo change is not. The two thresholds
# above, applied alone, flagged a single genuine section change (e.g.
# 140 -> 80 BPM thirty bars in, as an orchestral or DAW export carries) as
# "erratic", discarding it and playing the rest of the piece at the
# opening tempo. So:
# - a >TEMPO_CURVE_JUMP_THRESHOLD_BPM jump only counts as jitter when its
#   two events are less than TEMPO_JUMP_MAX_SPACING_BEATS apart
#   (RiverFlowInYou's big jumps are all 1-2 beats apart);
# - the slowest/fastest ratio test only applies when the curve is dense,
#   i.e. its events average less than TEMPO_DENSE_SPACING_BEATS apart
#   (RiverFlowInYou: about one event per bar).
# Same single real positive case as above, plus one synthetic negative (a
# sparse two-event section change). RiverFlowInYou is still flagged
# exactly as before.
TEMPO_JUMP_MAX_SPACING_BEATS = 4
TEMPO_DENSE_SPACING_BEATS = 8


def assess_tempo_curve(curve):
    """curve: a read_tempo_curve() result, [(tick, raw_tempo), ...] in tick
    order, ticks in the internal TICKS_PER_BEAT space. Returns (is_erratic,
    ratio, max_jump) -- ratio is slowest/fastest spread, max_jump is the
    largest BPM change between adjacent events (reported regardless of
    spacing). See the threshold constants above for what counts as
    'erratic'."""
    ticks = [t for t, _ in curve]
    bpms = [round(mido.tempo2bpm(v), 1) for _, v in curve]
    ratio = max(bpms) / min(bpms) if min(bpms) > 0 else float('inf')
    pairs = list(zip(bpms, bpms[1:], ticks, ticks[1:]))
    max_jump = max((abs(b - a) for a, b, _, _ in pairs), default=0.0)
    close_jump = max((abs(b - a) for a, b, ta, tb in pairs
                      if (tb - ta) < TEMPO_JUMP_MAX_SPACING_BEATS * TICKS_PER_BEAT), default=0.0)
    avg_spacing_beats = (ticks[-1] - ticks[0]) / max(1, len(ticks) - 1) / TICKS_PER_BEAT
    dense = avg_spacing_beats < TEMPO_DENSE_SPACING_BEATS
    is_erratic = ((dense and ratio > TEMPO_CURVE_RATIO_THRESHOLD)
                  or close_jump > TEMPO_CURVE_JUMP_THRESHOLD_BPM)
    return is_erratic, ratio, max_jump


def estimate_representative_tempo(mid, notes):
    """Best single flat-tempo guess when there's no tempo curve worth
    trusting (none present, a single generic-default stamp, or a
    multi-event curve judged too erratic to be genuine tempo direction).
    Prefers the source file's own declared tempo if it looks like it was
    actually measured (not the untouched MIDI spec default); falls back
    to rhythm-based estimation from note-onset spacing otherwise. Returns
    (tempo, description) where description explains which path was used,
    for the caller to report however fits its own messaging."""
    detected, tempo_is_generic = detect_source_tempo(mid)
    if detected is not None and not tempo_is_generic:
        return detected, f"using source file tempo: {detected} BPM"
    prefix = ""
    if detected is not None and tempo_is_generic:
        prefix = (f"source file tempo ({detected} BPM) is byte-identical to the untouched MIDI spec "
                  f"default, which most transcription tools stamp automatically without actually "
                  f"measuring it. Falling through to rhythm-based estimation instead.\n")
    ranked = estimate_tempo_candidates(mid, notes)
    estimated = ranked[0][0] if ranked else None
    tempo = estimated if estimated is not None else 120.0
    desc = prefix + (f"estimated from note-onset rhythm: {tempo} BPM" if estimated is not None else
                      f"no usable tempo info at all -- using neutral default: {tempo} BPM")
    ambiguity = describe_tempo_ambiguity(ranked) if ranked else None
    if ambiguity:
        desc += f"\n{ambiguity}"
    return tempo, desc


def rebase_tempo_curve(curve, leading_offset):
    """Shift a read_tempo_curve() result to start at the same zero-point
    as leading-silence-rebased notes. Events at or before leading_offset
    are dropped except the last one -- whichever tempo was actually in
    effect at the moment the piece now starts -- which becomes the new
    tick-0 event, so playback never loses track of "what tempo are we
    even at" right as the rebased piece begins."""
    if leading_offset <= 0 or not curve:
        return curve
    before = [(t, v) for t, v in curve if t <= leading_offset]
    after = [(t - leading_offset, v) for t, v in curve if t > leading_offset]
    carried = before[-1][1] if before else curve[0][1]
    return [(0, carried)] + after


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
    onset data.

    `notes` must already be in the internal TICKS_PER_BEAT space (both
    callers rescale by tick_scale before calling this), so ticks are
    converted to seconds at TICKS_PER_BEAT -- NOT the source file's own
    ticks_per_beat. Using the source's value here double-counted the
    rescale on any non-384-PPQ file: a 120 BPM eighth-note pulse read back
    as 75 BPM from a 480-PPQ file and 150 BPM from a 960-PPQ one."""
    scaling_bpm, _ = detect_source_tempo(mid)
    scaling_tempo = mido.bpm2tempo(scaling_bpm or 120.0)
    tpb = TICKS_PER_BEAT

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
          continuity would prefer;
      (c) it isn't a solo note (the only note at its onset) sitting
          inside an unbroken run of its own naively-assigned hand --
          i.e. when a note has no simultaneous chord-mate, at least one
          of the immediately neighboring onsets must ALSO naively
          belong to the other hand. Without this, a single melodic dip
          in the middle of one continuous phrase (both neighbors
          naively the same hand as the note itself) can get mistaken
          for a hand crossing purely because a locally-shifted split
          point (--dynamic-split) happens to put it in the ambiguity
          zone; centroid distance alone can't tell "boundary-hugging
          note that's really part of the OTHER hand's phrase" apart
          from "brief dip within THIS hand's own phrase" -- neighbor
          naive-hand agreement can. This guard only applies to solo
          onsets: a note with a simultaneous chord-mate (e.g. an
          octave/interval doubling meant to be split across hands) is
          a deliberate voicing decision, not a passing tone, so it's
          still governed only by (a) and (b).

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

    naive_hands_at = [set(naive[id(n)] for n in by_onset[onset]) for onset in onsets]

    prev_centroid = {'treble': None, 'bass': None}
    warnings = []
    for i, onset in enumerate(onsets):
        event = by_onset[onset]
        split = event[0]['_split_pitch']

        neighbor_hands = set()
        if i > 0:
            neighbor_hands |= naive_hands_at[i - 1]
        if i + 1 < len(onsets):
            neighbor_hands |= naive_hands_at[i + 1]

        for n in event:
            if abs(n['pitch'] - split) > ambiguity_zone:
                continue
            other = 'bass' if n['hand'] == 'treble' else 'treble'
            if len(event) == 1 and other not in neighbor_hands:
                continue
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


# ---------------------------------------------------------------------------
# Ornament detection (experimental) -- recognizes mordents, turns and trills
# as figures. Only tags notes; never changes timing or MIDI output. The
# MusicXML writer collapses a tagged figure into one note with an ornament
# symbol.
# ---------------------------------------------------------------------------

# An ornament's notes must follow each other faster than a 32nd note at the
# notated tempo. Chosen from real data: the figures in RiverFlowInYou that
# are ornaments by ear and by its reference score have 24-47 tick gaps
# between notes, while its ordinary 16th-note runs with the same pitch
# shapes (e.g. note, step up, back) sit at 90-95 ticks. Nothing in between
# was found, so a 32nd (48 ticks) separates them with room on both sides.
# One piece's worth of calibration, like the tuplet thresholds above.
ORNAMENT_MAX_IOI = TICKS_PER_BEAT // 8
ORNAMENT_CHORD_TOLERANCE = 6     # onsets this close (ticks) count as struck together
ORNAMENT_MAX_STEP = 2            # a neighbor note is a half or whole step away
TRILL_MIN_NOTES = 5              # shortest alternation called a trill (e.g. A B A B A)


def detect_ornaments(notes, id_start=0):
    """Find mordents, turns and trills in ONE staff's notes, using the raw
    (unquantized) onsets. Run after hands are assigned, so a figure can
    never straddle the two staves.

    A figure is a run of lone notes, each followed within ORNAMENT_MAX_IOI
    by the next, moving by step around one principal pitch and ending on a
    landing note:
      mordent           P, lower neighbor, P
      inverted-mordent  P, upper neighbor, P      (the sign without the stroke)
      turn              upper, P, lower, P   or   P, upper, P, lower, P
      inverted-turn     lower, P, upper, P   or   P, lower, P, upper, P
      trill             two pitches a step apart alternating, 5+ notes
    The landing note may be part of a chord; the quick notes before it may
    not. Longest pattern wins, so a trill is never read as a chain of
    mordents. A note inside a beat-scale tuplet group (quintuplet etc.) is
    never part of a figure -- that passage already has an explicit rhythm.

    Tags each quick note with '_orn_aux' (the figure id) and the landing
    note with '_orn' (type, principal pitch, neighbor pitches). Nothing
    else is touched. Returns a list of (type, landing_note) per figure."""
    def played(n):   # onset as played, before any collision nudge moved it
        return n.get('_played_start', n['start'])

    ns = sorted(notes, key=lambda n: (played(n), n['pitch']))
    groups = []
    for n in ns:
        if groups and played(n) - played(groups[-1][0]) <= ORNAMENT_CHORD_TOLERANCE:
            groups[-1].append(n)
        else:
            groups.append([n])

    def in_beat_tuplet(n):
        return bool(n.get('_tuplet_group')) and n['_tuplet_group'][1] != GRID

    def quick(j):
        return (j + 1 < len(groups) and len(groups[j]) == 1 and not in_beat_tuplet(groups[j][0])
                and played(groups[j + 1][0]) - played(groups[j][0]) <= ORNAMENT_MAX_IOI)

    def step(d):
        return 1 <= d <= ORNAMENT_MAX_STEP

    figures = []
    i = 0
    while i < len(groups):
        run = 0
        while quick(i + run):
            run += 1
        # groups i .. i+run-1 are quick lone notes; group i+run is where the run lands
        match = None
        if run >= 2:
            p = [groups[i + k][0]['pitch'] for k in range(run)]

            def landing(k, pitch):
                if run < k - 1:
                    return None
                return next((n for n in groups[i + k - 1]
                             if n['pitch'] == pitch and not in_beat_tuplet(n)), None)

            # trill: as many alternating notes as the run holds
            if step(abs(p[0] - p[1])):
                count = 2
                while count <= run:
                    want = p[count % 2]
                    if not any(n['pitch'] == want for n in groups[i + count]):
                        break
                    count += 1
                if count >= TRILL_MIN_NOTES:
                    land = landing(count, p[(count - 1) % 2])
                    if land is not None:
                        match = ('trill', count, land, min(p[0], p[1]), max(p[0], p[1]), None)
            if match is None and run >= 4:
                P = p[0]
                land = landing(5, P)
                if land is not None and p[2] == P:
                    if step(p[1] - P) and step(P - p[3]):
                        match = ('turn', 5, land, P, p[1], p[3])
                    elif step(P - p[1]) and step(p[3] - P):
                        match = ('inverted-turn', 5, land, P, p[3], p[1])
            if match is None and run >= 3:
                P = p[1]
                land = landing(4, P)
                if land is not None:
                    if step(p[0] - P) and step(P - p[2]):
                        match = ('turn', 4, land, P, p[0], p[2])
                    elif step(P - p[0]) and step(p[2] - P):
                        match = ('inverted-turn', 4, land, P, p[2], p[0])
            if match is None:
                P = p[0]
                land = landing(3, P)
                if land is not None:
                    if step(p[1] - P):
                        match = ('inverted-mordent', 3, land, P, p[1], None)
                    elif step(P - p[1]):
                        match = ('mordent', 3, land, P, None, p[1])
        if match is None:
            i += 1
            continue
        kind, size, land, principal, upper, lower = match
        fid = id_start + len(figures) + 1
        for k in range(size - 1):
            groups[i + k][0]['_orn_aux'] = fid
        land['_orn'] = {'id': fid, 'type': kind, 'pitch': principal, 'upper': upper, 'lower': lower}
        figures.append((kind, land))
        i += size
    return figures


# ---------------------------------------------------------------------------
# Key estimation and pitch spelling. Only MusicXML needs these: a MIDI note
# number doesn't say whether it's a C# or a Db, a written note has to.
# ---------------------------------------------------------------------------

# Temperley's key profiles (how strongly each scale degree belongs to a
# major / minor key), correlated against the piece's own duration-weighted
# pitch-class histogram. Chosen by test, not taste: on 50 engraved scores
# from the music21 corpus (Bach chorales plus Chopin, Schumann, Mozart,
# Beethoven, Joplin, Haydn, Schubert, Handel) these picked the printed key
# signature 47 times; the better-known Krumhansl-Kessler profiles managed 37.
_KEY_PROFILE = {
    'major': (5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0),
    'minor': (5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0),
}
_FIFTHS_LETTERS = 'FCGDAEB'       # line of fifths: F=-1, C=0, G=1, ... then F#=6, C#=7, ...
_SCALE_LETTERS = 'CDEFGAB'
_LETTER_PC = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
_ACCIDENTAL_NAME = {-2: 'flat-flat', -1: 'flat', 0: 'natural', 1: 'sharp', 2: 'double-sharp'}


def _lof_name(pos):
    """Line-of-fifths position -> (letter, alter)."""
    return _FIFTHS_LETTERS[(pos + 1) % 7], (pos + 1) // 7


def _fifths_for(tonic_pc, mode):
    major_pc = tonic_pc if mode == 'major' else (tonic_pc + 3) % 12
    f = (major_pc * 7) % 12
    if f > 6 or (f == 6 and mode == 'minor'):
        f -= 12       # 6 sharps for F# major, but 6 flats for Eb minor
    return f


def key_name(fifths, mode):
    letter, alter = _lof_name(fifths + (3 if mode == 'minor' else 0))
    sig = 'no sharps or flats' if fifths == 0 else f"{abs(fifths)} {'sharp' if fifths > 0 else 'flat'}{'s' if abs(fifths) != 1 else ''}"
    return f"{letter}{'#' * alter if alter > 0 else 'b' * -alter} {mode} ({sig})"


def parse_key(s):
    """'A', 'F#m', 'Bb minor', 'Eb major', or a number of sharps (+) / flats
    (-) like '-3' -> (fifths, mode)."""
    s = s.strip()
    try:
        fifths = int(s)
    except ValueError:
        m = re.fullmatch(r'([A-Ga-g])\s*([#b]?)\s*(m|min|minor|maj|major)?', s)
        if not m:
            raise ValueError("expected a key like A, F#m, 'Bb minor', or a number of sharps/flats like -3")
        mode = 'minor' if m.group(3) in ('m', 'min', 'minor') else 'major'
        pos = _FIFTHS_LETTERS.index(m.group(1).upper()) - 1 + 7 * {'#': 1, 'b': -1, '': 0}[m.group(2)]
        return _checked_fifths(pos - (3 if mode == 'minor' else 0)), mode
    return _checked_fifths(fifths), 'major'


def _checked_fifths(fifths):
    if not -7 <= fifths <= 7:
        raise ValueError("that key needs more than 7 sharps or flats")
    return fifths


def estimate_key(notes):
    """Best-fitting key for the whole piece. Returns (fifths, mode,
    runner_up) where runner_up is (fifths, mode) of the best candidate with
    a DIFFERENT key signature if it fits nearly as well (so the caller can
    say the call was close), else None. A relative major/minor mix-up (C
    major vs A minor) doesn't matter for the key signature and is never
    reported as ambiguity."""
    hist = [0.0] * 12
    for n in notes:
        hist[n['pitch'] % 12] += max(1, n['f_end'] - n['f_start'])
    if not any(hist):
        return 0, 'major', None
    mean_h = sum(hist) / 12
    dev_h = [h - mean_h for h in hist]
    norm_h = sum(d * d for d in dev_h) ** 0.5 or 1.0
    scored = []
    for mode, prof in _KEY_PROFILE.items():
        mean_p = sum(prof) / 12
        dev_p = [x - mean_p for x in prof]
        norm_p = sum(d * d for d in dev_p) ** 0.5
        for tonic in range(12):
            r = sum(dev_h[(tonic + k) % 12] * dev_p[k] for k in range(12)) / (norm_h * norm_p)
            scored.append((r, _fifths_for(tonic, mode), mode))
    scored.sort(key=lambda x: -x[0])
    best_r, fifths, mode = scored[0]
    runner = next(((f, m) for r, f, m in scored[1:] if f != fifths and r >= best_r - 0.05), None)
    return fifths, mode, runner


def _key_alter(letter, fifths):
    i = _FIFTHS_LETTERS.index(letter)
    if fifths > i:
        return 1
    if fifths < 0 and (6 - i) < -fifths:
        return -1
    return 0


def spell_pitch(pitch, fifths, mode, lean=0):
    """MIDI pitch -> (letter, alter, octave) for a key.

    Every pitch class gets the spelling nearest the key on the line of
    fifths: a major key takes its chromatic notes as b2 b3 #4 b6 b7, a
    minor key (same signature) leans sharp so its raised 6th/7th and the
    major 3rd come out as sharps (G#, not Ab, in A minor). lean=+1 / -1
    asks for the sharp / flat spelling of a note outside the key instead,
    used when the line moves on by a half step up / down (see
    spell_staff)."""
    lo = fifths - (2 if mode == 'minor' else 5)
    pos = lo + ((pitch * 7 - lo) % 12)
    if lean > 0 and pos < fifths - 1 and pos + 12 <= fifths + 10:
        pos += 12
    elif lean < 0 and pos > fifths + 5 and pos - 12 >= fifths - 6:
        pos -= 12
    letter, alter = _lof_name(pos)
    return letter, alter, (pitch - alter) // 12 - 1


def spell_staff(notes, fifths, mode):
    """Set n['_spell'] = (letter, alter, octave) on every note of one staff.
    Notes in the key are spelled by the key. A note outside the key looks
    at where its line goes next -- the nearest pitch at the staff's next
    onset: if that's a half step up it is spelled as a sharp (F# -> G), a
    half step down as a flat (Ab -> G), so the resolution reads as a step
    to a different letter. Otherwise the key's default spelling is used.

    Measured on the same 50 corpus scores as the key profiles (28,274
    notes, spelled from MIDI numbers alone and compared with the printed
    note names): 98.9% of all notes match, and 89% of the notes outside
    the key signature -- up from 74% without the half-step rule."""
    by_onset = defaultdict(list)
    for n in notes:
        by_onset[n['f_start']].append(n)
    onsets = sorted(by_onset)
    nxt = {a: b for a, b in zip(onsets, onsets[1:])}
    for n in notes:
        lean = 0
        following = by_onset.get(nxt.get(n['f_start']), [])
        if following:
            d = min((m['pitch'] - n['pitch'] for m in following), key=abs)
            if abs(d) == 1:
                lean = d
        n['_spell'] = spell_pitch(n['pitch'], fifths, mode, lean)


# ---------------------------------------------------------------------------
# MusicXML export. Writes the score exactly as decided above -- staff per
# hand, each note's written length and ties, tuplet groups, ornaments --
# instead of leaving a MIDI importer to guess them again.
# ---------------------------------------------------------------------------

_NOTE_TYPES = [(Fraction(16), 'long'), (Fraction(8), 'breve'), (Fraction(4), 'whole'),
               (Fraction(2), 'half'), (Fraction(1), 'quarter'), (Fraction(1, 2), 'eighth'),
               (Fraction(1, 4), '16th'), (Fraction(1, 8), '32nd'), (Fraction(1, 16), '64th'),
               (Fraction(1, 32), '128th')]
_TYPE_OF = dict(_NOTE_TYPES)
_ORNAMENT_TAG = {'trill': 'trill-mark', 'mordent': 'mordent', 'inverted-mordent': 'inverted-mordent',
                 'turn': 'turn', 'inverted-turn': 'inverted-turn'}


def _q(ticks):
    """Internal ticks -> exact position in quarter notes."""
    return Fraction(ticks, TICKS_PER_BEAT)


def _note_values(length, dotted=True):
    """Split a length (in quarters) into noteheads to tie together, largest
    first -- the same greedy split minimal_tie_count() counts, so a note
    gets exactly as many tied noteheads here as the optimizer budgeted for.
    Returns [(length, type, dots), ...]."""
    cands = []
    for v, t in _NOTE_TYPES:
        cands.append((v, t, 0))
        if dotted and v >= Fraction(1, 16):
            cands.append((v * 3 / 2, t, 1))
    cands.sort(key=lambda c: -c[0])
    out, rem = [], length
    while rem > 0:
        pick = next((c for c in cands if c[0] <= rem), None)
        if pick is None:
            raise ValueError(f"length {length} quarters can't be written with standard note values")
        out.append(pick)
        rem -= pick[0]
    return out


def _order_values(vals, pos):
    """Which way round to tie a multi-notehead note. Same noteheads either
    way (so the same tie count); pick the order that puts more of them on a
    position their own length divides -- e.g. a 16th then a quarter when the
    note starts a 16th before the beat, not a quarter hanging over it."""
    def aligned(seq):
        p, score = pos, 0
        for v, _t, dots in seq:
            score += (p % (v * 2 / 3 if dots else v) == 0)
            p += v
        return score
    rev = vals[::-1]
    return rev if aligned(rev) > aligned(vals) else vals


def _rest_values(a, b, bar_start):
    """Rests filling [a, b): at each point the longest plain rest that both
    fits and sits on its own grid within the bar."""
    out = []
    while a < b:
        pick = next(((v, t, 0) for v, t in _NOTE_TYPES if v <= b - a and (a - bar_start) % v == 0), None)
        if pick is None:
            raise ValueError(f"rest from {a} to {b} can't be written with standard rest values")
        out.append(pick)
        a += pick[0]
    return out


def _mx_staff_items(notes, bar_q, stats):
    """One staff's notes -> the things a voice is made of: chords
    ('ev': start, end, notes) and bracketed tuplet groups ('block')."""
    grid_q = _q(GRID)
    fig_start = {}
    for n in notes:
        if n.get('_orn_aux'):
            fig_start[n['_orn_aux']] = min(fig_start.get(n['_orn_aux'], n['f_start']), n['f_start'])

    plain = []                      # [start, end, note, ornament]
    blocks = defaultdict(dict)      # (group start, span, N) -> {slot: [notes]}
    broken = set()                  # tuplet groups that lost a member to an ornament
    for n in notes:
        g = n.get('_tuplet_group')
        if n.get('_orn_aux'):
            if g:
                broken.add(g[:3])
            continue
        if n.get('_orn'):
            # the whole figure becomes this one note, from where the figure began
            s = (min(fig_start.get(n['_orn']['id'], n['f_start']), n['f_start']) // GRID) * GRID
            e = -(-n['f_end'] // GRID) * GRID
            if g:
                broken.add(g[:3])
            plain.append([_q(s), _q(max(e, s + GRID)), n, n['_orn']])
        elif g:
            blocks[g[:3]].setdefault(g[3], []).append(n)
        else:
            s, e = n['f_start'], n['f_end']
            if n.get('_tuplet'):    # defensive: evenly-divided note with no group info
                s = (s // GRID) * GRID
                e = max(s + GRID, -(-e // GRID) * GRID)
            plain.append([_q(s), _q(e), n, None])

    items = []
    for key, slots in blocks.items():
        group_start, span, n_cand = key
        a = _q(round(group_start / GRID) * GRID)    # a bracket has to start on the grid
        b = a + _q(span)
        crosses_bar = a // bar_q != (b - Fraction(1, 1024)) // bar_q
        if n_cand > 2 and set(slots) == {0}:
            # this staff only plays the group's first note (the rest of the
            # run is in the other hand): one ordinary note, no bracket of rests
            normal = 1 << (n_cand.bit_length() - 1)
            for n in slots[0]:
                plain.append([a, a + _q(span) / normal, n, None])
            continue
        if n_cand > 2 and key not in broken and not crosses_bar:
            pitches = [n['pitch'] for ns in slots.values() for n in ns]
            items.append({'kind': 'block', 'start': a, 'end': b, 'n': n_cand, 'slots': slots,
                          'pitch': sum(pitches) / len(pitches)})
            stats['tuplet_keys'].add(key)
            continue
        # Can't be drawn as one bracket: two notes sharing a 16th are simply
        # two 32nds; a group straddling a barline, or one that an ornament
        # took notes out of, is written as plain notes on the coarsest
        # power-of-two grid that keeps every onset distinct.
        bounds = None
        for res in (grid_q, grid_q / 2, grid_q / 4, grid_q / 8):
            cand = [a + round((_q(span) * k / n_cand) / res) * res for k in range(n_cand)] + [b]
            if all(x < y for x, y in zip(cand, cand[1:])):
                bounds = cand
                break
        if bounds is None:
            stats['dropped'] += sum(len(ns) for ns in slots.values())
            continue
        if n_cand > 2 and crosses_bar:
            stats['flattened_keys'].add(key)
        for k, ns in slots.items():
            for n in ns:
                plain.append([bounds[k], bounds[k + 1], n, None])

    chords = {}
    for s, e, n, orn in plain:
        it = chords.setdefault((s, e), {'kind': 'ev', 'start': s, 'end': e, 'notes': [], 'orn': None})
        it['notes'].append(n)
        it['orn'] = it['orn'] or orn
    for it in chords.values():
        it['pitch'] = sum(n['pitch'] for n in it['notes']) / len(it['notes'])
        items.append(it)
    return items


def _mx_assign_voices(items, max_voices, stats):
    """Give every item a voice number (1 = upper). ScorePrep's durations
    let notes in one staff overlap freely -- fine in MIDI, but on paper
    every overlap needs its own voice, and a pedaled arpeggio can stack up
    eight deep.

    First, fit the staff into max_voices lines: an item joins a line
    that's free (the one that last played nearest its pitch); if none is
    free and max_voices are already going, the sounding note nearest in
    pitch is cut off where the new one starts -- how the passage would be
    written by hand, with the ring-over left to the pedal. Two items
    starting together that can't each have a line become one chord.

    Then number them. An item is voice 1 unless something higher sounds
    during it (now or later), in which case it sits that many voices
    down -- so a note playing alone is always voice 1, and a held bass
    note under a moving line is voice 2 from its first beat, not only
    once the line enters.

    Before either: a note that only just overlaps the next one -- its last
    16th, or its last quarter of its own length, whichever is more -- is
    simply ended where that next note starts. That's fingers holding over
    (legato), not a second voice, and writing it as one would hang a whole
    extra voice and a bar of rests on a 16th of overlap."""
    grid_q = _q(GRID)
    items.sort(key=lambda it: (it['start'], -it['pitch']))
    lines, placed = [], []
    for it in items:
        for v in lines:
            x = v['last']
            if (x['kind'] == 'ev' and x['start'] < it['start'] < x['end']
                    and x['end'] - it['start'] <= max(grid_q, (x['end'] - x['start']) / 4)):
                x['end'] = it['start']
                stats['trimmed'] += len(x['notes'])
        near = lambda v: abs(v['last']['pitch'] - it['pitch'])
        free = [v for v in lines if v['last']['end'] <= it['start']]
        if free:
            v = min(free, key=near)
        elif len(lines) < max_voices:
            v = {}
            lines.append(v)
        else:
            cuttable = [v for v in lines if v['last']['kind'] == 'ev']
            together = [v for v in cuttable if v['last']['start'] == it['start']]
            earlier = [v for v in cuttable if v['last']['start'] < it['start']]
            if it['kind'] == 'ev' and together:
                host = min(together, key=near)['last']
                host['notes'].extend(it['notes'])
                host['orn'] = host['orn'] or it['orn']
                host['pitch'] = sum(n['pitch'] for n in host['notes']) / len(host['notes'])
                stats['merged'] += len(it['notes'])
                continue
            if not earlier:
                # only tuplet groups are sounding, and those can't be cut
                stats['dropped'] += (len(it['notes']) if it['kind'] == 'ev'
                                     else sum(len(x) for x in it['slots'].values()))
                continue
            v = min(earlier, key=near)
            v['last']['end'] = it['start']
            stats['shortened'] += len(v['last']['notes'])
        v['last'] = it
        placed.append(it)

    placed.sort(key=lambda it: (it['start'], -it['pitch']))
    sounding = []
    for i, it in enumerate(placed):
        sounding = [o for o in sounding if o['end'] > it['start']]
        j = i + 1
        while j < len(placed) and placed[j]['start'] < it['end']:
            j += 1
        marks = []
        for o in sounding + placed[i + 1:j]:
            if o['pitch'] > it['pitch']:
                marks += [(max(o['start'], it['start']), 1), (min(o['end'], it['end']), -1)]
        depth = above = 0
        for _t, d in sorted(marks):
            depth += d
            above = max(above, depth)
        want = min(max_voices, 1 + above)
        taken = {o['voice'] for o in sounding}
        it['voice'] = min((v for v in range(1, max_voices + 1) if v not in taken),
                          key=lambda v: (abs(v - want), v), default=want)
        sounding.append(it)
    return placed


def _mx_ornament(orn, fifths, mode):
    """<ornaments> element for a detected figure, with an accidental above
    or below the sign when the neighbor note isn't the one the key
    signature would give."""
    letter, alter, _octave = spell_pitch(orn['pitch'], fifths, mode)
    natural = orn['pitch'] - alter
    idx = _SCALE_LETTERS.index(letter)
    marks = ''
    for placement, pitch, to in (('above', orn['upper'], 1), ('below', orn['lower'], -1)):
        if pitch is None:
            continue
        nb = _SCALE_LETTERS[(idx + to) % 7]
        gap = (_LETTER_PC[nb] - _LETTER_PC[letter]) % 12 if to > 0 else -((_LETTER_PC[letter] - _LETTER_PC[nb]) % 12)
        nb_alter = pitch - (natural + gap)
        if nb_alter != _key_alter(nb, fifths) and nb_alter in _ACCIDENTAL_NAME:
            marks += f'<accidental-mark placement="{placement}">{_ACCIDENTAL_NAME[nb_alter]}</accidental-mark>'
    return f'<ornaments><{_ORNAMENT_TAG[orn["type"]]}/>{marks}</ornaments>'


def _mx_staff_measures(items, n_measures, bar_q, dotted, fifths, mode):
    """Lay one staff's voiced items out bar by bar. Returns, per measure, a
    list of voices, each a list of element dicts (note / rest / forward)."""
    per = [defaultdict(list) for _ in range(n_measures)]
    for it in items:
        if it['kind'] == 'block':
            per[int(it['start'] // bar_q)][it['voice']].append((it['start'], it['end'], it, True, True))
            continue
        a = it['start']
        while a < it['end']:
            m = int(a // bar_q)
            b = min(it['end'], (m + 1) * bar_q)
            per[m][it['voice']].append((a, b, it, a == it['start'], b == it['end']))
            a = b

    def chord_pitches(it):
        seen = {}
        for n in it['notes']:
            is_landing = it['orn'] is not None and n.get('_orn') is it['orn']
            pitch = it['orn']['pitch'] if is_landing else n['pitch']
            spell = spell_pitch(pitch, fifths, mode) if is_landing else n['_spell']
            seen.setdefault(pitch, (pitch, spell, n['vel']))
        return [seen[p] for p in sorted(seen)]

    measures = []
    for m in range(n_measures):
        m0, m1 = m * bar_q, (m + 1) * bar_q
        out = []
        if not per[m]:
            out.append([{'x': 'rest', 'pos': m0, 'dur': bar_q, 'measure': True, 'voice': 1}])
        for vi, voice in enumerate(sorted(per[m])):
            els, cur = [], m0

            def fill(a, b):
                if vi == 0:     # the staff's top voice in this bar shows its rests
                    for v, t, d in _rest_values(a, b, m0):
                        els.append({'x': 'rest', 'pos': a, 'dur': v, 'type': t, 'dots': d, 'voice': voice})
                        a += v
                else:           # other voices just skip ahead
                    els.append({'x': 'forward', 'pos': a, 'dur': b - a, 'voice': voice})

            for a, b, it, first, last in sorted(per[m][voice], key=lambda seg: seg[0]):
                if a > cur:
                    fill(cur, a)
                if it['kind'] == 'block':
                    n = it['n']
                    normal = 1 << (n.bit_length() - 1)
                    slot = (b - a) / n
                    for k in range(n):
                        el = {'pos': a + k * slot, 'dur': slot, 'type': _TYPE_OF[(b - a) / normal], 'dots': 0,
                              'voice': voice, 'tm': (n, normal),
                              'tuplet': 'start' if k == 0 else 'stop' if k == n - 1 else None}
                        if k in it['slots']:
                            el.update(x='note', tie_stop=False, tie_start=False, orn=None,
                                      pitches=chord_pitches({'notes': it['slots'][k], 'orn': None}))
                        else:
                            el['x'] = 'rest'
                        els.append(el)
                else:
                    vals = _order_values(_note_values(b - a, dotted), a - m0)
                    pitches = chord_pitches(it)
                    pos = a
                    for i, (v, t, d) in enumerate(vals):
                        els.append({'x': 'note', 'pos': pos, 'dur': v, 'type': t, 'dots': d, 'voice': voice,
                                    'pitches': pitches,
                                    'tie_stop': not (first and i == 0),
                                    'tie_start': not (last and i == len(vals) - 1),
                                    'orn': (_mx_ornament(it['orn'], fifths, mode)
                                            if it['orn'] is not None and first and i == 0 else None)})
                        pos += v
                cur = b
            if cur < m1 and vi == 0:
                fill(cur, m1)
            out.append(els)

        # accidentals, in time order across the staff's voices
        state = {}
        for el in sorted((e for els in out for e in els if e['x'] == 'note'), key=lambda e: e['pos']):
            el['acc'] = []
            for _pitch, (letter, alter, octave), _vel in el['pitches']:
                shown = None
                if not el['tie_stop']:
                    if alter != state.get((letter, octave), _key_alter(letter, fifths)):
                        shown = _ACCIDENTAL_NAME.get(alter)
                    state[(letter, octave)] = alter
                el['acc'].append(shown)
        measures.append(out)
    return measures


def write_musicxml(path, treble, bass, time_sig, bar_ticks, tempo, tempo_curve, fifths, mode,
                   title='', max_voices=2, dotted=True):
    """Write the finished score as MusicXML: one piano part, treble staff
    from `treble`, bass staff from `bass`, both already carrying their
    final 'f_start'/'f_end' and '_spell'. Returns a stats dict (voices cut,
    tuplets written, ...) for run() to report."""
    stats = defaultdict(int)
    stats['tuplet_keys'], stats['flattened_keys'] = set(), set()
    bar_q = _q(bar_ticks)
    staves = []
    for notes in (treble, bass):
        staves.append(_mx_assign_voices(_mx_staff_items(notes, bar_q, stats), max_voices, stats))
    last_end = max((it['end'] for items in staves for it in items), default=bar_q)
    n_measures = max(1, -(-last_end // bar_q))
    laid = [_mx_staff_measures(items, n_measures, bar_q, dotted, fifths, mode) for items in staves]
    stats['ornaments'] = sum(1 for items in staves for it in items if it['kind'] == 'ev' and it['orn'] is not None)
    stats['measures'] = n_measures
    stats['tuplets'] = len(stats['tuplet_keys'])
    stats['tuplets_flattened'] = len(stats['flattened_keys'])
    stats['voices'] = max((it['voice'] for items in staves for it in items), default=1)

    tempos = ([(_q(t), mido.tempo2bpm(raw)) for t, raw in tempo_curve] if len(tempo_curve) > 1
              else [(Fraction(0), float(tempo))])
    tempo_at = defaultdict(list)
    for pos, bpm in tempos:
        m = min(n_measures - 1, int(pos // bar_q))
        tempo_at[m].append((pos - m * bar_q, round(bpm, 2)))

    # smallest <divisions> that makes every duration a whole number
    div = 1
    durs = [el['dur'] for staff in laid for meas in staff for els in meas for el in els]
    durs += [off for offs in tempo_at.values() for off, _ in offs] + [bar_q]
    for d in durs:
        div = div * d.denominator // gcd(div, d.denominator)

    def D(x):
        return (x * div).numerator

    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN" '
         '"http://www.musicxml.org/dtds/partwise.dtd">',
         '<score-partwise version="3.1">',
         f'  <work><work-title>{xml_escape(title)}</work-title></work>',
         '  <identification><encoding><software>ScorePrep</software></encoding></identification>',
         '  <part-list><score-part id="P1"><part-name>Piano</part-name>'
         '<score-instrument id="P1-I1"><instrument-name>Piano</instrument-name></score-instrument>'
         '<midi-instrument id="P1-I1"><midi-channel>1</midi-channel><midi-program>1</midi-program>'
         '</midi-instrument></score-part></part-list>',
         '  <part id="P1">']
    for m in range(n_measures):
        L.append(f'    <measure number="{m + 1}">')
        if m == 0:
            L.append(f'      <attributes><divisions>{div}</divisions>'
                     f'<key><fifths>{fifths}</fifths><mode>{mode}</mode></key>'
                     f'<time><beats>{time_sig[0]}</beats><beat-type>{time_sig[1]}</beat-type></time>'
                     f'<staves>2</staves>'
                     f'<clef number="1"><sign>G</sign><line>2</line></clef>'
                     f'<clef number="2"><sign>F</sign><line>4</line></clef></attributes>')
        for off, bpm in tempo_at.get(m, []):
            offset = f'<offset sound="yes">{D(off)}</offset>' if off else ''
            L.append(f'      <direction placement="above"><direction-type><metronome>'
                     f'<beat-unit>quarter</beat-unit><per-minute>{bpm:g}</per-minute></metronome>'
                     f'</direction-type>{offset}<staff>1</staff><sound tempo="{bpm:g}"/></direction>')
        m0 = m * bar_q
        cursor = m0
        for si, staff in enumerate(laid):
            for els in staff[m]:
                if cursor > m0:
                    L.append(f'      <backup><duration>{D(cursor - m0)}</duration></backup>')
                cursor = m0
                for el in els:
                    voice = el['voice'] + 4 * si
                    tail = f'<voice>{voice}</voice>'
                    if el['x'] == 'forward':
                        L.append(f'      <forward><duration>{D(el["dur"])}</duration>{tail}<staff>{si + 1}</staff></forward>')
                        cursor += el['dur']
                        continue
                    if not el.get('measure'):
                        tail += f'<type>{el["type"]}</type>' + '<dot/>' * el['dots']
                    tm = ''
                    if el.get('tm'):
                        tm = (f'<time-modification><actual-notes>{el["tm"][0]}</actual-notes>'
                              f'<normal-notes>{el["tm"][1]}</normal-notes></time-modification>')
                    tup = f'<tuplet type="{el["tuplet"]}" bracket="yes"/>' if el.get('tuplet') else ''
                    if el['x'] == 'rest':
                        notations = f'<notations>{tup}</notations>' if tup else ''
                        L.append(f'      <note><rest{" measure=\"yes\"" if el.get("measure") else ""}/>'
                                 f'<duration>{D(el["dur"])}</duration>{tail}{tm}<staff>{si + 1}</staff>{notations}</note>')
                    else:
                        ties = (('<tie type="stop"/>' if el['tie_stop'] else '')
                                + ('<tie type="start"/>' if el['tie_start'] else ''))
                        tied = (('<tied type="stop"/>' if el['tie_stop'] else '')
                                + ('<tied type="start"/>' if el['tie_start'] else ''))
                        for i, ((_p, (letter, alter, octave), vel), acc) in enumerate(zip(el['pitches'], el['acc'])):
                            extra = tied + (tup + (el['orn'] or '') if i == 0 else '')
                            L.append(f'      <note dynamics="{vel / 90 * 100:.1f}">{"<chord/>" if i else ""}'
                                     f'<pitch><step>{letter}</step>{f"<alter>{alter}</alter>" if alter else ""}'
                                     f'<octave>{octave}</octave></pitch>'
                                     f'<duration>{D(el["dur"])}</duration>{ties}{tail}'
                                     f'{f"<accidental>{acc}</accidental>" if acc else ""}{tm}'
                                     f'<staff>{si + 1}</staff>'
                                     f'{f"<notations>{extra}</notations>" if extra else ""}</note>')
                    cursor += el['dur']
        if m == n_measures - 1:
            L.append('      <barline location="right"><bar-style>light-heavy</bar-style></barline>')
        L.append('    </measure>')
    L += ['  </part>', '</score-partwise>', '']
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return stats


# ---------------------------------------------------------------------------
# Beat tracking: find the beats from when the notes were actually played.
#
# A transcription of a live performance carries real onset times; whatever
# tempo map came with it is some tool's guess at where the beats fall. When
# that guess is bad (see assess_tempo_curve), the note positions in beats
# are bad too -- flattening the tempo then keeps the wrong beat positions
# AND throws away the timing that made them sound right. So instead: go
# back to seconds and work the beats out here.
# ---------------------------------------------------------------------------

# How the tracker weighs its evidence. Tuned against one real case with a
# known answer: RiverFlowInYou.mid (a rubato performance, 57-76 BPM with
# phrase-end ritardandos) checked note by note against the published score.
# Its own tempo map puts 68% of the score's on-beat notes on a beat; these
# settings put 87% there. The score moves by only a point or two anywhere
# near these values, so they are not finely balanced -- but it is one piece.
BEAT_TRACK_TOLERANCE = 0.15          # timing scatter allowed per step, in grid steps
BEAT_TRACK_TEMPO_STIFFNESS = 12.0    # cost of the tempo changing between two notes
# cost of a note landing on each half-step of the beat: on the beat, then
# 32nd / 16th / 32nd / 8th / 32nd / 16th / 32nd after it
BEAT_TRACK_METER_COST = (0.0, 4.0, 2.0, 4.0, 0.7, 4.0, 2.0, 4.0)
BEAT_TRACK_ORNAMENT_GAP = 0.4        # onsets closer than this (in grid steps) ride along with the note before
# cost of a bass note (lowest note struck within 1.5 s either side) landing
# anywhere but a strong beat. Adding this took the River test from 87% to
# 91% of on-beat notes on beats, and 90% to 94% of the score's downbeats on
# one consistent bar position; any value from 1.5 to 6 scored the same.
BEAT_TRACK_BASS_COST = 3.0


def ticks_to_seconds_fn(curve, fallback_tempo):
    """Function mapping an internal tick to real seconds, through a
    read_tempo_curve() result (or one flat tempo, in microseconds per beat,
    when the curve is empty)."""
    events = list(curve) if curve else [(0, fallback_tempo)]
    if events[0][0] > 0:
        events.insert(0, (0, events[0][1]))
    ticks = [t for t, _ in events]
    cum = [0.0]
    for (t0, u0), (t1, _u1) in zip(events, events[1:]):
        cum.append(cum[-1] + (t1 - t0) * u0 / 1e6 / TICKS_PER_BEAT)

    def to_sec(tick):
        lo, hi = 0, len(ticks) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ticks[mid] <= tick:
                lo = mid
            else:
                hi = mid - 1
        return cum[lo] + (tick - ticks[lo]) * events[lo][1] / 1e6 / TICKS_PER_BEAT
    return to_sec


def track_beat_grid(onsets, cycle_beats=2):
    """onsets: [(seconds, pitch), ...] for every note. Returns (per_beat,
    anchors): per_beat is how many grid steps make a beat (4 when the
    commonest gap between notes is a 16th), anchors is [(seconds, position
    in grid steps), ...] for every note taken as a grid point. Positions
    rise by whole or half steps; multiples of per_beat are beats, and
    multiples of per_beat * cycle_beats are strong beats (bar or half-bar
    starts). Returns (None, []) if there is too little to go on.

    Each gap between consecutive notes is read as some number of grid steps
    at some local tempo, and the whole piece's reading is chosen at once
    (shortest-path search) to balance four things: gaps should be close to
    a whole number of steps; the tempo shouldn't lurch between neighboring
    notes; notes should fall on beats and 8ths more than in between
    (BEAT_TRACK_METER_COST); and the lowest note of its surroundings -- a
    bass note -- should fall on a strong beat (BEAT_TRACK_BASS_COST). The
    last two are what let a slowing phrase ending be heard as slowing
    rather than as extra beats.

    Very quick notes (ornaments) aren't grid points; they're placed between
    the two around them."""
    from math import log, log2, exp
    heads, lows = [], []
    for t, p in sorted(onsets):
        if not heads or t - heads[-1] > 0.03:
            heads.append(t)
            lows.append(p)
        else:
            lows[-1] = min(lows[-1], p)
    gaps = [b - a for a, b in zip(heads, heads[1:]) if 0.04 < b - a < 1.2]
    if len(gaps) < 12:
        return None, []
    step = max(gaps, key=lambda c: sum(exp(-log(d / c) ** 2 / 0.0128) for d in gaps))
    # grid steps per beat: a 16th-note step unless that makes the tempo absurd
    per_beat = 4
    while 60 / (per_beat * step) < 45 and per_beat > 1:
        per_beat //= 2
    while 60 / (per_beat * step) > 190:
        per_beat *= 2

    ev, low, prev = [heads[0]], [lows[0]], heads[0]
    for t, p in zip(heads[1:], lows[1:]):
        if t - prev >= BEAT_TRACK_ORNAMENT_GAP * step:
            ev.append(t)
            low.append(p)
        else:
            low[-1] = min(low[-1], p)
        prev = t
    # a bass note: below middle C and nothing lower struck within 1.5 s either side
    is_bass, a, b = [], 0, 0
    for i, t in enumerate(ev):
        while ev[a] < t - 1.5:
            a += 1
        while b < len(ev) and ev[b] <= t + 1.5:
            b += 1
        is_bass.append(low[i] < 60 and low[i] <= min(low[a:b]))

    P = max(2, round(cycle_beats * per_beat * 2))      # half-steps per strong-beat cycle
    in_beat = 2 * per_beat

    def meter_cost(ph, bass):
        f = (ph % in_beat) * 8 / in_beat
        c = BEAT_TRACK_METER_COST[int(f)] if f == int(f) else BEAT_TRACK_METER_COST[1]
        return c + (BEAT_TRACK_BASS_COST if bass and ph % P else 0.0)

    S = 31                               # candidate local step lengths: 0.6x .. 2.5x the commonest
    qs = [step * 2 ** (-0.7 + 2.0 * j / (S - 1)) for j in range(S)]
    lq = [log2(q) for q in qs]
    dl = [[(lq[x] - lq[y]) ** 2 for y in range(S)] for x in range(S)]
    sigma = BEAT_TRACK_TOLERANCE
    INF = float('inf')
    cost = [[abs(lq[j] - log2(step)) * 4 + meter_cost(ph, is_bass[0]) for ph in range(P)] for j in range(S)]
    back = []
    for i in range(1, len(ev)):
        d = ev[i] - ev[i - 1]
        stiff = BEAT_TRACK_TEMPO_STIFFNESS / max(1.0, d / step)
        mc = [meter_cost(ph, is_bass[i]) for ph in range(P)]
        best_prev = [[None] * P for _ in range(S)]
        for j in range(S):
            row = dl[j]
            for pp in range(P):
                bc, bj = INF, 0
                for jp in range(max(0, j - 8), min(S, j + 9)):
                    c = cost[jp][pp] + stiff * row[jp]
                    if c < bc:
                        bc, bj = c, jp
                best_prev[j][pp] = (bc, bj)
        new = [[INF] * P for _ in range(S)]
        bk = [[None] * P for _ in range(S)]
        for j in range(S):
            x = 2 * d / qs[j]            # the gap in half-steps at this local tempo
            base = int(x)
            for n2 in (base - 1, base, base + 1, base + 2):
                if n2 < 1:
                    continue
                o = ((x - n2) / 2 / sigma) ** 2 / max(1.0, n2 / 2) ** 0.5
                if o > 30:
                    continue
                for pp in range(P):
                    ph = (pp + n2) % P
                    c = best_prev[j][pp][0] + o + mc[ph]
                    if c < new[j][ph]:
                        new[j][ph] = c
                        bk[j][ph] = (best_prev[j][pp][1], pp, n2)
        if min(min(r) for r in new) == INF:
            return None, []              # a gap nothing fits (shouldn't happen)
        cost = new
        back.append(bk)
    j, ph = min(((j, ph) for j in range(S) for ph in range(P)), key=lambda st: cost[st[0]][st[1]])
    steps = []
    for bk in reversed(back):
        j, ph, n2 = bk[j][ph]
        steps.append(n2)
    steps.reverse()
    k = ph / 2.0
    anchors = [(ev[0], k)]
    for t, n2 in zip(ev[1:], steps):
        k += n2 / 2
        anchors.append((t, k))
    return per_beat, anchors


def retime_by_beat_tracking(notes, to_sec, time_sig):
    """Replace every note's 'start'/'end' tick with its position on a beat
    grid tracked from the real onset times (to_sec turns the current ticks
    into seconds). Returns None if there's too little to track, else
    (tempo, tempo_curve, info): the typical tempo, a beat-by-beat tempo
    curve that plays the new positions back at the original timing, and
    numbers for the log.

    Barlines: the first strong beat the piece reaches is taken as a
    downbeat; any notes before it are a pickup, written at the end of an
    opening bar of rests."""
    num, den = time_sig
    bar_beats = num * 4 / den
    cycle_beats = 1.5 if (den == 8 and num % 3 == 0) else 3 if num == 3 else 2
    for n in notes:
        n['_sec'] = (to_sec(n['start']), to_sec(n['end']))
    per_beat, anchors = track_beat_grid([(n['_sec'][0], n['pitch']) for n in notes], cycle_beats)
    if len(anchors) < 8:
        for n in notes:
            del n['_sec']
        return None
    at = [a for a, _ in anchors]
    ak = [k for _, k in anchors]

    def interp(xs, ys, x):
        lo, hi = 0, len(xs) - 2
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid - 1
        return ys[lo] + (x - xs[lo]) * (ys[lo + 1] - ys[lo]) / (xs[lo + 1] - xs[lo])

    cycle = cycle_beats * per_beat
    first_k = ak[0]
    downbeat = -(-(first_k - 0.01) // cycle) * cycle           # first strong beat at or after the first note
    origin = downbeat if downbeat <= first_k + 0.01 else downbeat - bar_beats * per_beat
    tick_per_step = TICKS_PER_BEAT / per_beat
    for n in notes:
        s, e = n.pop('_sec')
        n['start'] = max(0, round((interp(at, ak, s) - origin) * tick_per_step))
        n['end'] = max(n['start'] + 1, round((interp(at, ak, e) - origin) * tick_per_step))

    # tempo per beat, so playback keeps the performance's own pacing
    last_beat = int((ak[-1] - origin) / per_beat) + 1
    times = [interp(ak, at, origin + b * per_beat) for b in range(last_beat + 2)]
    curve, bpms = [], []
    for b in range(last_beat + 1):
        bpm = max(20.0, min(300.0, 60.0 / max(1e-3, times[b + 1] - times[b])))
        bpms.append(bpm)
        if not curve or abs(bpm - curve[-1][2]) / curve[-1][2] > 0.01:
            curve.append((b * TICKS_PER_BEAT, mido.bpm2tempo(bpm), bpm))
    inner = sorted(bpms[1:-1] or bpms)
    tempo = float(round(inner[len(inner) // 2]))
    return tempo, [(t, raw) for t, raw, _ in curve], {
        'beats': last_beat, 'slowest': inner[len(inner) // 20], 'fastest': inner[-1 - len(inner) // 20],
        'pickup_beats': (downbeat - first_k) / per_beat if downbeat > first_k + 0.01 else 0.0}


def resolve_output_paths(output_path, output_format=None):
    """(midi_path, musicxml_path) for a run; either may be None. With no
    --format the output file's own extension decides (.musicxml / .xml ->
    MusicXML only, anything else -> MIDI only). 'both' writes the two side
    by side under the same name."""
    stem, ext = os.path.splitext(output_path)
    is_xml = ext.lower() in ('.musicxml', '.xml')
    fmt = output_format or ('musicxml' if is_xml else 'midi')
    midi_path = None if fmt == 'musicxml' else (stem + '.mid' if is_xml else output_path)
    xml_path = None if fmt == 'midi' else (output_path if is_xml else stem + '.musicxml')
    return midi_path, xml_path


def run(input_path, output_path, tempo, split_pitch, temperature, time_sig=None,
        pedal_mode='ignore', min_note_ticks=None, playback_sustain=True, grid_mode='straight',
        track_selector=None, channel_override=None, duration_style='dotted',
        min_velocity=0, velocity_mode='passthrough', velocity_scale=1.0,
        tie_weight=None, rest_weight=None, artic_weight=None,
        preserve_source_duration=True, melody_preservation=False,
        dynamic_split=False, split_window_bars=8,
        hand_assignment=False, max_hand_span=16, hand_ambiguity_zone=3,
        tuplet_detection='off', trust_tempo_curve=False, tuplet_fit_mode='relative',
        output_format=None, key=None, ornament_detection='off', max_voices=2, beat_tracking='auto',
        fast_notes='auto'):
    """Runs the full cleanup pipeline. Shared by --interactive and normal
    CLI-argument mode. tempo, split_pitch, and time_sig may be None, in
    which case they're estimated from the source file."""
    temperature = max(0.0, min(1.0, temperature))
    configure_grid(grid_mode, duration_style)
    midi_path, xml_path = resolve_output_paths(output_path, output_format)
    for out_path in (midi_path, xml_path):
        if out_path and os.path.abspath(out_path) == os.path.abspath(input_path):
            sys.exit(f"Error: the output file would overwrite the input file ({input_path}). "
                     f"Give the output a different name.")
    if xml_path and grid_mode != 'straight':
        sys.exit("Error: MusicXML output needs --grid straight for now. The triplet grid puts notes on "
                 "positions that need triplet brackets worked out beat by beat, which isn't written yet. "
                 "Use --tuplet-detection auto for tuplets in MusicXML, or --format midi with --grid triplet.")
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
        tick_scale = TICKS_PER_BEAT / mid.ticks_per_beat
        print(f"NOTE: source ticks_per_beat={mid.ticks_per_beat} (this script's internal grid "
              f"is {TICKS_PER_BEAT}) -- rescaling every extracted tick position by "
              f"{tick_scale:.4f}x so grid/bar/tempo math stays correct.", file=sys.stderr)
    else:
        tick_scale = 1.0

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
    if tick_scale != 1.0:
        for n in notes:
            n['start'] = round(n['start'] * tick_scale)
            n['end'] = round(n['end'] * tick_scale)
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
    def drop_noise(notes):
        notes, dropped = filter_noise_notes(notes, min_note_ticks)
        if dropped:
            print(f"Dropped {dropped} note(s) shorter than {min_note_ticks} ticks "
                  f"({'auto threshold' if auto_min else 'explicit --min-note-ticks'}) "
                  f"as likely transcription noise", file=sys.stderr)
        return notes
    # If the beats are about to be re-tracked, tick lengths don't mean
    # anything yet (they sit on the source's untrusted beat map, where a
    # real 32nd can measure under the noise threshold) -- filter afterwards.
    raw_curve = read_tempo_curve(mid, tick_scale)
    will_track = tempo is None and (beat_tracking == 'on' or (
        beat_tracking == 'auto' and len(raw_curve) > 1 and assess_tempo_curve(raw_curve)[0]
        and not trust_tempo_curve))
    if not will_track:
        notes = drop_noise(notes)

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
    # A GENUINE multi-beat gap is almost always real silence at the start
    # of the source audio (an intro, spoken section, etc.) faithfully
    # transcribed, not a bug -- but a SUB-BEAT gap is almost always just
    # the piece not starting exactly on beat 1 (a short rest, a pickup),
    # which is completely normal notation and must NOT be stripped, or
    # every downstream barline silently shifts by that amount -- found
    # via a real piece that opens with an eighth-rest pickup: MuseScore
    # comparison showed content that should start measure 2 pulled back
    # into measure 1, entirely from this 0.59-beat rebase, with no
    # warning printed at all since the print threshold was already 1
    # beat but the strip itself had no lower bound to match it.
    leading_offset = min((n['start'] for n in notes), default=0)
    gap_beats = leading_offset / TICKS_PER_BEAT if leading_offset else 0.0
    if gap_beats >= 1.0:
        for n in notes:
            n['start'] -= leading_offset
            n['end'] -= leading_offset
        print(f"First note begins {gap_beats:.1f} beats ({leading_offset} ticks) into the "
              f"source file -- almost always real leading silence in the source audio (e.g. "
              f"an intro before playing starts), faithfully transcribed, not a bug. Rebasing "
              f"so the output score starts at the first note instead of showing empty leading "
              f"measures.", file=sys.stderr)
    else:
        leading_offset = 0  # sub-beat gap -- leave it as a real notated rest, don't rebase

    if pedal_mode == 'reflect':
        pedal_intervals = read_pedal_intervals(mid)
        if tick_scale != 1.0:
            pedal_intervals = [(round(d * tick_scale), round(u * tick_scale)) for d, u in pedal_intervals]
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

    time_sig_given = time_sig is not None
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

    tempo_curve = rebase_tempo_curve(read_tempo_curve(mid, tick_scale), leading_offset)

    # Beat tracking: when the source's tempo map can't be trusted (or on
    # request), work the beats out from the real onset times instead of
    # keeping the map's beat positions.
    beat_tracked = False
    curve_untrusted = (len(tempo_curve) > 1 and assess_tempo_curve(tempo_curve)[0] and not trust_tempo_curve)
    if tempo is None and (beat_tracking == 'on' or (beat_tracking == 'auto' and curve_untrusted)):
        src_bpm, _ = detect_source_tempo(mid)
        to_sec = ticks_to_seconds_fn(tempo_curve, mido.bpm2tempo(src_bpm or 120.0))
        if not time_sig_given and time_sig != DEFAULT_TIME_SIG:
            print(f"The source's time signature ({time_sig[0]}/{time_sig[1]}) came from the same beat analysis "
                  f"as its tempo map, which is being set aside -- using {DEFAULT_TIME_SIG[0]}/{DEFAULT_TIME_SIG[1]} "
                  f"instead. Pass --time-sig if the piece is in something else.", file=sys.stderr)
            time_sig = DEFAULT_TIME_SIG
            bar_ticks = bar_ticks_for(time_sig)
        tracked = retime_by_beat_tracking(notes, to_sec, time_sig)
        if tracked is None:
            print("beat-tracking -- too few notes to find a beat from; falling back to the tempo logic below",
                  file=sys.stderr)
        else:
            tempo, tempo_curve, bt = tracked
            beat_tracked = True
            pickup = (f", with a {bt['pickup_beats']:g}-beat pickup before the first barline"
                      if bt['pickup_beats'] else "")
            why = (f"the source's tempo map looks erratic ({len(read_tempo_curve(mid, tick_scale))} tempo changes, "
                   f"big jumps within a bar)" if curve_untrusted else "--beat-tracking on")
            print(f"beat-tracking -- {why}, so the beats were worked out from when the notes were actually "
                  f"played: {bt['beats']} beats, typically {tempo:.0f} BPM (the playing ranges about "
                  f"{bt['slowest']:.0f}-{bt['fastest']:.0f})"
                  f"{pickup}. Notes are placed on that beat grid, and the output "
                  f"carries a beat-by-beat tempo curve so playback keeps the original pacing. Pass "
                  f"--beat-tracking off for the old behavior, or --trust-tempo-curve to keep the source's map.",
                  file=sys.stderr)

    if will_track:
        notes = drop_noise(notes)
    if beat_tracked:
        pass
    elif tempo is None:
        use_curve = False
        if len(tempo_curve) > 1:
            bpms = [round(mido.tempo2bpm(t), 1) for _, t in tempo_curve]
            is_erratic, ratio, max_jump = assess_tempo_curve(tempo_curve)
            if is_erratic and not trust_tempo_curve:
                print(f"No --tempo given -- source file has {len(tempo_curve)} tempo changes "
                      f"({min(bpms):.1f}-{max(bpms):.1f} BPM), but the curve looks erratic "
                      f"({ratio:.1f}x between its slowest and fastest event, up to a {max_jump:.1f} "
                      f"BPM jump between adjacent events) -- more likely transcription timing jitter "
                      f"mislabeled as tempo changes than genuine macro tempo direction. Discarding it "
                      f"and estimating a single representative tempo instead (pass --trust-tempo-curve "
                      f"to force preserving it as-is, or --tempo to set one manually).", file=sys.stderr)
                tempo_curve = []
            else:
                use_curve = True
                tempo = bpms[0]
                note = " (curve looks erratic, but preserving anyway per --trust-tempo-curve)" if is_erratic else ""
                print(f"No --tempo given -- source file has {len(tempo_curve)} tempo changes "
                      f"({min(bpms):.1f}-{max(bpms):.1f} BPM) -- preserving the full tempo curve "
                      f"in the output instead of flattening to one value{note}", file=sys.stderr)
        if not use_curve:
            tempo_curve = []  # a single event, none, or a discarded erratic curve -- flat path below
            tempo, desc = estimate_representative_tempo(mid, notes)
            print(f"No --tempo given -- {desc}", file=sys.stderr)
    else:
        tempo_curve = []  # explicit --tempo always wins: flat single tempo, no curve

    if split_pitch is None:
        split_pitch = estimate_split_pitch(notes)
        print(f"No --split-pitch given -- estimated natural treble/bass split at "
              f"pitch {split_pitch} (from pitch distribution)", file=sys.stderr)

    source_encoding_tempo, _ = detect_source_tempo(mid)
    source_encoding_tempo = source_encoding_tempo or 120.0
    if beat_tracked:
        pass    # notes were already placed by real time; nothing to rescale
    elif source_encoding_tempo != tempo:
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

    tuplet_groups, rejected_tuplet_windows = (
        detect_tuplet_groups(notes, tuplet_fit_mode) if tuplet_detection == 'auto' else (0, []))
    if tuplet_groups:
        mode_note = " (--tuplet-fit-tolerance=absolute)" if tuplet_fit_mode == 'absolute' else ""
        print(f"tuplet-detection=auto -- found {tuplet_groups} local tuplet group(s) "
              f"(quintuplet/septuplet/nontuplet/etc.); those onsets are evenly divided "
              f"directly instead of snapped independently to the --grid {grid_mode} grid"
              f"{mode_note}", file=sys.stderr)

    subgrid_clusters = detect_subgrid_clusters(notes) if tuplet_detection == 'auto' else 0
    if subgrid_clusters:
        print(f"tuplet-detection=auto -- found {subgrid_clusters} sub-grid cluster(s) (2-3 "
              f"onsets too close together for the --grid {grid_mode} grid to tell apart, e.g. a "
              f"quick melodic turn/mordent); those are evenly divided within their own grid slot "
              f"instead of colliding onto the same tick", file=sys.stderr)

    rejected_collisions = (resolve_rejected_tuplet_collisions(notes, rejected_tuplet_windows)
                            if tuplet_detection == 'auto' else 0)
    if rejected_collisions:
        print(f"tuplet-detection=auto -- nudged {rejected_collisions} note(s) that would "
              f"otherwise have collided onto the same grid tick after their tuplet candidate "
              f"window was correctly rejected as not a clean tuplet", file=sys.stderr)

    half_placed = place_half_grid_notes(notes) if fast_notes == 'auto' else 0
    if half_placed:
        print(f"fast-notes=auto -- {half_placed} note(s) too quick for the 16th grid would have landed on "
              f"the same tick as their neighbor (merging into a chord, or vanishing if the pitch repeats); "
              f"written as 32nd notes instead. Pass --fast-notes off for the old behavior.", file=sys.stderr)

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
    treble_opt_stats, bass_opt_stats = {}, {}
    optimize_staff_durations(treble, temperature, bar_ticks, weights, treble_opt_stats,
                              melody_preservation=melody_preservation)
    optimize_staff_durations(bass, temperature, bar_ticks, weights, bass_opt_stats,
                              melody_preservation=melody_preservation)
    if ornament_detection == 'auto':
        ornaments = detect_ornaments(treble) + detect_ornaments(bass, id_start=1_000_000)
        if ornaments:
            kinds = defaultdict(list)
            for kind, land in ornaments:
                kinds[kind].append(land['f_start'] // bar_ticks + 1)
            listing = "; ".join(f"{len(bars)} {kind.replace('-', ' ')}{'s' if len(bars) != 1 else ''} "
                                f"(measure {', '.join(map(str, bars))})" for kind, bars in sorted(kinds.items()))
            print(f"ornament-detection=auto -- found {len(ornaments)} ornament figure(s): {listing}. "
                  f"{'Each is written as one note with its ornament sign in the MusicXML output; ' if xml_path else ''}"
                  f"MIDI output keeps the literal notes"
                  f"{'' if xml_path else ' (ornament signs only exist in MusicXML -- use --format musicxml or both to see them)'}.",
                  file=sys.stderr)
        else:
            print("ornament-detection=auto -- no mordents, turns or trills found", file=sys.stderr)

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
    if len(tempo_curve) > 1:
        last_tick = 0
        for tick, raw_tempo in tempo_curve:
            tempo_track.append(MetaMessage('set_tempo', tempo=raw_tempo, time=max(0, tick - last_tick)))
            last_tick = tick
    else:
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

    if midi_path:
        out.save(midi_path)
        print(f"Saved {midi_path}", file=sys.stderr)

    if xml_path:
        if key is None:
            fifths, mode, runner = estimate_key(notes)
            close = (f" {key_name(*runner)} fits nearly as well -- pass --key if the signature looks wrong."
                     if runner else "")
            print(f"No --key given -- estimated key: {key_name(fifths, mode)}, from which pitches the "
                  f"piece spends its time on.{close}", file=sys.stderr)
        else:
            fifths, mode = key
            print(f"--key given -- writing in {key_name(fifths, mode)}", file=sys.stderr)
        spell_staff(treble, fifths, mode)
        spell_staff(bass, fifths, mode)
        title = os.path.splitext(os.path.basename(input_path))[0]
        mx = write_musicxml(xml_path, treble, bass, time_sig, bar_ticks, tempo,
                            [] if beat_tracked else tempo_curve, fifths, mode,
                            title=title, max_voices=max_voices, dotted=(duration_style == 'dotted'))
        print(f"\n===== MusicXML =====", file=sys.stderr)
        print(f"{mx['measures']} measures, up to {mx['voices']} voice(s) per staff (--max-voices {max_voices})",
              file=sys.stderr)
        if mx['trimmed']:
            print(f"  {mx['trimmed']} note(s) that only just overlap the next one (held over by the fingers or "
                  f"pedal) are written ending where that next note starts", file=sys.stderr)
        if mx['shortened'] or mx['merged']:
            joined = f"; {mx['merged']} note(s) joined a chord starting at the same time" if mx['merged'] else ""
            print(f"  {mx['shortened']} more note(s) are cut short because they overlap more notes than "
                  f"{max_voices} voice(s) per staff can show -- raise --max-voices to keep them{joined}",
                  file=sys.stderr)
        if mx['trimmed'] or mx['shortened']:
            print("  (the MIDI output keeps every note's full length)", file=sys.stderr)
        if mx['tuplets'] or mx['tuplets_flattened']:
            flat = (f"; {mx['tuplets_flattened']} tuplet group(s) straddle a barline and were written as "
                    f"plain fast notes instead" if mx['tuplets_flattened'] else "")
            print(f"  {mx['tuplets']} tuplet bracket(s) written{flat}", file=sys.stderr)
        if mx['ornaments']:
            print(f"  {mx['ornaments']} ornament sign(s) written", file=sys.stderr)
        if mx['dropped']:
            print(f"  WARNING: {mx['dropped']} note(s) could not be placed and were left out", file=sys.stderr)
        print(f"Saved {xml_path}", file=sys.stderr)
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
    tick_scale = TICKS_PER_BEAT / mid.ticks_per_beat if mid.ticks_per_beat != TICKS_PER_BEAT else 1.0

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
    if tick_scale != 1.0:
        for n in notes:
            n['start'] = round(n['start'] * tick_scale)
            n['end'] = round(n['end'] * tick_scale)
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
    output_path = _prompt("Output file path", default=default_output)

    print("\nOutput format: 'midi' is the usual grand-staff MIDI file. 'musicxml' writes a")
    print("MusicXML score instead, which carries every decision made here (staff per hand,")
    print("note lengths and ties, key signature, tuplet brackets, ornament signs) straight")
    print("into MuseScore instead of leaving its MIDI import to guess them again. 'both'")
    print("writes the two side by side.")
    inferred_format = 'midi' if resolve_output_paths(output_path)[0] else 'musicxml'
    output_format = _prompt("Output format (midi/musicxml/both)", default=inferred_format,
                             validate=lambda v: (v in ('midi', 'musicxml', 'both'),
                                                  "Enter 'midi', 'musicxml', or 'both'."))
    wants_xml = output_format != 'midi'
    key, ornament_detection, max_voices = None, 'off', 2   # advanced options below may change these

    tempo_curve_preview = read_tempo_curve(mid, tick_scale)
    tempo_default = None  # only meaningful for the flat-tempo branches below
    trust_tempo_curve = False  # only set True if user explicitly preserves an erratic curve anyway
    if len(tempo_curve_preview) > 1:
        bpms = [round(mido.tempo2bpm(t), 1) for _, t in tempo_curve_preview]
        is_erratic, ratio, max_jump = assess_tempo_curve(tempo_curve_preview)
        print(f"\nSource file has {len(tempo_curve_preview)} tempo changes "
              f"({min(bpms):.1f}-{max(bpms):.1f} BPM).")
        if is_erratic:
            print(f"This curve looks erratic ({ratio:.1f}x between its slowest and fastest event, "
                  f"up to a {max_jump:.1f} BPM jump between adjacent events) -- more likely "
                  f"transcription timing jitter mislabeled as tempo changes than genuine macro "
                  f"tempo direction (verse/chorus rubato, a real accelerando, etc). Its beat "
                  f"positions can't be trusted either, so the default is to set it aside and work "
                  f"the beats out from when the notes were actually played.")
        else:
            print("Preserve that full tempo curve in the output (recommended), or flatten the whole")
            print("piece to one tempo? Flattening will make faster/slower passages sound wrong")
            print("relative to each other, since they'll all play back at the same single speed.")
        if _prompt_bool("Preserve the full tempo curve?", default=not is_erratic):
            tempo = None
            trust_tempo_curve = is_erratic  # explicit override -- tell run() not to re-discard it
        elif is_erratic and _prompt_bool("Work the beats out from the playing itself (recommended)?", default=True):
            tempo = None    # run() beat-tracks an untrusted curve when no tempo is given
        else:
            if is_erratic:
                # Curve itself is untrustworthy -- don't derive the flat default from it either.
                # Same fallback as the no-curve case: source's own tempo if it looks real, else
                # rhythm-based estimate (not raw rhythm estimation alone, which can land on a
                # fast subdivision rather than the true beat tempo on dense/ornamental material).
                tempo_default, _ = estimate_representative_tempo(mid, notes)
            else:
                tempo_default = min(bpms)
            tempo = _prompt("Flat tempo (BPM)", default=tempo_default, cast=float)
    else:
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
    if tempo is not None and tempo != tempo_default:
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

        print("\nTuplet detection [experimental]: finds local n-tuplet bursts")
        print("(quintuplets, septuplets, nontuplets, etc. against a 1- or 2-beat")
        print("span), plus smaller 2-3 note clusters too close together for the")
        print("grid to tell apart (e.g. a quick melodic turn/mordent), directly")
        print("in the source, and snaps just those onsets to exact even division")
        print("instead of each one rounding independently to the --grid above.")
        print("Independent of --grid; leaves everything outside a detected group")
        print("on plain grid quantization.")
        tuplet_detection = 'auto' if _prompt_bool("Enable tuplet detection?", default=False) else 'off'
        if tuplet_detection == 'auto':
            print(f"\nTuplet fit tolerance: 'relative' (recommended) requires a candidate group "
                  f"to fit within {TUPLET_FIT_TOLERANCE_PCT*100:.0f}% of its own subdivision width -- "
                  f"a tightly-packed group (small span, high N) is held to a proportionally stricter "
                  f"standard, so one badly-fitting member's real recorded timing can't get silently "
                  f"overridden just because the group's average looks fine. 'absolute' uses the older "
                  f"flat {TUPLET_FIT_TOLERANCE_TICKS}-tick threshold regardless of subdivision width.")
            tuplet_fit_mode = _prompt("Tuplet fit tolerance (relative/absolute)", default="relative",
                                       validate=lambda v: (v in ("relative", "absolute"),
                                                            "Enter 'relative' or 'absolute'."))
        else:
            tuplet_fit_mode = 'relative'

        print("\nOrnament detection [experimental]: recognizes mordents, turns and trills")
        print("(very quick stepwise figures around one note) and writes each as a single")
        print("note with its ornament sign. Only shows up in MusicXML output; the MIDI")
        print("file keeps the literal notes either way.")
        ornament_detection = 'auto' if _prompt_bool("Enable ornament detection?", default=False) else 'off'

        if wants_xml:
            print("\nKey for the MusicXML score: decides the key signature and how notes are")
            print("spelled (C# or Db). Leave blank to estimate it from the notes, or type a")
            print("key like A, F#m, 'Bb minor', or a number of sharps/flats like -3.")
            while True:
                raw_key = input("Key [estimate]: ").strip()
                if not raw_key:
                    break
                try:
                    key = parse_key(raw_key)
                    break
                except ValueError as e:
                    print(f"  {e}")
            print("\nVoices per staff in the MusicXML score: notes overlapping more deeply")
            print("than this are written shorter, ending where the next note in their voice")
            print("starts. 2 suits most piano music; 1 is the plainest read.")
            max_voices = _prompt("Max voices per staff (1-4)", default=2, cast=int,
                                  validate=lambda v: (1 <= v <= 4, "Enter a number from 1 to 4."))
    else:
        melody_preservation = False
        dynamic_split = False
        split_window_bars = 8
        hand_assignment = False
        max_hand_span, hand_ambiguity_zone = 16, 3
        tuplet_detection = 'off'
        tuplet_fit_mode = 'relative'

    print()
    run(input_path, output_path, tempo, split_pitch, temperature, time_sig,
        pedal_mode, min_note_ticks, playback_sustain, grid_mode,
        ','.join(map(str, track_indices)), channel_override,
        duration_style, min_velocity, velocity_mode, velocity_scale,
        tie_weight, rest_weight, artic_weight, preserve_source_duration,
        melody_preservation, dynamic_split, split_window_bars,
        hand_assignment, max_hand_span, hand_ambiguity_zone,
        tuplet_detection, trust_tempo_curve, tuplet_fit_mode,
        output_format, key, ornament_detection, max_voices)


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
    ap.add_argument('--tuplet-detection', choices=['auto', 'off'], default='off',
                     help='[experimental, default off -- not yet validated against real pieces, '
                          'same status as --melody-preservation/--dynamic-split/--hand-assignment] '
                          '"auto": detect local n-tuplet bursts (quintuplets, septuplets, nontuplets, '
                          'etc. against a 1- or 2-beat span), plus smaller 2-3 note clusters too close '
                          'together for the outer grid to tell apart (e.g. a quick melodic '
                          'turn/mordent), directly in the source, independent of --grid, and quantize '
                          'just those onsets to exact even division instead of snapping each one '
                          'independently to the outer grid -- fixes uneven durations on genuinely fast '
                          'ornamental material. "off" (default): use only the --grid straight/triplet '
                          'grid everywhere.')
    ap.add_argument('--tuplet-fit-tolerance', choices=['relative', 'absolute'], default='relative',
                     help=(f'Only relevant with --tuplet-detection auto. "relative" (default): a '
                           f'candidate n-tuplet group must fit within {TUPLET_FIT_TOLERANCE_PCT * 100:.0f}%% '
                           f'of its own subdivision width -- a tightly-packed group (small span, high N) '
                           f'is held to a proportionally stricter standard than a loosely-packed one, so a '
                           f'single badly-fitting member (real recorded nuance) cannot slip through just '
                           f'because the group average looks fine. "absolute": the older flat '
                           f'{TUPLET_FIT_TOLERANCE_TICKS}-tick threshold regardless of subdivision width, '
                           f'for anyone who wants the more lenient original behavior.'))
    ap.add_argument('--trust-tempo-curve', action='store_true',
                     help='When --tempo is omitted and the source has a multi-event tempo curve that '
                          'looks erratic, the default is to set it aside and find the beats from the '
                          'real note timing instead (see --beat-tracking). Pass this to keep the '
                          'source\'s curve and its beat positions exactly as they are.')
    ap.add_argument('--beat-tracking', choices=['auto', 'on', 'off'], default='auto',
                     help='Work out where the beats fall from when the notes were actually played, instead '
                          'of trusting the beat positions in the source file. "auto" (default): do this when '
                          'the source has a tempo map that looks erratic. "on": always. "off": never (an '
                          'erratic tempo map is then flattened to one tempo, as before). Ignored when '
                          '--tempo is given.')
    ap.add_argument('--fast-notes', choices=['auto', 'off'], default='auto',
                     help='"auto" (default): two notes that are too quick for the 16th-note grid and would '
                          'land on the same tick (merging into a chord, or losing a note if the pitch '
                          'repeats) are written as 32nd notes instead. "off": leave them on the 16th grid.')
    ap.add_argument('--format', dest='output_format', choices=['midi', 'musicxml', 'both'], default=None,
                     help='What to write. "midi": the grand-staff MIDI file. "musicxml": a MusicXML score '
                          'that carries every engraving decision made here straight into notation software '
                          '(staff per hand, note lengths and ties, key signature and note spelling, tuplet '
                          'brackets, ornament signs) instead of leaving its MIDI importer to guess them '
                          'again. "both": the two side by side under the same name. Default: decided by '
                          'the output file\'s extension -- .musicxml or .xml means MusicXML, anything else '
                          'MIDI.')
    ap.add_argument('--key', default=None, metavar='KEY',
                     help='MusicXML only. Key to write the score in: a name like A, F#m, "Bb minor", or a '
                          'number of sharps (positive) / flats (negative) like -3. Decides the key signature '
                          'and how notes are spelled (C# or Db). Default: estimated from the notes.')
    ap.add_argument('--max-voices', type=int, default=2, choices=[1, 2, 3, 4], metavar='N',
                     help='MusicXML only. Most independent voices to write per staff (1-4, default 2). '
                          'Notes overlapping more deeply than this are written shorter, ending where the '
                          'next note in their voice starts.')
    ap.add_argument('--ornament-detection', choices=['auto', 'off'], default='off',
                     help='[experimental, default off] "auto": recognize mordents, turns and trills -- very '
                          'quick stepwise figures around one note -- and write each as a single note with '
                          'its ornament sign in MusicXML output. MIDI output is not changed by this.')
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
    key = None
    if args.key is not None:
        try:
            key = parse_key(args.key)
        except ValueError as e:
            ap.error(f"--key: {e}")

    run(args.input, args.output, args.tempo, args.split_pitch, args.tie_temperature, time_sig,
        args.pedal_mode, args.min_note_ticks, args.playback_sustain == 'on', args.grid,
        args.track, args.channel, args.clean_durations, args.min_velocity,
        args.velocity_mode, args.velocity_scale,
        args.tie_weight, args.rest_weight, args.articulation_weight,
        args.tempo_rescale == 'preserve-duration', args.melody_preservation == 'on',
        args.dynamic_split == 'on', args.split_window_bars,
        args.hand_assignment == 'on', args.max_hand_span, args.hand_ambiguity_zone,
        args.tuplet_detection, args.trust_tempo_curve, args.tuplet_fit_tolerance,
        args.output_format, key, args.ornament_detection, args.max_voices, args.beat_tracking,
        args.fast_notes)


if __name__ == '__main__':
    main()
