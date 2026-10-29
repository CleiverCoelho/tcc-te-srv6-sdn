#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sonda_lag.py — detecta travadas da VM: imprime "epoch atraso_ms" sempre que um
sleep(10 ms) leva mais de 100 ms. Roda ao lado do experimento (sem depender do
Mininet) e serve para marcar repetições afetadas; não descarta nada sozinha.

Uso: python3 -I experimentos/lib/sonda_lag.py > rep/lag.txt &
"""
import time

while True:
    a = time.time()
    time.sleep(0.01)
    g = time.time() - a
    if g > 0.1:
        print("%.3f %.0f" % (a, g * 1000), flush=True)
