"""2D snake scan on the simulated instruments:
    gm run examples/gate_sweep.py --station "Instrument config/sim.toml" --sample demo

Sim source - 1 plays the gate, Sim source - 2 the bias; the lock-in measures
source 1.
"""

from gmcore.engine import RetryPolicy, range_watchdog
from gmcore.plans import Axis, Scan, linear

SCAN = Scan(
    axes=(Axis("Sim source - 2", linear(0.0, 0.5, 3)),
          Axis("Sim source - 1", linear(-5.0, 5.0, 21), settle_s=0.01, snake=True)),
    readables=("Sim lock-in - 1",),
    metadata={"purpose": "demo"},
    watchdogs=(range_watchdog("Sim lock-in - 1.x", -1.0, 1.0),),
)
RETRY = RetryPolicy(retries=2, on_exhausted="pause", backoff_s=0.5)
