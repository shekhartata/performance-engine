"""Domain errors for the Performance Envelope Engine."""


class PerfEnvelopeError(Exception):
    """Base error for all engine failures."""


class ConfigError(PerfEnvelopeError):
    """Invalid or incomplete project configuration."""


class SafetyError(PerfEnvelopeError):
    """Operation blocked by safety policy."""


class EnvironmentError(PerfEnvelopeError):
    """MongoDB environment discovery or connection failure."""


class DatasetError(PerfEnvelopeError):
    """Dataset generation or verification failure."""


class GuardrailError(PerfEnvelopeError):
    """Estimated resource usage exceeds configured limits."""


class ExecutionError(PerfEnvelopeError):
    """Workload or experiment execution failure."""


class AnalysisError(PerfEnvelopeError):
    """Modeling, envelope, or report generation failure."""
