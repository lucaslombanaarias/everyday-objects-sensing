function M = wavelet_band_metrics(Wc, t, fr, t0, N, P)
%WAVELET_BAND_METRICS Band levels, ring times, ringing frequencies and overall peak of one window.
%
%   M = wavelet_band_metrics(Wc, t, fr, t0, N, P)
%
%   Wc, t and fr come from wavelet_window, t0 is the event time (s), and P from wavelet_params.
%   N is the floor (the background, from wavelet_conditions' quiet_floor): N.env_db (1 x bands,
%   median band envelope, dB re 1 count), N.psd (1 x P.Nf, median of Wc^2 at each frequency,
%   counts^2) and N.bg_pow (1 x bands, mean square band envelope, counts^2). Use
%   N.env_db = -Inf, N.psd = 0 and N.bg_pow = 0 for no floor. Times are in ms from t0.
%
%   In each band of P.Bands:
%   - band envelope: the RMS of Wc over the band's frequencies, at each sample (counts).
%   - level_db: 10*log10 of the envelope's energy over the window, dB re 1 count^2 s.
%     level_above_floor_db subtracts the floor envelope's energy over the same length.
%   - band_peak_db and band_peak_ms: the envelope's peak (dB re 1 count) and its time.
%   - ring_ms: from the envelope's peak until it stays P.RingDropDb below it. NaN, with
%     ring_status "not resolved: floor", if the envelope reaches the floor first (or the
%     peak is less than P.RingDropDb above it), and "not resolved: window end" if the
%     envelope is still within P.RingDropDb of the peak at the end of the window.
%   - ring_first_ms: when the envelope first falls P.RingDropDb below the peak, whether or not
%     it stays there (it differs from ring_ms when something re-excites the band).
%   - ring_hz: of the energy from P.RingFrom to P.RingTo after the band peak, the strongest
%     local maximum inside the band (not its lowest or highest frequency) that is at least
%     P.RingSnrDb above N.psd there (ring_hz_snr_db); "no peak in band" if there is none.
%     ring_hz_in_background is "yes" if N.psd also has a local maximum within one frequency
%     of it, that is, if the background peaks there too.
%   - bg_share_db: the floor's energy over the window (N.bg_pow times the window length)
%     over the window's energy in the band, in dB.
%   And over all frequencies, peak_db, peak_ms and peak_hz: the largest value of Wc.

dt = t(2) - t(1);
T = numel(t) * dt;
nb = size(P.Bands, 1);
M.level_db = nan(1, nb);
M.level_above_floor_db = nan(1, nb);
M.band_peak_db = nan(1, nb);
M.band_peak_ms = nan(1, nb);
M.ring_ms = nan(1, nb);
M.ring_status = strings(1, nb);
M.ring_first_ms = nan(1, nb);
M.ring_hz = nan(1, nb);
M.ring_hz_snr_db = nan(1, nb);
M.ring_hz_status = strings(1, nb);
M.ring_hz_in_background = strings(1, nb);
M.bg_share_db = nan(1, nb);

bgPeak = false(size(N.psd));                       % local maxima of the floor's spectrum
if numel(N.psd) > 2
    bgPeak(2:end - 1) = N.psd(2:end - 1) > N.psd(1:end - 2) & N.psd(2:end - 1) > N.psd(3:end);
end

for b = 1:nb
    ib = find(fr >= P.Bands(b, 1) & fr < P.Bands(b, 2));
    env = sqrt(mean(Wc(:, ib).^2, 2));
    envDb = 20 * log10(env);
    M.level_db(b) = 10 * log10(sum(env.^2) * dt);
    M.level_above_floor_db(b) = M.level_db(b) - (N.env_db(b) + 10 * log10(T));
    M.bg_share_db(b) = 10 * log10(N.bg_pow(b) * T) - M.level_db(b);

    [pk, p] = max(envDb);
    M.band_peak_db(b) = pk;
    M.band_peak_ms(b) = (t(p) - t0) * 1e3;

    % Ring time
    thr = pk - P.RingDropDb;
    last = find(envDb >= thr, 1, "last");          % at or after the peak
    first = find(envDb(p:end) < thr, 1);
    if ~isempty(first)
        M.ring_first_ms(b) = (first - 1) * dt * 1e3;
    end
    if thr <= N.env_db(b)
        M.ring_status(b) = "not resolved: floor";
    elseif last == numel(envDb)
        M.ring_status(b) = "not resolved: window end";
    elseif any(envDb(p:last) <= N.env_db(b))
        M.ring_status(b) = "not resolved: floor";
    else
        M.ring_status(b) = "resolved";
        M.ring_ms(b) = (last + 1 - p) * dt * 1e3;
    end

    % Ringing frequency
    tA = t(p) + P.RingFrom;
    tB = min(t(p) + P.RingTo, t(end));
    if tB - tA < P.RingMinS
        M.ring_hz_status(b) = "not resolved: window end";
        continue
    end
    sel = t >= tA & t <= tB;
    e = mean(Wc(sel, ib).^2, 1);
    snr = 10 * log10(e ./ N.psd(ib));
    k = 2:numel(ib) - 1;
    ok = false(1, numel(ib));
    ok(k) = e(k) > e(k - 1) & e(k) > e(k + 1) & snr(k) >= P.RingSnrDb;
    if ~any(ok)
        M.ring_hz_status(b) = "no peak in band";
        continue
    end
    e(~ok) = -Inf;
    [~, j] = max(e);
    g = ib(j);
    M.ring_hz(b) = fr(g);
    M.ring_hz_snr_db(b) = snr(j);
    M.ring_hz_status(b) = "resolved";
    if any(bgPeak(max(1, g - 1):min(numel(fr), g + 1)))
        M.ring_hz_in_background(b) = "yes";
    else
        M.ring_hz_in_background(b) = "no";
    end
end

[pk, k] = max(Wc(:));
[r, c] = ind2sub(size(Wc), k);
M.peak_db = 20 * log10(pk);
M.peak_ms = (t(r) - t0) * 1e3;
M.peak_hz = fr(c);
end
