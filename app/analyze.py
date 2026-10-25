
import os
import glob
import argparse
import pandas as pd
import matplotlib.pyplot as plt


def find_column(df, names):
    columns = {c.lower().strip(): c for c in df.columns}
    for name in names:
        if name.lower() in columns:
            return columns[name.lower()]
    return None


def load_logs(folder, pattern):
    files = glob.glob(os.path.join(folder, "**", pattern), recursive=True)
    frames = []

    for file in files:
        try:
            df = pd.read_csv(file)
            if not df.empty:
                df["source_file"] = os.path.basename(file)
                frames.append(df)
        except Exception as error:
            print(f"Could not read {file}: {error}")

    if frames:
        return pd.concat(frames, ignore_index=True)

    return pd.DataFrame()


def save_throughput_graph(sender, receiver, flows, output):
    plt.figure(figsize=(10, 5))
    plotted = False

    datasets = [
        ("Sender", sender),
        ("Receiver", receiver),
        ("Network", flows)
    ]

    for label, df in datasets:
        if df.empty:
            continue

        rate_col = find_column(
            df,
            ["rate_mbps", "throughput_mbps", "tx_mbps",
             "rx_mbps", "app_throughput_mbps",
             "network_throughput_mbps"]
        )

        if rate_col:
            values = pd.to_numeric(df[rate_col], errors="coerce")
            plt.plot(values.reset_index(drop=True), label=label)
            plotted = True

    if plotted:
        plt.xlabel("Sample")
        plt.ylabel("Throughput (Mbps)")
        plt.title("Application and Network Throughput")
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(output, "throughput.png"))
    else:
        print("Throughput columns not found. Check CSV column names.")

    plt.close()


def save_loss_graph(receiver, output):
    if receiver.empty:
        print("No receiver logs available for packet-loss graph.")
        return

    loss_col = find_column(
        receiver,
        ["loss_pct", "loss_percent", "packet_loss_pct"]
    )

    if not loss_col:
        print("No packet-loss column found in receiver CSV.")
        return

    values = pd.to_numeric(receiver[loss_col], errors="coerce")

    plt.figure(figsize=(10, 5))
    plt.plot(values.reset_index(drop=True))
    plt.xlabel("Sample")
    plt.ylabel("Packet Loss (%)")
    plt.title("Packet Loss")
    plt.tight_layout()
    plt.savefig(os.path.join(output, "packet_loss.png"))
    plt.close()


def save_byte_graph(sender, receiver, flows, output):
    measurements = {}

    for label, df, candidates in [
        ("Sender bytes", sender, ["tx_bytes", "bytes", "payload_bytes"]),
        ("Receiver bytes", receiver, ["rx_bytes", "bytes", "payload_bytes"]),
        ("Network bytes", flows, ["byte_count", "delta_bytes", "bytes"])
    ]:
        if df.empty:
            continue

        col = find_column(df, candidates)

        if col:
            values = pd.to_numeric(df[col], errors="coerce").dropna()

            if not values.empty:
                measurements[label] = values.max()

    if not measurements:
        print("No byte-count columns found.")
        return

    pd.DataFrame(
        list(measurements.items()),
        columns=["measurement", "bytes"]
    ).to_csv(os.path.join(output, "byte_comparison.csv"), index=False)

    plt.figure(figsize=(8, 5))
    plt.bar(list(measurements.keys()), list(measurements.values()))
    plt.ylabel("Bytes")
    plt.title("Byte Count Comparison")
    plt.tight_layout()
    plt.savefig(os.path.join(output, "byte_comparison.png"))
    plt.close()


def create_summary(sender, receiver, flows, output):
    rows = []

    for name, df in [
        ("sender", sender),
        ("receiver", receiver),
        ("flowstats", flows)
    ]:
        if df.empty:
            continue

        rate_col = find_column(
            df,
            ["rate_mbps", "throughput_mbps", "tx_mbps",
             "rx_mbps", "app_throughput_mbps",
             "network_throughput_mbps"]
        )

        bytes_col = find_column(
            df, ["tx_bytes", "rx_bytes", "byte_count", "bytes"]
        )

        packets_col = find_column(
            df, ["tx_pkts", "rx_pkts", "packet_count", "packets"]
        )

        loss_col = find_column(
            df, ["loss_pct", "loss_percent", "packet_loss_pct"]
        )

        rows.append({
            "log_type": name,
            "rows": len(df),
            "mean_throughput_mbps": (
                pd.to_numeric(df[rate_col], errors="coerce").mean()
                if rate_col else None
            ),
            "max_bytes": (
                pd.to_numeric(df[bytes_col], errors="coerce").max()
                if bytes_col else None
            ),
            "max_packets": (
                pd.to_numeric(df[packets_col], errors="coerce").max()
                if packets_col else None
            ),
            "mean_loss_pct": (
                pd.to_numeric(df[loss_col], errors="coerce").mean()
                if loss_col else None
            )
        })

    pd.DataFrame(rows).to_csv(
        os.path.join(output, "summary.csv"), index=False
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results")
    args = parser.parse_args()

    os.makedirs(args.results, exist_ok=True)
    output = os.path.join(args.results, "analysis")
    os.makedirs(output, exist_ok=True)

    sender = load_logs(args.results, "sender*.csv")
    receiver = load_logs(args.results, "receiver*.csv")
    flows = load_logs(args.results, "flowstats*.csv")
    events = load_logs(args.results, "controller_events*.csv")

    if sender.empty and receiver.empty and flows.empty:
        print("No CSV logs found in", args.results)
        print("Run an experiment and put its CSV files under results/.")
        return

    save_throughput_graph(sender, receiver, flows, output)
    save_loss_graph(receiver, output)
    save_byte_graph(sender, receiver, flows, output)
    create_summary(sender, receiver, flows, output)

    if not events.empty:
        events.to_csv(
            os.path.join(output, "controller_events.csv"),
            index=False
        )

    print("Analysis finished.")
    print("Output folder:", output)


if __name__ == "__main__":
    main()
