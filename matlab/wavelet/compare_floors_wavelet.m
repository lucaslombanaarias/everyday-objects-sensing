function varargout = compare_floors_wavelet(trials, steps, opts)
%COMPARE_FLOORS_WAVELET Wavelet scalograms of single steps from several trials, on one dB scale.
%
%   compare_floors_wavelet()
%       Events 2 to 4 of carpet_bare_walk_1m_01 against wood_bare_walk_1m_01 (2026-10-21).
%   compare_floors_wavelet(["carpet_bare_walk_2m_01" "wood_bare_walk_2m_01"], 1:5)
%   compare_floors_wavelet(trials, steps, Pre=0.15, Fmax=8000, SaveAs="steps.png")
%   R = compare_floors_wavelet(...)  also returns the table printed to the Command Window.
%
%   Run from the repo root with matlab/wavelet on the path. Each row of the figure is a trial
%   and each column an event; events are det-v1 event numbers from
%   data/controlled/analysis/<Date>/events_<trial>.csv. Some events are not steps (see
%   walking_steps_review.csv in the same folder), so check the event times before choosing.
%
%   How it differs from plotwt:
%   - Divides |W| from freq_inst_morlet by sqrt(Fa/(2f)), so a sine of amplitude A reads A at
%     every frequency. Without this, low frequencies read stronger than they are.
%   - Plots 20*log10 of that relative to the largest value in any panel, so colors compare
%     across panels. On a linear scale the low-frequency peak washes out everything above 1 kHz.
%   - Transforms Pad seconds of real signal on each side of the window and then crops it,
%     instead of mirroring, so the window edges are not distorted.
%   - Starts the window Pre seconds before the event time: det-v1 marks a step up to about
%     50 ms after it starts, so Pre = 0.1 s keeps the start of the step.

arguments
    trials (1,:) string = ["carpet_bare_walk_1m_01" "wood_bare_walk_1m_01"]
    steps (1,:) double {mustBeInteger, mustBePositive} = 2:4
    opts.Date (1,1) string = "2026-10-21"
    opts.Root (1,1) string = pwd
    opts.Pre (1,1) double {mustBePositive} = 0.10
    opts.Post (1,1) double {mustBePositive} = 0.25
    opts.Pad (1,1) double {mustBeNonnegative} = 0.10
    opts.Fmin (1,1) double {mustBePositive} = 50
    opts.Fmax (1,1) double {mustBePositive} = 10000
    opts.Nf (1,1) double {mustBeInteger, mustBePositive} = 300
    opts.Fa (1,1) double {mustBePositive} = 2
    opts.RangeDb (1,1) double {mustBePositive} = 40
    opts.SaveAs (1,1) string = ""
end

nT = numel(trials);
nS = numel(steps);
A    = cell(nT, nS);     % amplitude-corrected |W|, thinned in time for plotting
tms  = cell(nT, nS);     % time from event (ms) for each panel
rows = cell(nT * nS, 9);
n = 0;

for i = 1:nT
    name = char(trials(i));
    matFile = fullfile(opts.Root, "data", "controlled", "raw", opts.Date, name + ".mat");
    evFile  = fullfile(opts.Root, "data", "controlled", "analysis", opts.Date, "events_" + name + ".csv");
    m  = load(matFile, "counts", "fs_hz");
    ev = readtable(evFile);
    fs = double(m.fs_hz);
    x  = double(m.counts(:));

    for j = 1:nS
        s = steps(j);
        if s > height(ev)
            error("compare_floors_wavelet:event", "%s has %d events; there is no event %d.", ...
                name, height(ev), s);
        end
        ts = ev.t_s(s);
        i0 = round((ts - opts.Pre) * fs) + 1;      % first and last sample of the window
        i1 = round((ts + opts.Post) * fs) + 1;
        if i0 < 1 || i1 > numel(x)
            error("compare_floors_wavelet:window", ...
                "The window around event %d of %s (%.3f s) runs past the recording.", s, name, ts);
        end
        np = round(opts.Pad * fs);
        a  = max(1, i0 - np);
        b  = min(numel(x), i1 + np);

        [~, fr, W] = freq_inst_morlet(x(a:b), fs, opts.Fmin, opts.Fmax, opts.Nf, opts.Fa);
        W = W((i0 - a + 1):(i1 - a + 1), :);       % drop the padding
        W = W ./ sqrt(opts.Fa ./ (2 * fr));        % same scale at every frequency
        t = ((i0:i1)' - 1) / fs - ts;              % s from the event

        [pk, k] = max(W(:));
        [r, c] = ind2sub(size(W), k);
        hi = fr >= 1000;
        if any(hi)
            Wh = W(:, hi);
            fh = fr(hi);
            [pkHi, k2] = max(Wh(:));
            [r2, c2] = ind2sub(size(Wh), k2);
            hiRow = {pkHi, t(r2) * 1e3, fh(c2)};
        else
            hiRow = {NaN, NaN, NaN};
        end
        n = n + 1;
        rows(n, :) = [{name, s, ts, pk, t(r) * 1e3, fr(c)}, hiRow];

        dec = max(1, floor(fs / 12000));           % plot at about 12 kHz time resolution
        A{i, j}   = W(1:dec:end, :);
        tms{i, j} = t(1:dec:end) * 1e3;
    end
end

ref = max([rows{:, 4}]);                           % largest value in any panel

fig = figure("Name", "Wavelet comparison");
try
    fig.Theme = "light";                           % readable when exported for slides
catch
end
tl = tiledlayout(fig, nT, nS, "TileSpacing", "compact", "Padding", "compact");
for i = 1:nT
    for j = 1:nS
        ax = nexttile(tl);
        imagesc(ax, tms{i, j}, fr / 1000, 20 * log10(A{i, j}.' / ref));
        set(ax, "YDir", "normal", "CLim", [-opts.RangeDb 0]);
        colormap(ax, 1 - pink(256));
        xline(ax, 0, ":", "Color", [0.4 0.4 0.4]);
        title(ax, sprintf("%s, event %d", trials(i), steps(j)), "Interpreter", "none", "FontSize", 9);
        if j == 1, ylabel(ax, "Frequency (kHz)"); end
        if i == nT, xlabel(ax, "Time from event (ms)"); end
    end
end
cb = colorbar(ax);
cb.Layout.Tile = "east";
cb.Label.String = "dB re largest value in any panel";
title(tl, sprintf("Morlet wavelet, Fa = %g, amplitude-corrected, one shared scale", opts.Fa));

R = cell2table(rows, "VariableNames", ["trial" "event" "t_s" "peak_db" "peak_ms" "peak_hz" ...
    "peak_above_1khz_db" "above_1khz_ms" "above_1khz_hz"]);
R.peak_db            = round(20 * log10(R.peak_db / ref), 1);
R.peak_above_1khz_db = round(20 * log10(R.peak_above_1khz_db / ref), 1);
R.peak_ms            = round(R.peak_ms, 1);
R.above_1khz_ms      = round(R.above_1khz_ms, 1);
R.peak_hz            = round(R.peak_hz);
R.above_1khz_hz      = round(R.above_1khz_hz);
disp(R)

if strlength(opts.SaveAs) > 0
    exportgraphics(fig, opts.SaveAs, "Resolution", 200);
    fprintf("Saved %s\n", opts.SaveAs);
end
if nargout > 0
    varargout{1} = R;
end
end
