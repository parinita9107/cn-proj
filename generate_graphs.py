import csv
import os
from collections import defaultdict, Counter
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = "results/final/graphs"
os.makedirs(OUT, exist_ok=True)

# Dynamic traffic graph
data = defaultdict(list)

with open("results/final/flowstats.csv") as f:
    for r in csv.DictReader(f):
        if r["flow_id"] == "10.0.0.4>10.0.0.5:5002":
            data[r["dpid"]].append(
                (float(r["ts"]), float(r["rate_mbps"]))
            )

plt.figure(figsize=(11, 6))
for dpid, samples in sorted(data.items()):
    samples.sort()
    start = samples[0][0]
    plt.plot(
        [x[0] - start for x in samples],
        [x[1] for x in samples],
        label=f"Switch s{dpid}"
    )

plt.axhline(10, linestyle="--", label="Target: 10 Mbps")
plt.xlabel("Elapsed time (seconds)")
plt.ylabel("Traffic rate (Mbps)")
plt.title("Dynamic UDP Traffic Rate")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.savefig(f"{OUT}/dynamic_traffic_rate.png", dpi=200)
plt.close()

# Static vs dynamic bandwidth
plt.figure(figsize=(8, 5))
bars = plt.bar(["Static", "Dynamic"], [14.9, 10.3])
plt.axhline(10, linestyle="--", label="Target: 10 Mbps")
for bar, value in zip(bars, [14.9, 10.3]):
    plt.text(bar.get_x() + bar.get_width()/2,
             value + 0.2, f"{value} Mbps", ha="center")
plt.ylabel("Receiver bandwidth (Mbps)")
plt.title("Static vs Dynamic UDP Bandwidth")
plt.legend()
plt.tight_layout()
plt.savefig(f"{OUT}/static_vs_dynamic.png", dpi=200)
plt.close()

# Controller events
counts = Counter()
with open("results/final/controller_events.csv") as f:
    for r in csv.DictReader(f):
        counts[r["event"]] += 1

names = ["test_start", "rate_limit_on", "rate_limit_off", "flow_removed"]
labels = ["Test started", "Limit ON", "Limit OFF", "Flow removed"]
values = [counts[n] for n in names]

plt.figure(figsize=(9, 5))
bars = plt.bar(labels, values)
for bar, value in zip(bars, values):
    plt.text(bar.get_x() + bar.get_width()/2,
             value + 1, str(value), ha="center")
plt.ylabel("Event count")
plt.title("Dynamic Controller Events")
plt.tight_layout()
plt.savefig(f"{OUT}/controller_events.png", dpi=200)
plt.close()

print("Successfully generated:")
for name in os.listdir(OUT):
    print(name)
