close all
clear
clc

% Signal with fixed frequency content at 20,40,and 85 Hz
t = (0:1/4096:2)';
x = sin(2*pi*40*t) + sin(2*pi*20*t) + 2*sin(2*pi*85*t);

% plotwt(t,x,F0,Ff,Fn,Fa,miri,mirf,chp1,chp2,Label)

figure
subplot(211)
plot(t,x,'k-')
xlabel('Time (s)')
ylabel('Signal')

subplot(212)
plotwt(t,x,1,125,500,4,'o','o',0.2,0.2,'sig')

% Shock-type signal: half-sine pulse

t = (0:1/2^13:2)';
td = 0.001;
x = sin(pi/td*t).*(t<=td);

figure
subplot(211)
plot(t,x,'k-')
xlabel('Time (s)')
ylabel('Signal')

subplot(212)
plotwt(t,x,1,1000,1000,2,'','e',0.2,0.2,'sig')