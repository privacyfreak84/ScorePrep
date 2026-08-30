#!/usr/bin/env python3
"""
reduce_test.py -- prototype rule-based multi-track -> reduced piano MIDI.

Input: one multi-track MIDI (e.g. vocals+guitars merged, drums removed).
Output: single-track piano-density MIDI, thinned to <= max-chord-size notes
per slice, with repeated identical chord stabs collapsed into sustains.

This is a throwaway prototype to validate the core reduction rules before
any repo/architecture decisions. No staff-splitting/engraving here -- feed
the output into ScorePrep for that.
"""
import argparse
import os
import sys
import mido


class ReduceTestError(Exception):
    """Expected, user-facing failure (bad file, bad args) -- caught at the
    top level and printed as a clean one-line message instead of a raw
    traceback. Anything else propagating out is treated as a real bug."""


def load_notes(path):
    if not os.path.isfile(path):
        raise ReduceTestError(f"Input file not found: {path}")
    try:
        mid = mido.MidiFile(path)
    except Exception as e:
        raise ReduceTestError(f"Couldn't read '{path}' as a MIDI file: {e}")

    ppq = mid.ticks_per_beat
    tempo = None  # microseconds per beat, first set_tempo found in the file
    notes = []  # (start_tick, end_tick, pitch, velocity, track_idx, track_name)
    for ti, track in enumerate(mid.tracks):
        name = ""
        abs_t = 0
        active = {}  # pitch -> (start_tick, velocity)
        for msg in track:
            abs_t += msg.time
            if msg.type == "track_name":
                name = msg.name
            elif msg.type == "set_tempo" and tempo is None:
                tempo = msg.tempo
            elif msg.type == "note_on" and msg.velocity > 0:
                if getattr(msg, "channel", None) == 9:
                    continue  # drum channel: not pitched, exclude from reduction
                active[msg.note] = (abs_t, msg.velocity)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                if msg.note in active:
                    st, vel = active.pop(msg.note)
                    notes.append([st, abs_t, msg.note, vel, ti, name])
        for pitch, (st, vel) in active.items():
            notes.append([st, abs_t, pitch, vel, ti, name])
    notes.sort(key=lambda n: (n[0], -n[2]))
    return notes, ppq, tempo


def cluster_slices(notes, tolerance_ticks):
    slices = []
    cur = []
    cur_start = None
    for n in notes:
        st = n[0]
        if cur_start is None or st - cur_start <= tolerance_ticks:
            if cur_start is None:
                cur_start = st
            cur.append(n)
        else:
            slices.append((cur_start, cur))
            cur_start = st
            cur = [n]
    if cur:
        slices.append((cur_start, cur))
    return slices


def notes_to_slices(notes, tolerance_ticks):
    slices = cluster_slices(notes, tolerance_ticks)
    return [(st, dedupe_pitch(ns)) for st, ns in slices]


def collapse_per_track(notes, tolerance_ticks, gap_ticks):
    """Pipeline B: collapse each source track's own repeated chords in isolation,
    before merging with other tracks. Hypothesis: accompaniment self-repetition
    (e.g. hammered strums) should simplify regardless of what other tracks do.
    Caveat (per ChatGPT): can wrongly erase an intentionally sparse/interlocking
    part if that track's own repeats are actually doing rhythmic work together
    with another track. Benchmark against post-merge collapse before trusting."""
    by_track = {}
    for n in notes:
        by_track.setdefault(n[4], []).append(n)

    out = []
    for ti, tnotes in by_track.items():
        slices = notes_to_slices(tnotes, tolerance_ticks)
        collapsed = collapse_repeats(slices, gap_ticks)
        out.extend(collapsed)
    out.sort(key=lambda n: (n[0], -n[2]))
    return out


def filter_short_notes(notes, min_ticks):
    if min_ticks <= 0:
        return notes
    return [n for n in notes if (n[1] - n[0]) >= min_ticks]


def collapse_glides(notes, glide_tracks, max_note_ticks, max_gap_ticks, max_interval):
    """Merge short chromatic-step run of notes on a given track into one note
    at the run's final (settled) pitch. Targets pitch-tracked vocal glide/
    vibrato artifacts: several brief adjacent-semitone blips resolving to the
    intended sung note. Opt-in per track via --glide-tracks; changes pitch
    content, so off by default."""
    if not glide_tracks:
        return notes
    by_track = {}
    for n in notes:
        by_track.setdefault(n[4], []).append(n)

    out = []
    for ti, tnotes in by_track.items():
        name = tnotes[0][5] if tnotes else ""
        if not any(matches_track(key, ti, name) for key in glide_tracks):
            out.extend(tnotes)
            continue
        tnotes = sorted(tnotes, key=lambda n: n[0])
        run = [tnotes[0]]
        for n in tnotes[1:]:
            prev = run[-1]
            prev_dur = prev[1] - prev[0]
            gap = n[0] - prev[1]
            if prev_dur <= max_note_ticks and gap <= max_gap_ticks and abs(n[2] - prev[2]) <= max_interval:
                run.append(n)
            else:
                out.append(_flush_glide_run(run))
                run = [n]
        out.append(_flush_glide_run(run))
    out.sort(key=lambda n: (n[0], -n[2]))
    return out


def _flush_glide_run(run):
    if len(run) == 1:
        return run[0]
    first, last = run[0], run[-1]
    return [first[0], last[1], last[2], last[3], last[4], last[5]]


def dedupe_pitch(slice_notes):
    """Collapse duplicate note-on artifacts for the same pitch -- but only
    within the same source track. Two different instruments legitimately
    playing the same pitch at the same time (a unison) is real musical
    content, not a duplicate, and must not be discarded here."""
    best = {}
    for n in slice_notes:
        st, et, pitch, vel, ti, name = n
        key = (pitch, ti)
        dur = et - st
        if key not in best or dur > (best[key][1] - best[key][0]):
            best[key] = n
    return list(best.values())


def _norm(s):
    return (s or "").lower().replace("_", " ").replace("-", " ")


# Sentinel prefix for "match this exact track index," used when a track has
# no name to match by. Falling back to str(idx) as a substring (e.g. "1")
# would risk accidentally matching an unrelated but similarly-named track
# ("Guitar 1", "Verse 1 Vocal") -- an exact-index match can't collide.
_TRACK_IDX_PREFIX = "\x00idx:"


def make_track_key(idx, name):
    """The string stored for --melody-track/--bass-track/--track-priority/
    --glide-tracks when a track is selected by number (interactive mode) --
    its own name if it has one (substring-matched as normal), otherwise an
    exact-index sentinel instead of a bare number."""
    return name if name else f"{_TRACK_IDX_PREFIX}{idx}"


def matches_track(key, ti, name):
    if key.startswith(_TRACK_IDX_PREFIX):
        try:
            return ti == int(key[len(_TRACK_IDX_PREFIX):])
        except ValueError:
            return False
    return _norm(key) in _norm(name)


def build_movement_map(notes, ppq):
    """id(note) -> True if this note's pitch differs from the previous note
    in the same track (melodic motion), False if it's a repeat of the
    immediately preceding pitch (static/filler). First note per track counts
    as movement. Computed once per pipeline run, on whatever note set is
    about to be thinned (post-collapse in --pipeline pre, since collapse
    already removes most literal repeats there)."""
    by_track = {}
    for n in notes:
        by_track.setdefault(n[4], []).append(n)
    moved = {}
    for tnotes in by_track.values():
        tnotes = sorted(tnotes, key=lambda n: n[0])
        prev_pitch = None
        for n in tnotes:
            moved[id(n)] = (prev_pitch is None or n[2] != prev_pitch)
            prev_pitch = n[2]
    return moved


def note_importance(note, slice_min, slice_max, moved_map, ppq, weights):
    span = max(1, slice_max - slice_min)
    pitch_rank = (note[2] - slice_min) / span
    duration_norm = min(1.0, (note[1] - note[0]) / ppq)
    velocity_norm = note[3] / 127.0
    movement = 1.0 if moved_map.get(id(note), True) else 0.0
    w_pitch, w_dur, w_vel, w_move = weights
    return (w_pitch * pitch_rank + w_dur * duration_norm
            + w_vel * velocity_norm + w_move * movement)


def priority_rank(track_priority, name, ti):
    if not track_priority:
        return ti
    for i, key in enumerate(track_priority):
        if matches_track(key, ti, name):
            return i
    return len(track_priority) + ti


def thin_slice(slice_notes, max_chord_size, track_priority, melody_track, bass_track,
               stats=None, moved_map=None, ppq=480, importance_weights=(0.4, 0.25, 0.15, 0.2)):
    melody = None
    bass = None
    if melody_track:
        cands = [n for n in slice_notes if matches_track(melody_track, n[4], n[5])]
        if cands:
            melody = max(cands, key=lambda n: n[2])
            if stats is not None:
                stats["melody_present"] += 1
    if bass_track:
        cands = [n for n in slice_notes if matches_track(bass_track, n[4], n[5])]
        if cands:
            bass = min(cands, key=lambda n: n[2])
            if stats is not None:
                stats["bass_present"] += 1

    top_pitch = max(n[2] for n in slice_notes)
    bottom_pitch = min(n[2] for n in slice_notes)
    if melody is not None and melody[2] < top_pitch and stats is not None:
        stats["buried_melody"] += 1  # designated melody isn't the top voice: register collision
    if bass is not None and bass[2] > bottom_pitch and stats is not None:
        stats["buried_bass"] += 1

    if melody is None:
        melody = max(slice_notes, key=lambda n: n[2])
    if bass is None:
        bass = min(slice_notes, key=lambda n: n[2])
        if bass[2] == melody[2]:
            rest = [n for n in slice_notes if n is not melody]
            bass = min(rest, key=lambda n: n[2]) if rest else None

    if len(slice_notes) <= max_chord_size:
        return sorted(slice_notes, key=lambda n: n[2])

    if stats is not None:
        if melody_track and any(matches_track(melody_track, n[4], n[5]) for n in slice_notes):
            stats["melody_locked"] += 1
        if bass_track and any(matches_track(bass_track, n[4], n[5]) for n in slice_notes):
            stats["bass_locked"] += 1

    kept = [melody]
    if bass is not None and bass[2] != melody[2]:
        kept.append(bass)
    kept_pitches = {n[2] for n in kept}

    remaining = [n for n in slice_notes if n[2] not in kept_pitches]
    ranks = [priority_rank(track_priority, n[5], n[4]) for n in remaining]
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    order = sorted(range(len(remaining)),
                    key=lambda i: (ranks[i],
                                    -note_importance(remaining[i], bottom_pitch, top_pitch,
                                                      moved_map, ppq, importance_weights)
                                    if moved_map is not None else -remaining[i][2]))

    for i in order:
        if len(kept) >= max_chord_size:
            break
        n = remaining[i]
        kept.append(n)
        if stats is not None:
            explicit_match = track_priority and ranks[i] < len(track_priority)
            if explicit_match and rank_counts[ranks[i]] == 1:
                # priority uniquely singled this note out -- no tie to break
                stats["filled_by_priority"] += 1
            elif moved_map is not None:
                # either no explicit priority, or priority tied among several
                # candidates -- importance score is what actually chose
                stats["filled_by_importance"] += 1
            else:
                stats["filled_by_pitch"] += 1

    return sorted(kept, key=lambda n: n[2])


def collapse_repeats(slices, gap_ticks):
    """Merge consecutive slices with identical pitch sets into sustained notes."""
    if not slices:
        return []
    out = []
    prev_start, prev_notes = slices[0]
    prev_set = frozenset(n[2] for n in prev_notes)
    prev_end = max(n[1] for n in prev_notes)
    run = [(prev_start, prev_notes)]

    def flush(run):
        start = run[0][0]
        notes_by_pitch = {}
        for _, ns in run:
            for n in ns:
                p = n[2]
                if p not in notes_by_pitch or n[1] > notes_by_pitch[p][1]:
                    notes_by_pitch[p] = n
        result = []
        for n in notes_by_pitch.values():
            result.append([start, n[1], n[2], n[3], n[4], n[5]])
        return result

    for st, ns in slices[1:]:
        cur_set = frozenset(n[2] for n in ns)
        gap = st - prev_end
        if cur_set == prev_set and gap <= gap_ticks:
            run.append((st, ns))
            prev_end = max(prev_end, max(n[1] for n in ns))
        else:
            out.extend(flush(run))
            run = [(st, ns)]
            prev_set = cur_set
            prev_end = max(n[1] for n in ns)
    out.extend(flush(run))
    return out


def write_midi(notes, ppq, out_path, target_ppq=None, tempo=None):
    """target_ppq: rescale all tick values to this resolution before writing
    (ScorePrep's grid/quantization math assumes a fixed ticks-per-beat, so
    the output must match it rather than passing the source's own ppq
    through untouched). tempo: source's real set_tempo (microsec/beat), if
    known -- written into the output so downstream tools see genuine tempo
    info instead of falling back to a generic-encoding-tempo assumption."""
    scale = (target_ppq / ppq) if (target_ppq and target_ppq != ppq) else 1.0
    out_ppq = target_ppq or ppq

    events = []
    for st, et, pitch, vel, ti, name in notes:
        st2 = round(st * scale)
        et2 = round(et * scale)
        events.append((st2, 1, mido.Message("note_on", note=pitch, velocity=vel, time=0)))
        events.append((et2, 0, mido.Message("note_off", note=pitch, velocity=0, time=0)))
    events.sort(key=lambda e: (e[0], e[1]))

    mid = mido.MidiFile(ticks_per_beat=out_ppq)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    if tempo is not None:
        track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    last = 0
    for t, _, msg in events:
        msg.time = t - last
        track.append(msg)
        last = t
    mid.save(out_path)


def chord_size_stats(slices):
    sizes = [len(ns) for _, ns in slices if ns]
    if not sizes:
        return 0.0, 0
    return sum(sizes) / len(sizes), max(sizes)


def avg_hand_span(slices):
    spans = [max(n[2] for n in ns) - min(n[2] for n in ns) for _, ns in slices if ns]
    if not spans:
        return 0.0
    return sum(spans) / len(spans)


def track_coverage_pct(track_key, source_notes, final_notes):
    """% of source_notes (matched by track-name substring) that still have at
    least one same-track note overlapping their original time range in the
    final output. A rough 'did this line survive' measure -- not exact
    (collapse/thin can shift onsets slightly), but tracks preservation trend."""
    src = [n for n in source_notes if matches_track(track_key, n[4], n[5])]
    if not src:
        return None
    fin = [n for n in final_notes if matches_track(track_key, n[4], n[5])]
    covered = 0
    for s in src:
        if any(f[0] < s[1] and f[1] > s[0] for f in fin):
            covered += 1
    return 100.0 * covered / len(src)


def per_track_kept_stats(cleaned_notes, final_notes):
    totals, names = {}, {}
    for n in cleaned_notes:
        totals[n[4]] = totals.get(n[4], 0) + 1
        names[n[4]] = n[5] or f"track {n[4]}"
    kept = {}
    for n in final_notes:
        kept[n[4]] = kept.get(n[4], 0) + 1
    return totals, kept, names


def print_reduction_stats(raw_notes, cleaned_notes, collapsed_notes, pre_thin_slices,
                           post_thin_slices, final_notes, pipeline, melody_track, bass_track,
                           stats, no_collapse=False):
    avg_pre, max_pre = chord_size_stats(pre_thin_slices)
    avg_post, max_post = chord_size_stats(post_thin_slices)

    print()
    print("===== Reduction =====")
    print(f"Input notes (raw):          {len(raw_notes)}")
    print(f"After noise/glide filter:   {len(cleaned_notes)}")
    if no_collapse:
        print("Repeated attacks collapsed: disabled (--no-collapse)")
    elif pipeline == "pre":
        removed = len(cleaned_notes) - len(collapsed_notes)
        print(f"Repeated attacks collapsed: {removed}  (pre-merge per-track collapse)")
    else:
        print("Repeated attacks collapsed: n/a (--pipeline post -- collapse happens after "
              "thinning, see final note count instead)")
    print(f"Output notes:               {len(final_notes)}")
    print(f"Average chord size:         {avg_pre:.1f} -> {avg_post:.1f}")
    print(f"Maximum chord size:         {max_pre} -> {max_post}")
    print(f"Average hand span (semitones): {avg_hand_span(post_thin_slices):.1f}")

    print()
    print("===== Musical Preservation =====")
    print("(kept / total note events from that track -- see note below)")
    totals, kept, names = per_track_kept_stats(cleaned_notes, final_notes)
    for ti in sorted(totals, key=lambda t: -totals[t]):
        k, t = kept.get(ti, 0), totals[ti]
        print(f"  {names[ti]:<20s} {k:5d} / {t:<5d} ({100*k/t:.0f}%)")
    if melody_track:
        cov = track_coverage_pct(melody_track, cleaned_notes, final_notes)
        if cov is not None:
            print(f"  melody preserved (time coverage): {cov:.1f}%")
    if bass_track:
        cov = track_coverage_pct(bass_track, cleaned_notes, final_notes)
        if cov is not None:
            print(f"  bass preserved (time coverage):   {cov:.1f}%")
    print("  (the per-track counts above are individual note events -- collapse can")
    print("   merge several repeated notes into one longer note, which lowers this")
    print("   count even when melody/bass preserved shows 100% -- that one checks")
    print("   time coverage, not note-for-note survival)")

    if melody_track or bass_track:
        print()
        print("===== Register =====")
        if melody_track:
            natural = stats["melody_present"] - stats["buried_melody"]
            print(f"Melody naturally on top:    {natural}")
            print(f"Melody rescued by lock:     {stats['buried_melody']}")
        if bass_track:
            natural = stats["bass_present"] - stats["buried_bass"]
            print(f"Bass naturally on bottom:   {natural}")
            print(f"Bass rescued by lock:       {stats['buried_bass']}")

    total_filled = (stats["filled_by_priority"] + stats["filled_by_importance"]
                    + stats["filled_by_pitch"])
    if stats["melody_locked"] or stats["bass_locked"] or total_filled:
        print()
        print("===== Chord Selection =====")
        print("(only counts over-budget chords that needed thinning)")
        print(f"Melody lock:        {stats['melody_locked']}")
        print(f"Bass lock:          {stats['bass_locked']}")
        print(f"Track priority:     {stats['filled_by_priority']}")
        print(f"Importance score:   {stats['filled_by_importance']}")
        print(f"Plain pitch order:  {stats['filled_by_pitch']}")


def run(input_path, output_path, max_chord_size=5, tolerance_frac=1/16, collapse_gap_frac=0.5,
        no_collapse=False, pipeline="pre", track_priority=None, melody_track="", bass_track="",
        min_note_ticks=0, glide_tracks=None, glide_max_note_ticks=60, glide_max_gap_ticks=20,
        glide_max_interval=2, target_ppq=384, no_stats=False, no_importance_scoring=False,
        importance_weights=(0.4, 0.25, 0.15, 0.2)):
    track_priority = track_priority or []
    glide_tracks = glide_tracks or []

    notes, ppq, tempo = load_notes(input_path)
    if not notes:
        print(f"Warning: no pitched notes found in '{input_path}' -- writing an empty output file.",
              file=sys.stderr)
    tol = int(ppq * tolerance_frac)
    gap = int(ppq * collapse_gap_frac)

    raw_notes = notes
    cleaned_notes = filter_short_notes(raw_notes, min_note_ticks)
    cleaned_notes = collapse_glides(cleaned_notes, glide_tracks, glide_max_note_ticks,
                                     glide_max_gap_ticks, glide_max_interval)

    if pipeline == "pre" and not no_collapse:
        collapsed_notes = collapse_per_track(cleaned_notes, tol, gap)
    else:
        collapsed_notes = cleaned_notes

    pre_thin_slices = notes_to_slices(collapsed_notes, tol)
    stats = {"buried_melody": 0, "buried_bass": 0, "melody_present": 0, "bass_present": 0,
              "melody_locked": 0, "bass_locked": 0, "filled_by_priority": 0,
              "filled_by_importance": 0, "filled_by_pitch": 0}
    moved_map = None if no_importance_scoring else build_movement_map(collapsed_notes, ppq)
    post_thin_slices = [(st, thin_slice(ns, max_chord_size, track_priority,
                                         melody_track, bass_track, stats,
                                         moved_map, ppq, importance_weights))
                         for st, ns in pre_thin_slices]

    if pipeline == "post" and not no_collapse:
        final_notes = collapse_repeats(post_thin_slices, gap)
    else:
        final_notes = [n for _, ns in post_thin_slices for n in ns]

    out_dir = os.path.dirname(os.path.abspath(output_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
        write_midi(final_notes, ppq, output_path,
                   target_ppq=(target_ppq or None), tempo=tempo)
    except OSError as e:
        raise ReduceTestError(f"Couldn't write output file '{output_path}': {e}")

    total_in = len(collapsed_notes)
    total_out = len(final_notes)
    print(f"slices: {len(post_thin_slices)}  notes in: {total_in}  notes out: {total_out}  "
          f"({100 * total_out / max(total_in, 1):.0f}% kept)")
    if melody_track or bass_track:
        print(f"register collisions -- buried melody: {stats['buried_melody']}  "
              f"buried bass: {stats['buried_bass']}  (of {len(post_thin_slices)} slices)")

    if not no_stats:
        print_reduction_stats(raw_notes, cleaned_notes, collapsed_notes, pre_thin_slices,
                               post_thin_slices, final_notes, pipeline,
                               melody_track, bass_track, stats, no_collapse)

    print(f"\nSaved {output_path}")


def list_tracks(path):
    """Mirror ScorePrep's 'Found N track(s)' summary: track index, name,
    pitched note count (drum channel excluded), so a person can identify
    melody/bass/priority tracks by number without knowing raw file
    internals."""
    notes, ppq, tempo = load_notes(path)
    counts = {}
    names = {}
    for n in notes:
        counts[n[4]] = counts.get(n[4], 0) + 1
        names[n[4]] = n[5]
    order = sorted(counts, key=lambda ti: -counts[ti])
    busiest = order[0] if order else None
    print(f"\nFound {len(counts)} track(s) with pitched notes:")
    for ti in sorted(counts):
        tag = "  <- most notes" if ti == busiest else ""
        label = names[ti] or "(unnamed)"
        print(f"  track {ti}: \"{label}\" -- {counts[ti]} note(s){tag}")
    return counts, names


def _prompt(msg, default=None, cast=str, validate=None):
    """validate, if given, takes the cast value and returns (ok, error_msg)
    -- re-prompts on both a bad cast and a failed validation instead of
    silently accepting an out-of-range value (e.g. a negative chord size)."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{msg}{suffix}: ").strip()
        if not raw:
            return default
        try:
            value = cast(raw)
        except ValueError:
            print(f"  Couldn't read that as a {cast.__name__}, try again.")
            continue
        if validate is not None:
            ok, err = validate(value)
            if not ok:
                print(f"  {err}")
                continue
        return value


def _valid_positive(n):
    return (n >= 1, "Must be a positive number.")


def _valid_nonneg(n):
    return (n >= 0, "Must be zero or a positive number.")


def _prompt_yn(msg, default=True):
    d = "Y/n" if default else "y/N"
    raw = input(f"{msg} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _prompt_track_by_number(msg, counts, names, ppq_for_none=None):
    """Ask for a track by number (from list_tracks output) and return a key
    for melody-track/bass-track/track-priority matching -- the track's own
    name if it has one, otherwise an exact-index key (see make_track_key).
    Blank = skip/none."""
    raw = input(f"{msg} (track number, or Enter to skip): ").strip()
    if not raw:
        return ""
    try:
        idx = int(raw)
    except ValueError:
        print("  Not a number -- skipping.")
        return ""
    if idx not in names:
        print(f"  No track {idx} -- skipping.")
        return ""
    return make_track_key(idx, names[idx])


def interactive_main():
    print("=" * 60)
    print("reduce_test.py -- interactive mode")
    print("Collapses a merged multi-instrument MIDI (vocals/guitars/bass/")
    print("etc, drums excluded automatically) down to a single piano-")
    print("density part: fewer simultaneous notes, repeated chord stabs")
    print("simplified into held chords, melody/bass protected from being")
    print("silently dropped. Feed the result into ScorePrep next.")
    print("(press Enter to accept the [default] shown for any question)")
    print("=" * 60)

    input_path = _prompt("Input MIDI file path")
    while not input_path:
        input_path = _prompt("Input MIDI file path")

    counts, names = list_tracks(input_path)

    default_output = input_path.rsplit(".", 1)[0] + "_reduced.mid"
    output_path = _prompt("Output MIDI file path", default=default_output)

    print()
    print("Max chord size: how many notes can sound at once in the reduced")
    print("piano part. Lower = easier to actually play on two hands, at the")
    print("cost of thinning out dense chords more aggressively.")
    max_chord_size = _prompt("Max chord size", default=5, cast=int, validate=_valid_positive)

    print()
    print("Melody track: the track that should always be treated as the lead")
    print("line, even in moments where another instrument happens to play a")
    print("higher note at the same time (this prevents the real melody from")
    print("getting silently buried under a louder accompaniment part).")
    melody_track = _prompt_track_by_number("Melody track", counts, names)

    print()
    print("Bass track: same idea, but for the lowest voice.")
    bass_track = _prompt_track_by_number("Bass track", counts, names)

    print()
    print("Track priority: when a chord still has too many notes left over")
    print("after melody+bass are set aside, which remaining instrument's")
    print("notes should fill the leftover space first?")
    remaining_tracks = [ti for ti in sorted(counts)]
    print("  Tracks: " + ", ".join(f"{ti}={names[ti] or '(unnamed)'}" for ti in remaining_tracks))
    priority_raw = input("  Enter track numbers in priority order, comma-separated "
                          "(or Enter to skip): ").strip()
    track_priority = []
    if priority_raw:
        for tok in priority_raw.split(","):
            tok = tok.strip()
            if tok.isdigit() and int(tok) in names:
                track_priority.append(make_track_key(int(tok), names[int(tok)]))

    print()
    print("Pipeline: 'pre' simplifies each instrument's own repeated chords")
    print("first, then merges everyone together (recommended -- catches an")
    print("instrument hammering the same chord even while other instruments")
    print("keep moving). 'post' merges everything first and only simplifies")
    print("repeats afterward -- kept for comparison/debugging, but tested")
    print("worse so far.")
    pipeline = _prompt("Pipeline (pre/post)", default="pre")

    print()
    print("Voice-importance scoring: when filling leftover chord space,")
    print("prefer notes with more perceptual weight (higher pitch, longer")
    print("duration, louder, part of real melodic movement) instead of just")
    print("picking whichever note happens to be highest.")
    use_importance = _prompt_yn("Use voice-importance scoring?", default=True)

    show_advanced = _prompt_yn(
        "\nShow advanced options? (noise filtering, glide/vibrato cleanup, "
        "repeat-collapse tuning, output resolution, stats, importance weights)",
        default=False)

    no_collapse = False
    tolerance_frac = 1/16
    collapse_gap_frac = 0.5
    min_note_ticks = 0
    glide_tracks = []
    glide_max_note_ticks = 60
    glide_max_gap_ticks = 20
    glide_max_interval = 2
    target_ppq = 384
    no_stats = False
    importance_weights = (0.4, 0.25, 0.15, 0.2)

    if show_advanced:
        print()
        print("Collapse repeated chords: merges an instrument's own repeated")
        print("re-strikes of the same chord (e.g. a strummed guitar hitting")
        print("the same notes over and over) into one held chord instead of")
        print("many separate notes. Recommended: leave this ON. Only turn it")
        print("off if you specifically want every literal repeat kept as its")
        print("own separate note -- a rare, mostly-for-debugging case.")
        collapse_on = _prompt_yn("Collapse repeated chords? (recommended: yes)", default=True)
        no_collapse = not collapse_on

        if collapse_on:
            print()
            print("Onset clustering tolerance: how close together in time two")
            print("notes from different instruments have to start to count as")
            print("'the same musical moment' for chord-thinning purposes.")
            print("Given as a fraction of one beat -- smaller catches only")
            print("near-perfectly-aligned notes, larger is more forgiving of")
            print("small timing differences between instruments.")
            tolerance_frac = _prompt("Onset tolerance (fraction of a beat)",
                                      default=1/16, cast=float, validate=_valid_nonneg)

            print()
            print("Collapse gap: how much of a time gap between two identical")
            print("repeated chords is still short enough to merge into one")
            print("held chord. Given in beats.")
            collapse_gap_frac = _prompt("Collapse gap (beats)", default=0.5, cast=float, validate=_valid_nonneg)

        print()
        print("Minimum note length: raw notes shorter than this (in ticks)")
        print("are dropped as likely transcription noise. 0 = off.")
        min_note_ticks = _prompt("Minimum note length in ticks", default=0, cast=int, validate=_valid_nonneg)

        print()
        print("Glide/vibrato cleanup: pitch-tracked vocal transcription often")
        print("produces several tiny adjacent-pitch blips as a singer glides")
        print("or wavers into a note, instead of one clean note. This merges")
        print("those into a single note at the pitch the voice actually")
        print("settles on. Pick which track(s) this applies to (usually just")
        print("the vocal track) -- leave blank to disable.")
        glide_raw = input("  Track numbers, comma-separated (or Enter to skip): ").strip()
        if glide_raw:
            for tok in glide_raw.split(","):
                tok = tok.strip()
                if tok.isdigit() and int(tok) in names:
                    glide_tracks.append(make_track_key(int(tok), names[int(tok)]))
        if glide_tracks:
            glide_max_note_ticks = _prompt("  Max fragment length (ticks)", default=60, cast=int, validate=_valid_nonneg)
            glide_max_gap_ticks = _prompt("  Max gap between fragments (ticks)",
                                           default=20, cast=int, validate=_valid_nonneg)
            glide_max_interval = _prompt("  Max semitone step between fragments",
                                          default=2, cast=int, validate=_valid_nonneg)

        print()
        print("Output resolution (ticks-per-beat): 384 matches what ScorePrep")
        print("expects for its grid/quantization math -- change this only if")
        print("this file is NOT going into ScorePrep next.")
        target_ppq = _prompt("Output ticks-per-beat", default=384, cast=int, validate=_valid_nonneg)

        print()
        print("Voice-importance weights (pitch, duration, velocity, movement):")
        print("only relevant if voice-importance scoring is on above. These")
        print("are starting-point defaults, not yet validated against real")
        print("pieces -- change only if you want to experiment directly.")
        weights_raw = _prompt("Weights (comma-separated)", default="0.4,0.25,0.15,0.2")
        while True:
            try:
                importance_weights = parse_importance_weights(weights_raw)
                break
            except ReduceTestError as e:
                print(f"  {e}")
                weights_raw = _prompt("Weights (comma-separated)", default="0.4,0.25,0.15,0.2")

        print()
        no_stats = not _prompt_yn("Show detailed reduction stats at the end?", default=True)

    print()
    run(input_path, output_path, max_chord_size, tolerance_frac, collapse_gap_frac,
        no_collapse, pipeline, track_priority, melody_track, bass_track,
        min_note_ticks, glide_tracks, glide_max_note_ticks, glide_max_gap_ticks,
        glide_max_interval, target_ppq, no_stats, not use_importance, importance_weights)


def parse_importance_weights(raw, ap=None):
    """Parses '--importance-weights'/interactive weights input into exactly
    four non-negative floats. Raises ReduceTestError (or calls ap.error, if
    an argparse parser is given) on anything else -- a bad count or a
    negative weight used to fail deep inside note_importance's tuple
    unpack with an unhelpful traceback instead of here."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    err = None
    if len(parts) != 4:
        err = f"--importance-weights needs exactly 4 comma-separated values (pitch,duration,velocity,movement), got {len(parts)}: {raw!r}"
    else:
        try:
            weights = tuple(float(p) for p in parts)
        except ValueError:
            err = f"--importance-weights values must all be numbers, got: {raw!r}"
        else:
            if any(w < 0 for w in weights):
                err = f"--importance-weights values must all be zero or positive, got: {raw!r}"
    if err:
        if ap is not None:
            ap.error(err)
        raise ReduceTestError(err)
    return weights


def cli_main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output", nargs="?",
                     help="required unless --list-tracks is given")
    ap.add_argument("--list-tracks", action="store_true",
                     help="print the track listing (index, name, pitched note count) "
                          "and exit, without running the reduction -- use this first to "
                          "find melody/bass/priority track numbers")
    ap.add_argument("--max-chord-size", type=int, default=5)
    ap.add_argument("--tolerance-frac", type=float, default=1 / 16,
                     help="onset clustering tolerance, as a fraction of a beat")
    ap.add_argument("--collapse-gap-frac", type=float, default=0.5,
                     help="max gap (in beats) to still merge identical repeated chords")
    ap.add_argument("--no-collapse", action="store_true")
    ap.add_argument("--pipeline", choices=["pre", "post"], default="pre",
                     help="pre: collapse each track's own repeats before merging "
                          "(hypothesis, may erase intentional interlocking parts). "
                          "post: old behaviour, collapse only after merge+thin.")
    ap.add_argument("--track-priority", default="",
                     help="comma-separated substrings, highest priority first, "
                          "matched against track names")
    ap.add_argument("--melody-track", default="",
                     help="substring identifying the melody-lead track (e.g. 'voice'). "
                          "Forces that track's note as melody even if not the top pitch "
                          "in the slice -- fixes buried-melody register collisions.")
    ap.add_argument("--bass-track", default="",
                     help="substring identifying the bass track (e.g. 'electric bass').")
    ap.add_argument("--min-note-ticks", type=int, default=0,
                     help="drop notes shorter than this many ticks (noise filter)")
    ap.add_argument("--glide-tracks", default="",
                     help="comma-separated substrings of tracks to apply glide-run "
                          "collapse to (targets pitch-tracked vocal glide/vibrato "
                          "artifacts). Empty = disabled.")
    ap.add_argument("--glide-max-note-ticks", type=int, default=60,
                     help="max duration for a note to count as a glide fragment")
    ap.add_argument("--glide-max-gap-ticks", type=int, default=20,
                     help="max gap between fragments to still be one glide run")
    ap.add_argument("--glide-max-interval", type=int, default=2,
                     help="max semitone step between consecutive glide fragments")
    ap.add_argument("--target-ppq", type=int, default=384,
                     help="output ticks-per-beat -- match ScorePrep's assumed "
                          "resolution (384) so its grid/quantization math is "
                          "correct. Set 0 to pass the source's own ppq through "
                          "unchanged.")
    ap.add_argument("--no-stats", action="store_true",
                     help="skip the detailed reduction-stats block")
    ap.add_argument("--no-importance-scoring", action="store_true",
                     help="fall back to plain pitch-height ordering for filling "
                          "remaining chord slots, instead of the composite "
                          "pitch+duration+velocity+movement score (default on).")
    ap.add_argument("--importance-weights", default="0.4,0.25,0.15,0.2",
                     help="pitch,duration,velocity,movement weights (comma-separated, "
                          "advanced override -- defaults are reasonable starting points, "
                          "not yet benchmarked against real pieces)")
    args = ap.parse_args()

    if args.list_tracks:
        list_tracks(args.input)
        return

    if args.output is None:
        ap.error("output is required unless --list-tracks is given")
    if args.max_chord_size < 1:
        ap.error("--max-chord-size must be at least 1")
    if args.tolerance_frac < 0:
        ap.error("--tolerance-frac must be zero or positive")
    if args.collapse_gap_frac < 0:
        ap.error("--collapse-gap-frac must be zero or positive")
    if args.min_note_ticks < 0:
        ap.error("--min-note-ticks must be zero or positive")
    if args.glide_max_note_ticks < 0:
        ap.error("--glide-max-note-ticks must be zero or positive")
    if args.glide_max_gap_ticks < 0:
        ap.error("--glide-max-gap-ticks must be zero or positive")
    if args.glide_max_interval < 0:
        ap.error("--glide-max-interval must be zero or positive")
    if args.target_ppq < 0:
        ap.error("--target-ppq must be zero (passthrough) or positive")

    track_priority = [s.strip() for s in args.track_priority.split(",") if s.strip()]
    glide_tracks = [s.strip() for s in args.glide_tracks.split(",") if s.strip()]
    weights = parse_importance_weights(args.importance_weights, ap)

    run(args.input, args.output, args.max_chord_size, args.tolerance_frac,
        args.collapse_gap_frac, args.no_collapse, args.pipeline, track_priority,
        args.melody_track, args.bass_track, args.min_note_ticks, glide_tracks,
        args.glide_max_note_ticks, args.glide_max_gap_ticks, args.glide_max_interval,
        args.target_ppq, args.no_stats, args.no_importance_scoring, weights)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 1:
            interactive_main()
        else:
            cli_main()
    except ReduceTestError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)
