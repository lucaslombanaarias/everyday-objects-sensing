function plotwt(t1,x1,F0,Ff,Fn,Fa,miri,mirf,chp1,chp2,Label)
% Morlet wavelet scalogram with optional odd/even edge mirroring.
%   t1,x1   : time and signal
%   F0,Ff   : frequency range (Hz), F0 must be > 0
%   Fn      : number of frequency points
%   Fa      : Morlet center frequency (2-6 typical)
%   miri    : start mirror: 'o' odd, 'e' even, '' none
%   mirf    : end mirror:   'o' odd, 'e' even, '' none
%   chp1/2  : fraction of signal length used for start/end mirror
%   Label   : 'IMF' squares the plotted magnitude; anything else does not

    if nargin < 11, Label = ''; end

    t1 = t1(:);  x1 = x1(:);
    dt1 = t1(2) - t1(1);
    L   = numel(x1);

    % Mirrored padding (empty if not requested)
    pre  = [];  post = [];
    if ismember(miri,{'o','e'})
        n   = min(max(2,round(L*chp1)),L);
        pre = mirrorStart(x1,n,miri);
    end
    if ismember(mirf,{'o','e'})
        n    = min(max(2,round(L*chp2)),L);
        post = mirrorEnd(x1,n,mirf);
    end
    x_mirror = [pre; x1; post];

    %% Wavelet transform
    [~,fr1,mods1] = freq_inst_morlet(x_mirror,1/dt1,F0,Ff,Fn,Fa);

    % keep only the rows belonging to the original signal
    mods1 = mods1(numel(pre) + (1:L),:);

    %% Plot
    NN = 1 + strcmp(Label,'IMF');
    Z  = (mods1.'/max(mods1(:))).^NN;
    imagesc(t1,fr1,Z)
    set(gca,'YDir','normal','CLim',[0 1])   % CLim works in all releases
    colormap(1-pink(256))
    xlabel('Time (s)')
    ylabel('Frequency (Hz)')
    grid on
end

%% Mirroring helpers
function xm = mirrorStart(x,n,type)
    seg  = x(1:n);
    refl = seg(n:-1:2);                 % n-1 samples
    if type == 'o'
        xm = 2*seg(1) - refl;           % odd about (t0,x0)
    else
        xm = refl;                      % even about t0
    end
end

function xm = mirrorEnd(x,n,type)
    seg  = x(end-n+1:end);
    refl = seg(end-1:-1:1);             % n-1 samples
    if type == 'o'
        xm = 2*seg(end) - refl;
    else
        xm = refl;
    end
end
