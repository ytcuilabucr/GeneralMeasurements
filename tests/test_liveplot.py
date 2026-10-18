import numpy as np

from generalmeasurements.devices.sim import SimLockin, SimMagnet, SimSource
from generalmeasurements.engine import Bus, run
from generalmeasurements.plans import Axis, FlyScan, Scan, linear
from generalmeasurements.sinks import LivePlotSink, PlotBuffer


class FakeChannel:
    def __init__(self):
        self.msgs, self.closed = [], False

    def put_nowait(self, m):
        self.msgs.append(m)

    def close(self):
        self.closed = True


def _meta(shape, mode="stepped", axes=("b", "g"), pts=None, fields=None):
    return {"kind": "start", "mode": mode, "axis_names": list(axes), "shape": list(shape),
            "axis_points": pts or {}, "fields": fields or {"s": "V", "g": "V"}}


def test_buffer_stepped_row_and_image():
    buf = PlotBuffer()
    buf.start(_meta((2, 3), pts={"b": [0, 1], "g": [-1, 0, 1]}))
    for bi in range(2):
        for gi in range(3):
            buf.add((bi, gi), {"b": bi, "g": gi - 1}, {"s": 10 * bi + gi, "g": gi - 1})
        if bi == 0:
            xs, ys = buf.series("s")                    # row 0 complete
            assert list(ys) == [0, 1, 2] and list(xs) == [-1, 0, 1]
    buf.add((1, 0), {}, {"s": 99})                      # revisit: current row is index 1
    xs, ys = buf.series("s")
    assert ys[0] == 99 and ys[1] == 11
    img = buf.image("s")
    assert img.shape == (2, 3) and img[0, 2] == 2 and img[1, 1] == 11


def test_buffer_partial_row_has_nans_not_stale_data():
    buf = PlotBuffer()
    buf.start(_meta((2, 3), pts={"b": [0, 1], "g": [0, 1, 2]}))
    buf.add((0, 0), {}, {"s": 1.0})
    buf.add((1, 0), {}, {"s": 5.0})
    _, ys = buf.series("s")
    assert ys[0] == 5.0 and np.isnan(ys[1:]).all()      # row 1 only, row 0 not mixed in


def test_buffer_x_can_be_measured_readback():
    buf = PlotBuffer()
    buf.start(_meta((3,), axes=("g",), pts={"g": [0, 1, 2]}))
    for i in range(3):
        buf.add((i,), {"g": i}, {"s": i * 2.0, "g": i + 0.01})
    xs, ys = buf.series("s", x="g")                     # "g" is also a data field: readback
    assert np.allclose(xs, [0.01, 1.01, 2.01]) and list(ys) == [0, 2, 4]


def test_buffer_fly_streams():
    buf = PlotBuffer()
    buf.start(_meta((), mode="fly", axes=("B",), fields={"B": "T", "s": "V"}))
    for k in range(4):
        buf.add((0, k), {"B": k * 0.1}, {"B": k * 0.1 + 0.001, "s": k})
    xs, ys = buf.series("s")
    assert np.allclose(xs, [0, 0.1, 0.2, 0.3]) and list(ys) == [0, 1, 2, 3]
    xs, _ = buf.series("s", x="B")                       # the field named B is readback...
    assert xs[1] == 0.1                                  # ...but axis name wins in fly streams
    assert buf.image("s") is None


async def test_sink_sends_messages_and_survives_dead_window():
    ch = FakeChannel()
    g = SimSource("g")
    bus = Bus()
    bus.add_sink("plot", LivePlotSink(channel=ch).consume, policy="drop", critical=False)
    stop = await run(Scan((Axis("g", linear(0, 1, 4)),), readables=("li",)),
                     {"g": g, "li": SimLockin("li", g)}, bus)
    await bus.join()
    kinds = [m["kind"] for m in ch.msgs]
    assert stop.status == "success" and kinds[0] == "start" and kinds[-1] == "stop"
    assert kinds.count("event") == 4 and ch.closed
    start = ch.msgs[0]
    assert start["axis_names"] == ["g"] and start["axis_points"]["g"][-1] == 1.0
    assert "li.x" in start["fields"] and start["fields"]["li.x"] == "V"
    assert all(isinstance(v, float) for v in ch.msgs[1]["data"].values())


async def test_sink_fly_nested_meta():
    ch = FakeChannel()
    g, m = SimSource("g"), SimMagnet("B", max_rate=50.0)
    bus = Bus()
    bus.add_sink("plot", LivePlotSink(channel=ch).consume, policy="drop", critical=False)
    await run(FlyScan("B", 0, 1, rate=50.0, dt=0.01, outer=(Axis("g", (0.0, 1.0)),)),
              {"g": g, "B": m}, bus)
    await bus.join()
    assert ch.msgs[0]["mode"] == "fly" and ch.msgs[0]["axis_names"] == ["g", "B"]
    buf = PlotBuffer()
    for msg in ch.msgs:
        if msg["kind"] == "start":
            buf.start(msg)
        elif msg["kind"] == "event":
            buf.add(msg["index"], msg["setpoints"], msg["data"])
    xs, ys = buf.series("B")
    assert len(xs) == len(ys) == buf.n
    assert xs[-1] == 1.0 and ys[-1] == 1.0     # last ramp (g=1) ends at B=1
