function [Wc, t, fr] = wavelet_window(x, fs, i0, i1, P)
%WAVELET_WINDOW Amplitude-corrected Morlet transform of samples i0 to i1 of x.
%
%   [Wc, t, fr] = wavelet_window(x, fs, i0, i1, P)
%
%   Transforms P.Pad seconds of real signal on each side of the window (less at the ends of
%   the recording) with freq_inst_morlet, crops back to i0:i1, and divides |W| by
%   sqrt(P.Fa/(2f)), so a steady sine of amplitude A counts reads A at its frequency (as in
%   compare_floors_wavelet). P comes from wavelet_params.
%
%   Wc is samples x frequencies, t the time of each sample (s, sample 1 at 0) and fr the
%   frequencies (Hz, 1 x P.Nf).

x = x(:);
np = round(P.Pad * fs);
a = max(1, i0 - np);
b = min(numel(x), i1 + np);
[~, fr, W] = freq_inst_morlet(x(a:b), fs, P.Fmin, P.Fmax, P.Nf, P.Fa);
Wc = W((i0 - a + 1):(i1 - a + 1), :) ./ sqrt(P.Fa ./ (2 * fr));
t = ((i0:i1)' - 1) / fs;
end
