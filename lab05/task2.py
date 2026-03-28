import numpy as np
import matplotlib.pyplot as plt

F = 15e9
u_s = 30e6
d_i = 2e6
Ns = [10, 100, 1000]
us_mbps = [0.3, 0.7, 2.0]
us_bps = [u * 1e6 for u in us_mbps]

labels = []
cs_times_hrs = []
p2p_times_hrs = []
for N in Ns:
    for u_mbps, u_bps in zip(us_mbps, us_bps):
        if u_mbps >= 1.0:
            labels.append(f"N={N}\nu={int(u_mbps)} Мбит/с")
        else:
            labels.append(f"N={N}\nu={int(u_mbps*1000)} Кбит/с")
        cs_times_hrs.append(max(N * F / u_s, F / d_i) / 3600)
        p2p_times_hrs.append(max(F / u_s, F / d_i, N * F / (u_s + N * u_bps)) / 3600)

x = np.arange(len(labels))
width = 0.38
plt.figure(figsize=(14, 7))
plt.bar(x - width / 2, cs_times_hrs, width, label="Клиент-серверная")
plt.bar(x + width / 2, p2p_times_hrs, width, label="Одноранговая")
plt.xticks(x, labels, rotation=0)
plt.ylabel("Минимальное время раздачи, часы")
plt.legend()
plt.grid(axis="y", linestyle="--", alpha=0.7)
plt.tight_layout()
plt.show()
