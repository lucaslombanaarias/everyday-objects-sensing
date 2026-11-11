function [tnew,interval_freq,module,resut3] = freq_inst_morlet(xnew,FS,fi,ff,nf,Fo)
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% Wavelet Transform using Morlet Wavelet (FFT based, block processed)
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
%
% Inputs
% ------
% xnew = signal (vector)
% FS   = sampling frequency (Hz)
% fi   = low frequency bound (Hz, must be > 0)
% ff   = high frequency bound (Hz)
% nf   = number of frequency points (100-500 is usually plenty)
% Fo   = Morlet center frequency (cycles per wavelet; 2 to 6 is typical)
%
% Outputs
% -------
% tnew          = time vector (1 x N)
% interval_freq = frequency vector (1 x nf)
% module        = |WT|, size N x nf
% resut3        = complex WT, N x nf (only computed if requested)

%% Setup
xnew = xnew(:);                         % force column vector
l_x  = numel(xnew);
dt   = 1/FS;
tnew = (0:l_x-1)*dt;                    % exactly l_x points (no colon round-off)

if fi <= 0
    fi = ff/nf;
    warning('freq_inst_morlet:fi','fi must be > 0 (a = Fo/0 = Inf). Using fi = %.4g Hz.',fi);
end
interval_freq = linspace(fi,ff,nf);     % exactly nf points
a = Fo./interval_freq;                  % 1 x nf scales

%% FFT of signal (positive frequencies only)
nfourier = 2^nextpow2(l_x);
npt      = nfourier/2;
freq     = (0:npt-1)'*(FS/nfourier);    % npt x 1
tff      = fft(xnew,nfourier);
tff      = tff(1:npt);
tff(1)   = 0;                           % remove DC

%% Wavelet transform, a block of frequencies at a time (limits memory)
wantComplex = nargout > 3;
module = zeros(l_x,nf);
if wantComplex
    resut3 = complex(zeros(l_x,nf));
end

blk = 64;
for k1 = 1:blk:nf
    idx = k1:min(k1+blk-1,nf);
    ab  = a(idx);
    K   = sqrt(2*ab).*exp(-0.5*(2*pi*(freq.*ab - Fo)).^2);  % npt x nb (real)
    W   = ifft(K.*tff,nfourier,1);      % zero-pads to nfourier
    W   = W(1:l_x,:);
    module(:,idx) = abs(W);
    if wantComplex
        resut3(:,idx) = W;
    end
end
if ~wantComplex
    resut3 = [];
end
end
