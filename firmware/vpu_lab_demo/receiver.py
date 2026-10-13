import socket
import csv
import os
from datetime import datetime

HOST = "0.0.0.0"
PORT = 5005

os.makedirs("data", exist_ok=True)

filename = os.path.join(
    "data",
    datetime.now().strftime("vpu_tcp_%Y%m%d_%H%M%S.csv")
)

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((HOST, PORT))
server.listen(1)

print(f"TCP receiver listening on port {PORT}")
print(f"Saving to: {filename}")
print("Waiting for ESP32...\n")

conn, addr = server.accept()

print(f"ESP32 connected from {addr[0]}")
print("Press Control+C to stop.\n")

reader = conn.makefile("r")

last_batch = None
total_samples = 0
missing_batches = 0

with open(filename, "w", newline="") as f:
    writer = csv.writer(f)

    writer.writerow([
        "batch",
        "sample_index",
        "esp_timestamp_us",
        "vibration"
    ])

    current_batch = None
    expected_samples = 0
    samples = []

    try:
        for raw_line in reader:
            line = raw_line.strip()

            if not line:
                continue

            if line.startswith("BATCH,"):
                parts = line.split(",")

                current_batch = int(parts[1])
                expected_samples = int(parts[2])
                samples = []

            elif line == "END":
                if current_batch is None:
                    continue

                if last_batch is not None:
                    expected_batch = last_batch + 1

                    if current_batch != expected_batch:
                        missed = current_batch - expected_batch

                        if missed > 0:
                            missing_batches += missed

                            print(
                                f"WARNING: missed {missed} batch(es) "
                                f"before batch {current_batch}"
                            )

                for i, (timestamp_us, vibration) in enumerate(samples):
                    writer.writerow([
                        current_batch,
                        i,
                        timestamp_us,
                        vibration
                    ])

                f.flush()

                total_samples += len(samples)

                print(
                    f"Batch {current_batch}: "
                    f"{len(samples)}/{expected_samples} samples"
                )

                last_batch = current_batch
                current_batch = None
                samples = []

            else:
                parts = line.split(",")

                if len(parts) == 2 and current_batch is not None:
                    timestamp_us = int(parts[0])
                    vibration = float(parts[1])

                    samples.append(
                        (timestamp_us, vibration)
                    )

    except KeyboardInterrupt:
        print("\nStopped.")

    finally:
        print(f"Total samples: {total_samples}")
        print(f"Missing batches: {missing_batches}")
        print(f"Saved to: {filename}")

        reader.close()
        conn.close()
        server.close()
