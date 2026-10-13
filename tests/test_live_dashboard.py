import numpy as np
import pytest

from live_dashboard import Stream, parse_line

# Exact line printed by firmware/vpu_lab_demo/VPU_Chair_Serial at ~200 lines/s.
VPU_LINE = "vibration:37.5\tfloor:0\tceiling:100"
VPU_LABELS = ["vibration", "floor", "ceiling"]


def vpu_lines(values):
    return [f"vibration:{v:.1f}\tfloor:0\tceiling:100" for v in values]


# --------------------------------------------------------------------------- parse_line


def test_parse_vpu_line_labels_and_values():
    assert parse_line(VPU_LINE) == (VPU_LABELS, [37.5, 0.0, 100.0])


@pytest.mark.parametrize(
    "text",
    ["1,2,3", "1 2 3", "1\t2\t3", "1;2;3", "  1, 2 ,3\r\n"],
    ids=["comma", "space", "tab", "semicolon", "padded"],
)
def test_parse_unlabelled_separators(text):
    assert parse_line(text) == (["", "", ""], [1.0, 2.0, 3.0])


def test_parse_label_separator_spacing_and_equals():
    assert parse_line("ax : 0.5 ay=-1e-3") == (["ax", "ay"], [0.5, -0.001])


@pytest.mark.parametrize(
    "text",
    [
        "# VPU chair sensor starting",
        "# calibrated, change threshold = 0.0412",
        "WiFi connected",
        "vibration:abc\tfloor:0",
        "vibration:nan",
        "vibration:inf",
        "",
        "   \r\n",
    ],
)
def test_parse_rejects_non_numeric(text):
    assert parse_line(text) is None


# --------------------------------------------------------------------------- Stream.feed


def test_feed_takes_labels_from_first_line():
    s = Stream()
    s.feed(vpu_lines([1.0, 2.0]))
    assert s.labels == VPU_LABELS
    assert (s.good, s.bad, s.total) == (2, 0, 2)


def test_feed_names_unlabelled_channels():
    s = Stream()
    s.feed(["1,2", "3,4"])
    assert s.labels == ["ch1", "ch2"]


def test_feed_decodes_bytes():
    s = Stream()
    s.feed([VPU_LINE.encode("ascii") + b"\r"])
    data, total, labels = s.snapshot(1)
    assert total == 1
    assert labels == VPU_LABELS
    np.testing.assert_array_equal(data, [[37.5, 0.0, 100.0]])


def test_feed_hash_lines_add_no_samples():
    s = Stream()
    s.feed(["# VPU chair sensor starting", "# calibrating, keep the chair still..."])
    assert s.snapshot(10) == (None, 0, [])
    s.feed(vpu_lines([5.0]) + ["# calibrated, change threshold = 0.0412"] + vpu_lines([6.0]))
    data, total, labels = s.snapshot(10)
    assert labels == VPU_LABELS
    assert total == s.good == 2
    np.testing.assert_array_equal(data[:, 0], [5.0, 6.0])


def test_feed_counts_bad_lines_and_keeps_last():
    s = Stream()
    s.feed(vpu_lines([1.0]) + ["garbage", "", "  ", "vibration:x"] + vpu_lines([2.0]))
    assert s.good == 2
    assert s.bad == 2  # blank lines are not counted
    assert s.last_bad == "vibration:x"


def test_feed_hash_lines_count_as_skipped():
    s = Stream()
    s.feed(["# VPU chair sensor starting"] + vpu_lines([1.0]))
    assert (s.good, s.bad) == (1, 1)
    assert s.last_bad == "# VPU chair sensor starting"


def test_feed_drops_torn_lines_with_wrong_channel_count():
    s = Stream()
    s.feed(vpu_lines([1.0, 2.0]) + ["vibration:3.0\tfloor:0", "0\tceiling:100"] + vpu_lines([4.0]))
    data, total, labels = s.snapshot(10)
    assert labels == VPU_LABELS
    assert (s.good, s.bad, total) == (3, 2, 3)
    np.testing.assert_array_equal(data[:, 0], [1.0, 2.0, 4.0])


def test_feed_switches_channel_count_after_ten_consistent_lines():
    s = Stream()
    s.feed(vpu_lines([1.0]))
    s.feed([f"{i}" for i in range(9)])
    assert s.labels == VPU_LABELS
    assert s.bad == 9
    s.feed(["9", "10"])
    data, total, labels = s.snapshot(10)
    assert labels == ["ch1"]
    assert total == 2
    np.testing.assert_array_equal(data[:, 0], [9.0, 10.0])


def test_feed_writes_rows_to_recorder():
    rows = []

    class Recorder:
        def writerows(self, it):
            rows.extend(it)

    s = Stream()
    s.recorder = Recorder()
    s.feed(vpu_lines([1.0, 2.0]) + ["# status"])
    assert [r[1:] for r in rows] == [[1.0, 0.0, 100.0], [2.0, 0.0, 100.0]]
    assert all(isinstance(r[0], float) for r in rows)


def test_reset_clears_data_and_stats():
    s = Stream()
    s.feed(vpu_lines([1.0]) + ["junk"])
    s.bytes = 99
    s.reset()
    assert s.snapshot(10) == (None, 0, [])
    assert (s.good, s.bad, s.bytes, s.labels, s.last_bad) == (0, 0, 0, [], "")


# --------------------------------------------------------------------------- Stream.snapshot


def test_snapshot_before_data():
    assert Stream().snapshot(100) == (None, 0, [])


@pytest.mark.parametrize("m, expected", [(0, 0), (3, 3), (5, 5), (50, 5)])
def test_snapshot_length_is_capped_by_total(m, expected):
    s = Stream()
    s.feed(vpu_lines([1.0, 2.0, 3.0, 4.0, 5.0]))
    data, total, labels = s.snapshot(m)
    assert data.shape == (expected, 3)
    assert total == 5
    assert labels == VPU_LABELS
    np.testing.assert_array_equal(data[:, 0], [1.0, 2.0, 3.0, 4.0, 5.0][5 - expected:])


def test_snapshot_wraps_ring_buffer_in_order():
    s = Stream()
    s.CAP = 8
    for chunk in range(4):
        s.feed(vpu_lines(range(chunk * 5, chunk * 5 + 5)))
    data, total, _ = s.snapshot(100)
    assert total == 20
    assert data.shape == (8, 3)
    np.testing.assert_array_equal(data[:, 0], np.arange(12, 20))


def test_snapshot_returns_a_copy():
    s = Stream()
    s.feed(vpu_lines([1.0, 2.0]))
    data, _, labels = s.snapshot(2)
    data[:] = -1
    labels.append("x")
    again, _, labels_again = s.snapshot(2)
    np.testing.assert_array_equal(again[:, 0], [1.0, 2.0])
    assert labels_again == VPU_LABELS
