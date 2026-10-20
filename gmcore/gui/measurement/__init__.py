"""General Measurement: builds and runs measurements after Labber's Measurement
Editor, on the Instrument Server's instruments (same program). The measurement
itself is gmcore/measurement/; the form is gui/forms/general_measurement.ui."""

from .window import GeneralMeasurementWindow

__all__ = ["GeneralMeasurementWindow"]
