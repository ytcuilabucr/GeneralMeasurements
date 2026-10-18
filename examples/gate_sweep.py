"""2D snake scan on the simulated station:
    gm run examples/gate_sweep.py --station station.sim.toml --sample demo
"""

from generalmeasurements.engine import RetryPolicy, range_watchdog
from generalmeasurements.plans import Axis, Scan, linear

SCAN = Scan(
    axes=(Axis("bias", linear(0.0, 0.5, 3)),
          Axis("gate", linear(-5.0, 5.0, 21), settle_s=0.01, snake=True)),
    readables=("lockin",),
    metadata={"purpose": "demo"},
    watchdogs=(range_watchdog("lockin.x", -1.0, 1.0),),
)
RETRY = RetryPolicy(retries=2, on_exhausted="pause", backoff_s=0.5)
