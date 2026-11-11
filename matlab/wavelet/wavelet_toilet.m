function varargout = wavelet_toilet(opts)
%WAVELET_TOILET Scalograms of an ice drop and a stream trial at each toilet sensor position.
%
%   wavelet_toilet()
%   R = wavelet_toilet(RangeDb=60, SaveDir="figures")
%
%   Run from the repo root with matlab/wavelet on the path. The 2026-11-04 toilet trials, rows
%   the sensor positions (base, behind the seat, tank lid). Saves three 300 dpi slide figures
%   to SaveDir (default docs/figures):
%
%   - toilet_wavelet.png: an ice drop and a stream trial side by side, all six panels on one
%     dB scale (relative to the largest value in any of them), so colors compare across
%     positions and between ice and stream.
%   - toilet_ice_wavelet.png and toilet_stream_wavelet.png: one column each, on a scale
%     shared by its three positions.
%
%   - Ice drop: of the 15 drops tagged true_impact at that position (3 trials of 5), the one
%     with the median det-v1 peak_db; window Pre before its event time to Post after it, as
%     in wavelet_conditions (wavelet_params).
%   - Stream trial (a cup of water poured slowly into the bowl, trial IDs
%     toilet_<position>_pour_<nn>): trial StreamTrial at that position, from PourPre before
%     the steady pour starts to PourPost after. The steady pour starts where the 5 ms RMS
%     above 100 Hz, in dB and averaged over 0.5 s, first rises PourRiseDb above its level 0.1
%     to 0.6 s into the recording, as in scripts/plot_toilet_slide_figures.py; a first
%     splash can come before it (on the tank lid, about 0.5 s before).
%
%   Transform: freq_inst_morlet through wavelet_window (100 Hz to 10 kHz, 300 frequencies,
%   Fa = 2, 0.1 s of real signal on each side, amplitude-corrected so a sine of amplitude A
%   reads A at its frequency). Returns the table it prints; peak_db_re_ref is relative to the
%   largest value in any of the six panels.

arguments
    opts.Date (1,1) string = "2026-11-04"
    opts.Root (1,1) string = pwd
    opts.StreamTrial (1,1) double {mustBeInteger, mustBePositive} = 2
    opts.PourPre (1,1) double {mustBePositive} = 0.5
    opts.PourPost (1,1) double {mustBePositive} = 1.5
    opts.PourRiseDb (1,1) double {mustBePositive} = 8
    opts.RangeDb (1,1) double {mustBePositive} = 70
    opts.SaveDir (1,1) string = ""
end

P = wavelet_params();
positions = ["base" "behindseat" "tanklid"];
labels = ["Base" "Behind seat" "Tank lid"];
raw = fullfile(opts.Root, "data", "controlled", "raw", opts.Date);
ana = fullfile(opts.Root, "data", "controlled", "analysis", opts.Date);
saveDir = opts.SaveDir;
if strlength(saveDir) == 0
    saveDir = fullfile(opts.Root, "docs", "figures");
end

panels = struct("A", {}, "t", {}, "title", {});
rows = cell(2 * numel(positions), 7);
fr = [];
for p = 1:numel(positions)
    % Ice: the median of the position's 15 tagged drops.
    drops = table();
    for n = 1:3
        name = sprintf("toilet_%s_ice_%02d", positions(p), n);
        ev = readtable(fullfile(ana, "events_" + name + ".csv"), "TextType", "string");
        ev = ev(ev.annotation == "true_impact", ["t_s" "peak_db"]);
        ev.trial = repmat(name, height(ev), 1);
        drops = [drops; ev]; %#ok<AGROW>
    end
    drops = sortrows(drops, "peak_db");
    d = drops(ceil(height(drops) / 2), :);
    [x, fs] = load_counts(raw, d.trial);
    i0 = round((d.t_s - P.Pre) * fs) + 1;
    i1 = round((d.t_s + P.Post) * fs) + 1;
    [Wc, t, fr] = wavelet_window(x, fs, i0, i1, P);
    panels(p, 1).A = Wc;
    panels(p, 1).t = (t - d.t_s) * 1e3;                    % ms from the drop
    panels(p, 1).title = labels(p);    % which drop and trial: the printed table and README
    rows(2 * p - 1, :) = {labels(p), "ice drop", d.trial, d.t_s, d.peak_db, 0, 0};

    % Stream: the start of the steady pour in trial StreamTrial.
    name = sprintf("toilet_%s_pour_%02d", positions(p), opts.StreamTrial);
    [x, fs] = load_counts(raw, name);
    t0 = pour_start(x, fs, opts.PourRiseDb);
    i0 = max(1, round((t0 - opts.PourPre) * fs) + 1);
    i1 = min(numel(x), round((t0 + opts.PourPost) * fs) + 1);
    [Wc, t, fr] = wavelet_window(x, fs, i0, i1, P);
    panels(p, 2).A = Wc;
    panels(p, 2).t = t - t0;                               % s from the start of the pour
    panels(p, 2).title = labels(p);
    rows(2 * p, :) = {labels(p), "stream", name, t0, NaN, 0, 0};
end

peak = arrayfun(@(s) max(s.A(:)), panels);                 % positions x [ice stream]
ref = max(peak(:));                                        % largest value in any panel
for k = 1:numel(panels)
    [pk, i] = max(panels(k).A(:));
    [~, c] = ind2sub(size(panels(k).A), i);
    [p, col] = ind2sub(size(panels), k);
    rows{2 * p - 2 + col, 6} = round(20 * log10(pk / ref), 1);
    rows{2 * p - 2 + col, 7} = round(fr(c));
end

% Slide figures, drawn at about the size they fill on a 13.33 x 7.5 in slide.
draw(panels, [1 2], fr, ref, opts.RangeDb, [10.0 5.8], fullfile(saveDir, "toilet_wavelet.png"));
draw(panels, 1, fr, max(peak(:, 1)), opts.RangeDb, [7.0 5.8], ...
    fullfile(saveDir, "toilet_ice_wavelet.png"));
draw(panels, 2, fr, max(peak(:, 2)), opts.RangeDb, [7.0 5.8], ...
    fullfile(saveDir, "toilet_stream_wavelet.png"));

R = cell2table(rows, "VariableNames", ["position" "kind" "trial" "t_s" "peak_db_detv1" ...
    "peak_db_re_ref" "peak_hz"]);
disp(R)
if nargout > 0
    varargout{1} = R;
end
end


function draw(panels, cols, fr, ref, rangeDb, sizeIn, path)
%DRAW The panels in columns cols, rows the positions, on one scale relative to ref. Drawn at
%   about the size it fills on a slide, with as few words as possible: the kind of event over
%   each column, the position beside each row, "kHz" on the top frequency tick, and the time
%   unit under each column. Smallest text FontPt.
FontPt = 20;
font = {"FontName", "Trebuchet MS"};
kinds = ["Ice drop" "Stream"];
timeLabels = ["Time (ms)" "Time (s)"];
fig = figure("Units", "inches", "Position", [0.5 0.5 sizeIn]);
try
    fig.Theme = "light";
catch
end
tl = tiledlayout(fig, size(panels, 1), numel(cols), "TileSpacing", "compact", ...
    "Padding", "compact");
for p = 1:size(panels, 1)
    for col = cols
        s = panels(p, col);
        [A, tt] = block_max(s.A, s.t, 1400);
        ax = nexttile(tl);
        imagesc(ax, tt, fr / 1000, 20 * log10(A.' / ref));
        set(ax, "YDir", "normal", "CLim", [-rangeDb 0], font{:}, "FontSize", FontPt, ...
            "YTick", [2 6 10], "YTickLabel", ["2" "6" "10 kHz"], "TickLength", [0.005 0.005]);
        colormap(ax, 1 - pink(256));
        xline(ax, 0, ":", "Color", [0.35 0.35 0.35], "LineWidth", 1.5);
        if p == 1
            title(ax, kinds(col), "FontSize", FontPt + 4, "FontWeight", "normal");
        end
        if col == cols(1)
            ylabel(ax, s.title, "Rotation", 0, "HorizontalAlignment", "right", ...
                "VerticalAlignment", "middle", "FontSize", FontPt + 2);
        else
            set(ax, "YTickLabel", []);      % same frequencies as the column on the left
        end
        if col == 1
            set(ax, "XTick", [0 100 200]);
        else
            set(ax, "XTick", [0 0.5 1]);
        end
        set(ax, "XTickLabelRotation", 0);
        if p == size(panels, 1)
            xlabel(ax, timeLabels(col), "FontSize", FontPt + 2);
        else
            set(ax, "XTickLabel", []);
        end
    end
end
cb = colorbar(ax);
cb.Layout.Tile = "east";
cb.Label.String = "Level (dB)";
cb.Ticks = -60:20:0;
cb.FontName = "Trebuchet MS";
cb.FontSize = FontPt;
cb.Label.FontSize = FontPt + 2;
exportgraphics(fig, path, "Resolution", 300);
fprintf("Saved %s\n", path);
close(fig);
end


function [x, fs] = load_counts(raw, name)
m = load(fullfile(raw, name + ".mat"), "counts", "fs_hz");
x = double(m.counts(:));
fs = double(m.fs_hz);
end


function t0 = pour_start(x, fs, riseDb)
%POUR_START Time (s) where the 5 ms RMS above 100 Hz, in dB and averaged over 0.5 s, first
%   rises riseDb above its level 0.1 to 0.6 s into the recording. The high-pass is an FFT
%   brick wall, so no toolbox is needed.
n = numel(x);
X = fft(x - mean(x));
f = (0:n - 1)' * fs / n;
X(f < 100 | f > fs - 100) = 0;
y = real(ifft(X));
level = 10 * log10(movmean(y .^ 2, round(0.005 * fs)) + eps);
step = round(fs / 1000);                                   % 1 kHz, as det-v1
level = level(1:step:end);
t = (0:numel(level) - 1)' / 1000;
smoothed = movmean(level, 500);
start = median(smoothed(t > 0.1 & t < 0.6));
t0 = t(find(smoothed > start + riseDb, 1));
end


function [A, t] = block_max(W, t, ncols)
%BLOCK_MAX At most ncols time columns, each the largest value of its block of samples, so
%   short impacts stay visible when a long window is drawn.
k = max(1, floor(size(W, 1) / ncols));
m = floor(size(W, 1) / k) * k;
A = squeeze(max(reshape(W(1:m, :), k, m / k, []), [], 1));
t = t(ceil(k / 2):k:m);
end
