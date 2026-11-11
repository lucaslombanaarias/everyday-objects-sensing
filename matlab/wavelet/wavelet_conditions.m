function wavelet_conditions(opts)
%WAVELET_CONDITIONS Wavelet comparison of carpet against wood, bare against enclosed (2026-10-21).
%
%   wavelet_conditions()                 run from the repo root with matlab/wavelet on the path
%   wavelet_conditions(Figures=false)    CSVs only
%
%   For every 4 in drop (drop4in) and every reviewed walking step, transforms a window from
%   Pre before the event to Post after it (wavelet_window, which calls freq_inst_morlet) and
%   measures, in each band, the band level, the ring time, the ringing frequency and the
%   background's share of the window, and the overall peak (wavelet_band_metrics). The
%   parameters are in wavelet_params.
%
%   Events:
%   - Drops: one per trial (impacts_made is 1): the det-v1 event with the highest peak_db.
%     The other det-v1 events in a drop trial are handling noise before the drop or bounces
%     after it (checked by eye on all 37 trials; see README.md).
%   - Steps: rebuilt as walking_trials() in scripts/plot_floor_slide_figures.py does: the
%     det-v1 events less the review's not_steps_at (within 0.05 s), plus its missed_at, from
%     walking_steps_review.csv. Trials with an empty steps_in_signal are left out, and a count
%     that differs from steps_in_signal is an error.
%   If the next det-v1 event or missed step falls inside the window, the window ends NextGap
%   before it, and the shortened length is recorded.
%
%   Floor (the background): each trial's own quiet segment, from QuietFrom after the start of
%   the recording to QuietGap before its first det-v1 event, through the same transform and
%   statistic as the events. Where that segment is too short for one window with its padding,
%   the floor is the median of the floors of the other trials with the same floor, condition
%   and excitation, or, if none of them has one, of the same floor and condition; the
%   floor_source column says which. The floor of each condition's separate noise recording
%   (<floor>_<condition>_noise_01) is kept as an extra column. It is not the floor: on wood,
%   enclosed, the background during the trials was well above wood_enclosed_noise_01, which
%   was recorded after them.
%
%   Writes, in data/controlled/analysis/<Date>/: wavelet_events.csv (one row per event),
%   wavelet_summary.csv (one row per excitation, floor, condition and radius) and
%   wavelet_background.csv (the quiet segment of every trial and noise recording, in
%   recording order: Welch spectrum peaks and band powers). With Figures, in
%   data/controlled/figures/<Date>/: wavelet_<excitation>_<r>m.png for 1 m and 2 m,
%   wavelet_ring_times.png and wavelet_background_spectra.png.

arguments
    opts.Root (1,1) string = pwd
    opts.Date (1,1) string = "2026-10-21"
    opts.Figures (1,1) logical = true
end

P = wavelet_params();
raw = fullfile(opts.Root, "data", "controlled", "raw", opts.Date);
ana = fullfile(opts.Root, "data", "controlled", "analysis", opts.Date);
figs = fullfile(opts.Root, "data", "controlled", "figures", opts.Date);

E = event_list(raw, ana);
fprintf("%d events: %d drops, %d steps in %d walking trials\n", height(E), ...
    sum(E.excitation == "drop4in"), sum(E.excitation == "walk"), ...
    numel(unique(E.trial(E.excitation == "walk"))));

% Floors: each trial's quiet segment, then the borrowed ones
trials = unique(E.trial, "stable");
first = arrayfun(@(i) find(E.trial == trials(i), 1), 1:numel(trials));
F = repmat(empty_floor(P), numel(trials), 1);
for i = 1:numel(trials)
    m = load(fullfile(raw, trials(i) + ".mat"), "counts", "fs_hz");
    x = double(m.counts(:));
    fs = double(m.fs_hz);
    [a, b] = quiet_samples(E.first_t_s(first(i)), fs, numel(x), P);
    F(i) = quiet_floor(x, fs, a, b, P);
    F(i).quiet_s = max(0, (b - a + 1) / fs);
end
fl = E.floor(first);
cond = E.condition(first);
exc = E.excitation(first);
own = [F.own]';
for i = find(~own)'
    src = own & fl == fl(i) & cond == cond(i) & exc == exc(i);
    if any(src)
        F(i).source = sprintf("median of %d %s_%s %s trials", nnz(src), fl(i), cond(i), exc(i));
    else
        src = own & fl == fl(i) & cond == cond(i);
        F(i).source = sprintf("median of %d %s_%s trials of other excitations", nnz(src), ...
            fl(i), cond(i));
    end
    if ~any(src)
        error("wavelet_conditions:floor", "No trial of %s_%s has a quiet segment.", fl(i), cond(i));
    end
    F(i).env_db = median(vertcat(F(src).env_db), 1);
    F(i).psd = median(vertcat(F(src).psd), 1);
    F(i).bg_pow = median(vertcat(F(src).bg_pow), 1);
    F(i).from = join(trials(src)', " ");
end
for i = find(own)'
    F(i).source = "own quiet segment";
    F(i).from = trials(i);
end
fprintf("Floors: %d trials with their own quiet segment, %d borrowed\n", nnz(own), nnz(~own));

% The separate noise recordings, kept for comparison
rec = struct();
for key = unique(E.floor + "_" + E.condition)'
    m = load(fullfile(raw, key + "_noise_01.mat"), "counts", "fs_hz");
    x = double(m.counts(:));
    fs = double(m.fs_hz);
    rec.(key) = quiet_floor(x, fs, round(P.QuietFrom * fs) + 1, numel(x), P);
    rec.(key).from = key + "_noise_01";
end

% Events
rows = cell(height(E), 1);
for i = 1:numel(trials)
    k = find(E.trial == trials(i));
    m = load(fullfile(raw, trials(i) + ".mat"), "counts", "fs_hz");
    fs = double(m.fs_hz);
    x = double(m.counts(:));
    R = rec.(E.floor(k(1)) + "_" + E.condition(k(1)));
    for j = k'
        [i0, i1] = window_samples(E(j, :), fs, numel(x), P);
        [Wc, t, fr] = wavelet_window(x, fs, i0, i1, P);
        M = wavelet_band_metrics(Wc, t, fr, E.t_s(j), F(i), P);
        rows{j} = event_row(E(j, :), M, t, F(i), R, P);
    end
end
T = tidy(struct2table([rows{:}]));
writetable(as_text(T), fullfile(ana, "wavelet_events.csv"));
fprintf("Wrote %s (%d rows)\n", fullfile(ana, "wavelet_events.csv"), height(T));

S = summary(T, P);
writetable(as_text(S), fullfile(ana, "wavelet_summary.csv"));
fprintf("Wrote %s (%d rows)\n", fullfile(ana, "wavelet_summary.csv"), height(S));

[B, spectra] = background(raw, ana, P);
writetable(as_text(B), fullfile(ana, "wavelet_background.csv"));
fprintf("Wrote %s (%d rows)\n", fullfile(ana, "wavelet_background.csv"), height(B));

if opts.Figures
    for ex = ["drop4in" "walk"]
        for r = [1 2]
            file = fullfile(figs, sprintf("wavelet_%s_%dm.png", ex, r));
            plot_grid(T, ex, r, raw, P, file);
            fprintf("Saved %s\n", file);
        end
    end
    file = fullfile(figs, "wavelet_ring_times.png");
    plot_ring_times(T, P, file);
    fprintf("Saved %s\n", file);
    file = fullfile(figs, "wavelet_background_spectra.png");
    plot_background(spectra, file);
    fprintf("Saved %s\n", file);
end
end

%% Events

function E = event_list(raw, ana)
% One row per event: the drop of each drop4in trial and each reviewed step of each walking trial.
review = read_review(fullfile(ana, "walking_steps_review.csv"));
files = dir(fullfile(raw, "*.json"));
C = {};
for f = files'
    id = string(erase(f.name, ".json"));
    tok = regexp(id, "^(carpet|wood)_(bare|enclosed)_(drop4in|walk)_(\d)m_(\d+)$", "tokens", "once");
    if isempty(tok)
        continue                                   % noise recordings and drop7in
    end
    meta = jsondecode(fileread(fullfile(f.folder, f.name)));
    ev = readtable(fullfile(ana, "events_" + id + ".csv"), "TextType", "string", "Delimiter", ",");
    others = ev.t_s(:);                            % every det-v1 event, for the window ends
    if tok(3) == "drop4in"
        if meta.impacts_made ~= 1
            error("wavelet_conditions:drop", "%s: impacts_made is %g, not 1.", id, meta.impacts_made);
        end
        [~, k] = max(ev.peak_db);
        ts = ev.t_s(k);
        det = ev.event(k);
        src = "det-v1";
    else
        r = review(review.trial_id == id, :);
        if height(r) ~= 1
            error("wavelet_conditions:review", "%s: %d rows in the review.", id, height(r));
        end
        if isnan(r.steps_in_signal)
            continue                               % the review could not decide
        end
        notSteps = review_times(r.not_steps_at);
        missed = review_times(r.missed_at);
        keep = arrayfun(@(t) all(abs(t - notSteps) > 0.05), ev.t_s);
        ts = [ev.t_s(keep); missed(:)];
        det = [ev.event(keep); nan(numel(missed), 1)];
        src = [repmat("det-v1", nnz(keep), 1); repmat("review missed_at", numel(missed), 1)];
        [ts, o] = sort(ts);
        det = det(o);
        src = src(o);
        if numel(ts) ~= r.steps_in_signal
            error("wavelet_conditions:steps", "%s: %d step times from the review's events, " + ...
                "but %d steps in the signal.", id, numel(ts), r.steps_in_signal);
        end
        others = [others; missed(:)]; %#ok<AGROW>
    end
    base = string(meta.baseline_noise_id);
    for j = 1:numel(ts)
        later = others(others > ts(j) + 1e-6);
        C(end + 1, :) = {id, tok(3), tok(1), tok(2), str2double(tok(4)), ...
            str2double(tok(5)), j, det(j), src(j), ts(j), min([later; Inf]), base, ...
            min(ev.t_s), string(meta.start_time)}; %#ok<AGROW>
    end
end
E = tidy(cell2table(C, "VariableNames", ["trial" "excitation" "floor" "condition" ...
    "radius_m" "rep" "event" "det_v1_event" "source" "t_s" "next_t_s" "sidecar_noise_id" ...
    "first_t_s" "start_time"]));
end

function R = read_review(file)
o = detectImportOptions(file, "TextType", "string", "Delimiter", ",");
o = setvartype(o, ["trial_id" "floor" "condition" "missed_at" "not_steps_at" "note"], "string");
o = setvartype(o, "steps_in_signal", "double");
R = readtable(file, o);
end

function t = review_times(s)
% The times in a review field such as "4.65 s 14.0 dB; 5.06 s spike" (as _times in Python).
t = zeros(1, 0);
if ismissing(s) || strlength(s) == 0
    return
end
tok = regexp(char(s), "([\d.]+) s(?!\w)", "tokens");
t = cellfun(@(c) str2double(c{1}), tok);
t = reshape(t, 1, []);
end

function [i0, i1] = window_samples(e, fs, n, P)
t1 = e.t_s + P.Post;
if e.next_t_s < t1
    t1 = e.next_t_s - P.NextGap;
end
i0 = max(1, round((e.t_s - P.Pre) * fs) + 1);
i1 = min(n, round(t1 * fs) + 1);
end

%% Floor

function [a, b] = quiet_samples(firstT, fs, n, P)
% First and last sample of the quiet segment before a trial's first det-v1 event.
a = round(P.QuietFrom * fs) + 1;
b = min(n, round((firstT - P.QuietGap) * fs));
end

function F = empty_floor(P)
nb = size(P.Bands, 1);
F = struct("own", false, "env_db", nan(1, nb), "psd", nan(1, P.Nf), "bg_pow", nan(1, nb), ...
    "windows", 0, "quiet_s", 0, "source", "", "from", "");
end

function F = quiet_floor(x, fs, a, b, P)
% Median band envelope, median Wc^2 per frequency and mean square band envelope over windows
% of samples a to b of x, each as long as an event window and padded inside the segment.
F = empty_floor(P);
n = round((P.Pre + P.Post) * fs) + 1;
np = round(P.Pad * fs);
first = a + np;
last = b - np - n + 1;
if last < first
    return                                         % too short for one window
end
nw = min(P.NoiseWindows, floor((last - first) / n) + 1);
starts = round(linspace(first, last, nw));
if nw == 1
    starts = first;
end
nb = size(P.Bands, 1);
env = cell(nw, 1);
pw = cell(nw, 1);
for w = 1:nw
    [Wc, ~, fr] = wavelet_window(x, fs, starts(w), starts(w) + n - 1, P);
    Wc = Wc(1:P.NoiseDecim:end, :);
    pw{w} = Wc.^2;
    e = zeros(size(Wc, 1), nb);
    for k = 1:nb
        in = fr >= P.Bands(k, 1) & fr < P.Bands(k, 2);
        e(:, k) = sqrt(mean(Wc(:, in).^2, 2));
    end
    env{w} = e;
end
env = vertcat(env{:});
F.own = true;
F.env_db = 20 * log10(median(env, 1));
F.psd = median(vertcat(pw{:}), 1);
F.bg_pow = mean(env.^2, 1);
F.windows = nw;
end

%% Background spectra

function [B, spectra] = background(raw, ana, P)
% Welch spectrum of the quiet segment of every trial and noise recording, in recording order.
files = dir(fullfile(raw, "*.json"));
C = {};
spectra = struct("trial", {}, "start", {}, "f", {}, "p", {});
for f = files'
    id = string(erase(f.name, ".json"));
    meta = jsondecode(fileread(fullfile(f.folder, f.name)));
    m = load(fullfile(raw, id + ".mat"), "counts", "fs_hz");
    x = double(m.counts(:));
    fs = double(m.fs_hz);
    noiseOnly = isfield(meta, "noise_only") && isequal(meta.noise_only, true);
    if noiseOnly
        a = round(P.QuietFrom * fs) + 1;
        b = numel(x);
    else
        evFile = fullfile(ana, "events_" + id + ".csv");
        if ~isfile(evFile)
            continue
        end
        ev = readtable(evFile, "TextType", "string", "Delimiter", ",");
        [a, b] = quiet_samples(min(ev.t_s), fs, numel(x), P);
    end
    q = (b - a + 1) / fs;
    pw = nan(1, size(P.BgBands, 1));
    pkHz = nan(1, size(P.BgPeakBands, 1));
    pkDb = pkHz;
    if q >= P.QuietMinS
        [fw, p] = welch_psd(x(a:b), fs, P.WelchSeg);
        df = fw(2) - fw(1);
        for k = 1:size(P.BgBands, 1)
            pw(k) = 10 * log10(sum(p(fw >= P.BgBands(k, 1) & fw < P.BgBands(k, 2))) * df);
            in = find(fw >= P.BgPeakBands(k, 1) & fw <= P.BgPeakBands(k, 2));
            [v, j] = max(p(in));
            pkHz(k) = fw(in(j));
            pkDb(k) = 10 * log10(v);
        end
        spectra(end + 1) = struct("trial", id, "start", string(meta.start_time), ...
            "f", fw, "p", p); %#ok<AGROW>
    end
    C(end + 1, :) = {id, string(meta.start_time), string(yesno(noiseOnly)), ...
        round(max(q, 0), 2), pw, pkHz, pkDb}; %#ok<AGROW>
end

% Each peak against the same frequency in the floor's two noise recordings
names = string(C(:, 1));
n = size(C, 1);
nk = size(P.BgPeakBands, 1);
aboveOwn = nan(n, nk);
aboveBare = nan(n, nk);
for i = 1:n
    tok = regexp(names(i), "^(carpet|wood)_(bare|enclosed)_", "tokens", "once");
    for k = 1:nk
        if isnan(C{i, 6}(k))
            continue
        end
        aboveOwn(i, k) = C{i, 7}(k) - psd_at(spectra, tok(1) + "_" + tok(2) + "_noise_01", C{i, 6}(k));
        aboveBare(i, k) = C{i, 7}(k) - psd_at(spectra, tok(1) + "_bare_noise_01", C{i, 6}(k));
    end
end

pw = vertcat(C{:, 5});
pkHz = vertcat(C{:, 6});
pkDb = vertcat(C{:, 7});
B = table(names, string(C(:, 2)), string(C(:, 3)), cell2mat(C(:, 4)), ...
    'VariableNames', ["trial" "start_time" "noise_recording" "quiet_s"]);
for k = 1:size(P.BgBands, 1)
    B.("power_db_" + P.BgTags(k)) = round(pw(:, k), 1);
end
for k = 1:nk
    B.("peak_hz_" + P.BgTags(k)) = round(pkHz(:, k));
    B.("peak_db_" + P.BgTags(k)) = round(pkDb(:, k), 1);
    B.("peak_above_own_noise_rec_db_" + P.BgTags(k)) = round(aboveOwn(:, k), 1);
    B.("peak_above_bare_noise_rec_db_" + P.BgTags(k)) = round(aboveBare(:, k), 1);
end
B = sortrows(B, "start_time");
B.fs_hz = repmat(48000, height(B), 1);
B.welch_segment = repmat(P.WelchSeg, height(B), 1);
B.quiet_from_s = repmat(P.QuietFrom, height(B), 1);
B.quiet_gap_s = repmat(P.QuietGap, height(B), 1);
B.units = repmat("power: dB re 1 count^2 (band integral of the PSD); peak: dB re 1 count^2/Hz", ...
    height(B), 1);
end

function v = psd_at(spectra, name, hz)
s = spectra([spectra.trial] == name);
[~, j] = min(abs(s.f - hz));
v = 10 * log10(s.p(j));
end

function [f, p] = welch_psd(x, fs, nseg)
% One-sided Welch PSD (counts^2/Hz): periodic Hann segments, half overlapping, mean removed
% from each, averaged (as scipy.signal.welch's defaults).
w = 0.5 - 0.5 * cos(2 * pi * (0:nseg - 1)' / nseg);
step = nseg / 2;
k = floor((numel(x) - nseg) / step) + 1;
acc = zeros(nseg / 2 + 1, 1);
for i = 1:k
    s = x((i - 1) * step + (1:nseg));
    X = fft((s - mean(s)) .* w);
    acc = acc + abs(X(1:nseg / 2 + 1)).^2;
end
p = acc / (k * fs * sum(w.^2));
p(2:end - 1) = 2 * p(2:end - 1);
f = (0:nseg / 2)' * fs / nseg;
end

%% Tables

function s = event_row(e, M, t, F, R, P)
s.trial = e.trial;
s.excitation = e.excitation;
s.floor = e.floor;
s.condition = e.condition;
s.radius_m = e.radius_m;
s.rep = e.rep;
s.event = e.event;
s.det_v1_event = e.det_v1_event;
s.source = e.source;
s.t_s = round(e.t_s, 3);
s.window_start_ms = round((t(1) - e.t_s) * 1e3, 1);
s.window_end_ms = round((t(end) - e.t_s) * 1e3, 1);
s.window_ms = round((t(end) - t(1)) * 1e3, 1);
s.shortened = yesno(e.next_t_s < e.t_s + P.Post);
s.quiet_s = round(F.quiet_s, 2);
s.floor_source = string(F.source);
s.floor_from = string(F.from);
s.noise_recording = R.from;
s.sidecar_noise_id = e.sidecar_noise_id;
for b = 1:size(P.Bands, 1)
    g = P.BandTags(b);
    s.("level_db_" + g) = round(M.level_db(b), 1);
    s.("level_above_floor_db_" + g) = round(M.level_above_floor_db(b), 1);
    s.("bg_share_db_" + g) = round(M.bg_share_db(b), 1);
    s.("band_peak_db_" + g) = round(M.band_peak_db(b), 1);
    s.("band_peak_ms_" + g) = round(M.band_peak_ms(b), 1);
    s.("ring_ms_" + g) = round(M.ring_ms(b), 1);
    s.("ring_status_" + g) = M.ring_status(b);
    s.("ring_first_ms_" + g) = round(M.ring_first_ms(b), 1);
    s.("ring_hz_" + g) = round(M.ring_hz(b));
    s.("ring_hz_snr_db_" + g) = round(M.ring_hz_snr_db(b), 1);
    s.("ring_hz_status_" + g) = M.ring_hz_status(b);
    s.("ring_hz_in_background_" + g) = M.ring_hz_in_background(b);
    s.("floor_env_db_" + g) = round(F.env_db(b), 1);
    s.("noise_rec_env_db_" + g) = round(R.env_db(b), 1);
end
s.peak_db = round(M.peak_db, 1);
s.peak_ms = round(M.peak_ms, 1);
s.peak_hz = round(M.peak_hz);
s.fmin_hz = P.Fmin;
s.fmax_hz = P.Fmax;
s.nf = P.Nf;
s.fa = P.Fa;
s.pre_ms = P.Pre * 1e3;
s.post_ms = P.Post * 1e3;
s.pad_ms = P.Pad * 1e3;
s.next_gap_ms = P.NextGap * 1e3;
s.quiet_from_s = P.QuietFrom;
s.quiet_gap_s = P.QuietGap;
s.ring_drop_db = P.RingDropDb;
s.ring_hz_from_ms = P.RingFrom * 1e3;
s.ring_hz_to_ms = P.RingTo * 1e3;
s.ring_hz_snr_min_db = P.RingSnrDb;
s.normalization = P.Normalization;
end

function S = summary(T, P)
% One row per excitation, floor, condition and radius.
[G, exc, fl, cond, rad] = findgroups(T.excitation, T.floor, T.condition, T.radius_m);
C = cell(max(G), 1);
for g = 1:max(G)
    k = G == g;
    reps = unique(T.rep(k));
    s = struct();
    s.excitation = exc(g);
    s.floor = fl(g);
    s.condition = cond(g);
    s.radius_m = rad(g);
    s.n_events = nnz(k);
    s.n_trials = numel(reps);
    s.trials = join(unique(T.trial(k))', " ");
    s.n_trials_own_floor = numel(unique(T.trial(k & T.floor_source == "own quiet segment")));
    borrowed = unique(T.floor_source(k & T.floor_source ~= "own quiet segment"));
    s.borrowed_floor = "";
    if ~isempty(borrowed)
        s.borrowed_floor = join(borrowed', "; ");
    end
    s.noise_recording = unique(T.noise_recording(k));
    s.n_shortened = nnz(T.shortened(k) == "yes");
    s.min_window_ms = min(T.window_ms(k));
    for b = 1:size(P.Bands, 1)
        tg = P.BandTags(b);
        s = stats(s, "level", "db", tg, T.("level_db_" + tg), T.rep, k, reps, 1);
        s.("level_above_floor_median_db_" + tg) = round(median(T.("level_above_floor_db_" + tg)(k)), 1);
        s.("level_enc_minus_bare_db_" + tg) = NaN;
        bg = T.("bg_share_db_" + tg)(k);
        s.("bg_share_median_db_" + tg) = round(median(bg), 1);
        s.("bg_share_max_db_" + tg) = max(bg);
        st = T.("ring_status_" + tg);
        s.("ring_resolved_" + tg) = nnz(k & st == "resolved");
        s.("ring_not_resolved_floor_" + tg) = nnz(k & st == "not resolved: floor");
        s.("ring_not_resolved_window_" + tg) = nnz(k & st == "not resolved: window end");
        s = stats(s, "ring", "ms", tg, T.("ring_ms_" + tg), T.rep, k, reps, 1);
        s.("ring_enc_minus_bare_ms_" + tg) = NaN;
        s = stats(s, "ring_first", "ms", tg, T.("ring_first_ms_" + tg), T.rep, k, reps, 1);
        hz = T.("ring_hz_" + tg);
        s.("ring_hz_n_" + tg) = nnz(k & ~isnan(hz));
        s.("ring_hz_in_background_n_" + tg) = nnz(k & T.("ring_hz_in_background_" + tg) == "yes");
        s = stats(s, "ring_hz", "hz", tg, hz, T.rep, k, reps, 0);
        s.("floor_env_median_db_" + tg) = round(median(T.("floor_env_db_" + tg)(k)), 1);
        s.("noise_rec_env_db_" + tg) = unique(T.("noise_rec_env_db_" + tg)(k));
    end
    s.peak_hz_median = round(median(T.peak_hz(k)));
    s.peak_hz_min = min(T.peak_hz(k));
    s.peak_hz_max = max(T.peak_hz(k));
    s.peak_ms_median = round(median(T.peak_ms(k)), 1);
    C{g} = s;
end
S = tidy(struct2table([C{:}]));
S = sortrows(S, ["excitation" "floor" "radius_m" "condition"]);

% Enclosed minus bare, on the enclosed rows
for i = find(S.condition == "enclosed")'
    j = find(S.excitation == S.excitation(i) & S.floor == S.floor(i) & ...
        S.radius_m == S.radius_m(i) & S.condition == "bare");
    if isempty(j)
        continue
    end
    for tg = P.BandTags
        S.("level_enc_minus_bare_db_" + tg)(i) = round(S.("level_median_db_" + tg)(i) - ...
            S.("level_median_db_" + tg)(j), 1);
        S.("ring_enc_minus_bare_ms_" + tg)(i) = round(S.("ring_median_ms_" + tg)(i) - ...
            S.("ring_median_ms_" + tg)(j), 1);
    end
end
end

function s = stats(s, name, unit, tg, v, rep, k, reps, digits)
% Median, min and max over the group's events (NaN left out), and each trial's median.
vk = v(k & ~isnan(v));
if isempty(vk)
    [md, lo, hi] = deal(NaN);
else
    md = round(median(vk), digits);
    lo = min(vk);
    hi = max(vk);
end
tm = strings(1, numel(reps));
for r = 1:numel(reps)
    vr = v(k & rep == reps(r) & ~isnan(v));
    if isempty(vr)
        tm(r) = "nr";
    else
        tm(r) = compose("%." + digits + "f", median(vr));
    end
end
s.(name + "_median_" + unit + "_" + tg) = md;
s.(name + "_min_" + unit + "_" + tg) = lo;
s.(name + "_max_" + unit + "_" + tg) = hi;
s.(name + "_trial_medians_" + unit + "_" + tg) = join(tm, " ");
end

function T = tidy(T)
% Cell columns of strings become string columns.
for v = string(T.Properties.VariableNames)
    if iscell(T.(v))
        T.(v) = string(T.(v));
    end
end
end

function T = as_text(T)
% Numbers as text for writetable, with NaN as an empty field (never a number).
for v = string(T.Properties.VariableNames)
    if isnumeric(T.(v))
        s = string(T.(v));
        s(isnan(T.(v))) = "";
        T.(v) = s;
    end
end
end

function s = yesno(c)
if c
    s = "yes";
else
    s = "no";
end
end

%% Figures

function plot_grid(T, exc, r, raw, P, file)
% 2 x 2 scalograms (rows carpet, wood; columns bare, enclosed) on one dB scale, each the
% event whose band levels are closest to its group's median band levels.
floors = ["carpet" "wood"];
conds = ["bare" "enclosed"];
lv = "level_db_" + P.BandTags;
D = cell(2, 2);
tms = cell(2, 2);
lab = strings(2, 2);
top = -Inf;
for i = 1:2
    for j = 1:2
        g = find(T.excitation == exc & T.floor == floors(i) & T.condition == conds(j) & ...
            T.radius_m == r);
        if isempty(g)
            continue
        end
        L = T{g, lv};
        [~, k] = min(sqrt(sum((L - median(L, 1)).^2, 2)));
        e = T(g(k), :);
        m = load(fullfile(raw, e.trial + ".mat"), "counts", "fs_hz");
        fs = double(m.fs_hz);
        i0 = round((e.t_s + e.window_start_ms / 1e3) * fs) + 1;
        i1 = round((e.t_s + e.window_end_ms / 1e3) * fs) + 1;
        [Wc, t, fr] = wavelet_window(double(m.counts(:)), fs, i0, i1, P);
        dec = 4;                                   % plot at 12 kHz time resolution
        D{i, j} = 20 * log10(Wc(1:dec:end, :)).';
        tms{i, j} = (t(1:dec:end) - e.t_s) * 1e3;
        top = max(top, max(D{i, j}(:)));
        if exc == "drop4in"
            what = "the drop";
        else
            what = sprintf("step %d of %d", e.event, nnz(T.trial == e.trial));
        end
        lab(i, j) = sprintf("%s, %s (%d events)", e.trial, what, numel(g));
    end
end

fig = figure("Visible", "off", "Units", "pixels", "Position", [0 0 1700 1150]);
fig.Theme = "light";
tl = tiledlayout(fig, 2, 2, "TileSpacing", "compact", "Padding", "compact");
for i = 1:2
    for j = 1:2
        ax = nexttile(tl);
        if isempty(D{i, j})
            axis(ax, "off");
            title(ax, sprintf("%s, %s: no trials", capitalize(floors(i)), conds(j)));
            continue
        end
        imagesc(ax, tms{i, j}, fr / 1000, D{i, j});
        set(ax, "YDir", "normal", "CLim", [top - P.RangeDb top]);
        colormap(ax, 1 - pink(256));
        xline(ax, 0, ":", "Color", [0.35 0.35 0.35], "LineWidth", 1.2);
        title(ax, {sprintf("%s, %s", capitalize(floors(i)), conds(j)), lab(i, j)}, ...
            "Interpreter", "none", "FontWeight", "normal");
        if j == 1
            ylabel(ax, "Frequency (kHz)");
        end
        if i == 2
            xlabel(ax, "Time from event (ms)");
        end
    end
end
cb = colorbar(ax);
cb.Layout.Tile = "east";
cb.Label.String = "dB re 1 count (amplitude-corrected |W|)";
if exc == "drop4in"
    name = "4 in drop";
else
    name = "Walking";
end
title(tl, sprintf("%s at %d m: Morlet scalograms (Fa = %g), one shared dB scale", name, r, P.Fa), ...
    "FontWeight", "bold");
subtitle(tl, "Each panel shows the event whose three band levels are closest to its group's median");
fontsize(fig, 15, "points");
exportgraphics(fig, file, "Resolution", 200);
close(fig);
end

function plot_ring_times(T, P, file)
% Ring time by band, floor, condition and radius; every event a dot.
excs = ["drop4in" "walk"];
names = ["4 in drop" "Walking"];
floors = ["carpet" "wood"];
conds = ["bare" "enclosed"];
col = [0.165 0.471 0.839; 0.922 0.408 0.204];   % bare blue, enclosed orange
yWin = 420;                                        % where "not resolved: window end" goes
yFloor = 640;                                      % where "not resolved: floor" goes
fig = figure("Visible", "off", "Units", "pixels", "Position", [0 0 2000 1150]);
fig.Theme = "light";
tl = tiledlayout(fig, 2, 3, "TileSpacing", "compact", "Padding", "compact");
h = gobjects(1, 4);
for ie = 1:2
    for b = 1:size(P.Bands, 1)
        tg = P.BandTags(b);
        ax = nexttile(tl);
        hold(ax, "on");
        set(ax, "YScale", "log");
        for f = 1:2
            for r = 1:3
                for c = 1:2
                    k = find(T.excitation == excs(ie) & T.floor == floors(f) & ...
                        T.condition == conds(c) & T.radius_m == r);
                    if isempty(k)
                        continue
                    end
                    x0 = (f - 1) * 3 + r + (c - 1.5) * 0.38;
                    jit = ((1:numel(k))' - (numel(k) + 1) / 2) / max(numel(k), 1) * 0.26;
                    v = T.("ring_ms_" + tg)(k);
                    st = T.("ring_status_" + tg)(k);
                    ok = st == "resolved";
                    w = st == "not resolved: window end";
                    nz = st == "not resolved: floor";
                    if any(ok)
                        h(c) = scatter(ax, x0 + jit(ok), v(ok), 30, col(c, :), "filled", ...
                            "MarkerFaceAlpha", 0.65);
                        plot(ax, x0 + [-0.15 0.15], median(v(ok)) * [1 1], "-", ...
                            "Color", [0.1 0.1 0.1], "LineWidth", 2);
                    end
                    if any(w)
                        h(3) = scatter(ax, x0 + jit(w), yWin * ones(nnz(w), 1), 30, col(c, :), "^");
                    end
                    if any(nz)
                        h(4) = scatter(ax, x0 + jit(nz), yFloor * ones(nnz(nz), 1), 30, col(c, :), "x");
                    end
                end
            end
        end
        xline(ax, 3.5, "-", "Color", [0.8 0.8 0.8]);
        yline(ax, 320, "--", "Color", [0.6 0.6 0.6]);
        set(ax, "XLim", [0.4 6.6], "YLim", [0.5 900], "XTick", 1:6, "XTickLabel", ...
            ["carpet 1 m" "carpet 2 m" "carpet 3 m" "wood 1 m" "wood 2 m" "wood 3 m"], ...
            "YTick", [1 3 10 30 100 300], "YGrid", "on");
        title(ax, sprintf("%s, %s", names(ie), P.BandNames(b)), "FontWeight", "normal");
        if b == 1
            ylabel(ax, "Ring time (ms)");
        end
    end
end
keep = isgraphics(h);
labels = ["bare" "enclosed" "not resolved: window end" "not resolved: floor"];
lg = legend(h(keep), labels(keep), "Orientation", "horizontal");
lg.Layout.Tile = "south";
title(tl, sprintf("Ring time (envelope %d dB below its peak) against each trial's own " + ...
    "background; every event a dot, bar = median of the resolved ones", P.RingDropDb), ...
    "FontWeight", "bold");
subtitle(tl, "Above the dashed line: not resolved (triangles: still ringing at the window end; " + ...
    "crosses: reached the background floor first)");
fontsize(fig, 14, "points");
exportgraphics(fig, file, "Resolution", 200);
close(fig);
end

function plot_background(spectra, file)
% Quiet-segment spectra on wood: the 9 enclosed walks (thin), one enclosed walk and one
% enclosed drop (thick), and the two wood noise recordings (dashed).
names = [spectra.trial];
hhmm = @(s) extractBetween(s.start, 12, 19);
fig = figure("Visible", "off", "Units", "pixels", "Position", [0 0 1900 1000]);
fig.Theme = "light";
ax = axes(fig);
hold(ax, "on");
h = gobjects(0);
lab = strings(0);
walks = find(startsWith(names, "wood_enclosed_walk_"));
for i = walks
    s = spectra(i);
    if s.trial == "wood_enclosed_walk_3m_02"
        continue
    end
    k = s.f >= 500 & s.f <= 8000;
    h(end + 1) = plot(ax, s.f(k) / 1000, 10 * log10(s.p(k)), "-", "Color", [0.96 0.72 0.6], ...
        "LineWidth", 0.7); %#ok<AGROW>
    lab(end + 1) = sprintf("%s, %s", s.trial, hhmm(s)); %#ok<AGROW>
end
thick = ["wood_enclosed_walk_3m_02" "wood_enclosed_drop4in_1m_02" ...
    "wood_enclosed_noise_01" "wood_bare_noise_01"];
style = {"-", [0.922 0.408 0.204], 2.0; "-", [0.62 0.16 0.08], 2.0; ...
    "--", [0.45 0.3 0.2], 2.2; "--", [0.165 0.471 0.839], 2.2};
for j = 1:numel(thick)
    s = spectra(names == thick(j));
    k = s.f >= 500 & s.f <= 8000;
    h(end + 1) = plot(ax, s.f(k) / 1000, 10 * log10(s.p(k)), style{j, 1}, "Color", style{j, 2}, ...
        "LineWidth", style{j, 3}); %#ok<AGROW>
    lab(end + 1) = sprintf("%s, %s", s.trial, hhmm(s)); %#ok<AGROW>
end
xlim(ax, [0.5 8]);
grid(ax, "on");
xlabel(ax, "Frequency (kHz)");
ylabel(ax, "PSD (dB re 1 count^2/Hz)");
title(ax, "Wood: the background before the first step or drop (Welch, 10 Hz resolution)", ...
    "FontWeight", "bold");
subtitle(ax, "Thin: the other 8 enclosed walks. Dashed: the separate noise recordings. " + ...
    "Times are when each recording started (2026-10-21)");
legend(ax, h, lab, "Location", "eastoutside", "Interpreter", "none");
fontsize(fig, 15, "points");
exportgraphics(fig, file, "Resolution", 200);
close(fig);
end

function s = capitalize(s)
s = upper(extractBefore(s, 2)) + extractAfter(s, 1);
end
