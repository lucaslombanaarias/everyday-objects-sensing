function P = wavelet_params()
%WAVELET_PARAMS Parameters of wavelet_conditions and test_wavelet_metrics, in one place.
%
%   Transform (freq_inst_morlet): Fmin to Fmax in Nf linearly spaced frequencies, Morlet
%   center frequency Fa. Fmin is 100 Hz because the VPU's specified band starts there and
%   det-v1 high-passes there too.
%
%   Window: Pre before the event time to Post after it, transformed with Pad of real signal
%   on each side and then cropped. If the next event falls inside the window, the window ends
%   NextGap before it.
%
%   Ring time: from the band envelope's peak until it stays RingDropDb below the peak.
%   Ringing frequency: the strongest local maximum inside the band (not its lowest or highest
%   frequency) of the energy from RingFrom to RingTo after the band peak (at least RingMinS of
%   that interval inside the window) that is at least RingSnrDb above the floor there.
%
%   Floor (background): each trial's quiet segment, from QuietFrom after the start of the
%   recording to QuietGap before its first det-v1 event (for a noise recording, everything
%   after QuietFrom), through the same transform as the events: up to NoiseWindows windows
%   as long as an event window, as many as fit without overlapping, every NoiseDecim-th
%   sample. wavelet_background.csv lists quiet segments of at least QuietMinS, with a Welch
%   spectrum (WelchSeg-sample Hann segments, half overlapping): band powers in BgBands and
%   peaks in BgPeakBands.

P.Fmin = 100;               % Hz
P.Fmax = 10000;             % Hz
P.Nf = 300;
P.Fa = 2;

P.Pre = 0.100;              % s
P.Post = 0.250;             % s
P.Pad = 0.100;              % s
P.NextGap = 0.010;          % s

P.Bands = [100 500; 500 2000; 2000 8000];       % Hz, lower edge included, upper excluded
P.BandNames = ["100-500 Hz" "500-2000 Hz" "2-8 kHz"];
P.BandTags = ["b100_500" "b500_2000" "b2k_8k"];

P.RingDropDb = 20;
P.RingFrom = 0.020;         % s after the band peak
P.RingTo = 0.100;           % s after the band peak
P.RingMinS = 0.040;         % s
P.RingSnrDb = 10;

P.QuietFrom = 1.0;          % s from the start of the recording, as det-v1
P.QuietGap = 0.15;          % s before the first det-v1 event
P.QuietMinS = 0.3;          % s
P.NoiseWindows = 40;
P.NoiseDecim = 24;          % 0.5 ms at 48 kHz
P.WelchSeg = 4800;          % samples: 10 Hz resolution at 48 kHz
P.BgBands = [4000 5000; 1500 1800];             % Hz
P.BgPeakBands = [4000 5100; 1500 1800];         % Hz
P.BgTags = ["4_5khz" "1p5_1p8khz"];

P.RangeDb = 50;             % color range of the scalogram figures
P.Normalization = "amplitude-corrected |W|: |W| / sqrt(Fa/(2f)), so a steady sine of " + ...
    "amplitude A counts reads A at its frequency";
end
