function test_wavelet_metrics(opts)
%TEST_WAVELET_METRICS Checks of the wavelet metrics used by wavelet_conditions.
%
%   test_wavelet_metrics()      run from the repo root with matlab/wavelet on the path
%
%   1. A 1 kHz sine of amplitude 100 counts at 48 kHz reads 100 within 1% after the
%      amplitude correction (the largest value over frequency, at every sample of a window).
%   2. Exponentially decaying 4.5 kHz sines with 20 dB decay times of 10 ms and 40 ms give
%      ring times in the 2-8 kHz band within 10% of those values.
%   3. For the drops, the sign of enclosed minus bare level in each band in
%      wavelet_summary.csv (written by wavelet_conditions) matches comparison.csv. Skipped if
%      wavelet_summary.csv does not exist yet.
%   Also printed, not checked: the ring time of a one-sample impulse in each band, which is
%   the shortest ring time the transform can show there, and the walking signs.
%
%   Errors at the end if any check fails.

arguments
    opts.Root (1,1) string = pwd
    opts.Date (1,1) string = "2026-10-21"
end

P = wavelet_params();
fs = 48000;
quiet.env_db = -Inf(1, size(P.Bands, 1));
quiet.psd = zeros(1, P.Nf);
t = (0:round(1.0 * fs) - 1)' / fs;
t0 = 0.4;
i0 = round((t0 - P.Pre) * fs) + 1;
i1 = round((t0 + P.Post) * fs) + 1;
ok = true;

% 1. Amplitude correction
x = 100 * sin(2 * pi * 1000 * t);
[Wc, ~, fr] = wavelet_window(x, fs, i0, i1, P);
[ridge, k] = max(Wc, [], 2);
err = max(abs(ridge - 100)) / 100;
pass = err <= 0.01;
ok = ok && pass;
fprintf("1. 1 kHz sine, amplitude 100: reads %.2f to %.2f (median %.2f) at %.0f Hz, " + ...
    "largest error %.2f%%: %s\n", min(ridge), max(ridge), median(ridge), median(fr(k)), ...
    100 * err, verdict(pass));

% 2. Ring time of decaying sines
for T20 = [0.010 0.040]
    tau = T20 / log(10);                           % amplitude falls by 10 (20 dB) in T20
    x = zeros(size(t));
    on = t >= t0;
    x(on) = 1000 * exp(-(t(on) - t0) / tau) .* sin(2 * pi * 4500 * (t(on) - t0));
    [Wc, tw, fr] = wavelet_window(x, fs, i0, i1, P);
    M = wavelet_band_metrics(Wc, tw, fr, t0, quiet, P);
    ring = M.ring_ms(3);
    pass = abs(ring - T20 * 1e3) <= 0.1 * T20 * 1e3;
    ok = ok && pass;
    fprintf("2. 4.5 kHz sine, 20 dB decay in %.0f ms: ring time %.2f ms (%s), " + ...
        "ringing frequency %.0f Hz: %s\n", T20 * 1e3, ring, M.ring_status(3), M.ring_hz(3), ...
        verdict(pass));
end

% Shortest ring time per band: a one-sample impulse
x = zeros(size(t));
x(round(t0 * fs) + 1) = 1000;
[Wc, tw, fr] = wavelet_window(x, fs, i0, i1, P);
M = wavelet_band_metrics(Wc, tw, fr, t0, quiet, P);
for b = 1:size(P.Bands, 1)
    fprintf("   Impulse, %s: ring time %.2f ms (not checked: the resolution limit)\n", ...
        P.BandNames(b), M.ring_ms(b));
end

% 3. Drops: sign of enclosed minus bare against comparison.csv
ana = fullfile(opts.Root, "data", "controlled", "analysis", opts.Date);
sFile = fullfile(ana, "wavelet_summary.csv");
if ~isfile(sFile)
    fprintf("3. Skipped: %s does not exist yet (run wavelet_conditions first)\n", sFile);
else
    S = readtable(sFile, "TextType", "string", "Delimiter", ",");
    C = readtable(fullfile(ana, "comparison.csv"), "TextType", "string", "Delimiter", ",");
    for exc = ["drop4in" "walk"]
        rows = find(S.excitation == exc & S.condition == "enclosed");
        nMatch = 0;
        nAll = 0;
        for i = rows'
            for b = 1:size(P.Bands, 1)
                w = S.("level_enc_minus_bare_db_" + P.BandTags(b))(i);
                ci = C.floor == S.floor(i) & C.excitation == exc & ...
                    C.radius_m == S.radius_m(i) & C.level == P.BandNames(b);
                if ~any(ci) || isnan(w)
                    continue
                end
                c = C.enclosed_minus_bare_db(ci);
                same = sign(w) == sign(c);
                nMatch = nMatch + same;
                nAll = nAll + 1;
                if exc == "drop4in"
                    ok = ok && same;
                end
                fprintf("3. %-7s %-6s %g m %-11s comparison.csv %+5.1f dB (separated: %-3s), " + ...
                    "wavelet %+5.1f dB: %s\n", exc, S.floor(i), S.radius_m(i), P.BandNames(b), ...
                    c, C.separated(ci), w, ternary(same, "same sign", "DIFFERENT SIGN"));
            end
        end
        fprintf("3. %s: %d of %d bands have the same sign%s\n", exc, nMatch, nAll, ...
            ternary(exc == "drop4in", "", " (walking: printed, not checked)"));
    end
end

if ~ok
    error("test_wavelet_metrics:fail", "At least one check failed (see above).");
end
fprintf("All checks passed.\n");
end

function s = verdict(pass)
s = ternary(pass, "PASS", "FAIL");
end

function s = ternary(c, a, b)
if c
    s = a;
else
    s = b;
end
end
